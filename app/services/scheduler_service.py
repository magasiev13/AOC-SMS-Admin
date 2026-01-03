"""Background scheduler for sending scheduled messages."""
import json
import atexit
from datetime import datetime
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.interval import IntervalTrigger

scheduler = None
_scheduler_initialized = False


def send_scheduled_messages(app):
    """Check for and send any pending scheduled messages."""
    with app.app_context():
        from datetime import timedelta
        from app import db
        from app.models import ScheduledMessage, MessageLog, CommunityMember, EventRegistration
        from app.services.twilio_service import get_twilio_service
        
        now = datetime.utcnow()
        processing_timeout = now - timedelta(minutes=10)
        stuck_processing = ScheduledMessage.query.filter(
            ScheduledMessage.status == 'processing',
            ScheduledMessage.scheduled_at <= processing_timeout
        ).all()
        for scheduled in stuck_processing:
            scheduled.status = 'failed'
            scheduled.error_message = 'Message processing timed out'
            scheduled.sent_at = now
        if stuck_processing:
            db.session.commit()
        # Messages older than 5 minutes are considered expired
        expiry_threshold = now - timedelta(minutes=5)
        
        pending = ScheduledMessage.query.filter(
            ScheduledMessage.status == 'pending',
            ScheduledMessage.scheduled_at <= now
        ).all()
        
        for scheduled in pending:
            # Mark as expired if too old (more than 5 minutes past scheduled time)
            if scheduled.scheduled_at < expiry_threshold:
                scheduled.status = 'expired'
                scheduled.error_message = 'Message expired - scheduled time was more than 5 minutes ago'
                scheduled.sent_at = now
                db.session.commit()
                print(f"[Scheduler] Expired scheduled message {scheduled.id} - was scheduled for {scheduled.scheduled_at}")
                continue
            # Mark as processing immediately to prevent race condition
            try:
                updated = ScheduledMessage.query.filter_by(
                    id=scheduled.id,
                    status='pending'
                ).update({'status': 'processing'}, synchronize_session=False)
                if not updated:
                    db.session.rollback()
                    continue  # Another process already grabbed this one
                db.session.commit()
                scheduled.status = 'processing'
            except Exception:
                db.session.rollback()
                continue  # Another process already grabbed this one
            try:
                # Test mode: send only to admin phone
                if scheduled.test_mode:
                    from flask import current_app
                    admin_phone = current_app.config.get('ADMIN_TEST_PHONE')
                    if not admin_phone:
                        scheduled.status = 'failed'
                        scheduled.error_message = 'ADMIN_TEST_PHONE not configured'
                        scheduled.sent_at = now
                        db.session.commit()
                        continue
                    recipient_data = [{'phone': admin_phone, 'name': 'Admin Test'}]
                elif scheduled.target == 'community':
                    members = CommunityMember.query.all()
                    recipient_data = [{'phone': m.phone, 'name': m.name} for m in members]
                else:
                    registrations = EventRegistration.query.filter_by(event_id=scheduled.event_id).all()
                    recipient_data = [{'phone': r.phone, 'name': r.name} for r in registrations]
                
                if not recipient_data:
                    scheduled.status = 'failed'
                    scheduled.error_message = 'No recipients found'
                    scheduled.sent_at = now
                    db.session.commit()
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
                
                print(f"[Scheduler] Sent scheduled message {scheduled.id}: {result['success_count']}/{result['total']} successful")
                
            except Exception as e:
                scheduled.status = 'failed'
                scheduled.error_message = str(e)
                scheduled.sent_at = now
                db.session.commit()
                print(f"[Scheduler] Failed to send scheduled message {scheduled.id}: {e}")


def init_scheduler(app):
    """Initialize the scheduler with the Flask app."""
    global scheduler, _scheduler_initialized
    
    if _scheduler_initialized:
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
    print("[Scheduler] Background scheduler started")


def shutdown_scheduler():
    """Shutdown the scheduler gracefully."""
    global scheduler
    if scheduler and scheduler.running:
        scheduler.shutdown()
        print("[Scheduler] Background scheduler stopped")
