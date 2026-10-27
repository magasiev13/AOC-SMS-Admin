import json

from flask import current_app
from rq import get_current_job
from app import create_app, db
from app.models import MessageLog, MessageDispatchAttempt, Organization, OutboundRequest, utc_now
from datetime import timedelta
from app.services.outbound_request_service import complete_request, start_request_execution
from app.services.recipient_permission_service import filter_permission_recipients, permission_context
from app.services.billing_service import organization_transmit_block_reason
from app.tenant import organization_context
from app.services.outbound_idempotency_service import (
    build_blast_send_fingerprint,
    recipient_fingerprint_phones,
)
from app.services.recipient_service import dedupe_recipients_by_phone
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


def _finish_durable_request(log: MessageLog, *, error: str | None = None) -> bool:
    if not current_app.config.get('SAAS_MODE'):
        return False
    incoming_details = _load_details(log)
    incoming_status = log.status
    incoming_total = log.total_recipients
    # Finalizers may finish in a different order from provider calls. Refresh
    # under one lock, and let committed attempts outrank stale worker results.
    with db.session.no_autoflush:
        log = MessageLog.query.filter_by(id=log.id).populate_existing().with_for_update().one()
        request = OutboundRequest.query.filter_by(message_log_id=log.id, kind='blast').populate_existing().with_for_update().first()
        attempts = MessageDispatchAttempt.query.filter_by(message_log_id=log.id).populate_existing().all()
    if request is not None and request.status == 'accepted':
        # A delayed duplicate cannot downgrade a completed accepted operation.
        db.session.commit()
        return True
    by_phone = {}
    other_details = []
    for detail in _load_details(log) + incoming_details:
        if not isinstance(detail, dict):
            continue
        phone = detail.get('phone') or detail.get('to')
        if phone:
            by_phone[phone] = detail
        elif detail not in other_details:
            other_details.append(detail)
    for attempt in attempts:
        if attempt.status == 'queued':
            continue
        detail = dict(by_phone.get(attempt.recipient_phone, {}))
        detail.update(
            phone=attempt.recipient_phone, sid=attempt.provider_message_sid,
            status=attempt.status, success=attempt.status == 'sent', error=attempt.last_error,
        )
        by_phone[attempt.recipient_phone] = detail
    details = list(by_phone.values()) + other_details
    log.total_recipients = incoming_total
    log.success_count = sum(row.get('success') is True for row in by_phone.values())
    log.failure_count = sum(row.get('success') is False and row.get('status') not in {'sending', 'ambiguous'} for row in by_phone.values())
    ambiguous = any(row.get('status') in {'sending', 'ambiguous'} for row in by_phone.values())
    all_submitted = bool(log.total_recipients) and log.success_count == log.total_recipients
    failed = bool(log.failure_count) or (incoming_status == 'failed' and not all_submitted)
    if all_submitted and error == 'No recipients remain eligible.':
        error = None
    log.status = 'ambiguous' if ambiguous else ('failed' if failed else 'sent')
    log.details = json.dumps(details)
    result = {'details': details, 'success_count': log.success_count,
              'failure_count': log.failure_count, 'error': error, 'success': not failed}
    if request is not None:
        complete_request(request, result)
    db.session.commit()
    return True


