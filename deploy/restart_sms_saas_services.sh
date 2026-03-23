#!/usr/bin/env bash
set -euo pipefail

readonly UNITS=(
  "sms-saas"
  "sms-saas-worker"
  "sms-saas-scheduler.timer"
  "sms-saas-billing-reconcile.timer"
)

check_units() {
  local unit
  for unit in "${UNITS[@]}"; do
    if ! systemctl cat "${unit}" >/dev/null 2>&1; then
      echo "Missing required systemd unit: ${unit}" >&2
      exit 1
    fi
  done
}

if [[ "${1:-}" == "--check" ]]; then
  check_units
  echo "restart-sms-saas-services helper is installed."
  exit 0
fi

if [[ $# -gt 0 ]]; then
  echo "Unsupported arguments." >&2
  exit 64
fi

check_units

readonly RESTART_UNIT="sms-saas-manual-restart-$(date +%s)-$$"
read -r -d '' RESTART_COMMAND <<'EOF' || true
sleep 2
systemctl restart sms-saas sms-saas-worker sms-saas-scheduler.timer sms-saas-billing-reconcile.timer
systemctl is-active --quiet sms-saas
systemctl is-active --quiet sms-saas-worker
systemctl is-active --quiet sms-saas-scheduler.timer
systemctl is-active --quiet sms-saas-billing-reconcile.timer
EOF

systemd-run \
  --quiet \
  --unit="${RESTART_UNIT}" \
  --description="Queued SMS SaaS service restart" \
  --property=Type=oneshot \
  /bin/bash -lc "${RESTART_COMMAND}"

echo "Queued SaaS service restart using transient unit ${RESTART_UNIT}."
