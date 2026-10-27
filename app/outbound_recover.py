"""Explicit oneshot outbox recovery; uses configured SaaS database and RQ queue."""
import json

from app import create_app
from app.services.outbound_request_service import reconcile_outbound_requests


def main():
    app = create_app(run_startup_tasks=False, start_scheduler=False)
    with app.app_context():
        if not app.config.get('SAAS_MODE'):
            raise RuntimeError('Outbound request recovery requires SAAS_MODE=1.')
        print(json.dumps(reconcile_outbound_requests(), sort_keys=True))


if __name__ == '__main__':
    main()
