import json

from flask import current_app
from rq import get_current_job
from app import create_app, db
from app.models import MessageLog
from app.tenant import organization_context
from app.services.suppression_service import process_failure_details
from app.services.suppression_backfill import backfill_suppressions
from app.services.twilio_service import (
    TwilioTransientError,
    get_twilio_service,
    record_usage_candidates,
)
from app.services.twilio_a2p_service import (
    process_a2p_onboarding,
    reconcile_pending_a2p_onboardings,
    sync_a2p_onboarding_status,
)


def _should_mark_failed() -> bool:
    job = get_current_job()
    if job is None:
        return True
    retries_left = getattr(job, 'retries_left', None)
    if retries_left is None:
        return True
    try:
        return int(retries_left) <= 0
    except (TypeError, ValueError):
        return True


def _load_details(log: MessageLog) -> list:
    if not log.details:
        return []
    try:
        return json.loads(log.details)
    except json.JSONDecodeError:
        return []


def _append_error_detail(details: list, error_message: str) -> list:
    payload = list(details) if isinstance(details, list) else []
    payload.append({'error': error_message})
    return payload


def _record_usage_candidates_safely(
    *,
    log_id: int,
    organization_id: int | None,
    details: list[dict] | None,
    source: str,
) -> None:
    try:
        record_usage_candidates(organization_id, details, source=source)
    except Exception as exc:
        db.session.rollback()
        current_app.logger.exception(
            "Failed recording usage candidates for log_id=%s organization_id=%s source=%s: %s",
            log_id,
            organization_id,
            source,
            exc,
        )


def send_bulk_job(
    log_id: int,
    organization_id: int | list | None = None,
    recipient_data: list | str | None = None,
    final_message: str | None = None,
    delay: float = 0.1,
) -> None:
    # Keep backward compatibility with older direct callers that passed
    # (log_id, recipient_data, final_message, delay=...).
    if isinstance(organization_id, list) and isinstance(recipient_data, str) and final_message is None:
        final_message = recipient_data
        recipient_data = organization_id
        organization_id = None

    if recipient_data is None or final_message is None:
        raise TypeError("send_bulk_job() requires recipient_data and final_message")

    app = create_app(run_startup_tasks=False, start_scheduler=False)
    with app.app_context():
        with organization_context(organization_id):
            current_app.logger.info(
                "Starting bulk send job log_id=%s organization_id=%s recipients=%s",
                log_id,
                organization_id,
                len(recipient_data),
            )
            log = MessageLog.query.filter_by(id=log_id).first()
            if not log:
                raise ValueError(f"MessageLog {log_id} not found")

            existing_details = []
            if log.details:
                try:
                    existing_details = json.loads(log.details)
                except json.JSONDecodeError:
                    existing_details = []
            if not isinstance(existing_details, list):
                existing_details = []

            existing_success = sum(1 for detail in existing_details if detail.get('success') is True)
            existing_failure = sum(1 for detail in existing_details if detail.get('success') is False)
            start_index = len(existing_details)
            remaining_recipients = recipient_data[start_index:]

            if not remaining_recipients:
                log.total_recipients = len(recipient_data)
                log.success_count = existing_success
                log.failure_count = existing_failure
                log.status = 'sent' if existing_failure == 0 else 'failed'
                db.session.commit()
                current_app.logger.info(
                    "Bulk send job log_id=%s organization_id=%s already complete status=%s",
                    log.id,
                    organization_id,
                    log.status,
                )
                return

            try:
                twilio = get_twilio_service(organization_id)
                result = twilio.send_bulk(remaining_recipients, final_message, delay=delay, raise_on_transient=True)
                combined_details = existing_details + result['details']
                log.total_recipients = len(recipient_data)
                log.success_count = existing_success + result['success_count']
                log.failure_count = existing_failure + result['failure_count']
                log.details = json.dumps(combined_details)
                log.status = 'sent' if log.failure_count == 0 else 'failed'
                db.session.commit()
                _record_usage_candidates_safely(
                    log_id=log.id,
                    organization_id=organization_id,
                    details=result['details'],
                    source='blast',
                )
                current_app.logger.info(
                    "Bulk send job finished log_id=%s organization_id=%s status=%s success_count=%s failure_count=%s",
                    log.id,
                    organization_id,
                    log.status,
                    log.success_count,
                    log.failure_count,
                )
                try:
                    process_failure_details(combined_details, log.id)
                except Exception as exc:
                    current_app.logger.exception(
                        "Failed processing suppression details for log_id=%s after successful send: %s",
                        log.id,
                        exc,
                    )
            except TwilioTransientError as exc:
                combined_details = existing_details
                if exc.results:
                    combined_details = existing_details + exc.results.get('details', [])
                    log.total_recipients = len(recipient_data)
                    log.success_count = existing_success + exc.results.get('success_count', 0)
                    log.failure_count = existing_failure + exc.results.get('failure_count', 0)
                    log.details = json.dumps(combined_details)
                    db.session.commit()
                    _record_usage_candidates_safely(
                        log_id=log.id,
                        organization_id=organization_id,
                        details=exc.results.get('details', []),
                        source='blast',
                    )
                if _should_mark_failed():
                    log.status = 'failed'
                    base_details = combined_details if exc.results else existing_details
                    combined_details = _append_error_detail(base_details, str(exc))
                    log.total_recipients = len(recipient_data)
                    log.failure_count = max((log.failure_count or 0), existing_failure + 1)
                    log.details = json.dumps(combined_details)
                    db.session.commit()
                try:
                    process_failure_details(combined_details, log.id)
                except Exception as process_exc:
                    current_app.logger.exception(
                        "Failed processing suppression details for log_id=%s after transient send error: %s",
                        log.id,
                        process_exc,
                    )
                current_app.logger.warning(
                    "Bulk send job transient failure log_id=%s organization_id=%s success_count=%s failure_count=%s",
                    log.id,
                    organization_id,
                    log.success_count,
                    log.failure_count,
                )
                raise
            except Exception as exc:
                combined_details = _load_details(log) or existing_details
                combined_details = _append_error_detail(combined_details, str(exc))
                log.total_recipients = len(recipient_data)
                log.success_count = max(log.success_count or 0, existing_success)
                log.failure_count = max(log.failure_count or 0, existing_failure + 1)
                log.status = 'failed'
                log.details = json.dumps(combined_details)
                db.session.commit()
                try:
                    process_failure_details(combined_details, log.id)
                except Exception as process_exc:
                    current_app.logger.exception(
                        "Failed processing suppression details for log_id=%s after non-transient send error: %s",
                        log.id,
                        process_exc,
                    )
                current_app.logger.error(
                    "Bulk send job failed log_id=%s organization_id=%s error=%s",
                    log.id,
                    organization_id,
                    exc,
                )


