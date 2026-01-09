#!/usr/bin/env bash
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DBDOCTOR_SRC="${REPO_ROOT}/bin/dbdoctor"
DBDOCTOR_DEST="${DBDOCTOR_DEST:-/usr/local/bin/dbdoctor}"
APP_ROOT="/opt/sms-admin"
INSTANCE_DIR="${APP_ROOT}/instance"
ENV_FILE="${APP_ROOT}/.env"
DEPLOY_USER="${SUDO_USER:-$(id -un)}"
DEFAULT_REDIS_URL="redis://localhost:6379/0"
DEFAULT_RQ_QUEUE_NAME="sms"

echo "============================================"
echo "  SMS Admin Install Script"
echo "============================================"

if [[ ! -f "${DBDOCTOR_SRC}" ]]; then
  echo "ERROR: ${DBDOCTOR_SRC} not found. Did you clone the repo to ${REPO_ROOT}?" >&2
  exit 1
fi

# Install dbdoctor CLI
sudo install -m 0755 "${DBDOCTOR_SRC}" "${DBDOCTOR_DEST}"
echo "✓ Installed dbdoctor to ${DBDOCTOR_DEST}"

# Ensure .env file exists with correct permissions
sudo touch "${ENV_FILE}"
sudo chown root:smsadmin "${ENV_FILE}"
sudo chmod 660 "${ENV_FILE}"
echo "✓ Environment file configured: ${ENV_FILE}"

# Add deploy user to smsadmin group if needed
if ! id -nG "${DEPLOY_USER}" | tr ' ' '\n' | grep -qx smsadmin; then
  sudo usermod -aG smsadmin "${DEPLOY_USER}"
  echo "✓ Added ${DEPLOY_USER} to smsadmin group for .env access"
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

# ============================================
# Fix SQLite database permissions
# ============================================
# This is critical: the scheduler runs as smsadmin and needs write access
# to instance/ directory and sms.db file.
echo ""
echo "Fixing database permissions..."

sudo mkdir -p "${INSTANCE_DIR}"
sudo chown -R smsadmin:smsadmin "${INSTANCE_DIR}"
sudo chmod 750 "${INSTANCE_DIR}"

# If sms.db exists, ensure it's writable
if [[ -f "${INSTANCE_DIR}/sms.db" ]]; then
  sudo chown smsadmin:smsadmin "${INSTANCE_DIR}/sms.db"
  sudo chmod 660 "${INSTANCE_DIR}/sms.db"
  echo "✓ Database file permissions fixed: ${INSTANCE_DIR}/sms.db"
fi

# Also fix WAL and SHM files if they exist (SQLite journal files)
for ext in db-wal db-shm; do
  if [[ -f "${INSTANCE_DIR}/sms.${ext}" ]]; then
    sudo chown smsadmin:smsadmin "${INSTANCE_DIR}/sms.${ext}"
    sudo chmod 660 "${INSTANCE_DIR}/sms.${ext}"
  fi
done

echo "✓ Instance directory permissions fixed: ${INSTANCE_DIR}"

# ============================================
# Run database migrations
# ============================================
echo ""
echo "Running dbdoctor --apply..."
if ! sudo -u smsadmin "${DBDOCTOR_DEST}" --apply; then
  echo ""
  echo "✗ ERROR: dbdoctor failed. Common causes:" >&2
  echo "  1. Database is read-only. Fix with:" >&2
  echo "     sudo chown -R smsadmin:smsadmin ${INSTANCE_DIR}" >&2
  echo "     sudo chmod 750 ${INSTANCE_DIR}" >&2
  echo "     sudo chmod 660 ${INSTANCE_DIR}/sms.db" >&2
  echo "  2. Missing .env configuration" >&2
  echo "  3. Database corruption (check with: sqlite3 ${INSTANCE_DIR}/sms.db 'PRAGMA integrity_check;')" >&2
  exit 1
fi
echo "✓ Database migrations applied"

# ============================================
# Install systemd services
# ============================================
echo ""
echo "Installing systemd services..."

# Ensure deploy directory exists in target
sudo mkdir -p "${APP_ROOT}/deploy"
sudo chown smsadmin:smsadmin "${APP_ROOT}/deploy"

