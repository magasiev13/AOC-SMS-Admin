import json
from rq import get_current_job
from app import create_app, db
from app.models import MessageLog
from app.services.twilio_service import TwilioTransientError, get_twilio_service


def _should_mark_failed() -> bool:
    job = get_current_job()
    if job is None:
        return True
    return job.retries_left == 0


def send_bulk_job(log_id: int, recipient_data: list, final_message: str, delay: float = 0.1) -> None:
    app = create_app()
    with app.app_context():
        log = MessageLog.query.get(log_id)
        if not log:
            raise ValueError(f"MessageLog {log_id} not found")

        try:
            twilio = get_twilio_service()
            result = twilio.send_bulk(recipient_data, final_message, delay=delay, raise_on_transient=True)
            log.total_recipients = result['total']
            log.success_count = result['success_count']
            log.failure_count = result['failure_count']
            log.details = json.dumps(result['details'])
            log.status = 'sent' if result['failure_count'] == 0 else 'failed'
            db.session.commit()
        except TwilioTransientError as exc:
            if _should_mark_failed():
                log.status = 'failed'
                log.details = json.dumps([{'error': str(exc)}])
                db.session.commit()
            raise
        except Exception as exc:
            log.status = 'failed'
            log.details = json.dumps([{'error': str(exc)}])
            db.session.commit()
