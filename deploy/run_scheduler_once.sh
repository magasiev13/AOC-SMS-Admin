#!/usr/bin/env bash
# run_scheduler_once.sh - Oneshot scheduler runner for systemd timer
# Activates venv, loads environment, runs send_scheduled_messages() once, then exits.
# Exit code: 0 on success, non-zero on failure (systemd will log the error).

set -euo pipefail

APP_ROOT="${APP_ROOT:-/opt/sms-admin}"
VENV_PYTHON="${APP_ROOT}/venv/bin/python"
ENV_FILE="${APP_ROOT}/.env"

"${APP_ROOT}/deploy/check_python_runtime.sh"

# Load environment file if it exists
if [[ -f "${ENV_FILE}" ]]; then
    set -a
    # shellcheck source=/dev/null
    source "${ENV_FILE}"
    set +a
fi

# Change to app directory
cd "${APP_ROOT}"

# Run the scheduler once
exec "${VENV_PYTHON}" -c "
import sys
import logging
import traceback

# Configure logging to stderr (captured by systemd journal)
logging.basicConfig(
    level=logging.INFO,
    format='%(levelname)s %(name)s: %(message)s',
    stream=sys.stderr
)

try:
    from app import create_app
    from app.services.scheduler_service import send_scheduled_messages

    app = create_app(run_startup_tasks=False)
    send_scheduled_messages(app)
    sys.exit(0)
except Exception as e:
    logging.error('Scheduler failed: %s', e)
    traceback.print_exc()
    sys.exit(1)
"
