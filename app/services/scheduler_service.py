"""Background scheduler for sending scheduled messages."""
import json
import logging
import atexit
from datetime import timedelta
from sqlalchemy import func
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.interval import IntervalTrigger
from twilio.base.exceptions import TwilioRestException
from app.services.twilio_service import TwilioTransientError, get_twilio_service

scheduler = None
_scheduler_initialized = False

# Configure module logger
logger = logging.getLogger(__name__)


def _is_transient_send_error(error: Exception) -> bool:
    """Classify retryable provider failures."""
    if isinstance(error, (TwilioTransientError, TimeoutError, ConnectionError)):
        return True
    if isinstance(error, TwilioRestException):
        status = getattr(error, 'status', None)
        return status in {429} or (isinstance(status, int) and status >= 500)
    return False


def _compute_retry_backoff_seconds(
    retry_number: int,
    base_backoff_seconds: int,
    max_backoff_seconds: int,
) -> int:
    """Compute exponential backoff for retry scheduling."""
    retry_number = max(1, int(retry_number))
    base_backoff_seconds = max(1, int(base_backoff_seconds))
    max_backoff_seconds = max(base_backoff_seconds, int(max_backoff_seconds))
    delay = base_backoff_seconds * (2 ** (retry_number - 1))
    return min(delay, max_backoff_seconds)


def _to_non_negative_int(value: object) -> int:
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return 0


def _coerce_detail_rows(details: object) -> list[dict]:
    if not isinstance(details, list):
        return []
    return [detail for detail in details if isinstance(detail, dict)]


def _load_log_detail_rows(details_json: object) -> list[dict]:
    if not details_json:
        return []
    if isinstance(details_json, list):
        return _coerce_detail_rows(details_json)
    if not isinstance(details_json, str):
        return []

    try:
        payload = json.loads(details_json)
    except (TypeError, ValueError):
        return []
    return _coerce_detail_rows(payload)


def _count_detail_outcomes(detail_rows: list[dict]) -> tuple[int, int]:
    success_count = sum(1 for detail in detail_rows if detail.get('success') is True)
    failure_count = sum(1 for detail in detail_rows if detail.get('success') is False)
    return success_count, failure_count


def _upsert_message_log_for_scheduled_send(
    *,
    scheduled,
    result: dict,
    MessageLog,
    db,
):
    detail_rows = _coerce_detail_rows(result.get('details'))
    result_total = _to_non_negative_int(result.get('total'))
    result_success = _to_non_negative_int(result.get('success_count'))
    result_failure = _to_non_negative_int(result.get('failure_count'))

    detail_success, detail_failure = _count_detail_outcomes(detail_rows)
    if detail_rows:
        result_success = max(result_success, detail_success)
        result_failure = max(result_failure, detail_failure)
    result_total = max(result_total, result_success + result_failure, len(detail_rows))

    log = None
    if scheduled.message_log_id:
        log = db.session.get(MessageLog, scheduled.message_log_id)

    if log is None:
        log = MessageLog(
            message_body=scheduled.message_body,
            target=scheduled.target,
            event_id=scheduled.event_id,
            total_recipients=result_total,
            success_count=result_success,
            failure_count=result_failure,
            details=json.dumps(detail_rows),
        )
        db.session.add(log)
        db.session.flush()
        scheduled.message_log_id = log.id
        return log

    existing_rows = _load_log_detail_rows(log.details)
    merged_rows = existing_rows + detail_rows
    merged_success, merged_failure = _count_detail_outcomes(merged_rows)

    if merged_rows:
        log.success_count = merged_success
        log.failure_count = merged_failure
    else:
        log.success_count = max(_to_non_negative_int(log.success_count), result_success)
        log.failure_count = max(_to_non_negative_int(log.failure_count), result_failure)

    log.total_recipients = max(
        _to_non_negative_int(log.total_recipients),
        result_total,
        log.success_count + log.failure_count,
        len(merged_rows),
    )
    log.details = json.dumps(merged_rows)
    db.session.flush()
    return log


