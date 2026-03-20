#!/usr/bin/env bash
# run_billing_reconcile_once.sh - Oneshot Stripe billing reconciliation runner.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP_ROOT="${APP_ROOT:-$(cd "${SCRIPT_DIR}/.." && pwd)}"
VENV_PYTHON="${APP_ROOT}/venv/bin/python"
ENV_FILE="${APP_ROOT}/.env"

"${APP_ROOT}/deploy/check_python_runtime.sh"

if [[ -f "${ENV_FILE}" ]]; then
    set -a
    # shellcheck source=/dev/null
    source "${ENV_FILE}"
    set +a
fi

cd "${APP_ROOT}"

exec "${VENV_PYTHON}" -c "
import sys
import logging
import traceback

logging.basicConfig(
    level=logging.INFO,
    format='%(levelname)s %(name)s: %(message)s',
    stream=sys.stderr
)

try:
    from app import create_app
    from app.services.billing_service import reconcile_billing_subscriptions

    app = create_app(run_startup_tasks=False, start_scheduler=False)
    with app.app_context():
        reconcile_billing_subscriptions()
    sys.exit(0)
except Exception as exc:
    logging.error('Stripe billing reconciliation failed: %s', exc)
    traceback.print_exc()
    sys.exit(1)
"
