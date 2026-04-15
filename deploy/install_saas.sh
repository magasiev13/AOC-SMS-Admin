#!/usr/bin/env bash
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
APP_ROOT="${APP_ROOT:-/opt/twinevia-saas}"
APP_USER="${APP_USER:-}"
APP_GROUP="${APP_GROUP:-}"
ENV_FILE="${APP_ROOT}/.env"
VENV_BIN="${APP_ROOT}/venv/bin"
PYTHON_BIN="${VENV_BIN}/python"
TWINEVIA_SAAS_DBDOCTOR_SRC="${REPO_ROOT}/bin/twinevia-saas-dbdoctor"
TWINEVIA_SAAS_DBDOCTOR_DEST="${TWINEVIA_SAAS_DBDOCTOR_DEST:-${SAAS_DBDOCTOR_DEST:-/usr/local/bin/twinevia-saas-dbdoctor}}"
TWINEVIA_SAAS_DBDOCTOR_ALIAS_SRC="${REPO_ROOT}/bin/saas-dbdoctor"
TWINEVIA_SAAS_DBDOCTOR_ALIAS_DEST="${TWINEVIA_SAAS_DBDOCTOR_ALIAS_DEST:-${SAAS_DBDOCTOR_ALIAS_DEST:-/usr/local/bin/saas-dbdoctor}}"
RESTART_HELPER_SRC="${REPO_ROOT}/deploy/restart_twinevia_saas_services.sh"
RESTART_HELPER_DEST="${RESTART_HELPER_DEST:-/usr/local/bin/restart-twinevia-saas-services}"
RESTART_SUDOERS_SRC="${REPO_ROOT}/deploy/twinevia-saas-restart.sudoers"
RESTART_SUDOERS_DEST="${RESTART_SUDOERS_DEST:-/etc/sudoers.d/twinevia-saas-restart}"
VISUDO_BIN="${VISUDO_BIN:-/usr/sbin/visudo}"
LOG_DIR="${LOG_DIR:-/var/log/twinevia-saas}"
REQUIRED_PYTHON="3.11"
LEGACY_SAAS_RUNTIME_UNITS=(
  "sms-saas"
  "sms-saas-worker"
  "sms-saas-scheduler.timer"
  "sms-saas-billing-reconcile.timer"
  "sms-saas-platform-restart-queue.timer"
  "sms-saas-a2p-reconcile.timer"
)

resolve_app_user() {
  if [[ -n "${APP_USER}" ]]; then
    printf '%s\n' "${APP_USER}"
    return
  fi
  if id -u twinevia >/dev/null 2>&1; then
    printf 'twinevia\n'
    return
  fi
  if id -u smsadmin >/dev/null 2>&1; then
    printf 'smsadmin\n'
    return
  fi
  printf 'twinevia\n'
}

resolve_app_group() {
  local resolved_user="$1"
  if [[ -n "${APP_GROUP}" ]]; then
    printf '%s\n' "${APP_GROUP}"
    return
  fi
  if getent group "${resolved_user}" >/dev/null 2>&1; then
    printf '%s\n' "${resolved_user}"
    return
  fi
  if getent group twinevia >/dev/null 2>&1; then
    printf 'twinevia\n'
    return
  fi
  if getent group smsadmin >/dev/null 2>&1; then
    printf 'smsadmin\n'
    return
  fi
  printf '%s\n' "${resolved_user}"
}

render_template() {
  local src="$1"
  local dest="$2"
  local mode="$3"
  local tmp_file

  tmp_file="$(mktemp)"
  sed \
    -e "s|__APP_USER__|${APP_USER}|g" \
    -e "s|__APP_GROUP__|${APP_GROUP}|g" \
    -e "s|__APP_ROOT__|${APP_ROOT}|g" \
    -e "s|__TWINEVIA_SAAS_DBDOCTOR_DEST__|${TWINEVIA_SAAS_DBDOCTOR_DEST}|g" \
    -e "s|__RESTART_HELPER_DEST__|${RESTART_HELPER_DEST}|g" \
    "${src}" > "${tmp_file}"
  sudo install -m "${mode}" "${tmp_file}" "${dest}"
  rm -f "${tmp_file}"
}

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