def _filter_previously_sent_recipients(
    *,
    scheduled,
    recipient_data: list[dict],
    MessageLog,
    db,
    normalize_phone,
) -> tuple[list[dict], int]:
    if not scheduled.message_log_id:
        return recipient_data, 0

    log = db.session.get(MessageLog, scheduled.message_log_id)
    if log is None:
        return recipient_data, 0

    delivered_phones = set()
    for detail in _load_log_detail_rows(log.details):
        if detail.get('success') is not True:
            continue
        phone = normalize_phone(detail.get('phone'))
        if phone:
            delivered_phones.add(phone)

    if not delivered_phones:
        return recipient_data, 0

    filtered_recipients = []
    skipped_count = 0
    for recipient in recipient_data:
        phone = normalize_phone(recipient.get('phone'))
        if phone and phone in delivered_phones:
            skipped_count += 1
            continue
        filtered_recipients.append(recipient)

    return filtered_recipients, skipped_count


def _handle_transient_failure(
    *,
    scheduled,
    error: Exception,
    now,
    max_retries: int,
    base_backoff_seconds: int,
    max_backoff_seconds: int,
    db,
) -> bool:
    """
    Handle transient failures for scheduled sends.

    Returns True when the message was re-queued for retry, False when marked failed.
    """
    attempt_count = int(scheduled.attempt_count or 0)
    retries_used = max(0, attempt_count - 1)
    retries_remaining = max_retries - retries_used
    if retries_remaining > 0:
        retry_number = retries_used + 1
        backoff_seconds = _compute_retry_backoff_seconds(
            retry_number=retry_number,
            base_backoff_seconds=base_backoff_seconds,
            max_backoff_seconds=max_backoff_seconds,
        )
        scheduled.status = 'pending'
        scheduled.processing_started_at = None
        scheduled.next_retry_at = now + timedelta(seconds=backoff_seconds)
        scheduled.error_message = (
            f'Transient send failure on attempt {attempt_count}: {error}'
        )
        db.session.commit()
        logger.warning(
            "[Scheduler] Message id=%d transient failure on attempt %d; retrying in %ds (remaining retries=%d): %s",
            scheduled.id,
            attempt_count,
            backoff_seconds,
            retries_remaining - 1,
            error,
        )
        return True

    scheduled.status = 'failed'
    scheduled.error_message = (
        f'Transient send failure exhausted retries after {attempt_count} attempts: {error}'
    )
    scheduled.sent_at = now
    scheduled.processing_started_at = None
    scheduled.next_retry_at = None
    db.session.commit()
    logger.error(
        "[Scheduler] Message id=%d transient failure exhausted retries after %d attempts: %s",
        scheduled.id,
        attempt_count,
        error,
    )
    return False


