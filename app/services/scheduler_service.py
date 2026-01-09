"""Background scheduler for sending scheduled messages."""
import json
import logging
import atexit
from datetime import datetime
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.interval import IntervalTrigger

scheduler = None
_scheduler_initialized = False

# Configure module logger
logger = logging.getLogger(__name__)


def send_scheduled_messages(app):
    """Check for and send any pending scheduled messages.
    
    This function is designed to be called repeatedly (e.g., by systemd timer).
    It handles:
    1. Marking stuck 'processing' messages as failed (10-minute timeout)
    2. Processing all pending messages with scheduled_at <= now
    
    All times are in UTC. The scheduled_at column stores UTC timestamps.
    """
    with app.app_context():
        from datetime import timedelta
        from flask import current_app
        from app import db
        from app.models import ScheduledMessage, MessageLog, CommunityMember, EventRegistration
        from app.services.recipient_service import (
            filter_suppressed_recipients,
            filter_unsubscribed_recipients,
        )
        from app.services.suppression_service import process_failure_details
        from app.services.twilio_service import get_twilio_service
        
        now = datetime.utcnow()
        logger.info("[Scheduler] Starting scheduled messages check at %s UTC", now.isoformat())
        
        # Step 1: Handle stuck 'processing' messages (timed out after 10 minutes)
        processing_timeout = now - timedelta(minutes=10)
        stuck_processing = ScheduledMessage.query.filter(
            ScheduledMessage.status == 'processing',
            ScheduledMessage.scheduled_at <= processing_timeout
        ).all()
        
        stuck_count = len(stuck_processing)
        for scheduled in stuck_processing:
            logger.warning(
                "[Scheduler] Marking stuck message id=%d as failed (was processing since %s)",
                scheduled.id, scheduled.scheduled_at.isoformat() if scheduled.scheduled_at else 'unknown'
            )
            scheduled.status = 'failed'
            scheduled.error_message = 'Message processing timed out'
            scheduled.sent_at = now
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
            ScheduledMessage.scheduled_at <= now
        ).all()
        
        pending_count = len(pending)
        logger.info("[Scheduler] Found %d pending message(s) due for sending", pending_count)
        
        if pending_count == 0:
            logger.info("[Scheduler] No messages to process, exiting")
            return
        
        processed_count = 0
        sent_count = 0
        failed_count = 0
        
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
                ).update({'status': 'processing'}, synchronize_session=False)
                if not updated:
                    db.session.rollback()
                    logger.info("[Scheduler] Message id=%d already claimed by another process, skipping", scheduled.id)
                    continue
                db.session.commit()
                scheduled.status = 'processing'
                logger.info("[Scheduler] Message id=%d status: pending -> processing", scheduled.id)
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

                if not recipient_data:
                    scheduled.status = 'failed'
                    scheduled.error_message = 'No recipients found (all recipients unsubscribed or empty list)'
                    scheduled.sent_at = now
                    db.session.commit()
                    failed_count += 1
                    logger.warning("[Scheduler] Message id=%d FAILED: no recipients found", scheduled.id)
                    continue
                
                # Send messages
                twilio = get_twilio_service()
                result = twilio.send_bulk(recipient_data, scheduled.message_body)
                
                # Create log entry
                log = MessageLog(
                    message_body=scheduled.message_body,
                    target=scheduled.target,
                    event_id=scheduled.event_id,
                    total_recipients=result['total'],
                    success_count=result['success_count'],
                    failure_count=result['failure_count'],
                    details=json.dumps(result['details'])
                )
                db.session.add(log)
                db.session.flush()
                
                # Update scheduled message
                scheduled.status = 'sent'
                scheduled.sent_at = now
                scheduled.message_log_id = log.id
                db.session.commit()

                process_failure_details(result.get('details', []), log.id)
                
                sent_count += 1
                logger.info(
                    "[Scheduler] Message id=%d SENT: %d/%d successful (status: processing -> sent)",
                    scheduled.id, result['success_count'], result['total']
                )
                
            except Exception as e:
                scheduled.status = 'failed'
                scheduled.error_message = str(e)
                scheduled.sent_at = now
                db.session.commit()
                failed_count += 1
                logger.error(
                    "[Scheduler] Message id=%d FAILED: %s (status: processing -> failed)",
                    scheduled.id, e
                )
        
        # Summary log
        logger.info(
            "[Scheduler] Completed: processed=%d, sent=%d, failed=%d",
            processed_count, sent_count, failed_count
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
