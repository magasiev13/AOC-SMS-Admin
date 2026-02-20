#!/usr/bin/env bash
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DBDOCTOR_SRC="${REPO_ROOT}/bin/dbdoctor"
DBDOCTOR_DEST="${DBDOCTOR_DEST:-/usr/local/bin/dbdoctor}"
DEPLOY_SCRIPT_SRC="${REPO_ROOT}/deploy/deploy_sms_admin.sh"
DEPLOY_SCRIPT_DEST="${DEPLOY_SCRIPT_DEST:-/usr/local/bin/deploy_sms_admin.sh}"
APP_ROOT="/opt/sms-admin"
INSTANCE_DIR="${APP_ROOT}/instance"
ENV_FILE="${APP_ROOT}/.env"
DEPLOY_USER="${SUDO_USER:-$(id -un)}"
DEFAULT_REDIS_URL="redis://localhost:6379/0"
DEFAULT_RQ_QUEUE_NAME="sms"
DEFAULT_TRUSTED_HOSTS="${DEFAULT_TRUSTED_HOSTS:-sms.theitwingman.com}"
REQUIRED_PYTHON="3.11"
APP_PYTHON_BIN="${APP_ROOT}/venv/bin/python"

echo "============================================"
echo "  SMS Admin Install Script"
echo "============================================"

if [[ ! -f "${DBDOCTOR_SRC}" ]]; then
  echo "ERROR: ${DBDOCTOR_SRC} not found. Did you clone the repo to ${REPO_ROOT}?" >&2
  exit 1
fi
if [[ ! -f "${DEPLOY_SCRIPT_SRC}" ]]; then
  echo "ERROR: ${DEPLOY_SCRIPT_SRC} not found. Did you clone the repo to ${REPO_ROOT}?" >&2
  exit 1
fi

if [[ ! -x "${APP_PYTHON_BIN}" ]]; then
  if ! command -v python3.11 >/dev/null 2>&1; then
    echo "ERROR: ${APP_PYTHON_BIN} not found and python3.11 is not installed." >&2
    echo "Install python3.11 and python3.11-venv, then rerun install.sh." >&2
    exit 1
  fi
  echo "Creating virtualenv with python3.11 at ${APP_ROOT}/venv ..."
  sudo -u smsadmin bash -c "cd \"${APP_ROOT}\" && python3.11 -m venv venv"
fi

APP_PYTHON_VERSION="$("${APP_PYTHON_BIN}" -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')"
if [[ "${APP_PYTHON_VERSION}" != "${REQUIRED_PYTHON}" ]]; then
  echo "ERROR: ${APP_PYTHON_BIN} uses Python ${APP_PYTHON_VERSION}; expected ${REQUIRED_PYTHON}." >&2
  echo "Recreate the venv with python3.11 before running install.sh." >&2
  exit 1
fi

# Install dbdoctor CLI
sudo install -m 0755 "${DBDOCTOR_SRC}" "${DBDOCTOR_DEST}"
echo "✓ Installed dbdoctor to ${DBDOCTOR_DEST}"

# Install deploy helper CLI
sudo install -m 0755 "${DEPLOY_SCRIPT_SRC}" "${DEPLOY_SCRIPT_DEST}"
echo "✓ Installed deploy script to ${DEPLOY_SCRIPT_DEST}"

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
    echo "✓ Appended missing key ${key}"
  fi
}

warn_if_non_recommended() {
  local key="$1"
  local recommended="$2"
  local current
  current="$(sudo grep -E "^${key}=" "${ENV_FILE}" | tail -n1 | cut -d= -f2- || true)"
  if [[ -n "${current}" && "${current}" != "${recommended}" ]]; then
    echo "⚠ ${key} is '${current}' (recommended: '${recommended}')"
  fi
}

ensure_env_key "REDIS_URL" "${DEFAULT_REDIS_URL}"
ensure_env_key "RQ_QUEUE_NAME" "${DEFAULT_RQ_QUEUE_NAME}"
ensure_env_key "AUTH_ATTEMPT_WINDOW_SECONDS" "300"
ensure_env_key "AUTH_LOCKOUT_SECONDS" "900"
ensure_env_key "AUTH_MAX_ATTEMPTS_IP_ACCOUNT" "5"
ensure_env_key "AUTH_MAX_ATTEMPTS_ACCOUNT" "8"
ensure_env_key "AUTH_MAX_ATTEMPTS_IP" "30"
ensure_env_key "SESSION_IDLE_TIMEOUT_MINUTES" "30"
ensure_env_key "REMEMBER_COOKIE_DURATION_DAYS" "7"
ensure_env_key "AUTH_PASSWORD_MIN_LENGTH" "12"
ensure_env_key "AUTH_PASSWORD_POLICY_ENFORCE" "1"
ensure_env_key "TRUSTED_HOSTS" "${DEFAULT_TRUSTED_HOSTS}"

