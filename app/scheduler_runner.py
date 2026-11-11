import signal
import time

from app import create_runtime_app
from app.services.scheduler_service import shutdown_scheduler


_shutdown_requested = False


def _handle_shutdown(signum, frame):
    global _shutdown_requested
    _shutdown_requested = True


def run_once(app):
    """Run the timer's recovery and scheduling work with quiet idle summaries."""
    from app.services.automation_outbox_service import recover_automation_replies
    from app.services.outbound_request_service import reconcile_outbound_requests
    from app.services.scheduler_service import send_scheduled_messages

    with app.app_context():
        if app.config.get('SAAS_MODE'):
            outbound = reconcile_outbound_requests()
            if any(outbound.values()):
                app.logger.info('Outbound recovery: %s', outbound)
            replies = recover_automation_replies()
            if any(replies.values()):
                app.logger.info('Automatic reply recovery: %s', replies)
    send_scheduled_messages(app)


def main():
    signal.signal(signal.SIGTERM, _handle_shutdown)
    signal.signal(signal.SIGINT, _handle_shutdown)
    app = create_runtime_app(start_scheduler=True)

    while not _shutdown_requested:
        time.sleep(1)

    shutdown_scheduler()


if __name__ == '__main__':
    main()