retire_legacy_saas_runtime() {
  local legacy_units=()
  local unit

  for unit in "${LEGACY_SAAS_RUNTIME_UNITS[@]}"; do
    if systemctl list-unit-files "${unit}" --no-legend | grep -q "^${unit}[[:space:]]"; then
      legacy_units+=("${unit}")
    fi
  done

  if [[ ${#legacy_units[@]} -eq 0 ]]; then
    return
  fi

  echo "==> Retiring legacy sms-saas runtime units"
  sudo systemctl disable --now "${legacy_units[@]}" || true
}

echo "============================================"
echo "  Twinevia SaaS Install Script"
echo "============================================"

APP_USER="$(resolve_app_user)"
APP_GROUP="$(resolve_app_group "${APP_USER}")"

if ! id -u "${APP_USER}" >/dev/null 2>&1; then
  echo "ERROR: SaaS app user ${APP_USER} does not exist." >&2
  echo "Create it first, for example: sudo adduser --system --group --home ${APP_ROOT} --shell /bin/bash ${APP_USER}" >&2
  exit 1
fi

if ! getent group "${APP_GROUP}" >/dev/null 2>&1; then
  echo "ERROR: SaaS app group ${APP_GROUP} does not exist." >&2
  exit 1
fi

if [[ ! -f "${TWINEVIA_SAAS_DBDOCTOR_SRC}" ]]; then
  echo "ERROR: ${TWINEVIA_SAAS_DBDOCTOR_SRC} not found." >&2
  exit 1
fi

if [[ ! -f "${TWINEVIA_SAAS_DBDOCTOR_ALIAS_SRC}" ]]; then
  echo "ERROR: ${TWINEVIA_SAAS_DBDOCTOR_ALIAS_SRC} not found." >&2
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

sudo install -m 0755 "${TWINEVIA_SAAS_DBDOCTOR_SRC}" "${TWINEVIA_SAAS_DBDOCTOR_DEST}"
sudo install -m 0755 "${TWINEVIA_SAAS_DBDOCTOR_ALIAS_SRC}" "${TWINEVIA_SAAS_DBDOCTOR_ALIAS_DEST}"
echo "✓ Installed twinevia-saas-dbdoctor to ${TWINEVIA_SAAS_DBDOCTOR_DEST}"
echo "✓ Installed saas-dbdoctor compatibility alias to ${TWINEVIA_SAAS_DBDOCTOR_ALIAS_DEST}"
sudo install -o root -g root -m 0755 "${RESTART_HELPER_SRC}" "${RESTART_HELPER_DEST}"
echo "✓ Installed SaaS restart helper to ${RESTART_HELPER_DEST}"
tmp_sudoers="$(mktemp)"
trap 'rm -f "${tmp_sudoers}"' EXIT
sed \
  -e "s|__APP_USER__|${APP_USER}|g" \
  -e "s|__RESTART_HELPER_DEST__|${RESTART_HELPER_DEST}|g" \
  "${RESTART_SUDOERS_SRC}" > "${tmp_sudoers}"
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
ensure_env_key "RQ_QUEUE_NAME" "twinevia-saas"
ensure_env_key "REDIS_URL" "redis://localhost:6379/0"
ensure_env_key "PLATFORM_SERVICE_RESTART_ENABLED" "0"
ensure_env_key "PLATFORM_SERVICE_RESTART_SCRIPT" "${RESTART_HELPER_DEST}"
ensure_env_key "TWILIO_A2P_ONBOARDING_ENABLED" "0"

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

a2p_enabled="$(current_env_value "TWILIO_A2P_ONBOARDING_ENABLED")"
if [[ "${a2p_enabled}" == "1" ]]; then
  a2p_required_keys=(
    TWILIO_PRIMARY_CUSTOMER_PROFILE_SID
  )
  missing_a2p=()
  for key in "${a2p_required_keys[@]}"; do
    value="$(current_env_value "${key}")"
    if [[ -z "${value}" ]]; then
      missing_a2p+=("${key}")
    fi
  done
  if [[ ${#missing_a2p[@]} -gt 0 ]]; then
    echo "ERROR: Missing required A2P env keys in ${ENV_FILE}: ${missing_a2p[*]}" >&2
    exit 1
  fi
fi

echo "==> Installing Python dependencies"
sudo -u "${APP_USER}" "${VENV_BIN}/pip" install -r "${APP_ROOT}/requirements.txt"

echo "==> Applying SaaS schema"
sudo -u "${APP_USER}" bash -lc "set -euo pipefail; cd \"${APP_ROOT}\"; set -a; source \"${ENV_FILE}\"; set +a; \"${TWINEVIA_SAAS_DBDOCTOR_DEST}\" --apply && \"${TWINEVIA_SAAS_DBDOCTOR_DEST}\" --ensure-platform-admin && \"${TWINEVIA_SAAS_DBDOCTOR_DEST}\" --doctor"

sudo mkdir -p "${LOG_DIR}"
sudo chown -R "${APP_USER}:${APP_GROUP}" "${LOG_DIR}"

echo "==> Installing SaaS systemd units"
render_template "${REPO_ROOT}/deploy/twinevia-saas.service" /etc/systemd/system/twinevia-saas.service 0644
render_template "${REPO_ROOT}/deploy/twinevia-saas-worker.service" /etc/systemd/system/twinevia-saas-worker.service 0644
render_template "${REPO_ROOT}/deploy/twinevia-saas-scheduler.service" /etc/systemd/system/twinevia-saas-scheduler.service 0644
render_template "${REPO_ROOT}/deploy/twinevia-saas-scheduler.timer" /etc/systemd/system/twinevia-saas-scheduler.timer 0644
render_template "${REPO_ROOT}/deploy/twinevia-saas-billing-reconcile.service" /etc/systemd/system/twinevia-saas-billing-reconcile.service 0644
render_template "${REPO_ROOT}/deploy/twinevia-saas-billing-reconcile.timer" /etc/systemd/system/twinevia-saas-billing-reconcile.timer 0644
render_template "${REPO_ROOT}/deploy/twinevia-saas-platform-restart-queue.service" /etc/systemd/system/twinevia-saas-platform-restart-queue.service 0644
render_template "${REPO_ROOT}/deploy/twinevia-saas-platform-restart-queue.timer" /etc/systemd/system/twinevia-saas-platform-restart-queue.timer 0644
render_template "${REPO_ROOT}/deploy/twinevia-saas-a2p-reconcile.service" /etc/systemd/system/twinevia-saas-a2p-reconcile.service 0644
render_template "${REPO_ROOT}/deploy/twinevia-saas-a2p-reconcile.timer" /etc/systemd/system/twinevia-saas-a2p-reconcile.timer 0644
sudo install -m 0755 "${REPO_ROOT}/deploy/check_python_runtime.sh" "${APP_ROOT}/deploy/check_python_runtime.sh"
sudo install -m 0755 "${REPO_ROOT}/deploy/run_scheduler_once.sh" "${APP_ROOT}/deploy/run_scheduler_once.sh"
sudo install -m 0755 "${REPO_ROOT}/deploy/run_worker.sh" "${APP_ROOT}/deploy/run_worker.sh"
sudo install -m 0755 "${REPO_ROOT}/deploy/run_billing_reconcile_once.sh" "${APP_ROOT}/deploy/run_billing_reconcile_once.sh"
sudo install -m 0755 "${REPO_ROOT}/deploy/run_platform_restart_queue_once.sh" "${APP_ROOT}/deploy/run_platform_restart_queue_once.sh"
sudo install -m 0755 "${REPO_ROOT}/deploy/run_a2p_reconcile_once.sh" "${APP_ROOT}/deploy/run_a2p_reconcile_once.sh"

sudo systemctl daemon-reload
retire_legacy_saas_runtime
sudo systemctl enable --now twinevia-saas twinevia-saas-worker twinevia-saas-scheduler.timer twinevia-saas-billing-reconcile.timer twinevia-saas-platform-restart-queue.timer twinevia-saas-a2p-reconcile.timer

echo "==> Verifying SaaS restart helper"
if ! sudo -u "${APP_USER}" sudo -n "${RESTART_HELPER_DEST}" --check >/dev/null; then
  echo "ERROR: ${APP_USER} cannot run ${RESTART_HELPER_DEST} with sudo -n." >&2
  exit 1
fi

echo "==> Verifying SaaS health"
HEALTH_HOST="$(resolve_health_host)"
if ! curl -fsS --connect-timeout 2 --max-time 5 -H "Host: ${HEALTH_HOST}" http://127.0.0.1:8100/health >/dev/null; then
  echo "WARNING: SaaS health check failed on http://127.0.0.1:8100/health (Host=${HEALTH_HOST})" >&2
  echo "Check: journalctl -u twinevia-saas -n 100 --no-pager" >&2
fi

echo "SaaS install completed."