warn_if_non_recommended "AUTH_ATTEMPT_WINDOW_SECONDS" "300"
warn_if_non_recommended "AUTH_LOCKOUT_SECONDS" "900"
warn_if_non_recommended "AUTH_MAX_ATTEMPTS_IP_ACCOUNT" "5"
warn_if_non_recommended "AUTH_MAX_ATTEMPTS_ACCOUNT" "8"
warn_if_non_recommended "AUTH_MAX_ATTEMPTS_IP" "30"
warn_if_non_recommended "SESSION_IDLE_TIMEOUT_MINUTES" "30"
warn_if_non_recommended "REMEMBER_COOKIE_DURATION_DAYS" "7"
warn_if_non_recommended "AUTH_PASSWORD_MIN_LENGTH" "12"
warn_if_non_recommended "AUTH_PASSWORD_POLICY_ENFORCE" "1"
warn_if_non_recommended "TRUSTED_HOSTS" "${DEFAULT_TRUSTED_HOSTS}"

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
  sudo chmod 640 "${INSTANCE_DIR}/sms.db"
  echo "✓ Database file permissions fixed: ${INSTANCE_DIR}/sms.db"
fi
sudo chmod 640 "${INSTANCE_DIR}/sms.db" 2>/dev/null || true

# Also fix WAL and SHM files if they exist (SQLite journal files)
for ext in db-wal db-shm; do
  if [[ -f "${INSTANCE_DIR}/sms.${ext}" ]]; then
    sudo chown smsadmin:smsadmin "${INSTANCE_DIR}/sms.${ext}"
    sudo chmod 640 "${INSTANCE_DIR}/sms.${ext}"
  fi
done

echo "✓ Instance directory permissions fixed: ${INSTANCE_DIR}"

# ============================================
# Run database migrations
# ============================================
echo ""
echo "Running dbdoctor --apply..."
if ! sudo -u smsadmin bash -c "cd \"${APP_ROOT}\" && \"${DBDOCTOR_DEST}\" --apply"; then
  echo ""
  echo "✗ ERROR: dbdoctor failed. Common causes:" >&2
  echo "  1. Database is read-only. Fix with:" >&2
  echo "     sudo chown -R smsadmin:smsadmin ${INSTANCE_DIR}" >&2
  echo "     sudo chmod 750 ${INSTANCE_DIR}" >&2
  echo "     sudo chmod 640 ${INSTANCE_DIR}/sms.db" >&2
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
sudo chown -R smsadmin:smsadmin "${APP_ROOT}/deploy"
sudo chmod 755 "${APP_ROOT}/deploy"

# Install service files
sudo install -m 0644 "${REPO_ROOT}/deploy/sms.service" /etc/systemd/system/sms.service
sudo install -m 0644 "${REPO_ROOT}/deploy/sms-worker.service" /etc/systemd/system/sms-worker.service
sudo install -m 0644 "${REPO_ROOT}/deploy/sms-scheduler.service" /etc/systemd/system/sms-scheduler.service
sudo install -m 0644 "${REPO_ROOT}/deploy/sms-scheduler.timer" /etc/systemd/system/sms-scheduler.timer
sudo install -m 0755 "${REPO_ROOT}/deploy/check_python_runtime.sh" "${APP_ROOT}/deploy/check_python_runtime.sh"
sudo install -m 0644 "${REPO_ROOT}/deploy/run_scheduler_once.sh" "${APP_ROOT}/deploy/run_scheduler_once.sh"
sudo install -m 0644 "${REPO_ROOT}/deploy/run_worker.sh" "${APP_ROOT}/deploy/run_worker.sh"
sudo chown smsadmin:smsadmin "${APP_ROOT}/deploy/check_python_runtime.sh"
sudo chown smsadmin:smsadmin "${APP_ROOT}/deploy/run_scheduler_once.sh"
sudo chown smsadmin:smsadmin "${APP_ROOT}/deploy/run_worker.sh"
sudo chmod 755 "${APP_ROOT}/deploy/check_python_runtime.sh"
sudo chmod 644 "${APP_ROOT}/deploy/run_scheduler_once.sh" "${APP_ROOT}/deploy/run_worker.sh"

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
systemctl is-active sms-worker sms 2>/dev/null || true

# Check scheduler timer
echo ""
echo "--- Scheduler Timer ---"
systemctl is-active sms-scheduler.timer 2>/dev/null || true

# Show timer schedule
echo ""
echo "--- Timer Schedule ---"
systemctl list-timers --no-pager 2>/dev/null | grep sms-scheduler || echo "No scheduler timer found"

# Show recent scheduler logs
echo ""
echo "--- Recent Scheduler Logs (last 30 lines) ---"
journalctl -u sms-scheduler.service -n 30 --no-pager 2>/dev/null || echo "No logs available yet"

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