def backfill_suppressions_job() -> dict:
    """Run suppression backfill as a background job."""
    app = create_app(run_startup_tasks=False, start_scheduler=False)
    with app.app_context():
        return backfill_suppressions()


def process_a2p_onboarding_job(organization_id: int, actor_user_id: int | None = None) -> dict:
    """Process or advance Twilio A2P onboarding for one organization."""
    app = create_app(run_startup_tasks=False, start_scheduler=False)
    with app.app_context():
        onboarding = process_a2p_onboarding(organization_id, actor_user_id=actor_user_id)
        return {
            "organization_id": organization_id,
            "onboarding_status": onboarding.onboarding_status,
            "brand_status": onboarding.brand_status,
            "campaign_status": onboarding.campaign_status,
        }


def sync_a2p_onboarding_status_job(organization_id: int, actor_user_id: int | None = None) -> dict:
    """Run the non-destructive Twilio A2P status refresh for one organization."""
    app = create_app(run_startup_tasks=False, start_scheduler=False)
    with app.app_context():
        onboarding = sync_a2p_onboarding_status(organization_id, actor_user_id=actor_user_id)
        return {
            "organization_id": organization_id,
            "onboarding_status": onboarding.onboarding_status,
            "brand_status": onboarding.brand_status,
            "campaign_status": onboarding.campaign_status,
        }


def reconcile_a2p_onboardings_job() -> dict[str, int]:
    """Poll pending Twilio A2P registrations and advance ready records."""
    app = create_app(run_startup_tasks=False, start_scheduler=False)
    with app.app_context():
        return reconcile_pending_a2p_onboardings()
