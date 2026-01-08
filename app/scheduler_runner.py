import signal
import time

from app import create_app
from app.services.scheduler_service import shutdown_scheduler


_shutdown_requested = False


def _handle_shutdown(signum, frame):
    global _shutdown_requested
    _shutdown_requested = True


signal.signal(signal.SIGTERM, _handle_shutdown)
signal.signal(signal.SIGINT, _handle_shutdown)


def main():
    create_app(start_scheduler=True)

    while not _shutdown_requested:
        time.sleep(1)

    shutdown_scheduler()


if __name__ == '__main__':
    main()
