#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP_ROOT="${APP_ROOT:-$(cd "${SCRIPT_DIR}/.." && pwd)}"
ENV_FILE="${TWINEVIA_ENV_FILE:-${APP_ROOT}/.env}"
PYTHON_BIN="${APP_ROOT}/venv/bin/python"
READINESS_OUTPUT="$(mktemp)"
trap 'rm -f "${READINESS_OUTPUT}"' EXIT

if [[ -f "${ENV_FILE}" ]]; then
  set -a
  source "${ENV_FILE}"
  set +a
fi

send_alert() {
  local message="$1"
  if [[ -z "${ALERT_WEBHOOK_URL:-}" ]]; then
    return
  fi
  ALERT_MESSAGE="${message}" "${PYTHON_BIN}" -c 'import json, os; print(json.dumps({"text": os.environ["ALERT_MESSAGE"]}))' \
    | curl --fail --silent --show-error --max-time 10 \
      -H 'Content-Type: application/json' \
      --data-binary @- \
      "${ALERT_WEBHOOK_URL}" >/dev/null
}

failure_message=""
if ! "${PYTHON_BIN}" -m app.readiness >"${READINESS_OUTPUT}" 2>&1; then
  failure_message="Twinevia application readiness checks failed on $(hostname)."
fi

required_units=(
  twinevia-saas.service
  twinevia-saas-worker.service
  twinevia-saas-scheduler.timer
  twinevia-saas-billing-reconcile.timer
  twinevia-saas-platform-restart-queue.timer
  twinevia-saas-a2p-reconcile.timer
  twinevia-saas-backup.timer
)
for unit in "${required_units[@]}"; do
  if ! systemctl is-active --quiet "${unit}"; then
    failure_message="Twinevia readiness failed on $(hostname): ${unit} is not active."
    break
  fi
done

if [[ -n "${failure_message}" ]]; then
  logger -t twinevia-readiness -- "${failure_message} Details: $(tr '\n' ' ' < "${READINESS_OUTPUT}" | cut -c1-2000)"
  send_alert "${failure_message}"
  exit 1
fi

if [[ -n "${UPTIME_MONITOR_HEARTBEAT_URL:-}" ]]; then
  curl --fail --silent --show-error --max-time 10 "${UPTIME_MONITOR_HEARTBEAT_URL}" >/dev/null
fi

logger -t twinevia-readiness -- "Twinevia readiness passed for release ${APP_RELEASE_ID:-unknown}."