def send_scheduled_messages(app):
    """Check for and send any pending scheduled messages.
    
    This function is designed to be called repeatedly (e.g., by systemd timer).
    It handles:
    1. Marking stuck 'processing' messages as failed (configurable timeout)
    2. Processing all pending messages with scheduled_at <= now
    3. Re-queueing transient provider failures with bounded backoff
    
    All times are in UTC. The scheduled_at column stores UTC timestamps.
    """
    with app.app_context():
        from flask import current_app
        from app import db
        from app.models import ScheduledMessage, MessageLog, CommunityMember, EventRegistration, utc_now
        from app.services.recipient_service import (
            filter_suppressed_recipients,
            filter_unsubscribed_recipients,
        )
        from app.services.suppression_service import process_failure_details
        from app.utils import normalize_phone
        now = utc_now().replace(tzinfo=None)
        processing_timeout_minutes = max(
            1,
            int(current_app.config.get('SCHEDULED_PROCESSING_TIMEOUT_MINUTES', 10)),
        )
        max_retries = max(0, int(current_app.config.get('SCHEDULED_SEND_MAX_RETRIES', 3)))
        retry_backoff_seconds = max(
            1,
            int(current_app.config.get('SCHEDULED_SEND_RETRY_BACKOFF_SECONDS', 60)),
        )
        retry_max_backoff_seconds = max(
            retry_backoff_seconds,
            int(current_app.config.get('SCHEDULED_SEND_RETRY_MAX_BACKOFF_SECONDS', 900)),
        )
        logger.info("[Scheduler] Starting scheduled messages check at %s UTC", now.isoformat())
        
        # Step 1: Handle stuck 'processing' messages (timed out after configured threshold)
        processing_timeout = now - timedelta(minutes=processing_timeout_minutes)
        stuck_processing = ScheduledMessage.query.filter(
            ScheduledMessage.status == 'processing',
            func.coalesce(
                ScheduledMessage.processing_started_at,
                ScheduledMessage.scheduled_at,
            ) <= processing_timeout
        ).all()
        
        stuck_count = len(stuck_processing)
        for scheduled in stuck_processing:
            processing_started_at = (
                scheduled.processing_started_at or scheduled.scheduled_at
            )
            logger.warning(
                "[Scheduler] Marking stuck message id=%d as failed (was processing since %s)",
                scheduled.id,
                processing_started_at.isoformat() if processing_started_at else 'unknown',
            )
            scheduled.status = 'failed'
            scheduled.error_message = 'Message processing timed out'
            scheduled.sent_at = now
            scheduled.next_retry_at = None
        if stuck_processing:
            db.session.commit()
            logger.info("[Scheduler] Marked %d stuck message(s) as failed", stuck_count)
        max_lag_minutes = current_app.config.get('SCHEDULED_MESSAGE_MAX_LAG')
        expiry_threshold = None
        if max_lag_minutes and max_lag_minutes > 0:
            expiry_threshold = now - timedelta(minutes=max_lag_minutes)
        
        # Step 2: Find and process pending messages due for sending
        pending = ScheduledMessage.query.filter(
            ScheduledMessage.status == 'pending',
            ScheduledMessage.scheduled_at <= now,
            func.coalesce(
                ScheduledMessage.next_retry_at,
                ScheduledMessage.scheduled_at,
            ) <= now,
        ).all()
        
        pending_count = len(pending)
        logger.info("[Scheduler] Found %d pending message(s) due for sending", pending_count)
        
        if pending_count == 0:
            logger.info("[Scheduler] No messages to process, exiting")
            return
        
        processed_count = 0
        sent_count = 0
        failed_count = 0
        retried_count = 0
        
        for scheduled in pending:
            processed_count += 1
            logger.info(
                "[Scheduler] Processing message id=%d (scheduled_at=%s, target=%s)",
                scheduled.id,
                scheduled.scheduled_at.isoformat() if scheduled.scheduled_at else 'unknown',
                scheduled.target
            )
            
            # Mark as expired if too old (beyond configured max lag)
            if expiry_threshold and scheduled.scheduled_at < expiry_threshold:
                scheduled.status = 'expired'
                scheduled.error_message = (
                    'Message expired - scheduled time exceeded max lag '
                    f'of {max_lag_minutes} minutes'
                )
                scheduled.sent_at = now
                scheduled.next_retry_at = None
                db.session.commit()
                failed_count += 1
                logger.warning(
                    "[Scheduler] Message id=%d EXPIRED - was scheduled for %s (exceeded %d min lag)",
                    scheduled.id,
                    scheduled.scheduled_at.isoformat() if scheduled.scheduled_at else 'unknown',
                    max_lag_minutes
                )
                continue
            if scheduled.scheduled_at < now:
                lag_seconds = (now - scheduled.scheduled_at).total_seconds()
                logger.info(
                    "[Scheduler] Message id=%d is %.1f seconds late, sending now",
                    scheduled.id, lag_seconds
                )
            # Mark as processing immediately to prevent race condition
            try:
                updated = ScheduledMessage.query.filter_by(
                    id=scheduled.id,
                    status='pending'
                ).update(
                    {
                        'status': 'processing',
                        'processing_started_at': now,
                        'last_attempt_at': now,
                        'attempt_count': func.coalesce(ScheduledMessage.attempt_count, 0) + 1,
                        'next_retry_at': None,
                    },
                    synchronize_session=False,
                )
                if not updated:
                    db.session.rollback()
                    logger.info("[Scheduler] Message id=%d already claimed by another process, skipping", scheduled.id)
                    continue
                db.session.commit()
                db.session.refresh(scheduled)
                scheduled.status = 'processing'
                scheduled.processing_started_at = now
                logger.info(
                    "[Scheduler] Message id=%d status: pending -> processing (attempt=%d)",
                    scheduled.id,
                    scheduled.attempt_count,
                )
            except Exception as e:
                db.session.rollback()
                logger.warning("[Scheduler] Message id=%d lock failed: %s", scheduled.id, e)
                continue
            try:
                # Test mode: send only to admin phone
                if scheduled.test_mode:
                    admin_phone = current_app.config.get('ADMIN_TEST_PHONE')
                    if not admin_phone:
                        scheduled.status = 'failed'
                        scheduled.error_message = 'ADMIN_TEST_PHONE not configured'
                        scheduled.sent_at = now
                        scheduled.next_retry_at = None
                        db.session.commit()
                        failed_count += 1
                        logger.error("[Scheduler] Message id=%d FAILED: ADMIN_TEST_PHONE not configured", scheduled.id)
                        continue
                    recipient_data = [{'phone': admin_phone, 'name': 'Admin Test'}]
                elif scheduled.target == 'community':
                    members = CommunityMember.query.all()
                    recipient_data = [{'phone': m.phone, 'name': m.name} for m in members]
                else:
                    registrations = EventRegistration.query.filter_by(event_id=scheduled.event_id).all()
                    recipient_data = [{'phone': r.phone, 'name': r.name} for r in registrations]
                
                if not scheduled.test_mode:
                    recipient_data, skipped, _ = filter_unsubscribed_recipients(recipient_data)
                    if skipped:
                        logger.info("[Scheduler] Message id=%d: skipped %d unsubscribed recipient(s)", scheduled.id, len(skipped))

                    recipient_data, suppressed_skipped, _ = filter_suppressed_recipients(recipient_data)
                    if suppressed_skipped:
                        logger.info("[Scheduler] Message id=%d: skipped %d suppressed recipient(s)", scheduled.id, len(suppressed_skipped))

                recipient_data, already_sent_skipped = _filter_previously_sent_recipients(
                    scheduled=scheduled,
                    recipient_data=recipient_data,
                    MessageLog=MessageLog,
                    db=db,
                    normalize_phone=normalize_phone,
                )
                if already_sent_skipped:
                    logger.info(
                        "[Scheduler] Message id=%d: skipped %d recipient(s) already delivered in prior partial attempts",
                        scheduled.id,
                        already_sent_skipped,
                    )

                if not recipient_data:
                    if already_sent_skipped:
                        scheduled.status = 'sent'
                        scheduled.sent_at = now
                        scheduled.error_message = None
                        scheduled.next_retry_at = None
                        db.session.commit()
                        sent_count += 1
                        logger.info(
                            "[Scheduler] Message id=%d SENT: all recipients already delivered in prior attempts",
                            scheduled.id,
                        )
                        continue

                    scheduled.status = 'failed'
                    scheduled.error_message = 'No recipients found (all recipients unsubscribed or empty list)'
                    scheduled.sent_at = now
                    scheduled.next_retry_at = None
                    db.session.commit()
                    failed_count += 1
                    logger.warning("[Scheduler] Message id=%d FAILED: no recipients found", scheduled.id)
                    continue
                
                # Send messages
                twilio = get_twilio_service()
                result = twilio.send_bulk(
                    recipient_data,
                    scheduled.message_body,
                    raise_on_transient=True,
                )
                
                # Create or append to existing log entry.
                log = _upsert_message_log_for_scheduled_send(
                    scheduled=scheduled,
                    result=result,
                    MessageLog=MessageLog,
                    db=db,
                )
                
                # Update scheduled message
                scheduled.status = 'sent'
                scheduled.sent_at = now
                scheduled.message_log_id = log.id
                scheduled.error_message = None
                scheduled.next_retry_at = None
                db.session.commit()

                sent_count += 1
                logger.info(
                    "[Scheduler] Message id=%d SENT: %d/%d successful (status: processing -> sent)",
                    scheduled.id, log.success_count, log.total_recipients
                )

                try:
                    process_failure_details(result.get('details', []), log.id)
                except Exception as e:
                    logger.exception(
                        "[Scheduler] Message id=%d sent, but suppression post-processing failed: %s",
                        scheduled.id,
                        e,
                    )
                
            except TwilioTransientError as e:
                partial_result = getattr(e, 'results', None)
                if not isinstance(partial_result, dict):
                    partial_result = {}
                if partial_result:
                    logger.warning(
                        "[Scheduler] Message id=%d transient failure after partial progress: success_count=%s failure_count=%s",
                        scheduled.id,
                        partial_result.get('success_count', 0),
                        partial_result.get('failure_count', 0),
                    )
                    _upsert_message_log_for_scheduled_send(
                        scheduled=scheduled,
                        result=partial_result,
                        MessageLog=MessageLog,
                        db=db,
                    )
                was_requeued = _handle_transient_failure(
                    scheduled=scheduled,
                    error=e,
                    now=now,
                    max_retries=max_retries,
                    base_backoff_seconds=retry_backoff_seconds,
                    max_backoff_seconds=retry_max_backoff_seconds,
                    db=db,
                )
                if was_requeued:
                    retried_count += 1
                else:
                    failed_count += 1
            except Exception as e:
                if _is_transient_send_error(e):
                    was_requeued = _handle_transient_failure(
                        scheduled=scheduled,
                        error=e,
                        now=now,
                        max_retries=max_retries,
                        base_backoff_seconds=retry_backoff_seconds,
                        max_backoff_seconds=retry_max_backoff_seconds,
                        db=db,
                    )
                    if was_requeued:
                        retried_count += 1
                    else:
                        failed_count += 1
                    continue

                scheduled.status = 'failed'
                scheduled.error_message = str(e)
                scheduled.sent_at = now
                scheduled.next_retry_at = None
                db.session.commit()
                failed_count += 1
                logger.error(
                    "[Scheduler] Message id=%d FAILED: %s (status: processing -> failed)",
                    scheduled.id, e
                )
        
        # Summary log
        logger.info(
            "[Scheduler] Completed: processed=%d, sent=%d, failed=%d, retried=%d",
            processed_count, sent_count, failed_count, retried_count
        )


def init_scheduler(app):
    """Initialize the scheduler with the Flask app."""
    global scheduler, _scheduler_initialized
    
    if _scheduler_initialized:
        app.logger.warning("Scheduler already initialized; skipping duplicate startup.")
        return
    
    _scheduler_initialized = True
    scheduler = BackgroundScheduler()
    
    # Check every 5 seconds for pending scheduled messages (precise timing)
    scheduler.add_job(
        func=lambda: send_scheduled_messages(app),
        trigger=IntervalTrigger(seconds=5),
        id='send_scheduled_messages',
        name='Send scheduled messages',
        replace_existing=True
    )
    
    scheduler.start()
    atexit.register(lambda: scheduler.shutdown() if scheduler and scheduler.running else None)
    app.logger.info("[Scheduler] Background scheduler started")


def shutdown_scheduler():
    """Shutdown the scheduler gracefully."""
    global scheduler
    if scheduler and scheduler.running:
        scheduler.shutdown()
        print("[Scheduler] Background scheduler stopped")
