#!/usr/bin/env bash
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DBDOCTOR_SRC="${REPO_ROOT}/bin/dbdoctor"
DBDOCTOR_DEST="${DBDOCTOR_DEST:-/usr/local/bin/dbdoctor}"
ENV_FILE="/opt/sms-admin/.env"
DEPLOY_USER="${SUDO_USER:-$(id -un)}"
DEFAULT_REDIS_URL="redis://localhost:6379/0"
DEFAULT_RQ_QUEUE_NAME="sms"

if [[ ! -f "${DBDOCTOR_SRC}" ]]; then
  echo "ERROR: ${DBDOCTOR_SRC} not found. Did you clone the repo to ${REPO_ROOT}?" >&2
  exit 1
fi

sudo install -m 0755 "${DBDOCTOR_SRC}" "${DBDOCTOR_DEST}"

echo "Installed dbdoctor to ${DBDOCTOR_DEST}"

sudo touch "${ENV_FILE}"
sudo chown root:smsadmin "${ENV_FILE}"
sudo chmod 660 "${ENV_FILE}"

if ! id -nG "${DEPLOY_USER}" | tr ' ' '\n' | grep -qx smsadmin; then
  sudo usermod -aG smsadmin "${DEPLOY_USER}"
  echo "Added ${DEPLOY_USER} to smsadmin group for .env access."
fi

ensure_env_key() {
  local key="$1"
  local value="$2"
  if ! sudo grep -qE "^${key}=" "${ENV_FILE}"; then
    echo "${key}=${value}" | sudo tee -a "${ENV_FILE}" >/dev/null
  fi
}

ensure_env_key "REDIS_URL" "${DEFAULT_REDIS_URL}"
ensure_env_key "RQ_QUEUE_NAME" "${DEFAULT_RQ_QUEUE_NAME}"

echo "Running dbdoctor --apply"
sudo "${DBDOCTOR_DEST}" --apply

# Ensure deploy directory exists in target
sudo mkdir -p /opt/sms-admin/deploy

sudo install -m 0644 "${REPO_ROOT}/deploy/sms.service" /etc/systemd/system/sms.service
sudo install -m 0644 "${REPO_ROOT}/deploy/sms-worker.service" /etc/systemd/system/sms-worker.service
sudo install -m 0644 "${REPO_ROOT}/deploy/sms-scheduler.service" /etc/systemd/system/sms-scheduler.service
sudo install -m 0644 "${REPO_ROOT}/deploy/sms-scheduler.timer" /etc/systemd/system/sms-scheduler.timer
sudo install -m 0755 "${REPO_ROOT}/deploy/run_scheduler_once.sh" /opt/sms-admin/deploy/run_scheduler_once.sh
sudo chown smsadmin:smsadmin /opt/sms-admin/deploy/run_scheduler_once.sh

sudo systemctl daemon-reload

# Enable main services (sms, sms-worker)
sudo systemctl enable --now sms sms-worker

# Stop the old long-running scheduler service if it exists, use timer instead
sudo systemctl stop sms-scheduler 2>/dev/null || true
sudo systemctl disable sms-scheduler 2>/dev/null || true

# Enable and start the scheduler timer (runs sms-scheduler.service every 60s)
sudo systemctl enable --now sms-scheduler.timer

echo ""
echo "============================================"
echo "  Scheduler Smoke Test"
echo "============================================"
echo "Running scheduler once to verify DB connection..."
set +e
sudo -u smsadmin /opt/sms-admin/deploy/run_scheduler_once.sh
SMOKE_EXIT=$?
set -e
if [[ ${SMOKE_EXIT} -eq 0 ]]; then
  echo "✓ Scheduler smoke test PASSED"
else
  echo "✗ Scheduler smoke test FAILED (exit code: ${SMOKE_EXIT})"
  echo "  Check logs: journalctl -u sms-scheduler.service -n 50"
fi

echo ""
echo "============================================"
echo "  Deploy Report"
echo "============================================"
set +e
if command -v redis-cli >/dev/null 2>&1; then
  echo "Redis: $(redis-cli ping)"
else
  echo "Redis: redis-cli not found"
fi
echo ""
echo "Service Status:"
systemctl status --no-pager sms sms-worker
echo ""
echo "Scheduler Timer Status:"
systemctl status --no-pager sms-scheduler.timer
echo ""
echo "Active Timers:"
systemctl list-timers --no-pager sms-scheduler.timer
echo ""
echo "Recent Scheduler Logs:"
journalctl -u sms-scheduler.service -n 20 --no-pager
set -e