# Install service files
sudo install -m 0644 "${REPO_ROOT}/deploy/sms.service" /etc/systemd/system/sms.service
sudo install -m 0644 "${REPO_ROOT}/deploy/sms-worker.service" /etc/systemd/system/sms-worker.service
sudo install -m 0644 "${REPO_ROOT}/deploy/sms-scheduler.service" /etc/systemd/system/sms-scheduler.service
sudo install -m 0644 "${REPO_ROOT}/deploy/sms-scheduler.timer" /etc/systemd/system/sms-scheduler.timer
sudo install -m 0755 "${REPO_ROOT}/deploy/run_scheduler_once.sh" "${APP_ROOT}/deploy/run_scheduler_once.sh"
sudo chown smsadmin:smsadmin "${APP_ROOT}/deploy/run_scheduler_once.sh"

sudo systemctl daemon-reload
echo "✓ Systemd units installed"

# Enable main services (sms web app, sms-worker for async jobs)
sudo systemctl enable --now sms sms-worker
echo "✓ Main services enabled: sms, sms-worker"

# ============================================
# Configure scheduler timer (replaces old long-running scheduler)
# ============================================
# IMPORTANT: We use a timer + oneshot service instead of a long-running daemon
# because the old approach was unreliable (process would start/stop without
# actually processing due messages).

# Stop and disable old long-running scheduler if it exists
if systemctl is-active --quiet sms-scheduler 2>/dev/null; then
  sudo systemctl stop sms-scheduler
  echo "✓ Stopped old long-running sms-scheduler service"
fi
if systemctl is-enabled --quiet sms-scheduler 2>/dev/null; then
  sudo systemctl disable sms-scheduler
  echo "✓ Disabled old long-running sms-scheduler service"
fi

# Enable and start the scheduler timer (runs every 30 seconds)
sudo systemctl enable --now sms-scheduler.timer
echo "✓ Scheduler timer enabled: sms-scheduler.timer (runs every 30s)"

# ============================================
# Scheduler Smoke Test
# ============================================
echo ""
echo "Running scheduler smoke test..."
set +e
sudo -u smsadmin "${APP_ROOT}/deploy/run_scheduler_once.sh" 2>&1
SMOKE_EXIT=$?
set -e

if [[ ${SMOKE_EXIT} -eq 0 ]]; then
  echo "✓ Scheduler smoke test PASSED"
else
  echo ""
  echo "✗ Scheduler smoke test FAILED (exit code: ${SMOKE_EXIT})" >&2
  echo "  Common causes:" >&2
  echo "  1. Database not writable by smsadmin user" >&2
  echo "  2. Missing Twilio credentials in .env" >&2
  echo "  3. Python/venv issues" >&2
  echo "  Check logs: journalctl -u sms-scheduler.service -n 50" >&2
fi

# ============================================
# Post-Install Verification
# ============================================
echo ""
echo "============================================"
echo "  POST-INSTALL VERIFICATION"
echo "============================================"
set +e

# Check Redis
echo ""
echo "--- Redis Status ---"
if command -v redis-cli >/dev/null 2>&1; then
  REDIS_PING=$(redis-cli ping 2>&1)
  if [[ "${REDIS_PING}" == "PONG" ]]; then
    echo "✓ Redis: ${REDIS_PING}"
  else
    echo "✗ Redis: ${REDIS_PING}"
  fi
else
  echo "⚠ redis-cli not found (optional for scheduled messages)"
fi

# Check main services
echo ""
echo "--- Service Status ---"
for svc in sms sms-worker; do
  if systemctl is-active --quiet "${svc}"; then
    echo "✓ ${svc}: active"
  else
    echo "✗ ${svc}: $(systemctl is-active ${svc})"
  fi
done

# Check scheduler timer
echo ""
echo "--- Scheduler Timer ---"
if systemctl is-active --quiet sms-scheduler.timer; then
  echo "✓ sms-scheduler.timer: active"
else
  echo "✗ sms-scheduler.timer: $(systemctl is-active sms-scheduler.timer)"
fi

# Show timer schedule
echo ""
echo "--- Timer Schedule ---"
systemctl list-timers sms-scheduler.timer --no-pager 2>/dev/null || echo "Could not list timers"

# Show recent scheduler logs
echo ""
echo "--- Recent Scheduler Logs (last 10 lines) ---"
journalctl -u sms-scheduler.service -n 10 --no-pager 2>/dev/null || echo "No logs available yet"

set -e

echo ""
echo "============================================"
echo "  INSTALL COMPLETE"
echo "============================================"
echo ""
echo "Verify scheduled messages are working:"
echo "  systemctl list-timers | grep sms-scheduler"
echo "  journalctl -u sms-scheduler.service -f"
echo ""
echo "If scheduler fails with 'readonly database', re-run:"
echo "  sudo chown -R smsadmin:smsadmin ${INSTANCE_DIR}"
echo ""
