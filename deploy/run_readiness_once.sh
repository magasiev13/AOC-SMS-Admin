#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP_ROOT="${APP_ROOT:-$(cd "${SCRIPT_DIR}/.." && pwd)}"
READINESS_APPLICATION_ROOT="${APP_ROOT}"
ENV_FILE="${TWINEVIA_ENV_FILE:-${APP_ROOT}/.env}"
PYTHON_BIN="${READINESS_APPLICATION_ROOT}/venv/bin/python"
READINESS_OUTPUT="$(mktemp)"
trap 'rm -f "${READINESS_OUTPUT}"' EXIT

if [[ -f "${ENV_FILE}" ]]; then
  set -a
  source "${ENV_FILE}"
  set +a
fi
cd "${READINESS_APPLICATION_ROOT}"
ALERT_STATE_FILE="${READINESS_ALERT_STATE_FILE:-${STATE_DIRECTORY:-${READINESS_APPLICATION_ROOT}/instance}/readiness-alert-state.json}"

send_alert() {
  local message="$1"
  if [[ -z "${ALERT_WEBHOOK_URL:-}" ]]; then
    logger -p user.warning -t twinevia-readiness -- "Alerting not configured: readiness notification could not be delivered because no alert destination is set."
    return 0
  fi
  ALERT_MESSAGE="${message}" "${PYTHON_BIN}" -c 'import json, os; print(json.dumps({"text": os.environ["ALERT_MESSAGE"]}))' \
    | curl --fail --silent --show-error --max-time 10 \
      -H 'Content-Type: application/json' \
      --data-binary @- \
      "${ALERT_WEBHOOK_URL}" >/dev/null
}

failure_message=""
if ! "${PYTHON_BIN}" -m app.readiness >"${READINESS_OUTPUT}" 2>&1; then
  failure_message="$("${PYTHON_BIN}" - "${READINESS_OUTPUT}" "$(hostname)" <<'PY'
import json
import sys
from pathlib import Path

try:
    payload = json.loads(Path(sys.argv[1]).read_text())
except (OSError, ValueError):
    payload = {}
if payload.get('state') == 'maintenance':
    maintenance = payload.get('maintenance') or {}
    details = '; '.join(f'{key}={str(maintenance.get(key) or "unknown").replace(chr(10), " ")[:400]}'
                        for key in ('held_units', 'actor', 'reason', 'declared_at', 'expires_at', 'release_sha'))
    print(f'Twinevia maintenance remains non-ready on {sys.argv[2]}: {details}')
else:
    print(f'Twinevia application readiness checks failed on {sys.argv[2]}.')
PY
  )"
fi

if ! canonical_units="$("${PYTHON_BIN}" -c 'from deploy.runtime_control import RUNTIME_UNITS; print("\n".join(RUNTIME_UNITS))')" || [[ -z "${canonical_units}" ]]; then
  failure_message="${failure_message:-Twinevia canonical runtime units could not be loaded on $(hostname).}"
else
  while IFS= read -r unit; do
    if ! systemctl is-active --quiet "${unit}"; then
      failure_message="${failure_message:-Twinevia readiness failed on $(hostname): ${unit} is not active.}"
      break
    fi
  done <<< "${canonical_units}"
fi

warning_message="$("${PYTHON_BIN}" - "${READINESS_OUTPUT}" <<'PY'
import json
import sys
from pathlib import Path

try:
    payload = json.loads(Path(sys.argv[1]).read_text())
except (OSError, ValueError):
    payload = {}
warnings = payload.get('warnings') or []
if isinstance(warnings, list):
    warnings = sorted(set(value.strip() for value in warnings if isinstance(value, str) and value.strip()))
    if warnings:
        print('Twinevia readiness warnings: ' + '; '.join(warnings))
PY
)"
notification="${failure_message}"
if [[ -n "${warning_message}" ]]; then
  notification="${notification:+${notification} }${warning_message}"
fi
if [[ -n "${notification}" ]]; then
  state_result=0
  "${PYTHON_BIN}" "${SCRIPT_DIR}/readiness_alert_state.py" due "${ALERT_STATE_FILE}" "${notification}" || state_result=$?
  if [[ "${state_result}" != "1" ]]; then
    logger -p user.warning -t twinevia-readiness -- "${notification}"
    if send_alert "${notification}"; then
      if ! "${PYTHON_BIN}" "${SCRIPT_DIR}/readiness_alert_state.py" record "${ALERT_STATE_FILE}" "${notification}"; then
        logger -p user.warning -t twinevia-readiness -- "Readiness alert state could not be saved; notifications may repeat."
      fi
    else
      logger -p user.warning -t twinevia-readiness -- "Readiness notification could not be delivered; delivery will be retried."
    fi
  fi
else
  if ! "${PYTHON_BIN}" "${SCRIPT_DIR}/readiness_alert_state.py" clear "${ALERT_STATE_FILE}"; then
    logger -p user.warning -t twinevia-readiness -- "Readiness alert state could not be cleared."
  fi
fi

if [[ -n "${failure_message}" ]]; then
  logger -t twinevia-readiness -- "${failure_message} Details: $(tr '\n' ' ' < "${READINESS_OUTPUT}" | cut -c1-2000)"
  exit 1
fi

if [[ -n "${UPTIME_MONITOR_HEARTBEAT_URL:-}" ]]; then
  curl --fail --silent --show-error --max-time 10 "${UPTIME_MONITOR_HEARTBEAT_URL}" >/dev/null
fi

logger -t twinevia-readiness -- "Twinevia readiness passed for release ${APP_RELEASE_ID:-unknown}."