def send_reply_request_job(organization_id: int, request_id: int) -> None:
    app = create_app(run_startup_tasks=False, start_scheduler=False)
    with app.app_context(), organization_context(organization_id):
        request = OutboundRequest.query.filter_by(id=request_id, kind='reply').first()
        if request is None or request.status in {'accepted', 'failed', 'ambiguous'}:
            return
        if not request.thread_id:
            complete_request(request, {'success': False, 'error': 'The saved conversation is unavailable.'})
            db.session.commit()
            return
        if not start_request_execution(request):
            return
        payload = json.loads(request.payload_json)
        from app.services.inbox_service import send_thread_reply_with_key
        result = send_thread_reply_with_key(
            request.thread_id, payload['message_body'], request.actor_key, f'reply-request:{request.id}',
        )
        db.session.refresh(request)
        if request.status not in {'accepted', 'failed'}:
            complete_request(request, result)
            db.session.commit()


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
            deduped_recipient_data, duplicate_recipients, _ = dedupe_recipients_by_phone(recipient_data)
            duplicate_count = len(duplicate_recipients)
            recipient_data = deduped_recipient_data
            send_fingerprint = build_blast_send_fingerprint(
                organization_id=organization_id,
                target=log.target if (log := MessageLog.query.filter_by(id=log_id).first()) else "community",
                event_id=log.event_id if log else None,
                test_mode=bool(log.test_mode) if log else False,
                final_message=final_message,
                recipient_data=recipient_data,
            )
            current_app.logger.info(
                "Starting bulk send job log_id=%s organization_id=%s unique_recipients=%s skipped_duplicates=%s fingerprint=%s phones=%s",
                log_id,
                organization_id,
                len(recipient_data),
                duplicate_count,
                send_fingerprint,
                recipient_fingerprint_phones(recipient_data),
            )
            if not log:
                raise ValueError(f"MessageLog {log_id} not found")
            if current_app.config.get('SAAS_MODE') and organization_id is None and log.organization_id:
                # Pre-tenant queue payloads contained only the log identifier.
                # Resolve its persisted owner once, then execute under scope.
                return send_bulk_job(log_id, log.organization_id, recipient_data, final_message, delay)

            saved_request = None
            if current_app.config.get('SAAS_MODE'):
                saved_request = OutboundRequest.query.filter_by(message_log_id=log.id, kind='blast').first()
                if saved_request is not None:
                    if saved_request.status in {'accepted', 'failed', 'ambiguous'}:
                        return
                    saved_payload = json.loads(saved_request.payload_json)
                    if saved_payload['recipients'] != recipient_data or saved_payload['message_body'] != final_message:
                        raise ValueError('Queued payload does not match the immutable saved send request.')
                    if not start_request_execution(saved_request):
                        return

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

            if current_app.config.get("SAAS_MODE"):
                organization = db.session.get(Organization, organization_id) if organization_id else None
                send_block_reason = organization_transmit_block_reason(organization)
                if send_block_reason:
                    combined_details = [
                        dict(recipient, success=False, status='blocked', error=send_block_reason)
                        for recipient in recipient_data
                    ]
                    log.total_recipients = len(recipient_data)
                    log.success_count = max(log.success_count or 0, existing_success)
                    log.failure_count = len(recipient_data)
                    log.status = "failed"
                    log.details = json.dumps(combined_details)
                    if not _finish_durable_request(log, error=send_block_reason):
                        db.session.commit()
                    current_app.logger.warning(
                        "Bulk send job blocked log_id=%s organization_id=%s reason=%s",
                        log.id,
                        organization_id,
                        send_block_reason,
                    )
                    return

            start_index = len(existing_details)
            remaining_recipients = recipient_data[start_index:]

            if not remaining_recipients and not current_app.config.get("SAAS_MODE"):
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
                if current_app.config.get("SAAS_MODE"):
                    permission_skips = []
                    if log.test_mode:
                        from app.services.test_recipient_service import list_test_recipients
                        saved_phones = {entry.phone for entry in list_test_recipients(organization_id)}
                        allowed = [row for row in recipient_data if row.get('phone') in saved_phones]
                        permission_skips = [dict(row, permission_reason='test_recipient_removed') for row in recipient_data if row.get('phone') not in saved_phones]
                    else:
                        allowed, permission_skips, _ = filter_permission_recipients(
                            recipient_data, organization_id=organization_id,
                            purpose=log.target, event_id=log.event_id if log.target == 'event' else None,
                        )
                    with permission_context(log.target, log.event_id if log.target == 'event' else None):
                        result = twilio.send_bulk_with_attempts(
                            allowed, final_message, f"blast-log:{log.id}", log.id,
                            None, delay, 'manual_live_test' if log.test_mode else 'blast',
                        )
                    combined_details = result['details'] + [
                        dict(row, success=None, status='excluded', error=row['permission_reason']) for row in permission_skips
                    ]
                    existing_success = 0
                    existing_failure = 0
                else:
                    result = twilio.send_bulk(
                        remaining_recipients,
                        final_message,
                        delay=delay,
                        raise_on_transient=True,
                        send_kind='blast',
                    )
                    combined_details = existing_details + result['details']
                log.total_recipients = len(recipient_data)
                log.success_count = existing_success + result['success_count']
                log.failure_count = existing_failure + result['failure_count']
                log.details = json.dumps(combined_details)
                log.status = 'sent' if log.failure_count == 0 else 'failed'
                if current_app.config.get('SAAS_MODE') and not allowed:
                    log.status = 'failed'
                    log.failure_count = 0
                if not _finish_durable_request(log, error='No recipients remain eligible.' if current_app.config.get('SAAS_MODE') and not allowed else None):
                    db.session.commit()
                _record_usage_candidates_safely(
                    log_id=log.id,
                    organization_id=organization_id,
                    details=result['details'],
                    source='blast',
                )
                current_app.logger.info(
                    "Bulk send job finished log_id=%s organization_id=%s status=%s success_count=%s failure_count=%s unique_recipients=%s skipped_duplicates=%s fingerprint=%s",
                    log.id,
                    organization_id,
                    log.status,
                    log.success_count,
                    log.failure_count,
                    len(recipient_data),
                    duplicate_count,
                    send_fingerprint,
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
                    if not _finish_durable_request(log, error=str(exc)):
                        db.session.commit()
                elif saved_request is not None:
                    OutboundRequest.query.filter(
                        OutboundRequest.id == saved_request.id,
                        ~OutboundRequest.status.in_(['accepted', 'failed', 'ambiguous']),
                    ).update({
                        OutboundRequest.status: 'queued',
                        OutboundRequest.next_enqueue_at: utc_now().replace(tzinfo=None) + timedelta(minutes=2),
                    }, synchronize_session=False)
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
                    "Bulk send job transient failure log_id=%s organization_id=%s success_count=%s failure_count=%s unique_recipients=%s skipped_duplicates=%s fingerprint=%s retries_left=%s",
                    log.id,
                    organization_id,
                    log.success_count,
                    log.failure_count,
                    len(recipient_data),
                    duplicate_count,
                    send_fingerprint,
                    getattr(get_current_job(), 'retries_left', None),
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
                if not _finish_durable_request(log, error=str(exc)):
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
                    "Bulk send job failed log_id=%s organization_id=%s error=%s unique_recipients=%s skipped_duplicates=%s fingerprint=%s",
                    log.id,
                    organization_id,
                    exc,
                    len(recipient_data),
                    duplicate_count,
                    send_fingerprint,
                )


def backfill_suppressions_job(organization_id: int) -> dict:
    """Run one organization's suppression backfill as a background job."""
    app = create_app(run_startup_tasks=False, start_scheduler=False)
    with app.app_context():
        with organization_context(organization_id):
            return backfill_suppressions(organization_id)


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
