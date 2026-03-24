#!/usr/bin/env bash
# run_platform_restart_queue_once.sh - Process one durable platform restart request.

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
import json
import logging
import sys
import traceback

logging.basicConfig(
    level=logging.INFO,
    format='%(levelname)s %(name)s: %(message)s',
    stream=sys.stderr
)

try:
    from app import create_app
    from app.services.platform_operations_service import process_platform_service_restart_queue

    app = create_app(run_startup_tasks=False, start_scheduler=False)
    with app.app_context():
        summary = process_platform_service_restart_queue()
    logging.info('Platform restart queue summary: %s', json.dumps(summary, sort_keys=True, default=str))
    sys.exit(0)
except Exception as exc:
    logging.error('Platform restart queue processor failed: %s', exc)
    traceback.print_exc()
    sys.exit(1)
"
