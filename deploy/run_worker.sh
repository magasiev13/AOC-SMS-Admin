#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP_ROOT="${APP_ROOT:-$(cd "${SCRIPT_DIR}/.." && pwd)}"
PATH="/usr/bin:/bin:${APP_ROOT}/venv/bin"

"${APP_ROOT}/deploy/check_python_runtime.sh"

if [ -f "${APP_ROOT}/.env" ]; then
  set -a
  . "${APP_ROOT}/.env"
  set +a
fi

: "${REDIS_URL:=redis://localhost:6379/0}"
: "${RQ_QUEUE_NAME:=sms}"

exec "${APP_ROOT}/venv/bin/python" -m rq worker --url "$REDIS_URL" "$RQ_QUEUE_NAME"
