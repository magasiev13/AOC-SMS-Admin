import json

from flask import current_app
from rq import get_current_job
from app import create_app, db
from app.models import MessageLog
from app.services.suppression_service import process_failure_details
from app.services.suppression_backfill import backfill_suppressions
from app.services.twilio_service import TwilioTransientError, get_twilio_service


def _should_mark_failed() -> bool:
    job = get_current_job()
    if job is None:
        return True
    return job.retries_left == 0


def _load_details(log: MessageLog) -> list:
    if not log.details:
        return []
    try:
        return json.loads(log.details)
    except json.JSONDecodeError:
        return []


def _persist_progress(
    log: MessageLog,
    total_recipients: int,
    success_count: int,
    failure_count: int,
    details: list,
) -> None:
    log.total_recipients = total_recipients
    log.success_count = success_count
    log.failure_count = failure_count
    log.details = json.dumps(details)
    db.session.commit()


def _append_error_detail(details: list, error_message: str) -> list:
    payload = list(details) if isinstance(details, list) else []
    payload.append({'error': error_message})
    return payload


def send_bulk_job(log_id: int, recipient_data: list, final_message: str, delay: float = 0.1) -> None:
    app = create_app(run_startup_tasks=False, start_scheduler=False)
    with app.app_context():
        log = db.session.get(MessageLog, log_id)
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
            return

        try:
            twilio = get_twilio_service()
            result = twilio.send_bulk(remaining_recipients, final_message, delay=delay, raise_on_transient=True)
            combined_details = existing_details + result['details']
            log.total_recipients = len(recipient_data)
            log.success_count = existing_success + result['success_count']
            log.failure_count = existing_failure + result['failure_count']
            log.details = json.dumps(combined_details)
            log.status = 'sent' if log.failure_count == 0 else 'failed'
            db.session.commit()
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


def backfill_suppressions_job() -> dict:
    """Run suppression backfill as a background job."""
    app = create_app(run_startup_tasks=False, start_scheduler=False)
    with app.app_context():
        return backfill_suppressions()
