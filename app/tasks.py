import json
import time
from rq import get_current_job
from app import create_app, db
from app.models import MessageLog
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


def send_bulk_job(log_id: int, recipient_data: list, final_message: str, delay: float = 0.1) -> None:
    app = create_app()
    with app.app_context():
        log = MessageLog.query.get(log_id)
        if not log:
            raise ValueError(f"MessageLog {log_id} not found")

        details = _load_details(log)
        success_count = sum(1 for detail in details if detail.get('success'))
        failure_count = sum(1 for detail in details if not detail.get('success'))
        total_recipients = len(recipient_data)
        start_index = len(details)

        twilio = get_twilio_service()

        for recipient in recipient_data[start_index:]:
            phone = recipient.get('phone')
            name = recipient.get('name', '')

            try:
                result = twilio.send_message(phone, final_message, raise_on_transient=True)
            except TwilioTransientError as exc:
                if _should_mark_failed():
                    details.append({
                        'phone': phone,
                        'name': name,
                        'success': False,
                        'error': str(exc),
                    })
                    failure_count += 1
                    log.status = 'failed'
                _persist_progress(log, total_recipients, success_count, failure_count, details)
                raise
            except Exception as exc:
                details.append({
                    'phone': phone,
                    'name': name,
                    'success': False,
                    'error': str(exc),
                })
                failure_count += 1
                log.status = 'failed'
                _persist_progress(log, total_recipients, success_count, failure_count, details)
                return

            details.append({
                'phone': phone,
                'name': name,
                'success': result['success'],
                'error': result.get('error'),
            })

            if result['success']:
                success_count += 1
            else:
                failure_count += 1

            if delay > 0:
                time.sleep(delay)

        log.status = 'sent' if failure_count == 0 else 'failed'
        _persist_progress(log, total_recipients, success_count, failure_count, details)
