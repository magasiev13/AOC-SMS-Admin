#!/usr/bin/env bash
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
APP_ROOT="${APP_ROOT:-/opt/sms-saas}"
APP_USER="${APP_USER:-smsadmin}"
APP_GROUP="${APP_GROUP:-smsadmin}"
ENV_FILE="${APP_ROOT}/.env"
VENV_BIN="${APP_ROOT}/venv/bin"
PYTHON_BIN="${VENV_BIN}/python"
SAAS_DBDOCTOR_SRC="${REPO_ROOT}/bin/saas-dbdoctor"
SAAS_DBDOCTOR_DEST="${SAAS_DBDOCTOR_DEST:-/usr/local/bin/saas-dbdoctor}"
LOG_DIR="${LOG_DIR:-/var/log/sms-saas}"
REQUIRED_PYTHON="3.11"

echo "============================================"
echo "  SMS SaaS Install Script"
echo "============================================"

if [[ ! -f "${SAAS_DBDOCTOR_SRC}" ]]; then
  echo "ERROR: ${SAAS_DBDOCTOR_SRC} not found." >&2
  exit 1
fi

if [[ ! -x "${PYTHON_BIN}" ]]; then
  if ! command -v python3.11 >/dev/null 2>&1; then
    echo "ERROR: ${PYTHON_BIN} not found and python3.11 is unavailable." >&2
    exit 1
  fi
  sudo -u "${APP_USER}" bash -c "cd \"${APP_ROOT}\" && python3.11 -m venv venv"
fi

PYTHON_VERSION="$("${PYTHON_BIN}" -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')"
if [[ "${PYTHON_VERSION}" != "${REQUIRED_PYTHON}" ]]; then
  echo "ERROR: ${PYTHON_BIN} uses Python ${PYTHON_VERSION}; expected ${REQUIRED_PYTHON}." >&2
  exit 1
fi

sudo install -m 0755 "${SAAS_DBDOCTOR_SRC}" "${SAAS_DBDOCTOR_DEST}"
echo "✓ Installed saas-dbdoctor to ${SAAS_DBDOCTOR_DEST}"

sudo touch "${ENV_FILE}"
sudo chown root:${APP_GROUP} "${ENV_FILE}"
sudo chmod 660 "${ENV_FILE}"

ensure_env_key() {
  local key="$1"
  local value="$2"
  if ! sudo grep -qE "^${key}=" "${ENV_FILE}"; then
    echo "${key}=${value}" | sudo tee -a "${ENV_FILE}" >/dev/null
    echo "✓ Appended missing key ${key}"
  fi
}

current_env_value() {
  local key="$1"
  sudo grep -E "^${key}=" "${ENV_FILE}" | tail -n1 | cut -d= -f2- || true
}

ensure_env_key "SAAS_MODE" "1"
ensure_env_key "SCHEDULER_ENABLED" "0"
ensure_env_key "RQ_QUEUE_NAME" "sms-saas"
ensure_env_key "REDIS_URL" "redis://localhost:6379/0"

required_keys=(
  DATABASE_URL
  SAAS_BASE_URL
  STRIPE_SECRET_KEY
  STRIPE_WEBHOOK_SECRET
  STRIPE_PRICE_ID
  TWILIO_ACCOUNT_SID
  TWILIO_AUTH_TOKEN
  SECRET_KEY
)

missing_required=()
for key in "${required_keys[@]}"; do
  value="$(current_env_value "${key}")"
  if [[ -z "${value}" ]]; then
    missing_required+=("${key}")
  fi
done

if [[ ${#missing_required[@]} -gt 0 ]]; then
  echo "ERROR: Missing required SaaS env keys in ${ENV_FILE}: ${missing_required[*]}" >&2
  exit 1
fi

echo "==> Installing Python dependencies"
sudo -u "${APP_USER}" "${VENV_BIN}/pip" install -r "${APP_ROOT}/requirements.txt"

echo "==> Applying SaaS schema"
sudo -u "${APP_USER}" bash -lc "set -euo pipefail; cd \"${APP_ROOT}\"; set -a; source \"${ENV_FILE}\"; set +a; \"${SAAS_DBDOCTOR_DEST}\" --apply && \"${SAAS_DBDOCTOR_DEST}\" --ensure-platform-admin && \"${SAAS_DBDOCTOR_DEST}\" --doctor"

sudo mkdir -p "${LOG_DIR}"
sudo chown -R "${APP_USER}:${APP_GROUP}" "${LOG_DIR}"

echo "==> Installing SaaS systemd units"
sudo install -m 0644 "${REPO_ROOT}/deploy/sms-saas.service" /etc/systemd/system/sms-saas.service
sudo install -m 0644 "${REPO_ROOT}/deploy/sms-saas-worker.service" /etc/systemd/system/sms-saas-worker.service
sudo install -m 0644 "${REPO_ROOT}/deploy/sms-saas-scheduler.service" /etc/systemd/system/sms-saas-scheduler.service
sudo install -m 0644 "${REPO_ROOT}/deploy/sms-saas-scheduler.timer" /etc/systemd/system/sms-saas-scheduler.timer
sudo install -m 0644 "${REPO_ROOT}/deploy/sms-saas-billing-reconcile.service" /etc/systemd/system/sms-saas-billing-reconcile.service
sudo install -m 0644 "${REPO_ROOT}/deploy/sms-saas-billing-reconcile.timer" /etc/systemd/system/sms-saas-billing-reconcile.timer
sudo install -m 0755 "${REPO_ROOT}/deploy/check_python_runtime.sh" "${APP_ROOT}/deploy/check_python_runtime.sh"
sudo install -m 0755 "${REPO_ROOT}/deploy/run_scheduler_once.sh" "${APP_ROOT}/deploy/run_scheduler_once.sh"
sudo install -m 0755 "${REPO_ROOT}/deploy/run_worker.sh" "${APP_ROOT}/deploy/run_worker.sh"
sudo install -m 0755 "${REPO_ROOT}/deploy/run_billing_reconcile_once.sh" "${APP_ROOT}/deploy/run_billing_reconcile_once.sh"

sudo systemctl daemon-reload
sudo systemctl enable --now sms-saas sms-saas-worker sms-saas-scheduler.timer sms-saas-billing-reconcile.timer

echo "==> Verifying SaaS health"
if ! curl -fsS --connect-timeout 2 --max-time 5 http://127.0.0.1:8100/health >/dev/null; then
  echo "WARNING: SaaS health check failed on http://127.0.0.1:8100/health" >&2
  echo "Check: journalctl -u sms-saas -n 100 --no-pager" >&2
fi

echo "SaaS install completed."
