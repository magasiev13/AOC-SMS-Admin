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
RESTART_HELPER_SRC="${REPO_ROOT}/deploy/restart_sms_saas_services.sh"
RESTART_HELPER_DEST="${RESTART_HELPER_DEST:-/usr/local/bin/restart-sms-saas-services}"
RESTART_SUDOERS_SRC="${REPO_ROOT}/deploy/sms-saas-restart.sudoers"
RESTART_SUDOERS_DEST="${RESTART_SUDOERS_DEST:-/etc/sudoers.d/sms-saas-restart}"
VISUDO_BIN="${VISUDO_BIN:-/usr/sbin/visudo}"
LOG_DIR="${LOG_DIR:-/var/log/sms-saas}"
REQUIRED_PYTHON="3.11"

resolve_health_host() {
  local trusted_hosts
  local first_host

  trusted_hosts="$(grep -E '^TRUSTED_HOSTS=' "${ENV_FILE}" | tail -n1 | cut -d= -f2- || true)"
  first_host="$(printf '%s' "${trusted_hosts}" | awk -F',' '{gsub(/^[[:space:]]+|[[:space:]]+$/, "", $1); print $1}')"
  if [[ -n "${first_host}" ]]; then
    printf '%s\n' "${first_host}"
    return
  fi
  printf '127.0.0.1\n'
}

echo "============================================"
echo "  SMS SaaS Install Script"
echo "============================================"

if [[ ! -f "${SAAS_DBDOCTOR_SRC}" ]]; then
  echo "ERROR: ${SAAS_DBDOCTOR_SRC} not found." >&2
  exit 1
fi

if [[ ! -f "${RESTART_HELPER_SRC}" ]]; then
  echo "ERROR: ${RESTART_HELPER_SRC} not found." >&2
  exit 1
fi

if [[ ! -f "${RESTART_SUDOERS_SRC}" ]]; then
  echo "ERROR: ${RESTART_SUDOERS_SRC} not found." >&2
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
sudo install -o root -g root -m 0755 "${RESTART_HELPER_SRC}" "${RESTART_HELPER_DEST}"
echo "✓ Installed SaaS restart helper to ${RESTART_HELPER_DEST}"
tmp_sudoers="$(mktemp)"
trap 'rm -f "${tmp_sudoers}"' EXIT
sed "s|__RESTART_HELPER_DEST__|${RESTART_HELPER_DEST}|g" "${RESTART_SUDOERS_SRC}" > "${tmp_sudoers}"
sudo install -o root -g root -m 0440 "${tmp_sudoers}" "${RESTART_SUDOERS_DEST}"
sudo "${VISUDO_BIN}" -cf "${RESTART_SUDOERS_DEST}" >/dev/null
echo "✓ Installed sudoers rule to ${RESTART_SUDOERS_DEST}"

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
ensure_env_key "PLATFORM_SERVICE_RESTART_ENABLED" "0"
ensure_env_key "PLATFORM_SERVICE_RESTART_SCRIPT" "${RESTART_HELPER_DEST}"

required_keys=(
  DATABASE_URL
  SAAS_BASE_URL
  STRIPE_SECRET_KEY
  STRIPE_WEBHOOK_SECRET
  STRIPE_PRICE_ID
  TWILIO_ACCOUNT_SID
  TWILIO_AUTH_TOKEN
  TWILIO_CREDENTIAL_ENCRYPTION_KEY
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
sudo install -m 0644 "${REPO_ROOT}/deploy/sms-saas-platform-restart-queue.service" /etc/systemd/system/sms-saas-platform-restart-queue.service
sudo install -m 0644 "${REPO_ROOT}/deploy/sms-saas-platform-restart-queue.timer" /etc/systemd/system/sms-saas-platform-restart-queue.timer
sudo install -m 0755 "${REPO_ROOT}/deploy/check_python_runtime.sh" "${APP_ROOT}/deploy/check_python_runtime.sh"
sudo install -m 0755 "${REPO_ROOT}/deploy/run_scheduler_once.sh" "${APP_ROOT}/deploy/run_scheduler_once.sh"
sudo install -m 0755 "${REPO_ROOT}/deploy/run_worker.sh" "${APP_ROOT}/deploy/run_worker.sh"
sudo install -m 0755 "${REPO_ROOT}/deploy/run_billing_reconcile_once.sh" "${APP_ROOT}/deploy/run_billing_reconcile_once.sh"
sudo install -m 0755 "${REPO_ROOT}/deploy/run_platform_restart_queue_once.sh" "${APP_ROOT}/deploy/run_platform_restart_queue_once.sh"

sudo systemctl daemon-reload
sudo systemctl enable --now sms-saas sms-saas-worker sms-saas-scheduler.timer sms-saas-billing-reconcile.timer sms-saas-platform-restart-queue.timer

echo "==> Verifying SaaS restart helper"
if ! sudo -u "${APP_USER}" sudo -n "${RESTART_HELPER_DEST}" --check >/dev/null; then
  echo "ERROR: ${APP_USER} cannot run ${RESTART_HELPER_DEST} with sudo -n." >&2
  exit 1
fi

echo "==> Verifying SaaS health"
HEALTH_HOST="$(resolve_health_host)"
if ! curl -fsS --connect-timeout 2 --max-time 5 -H "Host: ${HEALTH_HOST}" http://127.0.0.1:8100/health >/dev/null; then
  echo "WARNING: SaaS health check failed on http://127.0.0.1:8100/health (Host=${HEALTH_HOST})" >&2
  echo "Check: journalctl -u sms-saas -n 100 --no-pager" >&2
fi

echo "SaaS install completed."
