#!/usr/bin/env bash
set -euo pipefail

readonly UNITS=(
  "twinevia-saas"
  "twinevia-saas-worker"
  "twinevia-saas-scheduler.timer"
  "twinevia-saas-billing-reconcile.timer"
)
readonly TRANSIENT_UNIT_PATTERN='^twinevia-saas-manual-restart-[A-Za-z0-9_.:@-]+(\.service)?$'

emit_json() {
  python3 - "$@" <<'PY'
import json
import sys

payload = {}
for raw_pair in sys.argv[1:]:
    key, value = raw_pair.split("=", 1)
    payload[key] = None if value == "__NULL__" else value
print(json.dumps(payload))
PY
}

check_units() {
  local unit
  for unit in "${UNITS[@]}"; do
    if ! systemctl cat "${unit}" >/dev/null 2>&1; then
      echo "Missing required systemd unit: ${unit}" >&2
      exit 1
    fi
  done
}

queue_restart() {
  local restart_unit
  local restart_command
  local run_output
  local detail

  check_units

  restart_unit="twinevia-saas-manual-restart-$(date +%s)-$$"
  read -r -d '' restart_command <<'EOF' || true
sleep 2
systemctl restart twinevia-saas twinevia-saas-worker twinevia-saas-scheduler.timer twinevia-saas-billing-reconcile.timer
systemctl is-active --quiet twinevia-saas
systemctl is-active --quiet twinevia-saas-worker
systemctl is-active --quiet twinevia-saas-scheduler.timer
systemctl is-active --quiet twinevia-saas-billing-reconcile.timer
EOF

  if ! run_output="$(systemd-run \
    --quiet \
    --unit="${restart_unit}" \
    --description="Queued Twinevia SaaS service restart" \
    --property=Type=oneshot \
    /bin/bash -lc "${restart_command}" 2>&1)"; then
    detail="$(printf '%s' "${run_output}" | tail -n1 | tr -d '\r')"
    emit_json \
      "status=failed" \
      "summary=Failed to queue the SaaS restart." \
      "detail=${detail:-systemd-run failed.}" \
      "transient_unit=__NULL__"
    exit 1
  fi

  emit_json \
    "status=queued" \
    "summary=Restart queued. The SaaS services will recycle shortly." \
    "detail=Queued SaaS service restart using transient unit ${restart_unit}." \
    "transient_unit=${restart_unit}"
}

status_restart() {
  local transient_unit="$1"
  local show_output
  local show_status
  local load_state="unknown"
  local active_state="unknown"
  local sub_state="unknown"
  local result="unknown"
  local exec_main_status="unknown"
  local line
  local key
  local value

  if [[ ! "${transient_unit}" =~ ${TRANSIENT_UNIT_PATTERN} ]]; then
    emit_json \
      "status=failed" \
      "summary=Restart status check rejected." \
      "detail=Unsupported transient unit name." \
      "transient_unit=${transient_unit}"
    exit 64
  fi

  if ! show_output="$(systemctl show "${transient_unit}" \
    --no-pager \
    --property=LoadState \
    --property=ActiveState \
    --property=SubState \
    --property=Result \
    --property=ExecMainStatus 2>&1)"; then
    emit_json \
      "status=failed" \
      "summary=Restart status unavailable." \
      "detail=$(printf 'Failed to inspect transient unit %s: %s' "${transient_unit}" "$(printf '%s' "${show_output}" | tail -n1 | tr -d '\r')")" \
      "transient_unit=${transient_unit}"
    exit 0
  fi

  while IFS= read -r line; do
    key="${line%%=*}"
    value="${line#*=}"
    case "${key}" in
      LoadState) load_state="${value}" ;;
      ActiveState) active_state="${value}" ;;
      SubState) sub_state="${value}" ;;
      Result) result="${value}" ;;
      ExecMainStatus) exec_main_status="${value}" ;;
    esac
  done <<< "${show_output}"

  if [[ "${load_state}" == "not-found" ]]; then
    emit_json \
      "status=failed" \
      "summary=Restart status unavailable." \
      "detail=Transient unit ${transient_unit} was not found." \
      "transient_unit=${transient_unit}"
    exit 0
  fi

  if [[ "${active_state}" == "active" || "${active_state}" == "activating" ]]; then
    emit_json \
      "status=queued" \
      "summary=Restart queued. The SaaS services are restarting." \
      "detail=Transient unit ${transient_unit} is ${active_state}/${sub_state}." \
      "transient_unit=${transient_unit}"
    exit 0
  fi

  if [[ "${result}" == "success" || "${exec_main_status}" == "0" ]]; then
    emit_json \
      "status=succeeded" \
      "summary=Restart completed successfully." \
      "detail=Transient unit ${transient_unit} completed with result ${result}." \
      "transient_unit=${transient_unit}"
    exit 0
  fi

  emit_json \
    "status=failed" \
    "summary=Restart failed." \
    "detail=Transient unit ${transient_unit} finished with load=${load_state}, active=${active_state}, sub=${sub_state}, result=${result}, exit=${exec_main_status}." \
    "transient_unit=${transient_unit}"
}

main() {
  case "${1:-}" in
    --check)
      if [[ $# -ne 1 ]]; then
        echo "Unsupported arguments." >&2
        exit 64
      fi
      check_units
      echo "restart-twinevia-saas-services helper is installed."
      ;;
    --status)
      if [[ $# -ne 2 ]]; then
        echo "Usage: $0 --status <transient-unit>" >&2
        exit 64
      fi
      status_restart "$2"
      ;;
    "")
      if [[ $# -ne 0 ]]; then
        echo "Unsupported arguments." >&2
        exit 64
      fi
      queue_restart
      ;;
    *)
      echo "Unsupported arguments." >&2
      exit 64
      ;;
  esac
}

main "$@"
