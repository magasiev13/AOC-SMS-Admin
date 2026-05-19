#!/usr/bin/env bash
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
APP_ROOT="${APP_ROOT:-/opt/twinevia-saas}"
APP_USER="${APP_USER:-}"
APP_GROUP="${APP_GROUP:-}"
ENV_FILE="${APP_ROOT}/.env"
VENV_BIN="${APP_ROOT}/venv/bin"
PYTHON_BIN="${VENV_BIN}/python"
VENV_STATE_FILE=""
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
resolve_app_user() {
  if [[ -n "${APP_USER}" ]]; then
    printf '%s\n' "${APP_USER}"
    return
  fi
  if id -u twinevia >/dev/null 2>&1; then
    printf 'twinevia\n'
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

install_repo_file() {
  local src="$1"
  local dest="$2"
  local mode="$3"
  local src_real=""
  local dest_real=""

  if command -v realpath >/dev/null 2>&1; then
    src_real="$(realpath "${src}" 2>/dev/null || true)"
    dest_real="$(realpath "${dest}" 2>/dev/null || true)"
  fi

  if [[ -n "${src_real}" && -n "${dest_real}" && "${src_real}" == "${dest_real}" ]]; then
    return
  fi

  sudo install -m "${mode}" "${src}" "${dest}"
}

resolve_health_host() {
  local saas_base_url
  local canonical_host
  local trusted_hosts
  local first_host

  saas_base_url="$(current_env_value "SAAS_BASE_URL")"
  canonical_host="$(printf '%s' "${saas_base_url}" | awk '
    {
      gsub(/^[[:space:]]+|[[:space:]]+$/, "", $0)
      gsub(/^[A-Za-z][A-Za-z0-9+.-]*:\/\//, "", $0)
      sub(/\/.*$/, "", $0)
      sub(/^[^@]*@/, "", $0)
      sub(/:.*/, "", $0)
      print $0
    }
  ')"
  if [[ -n "${canonical_host}" ]]; then
    printf '%s\n' "${canonical_host}"
    return
  fi

  trusted_hosts="$(grep -E '^TRUSTED_HOSTS=' "${ENV_FILE}" | tail -n1 | cut -d= -f2- || true)"
  first_host="$(printf '%s' "${trusted_hosts}" | awk -F',' '{gsub(/^[[:space:]]+|[[:space:]]+$/, "", $1); print $1}')"
  if [[ -n "${first_host}" ]]; then
    printf '%s\n' "${first_host}"
    return
  fi
  printf '127.0.0.1\n'
}

check_saas_health() {
  local health_host="$1"
  local attempts="${2:-20}"
  local attempt

  for attempt in $(seq 1 "${attempts}"); do
    if curl -fsS --connect-timeout 2 --max-time 5 -H "Host: ${health_host}" http://127.0.0.1:8100/health >/dev/null; then
      return 0
    fi
    echo "Health check attempt ${attempt}/${attempts} failed for host ${health_host}; retrying..."
    sleep 2
  done
  return 1
}

rollback_promoted_venv() {
  local health_host="$1"

  if [[ -z "${VENV_STATE_FILE}" || ! -f "${VENV_STATE_FILE}" ]]; then
    return
  fi

  echo "==> Attempting virtualenv rollback after failed health check"
  if APP_USER="${APP_USER}" APP_GROUP="${APP_GROUP}" APP_ROOT="${APP_ROOT}" bash "${REPO_ROOT}/deploy/ensure_canonical_venv.sh" rollback "${VENV_STATE_FILE}"; then
    sudo systemctl restart twinevia-saas twinevia-saas-worker twinevia-saas-scheduler.timer twinevia-saas-billing-reconcile.timer twinevia-saas-platform-restart-queue.timer twinevia-saas-a2p-reconcile.timer || true
    if check_saas_health "${health_host}" 10; then
      echo "==> Health recovered after virtualenv rollback."
    fi
  fi
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

VENV_STATE_FILE="$(mktemp)"
echo "==> Ensuring canonical Python virtualenv"
APP_USER="${APP_USER}" APP_GROUP="${APP_GROUP}" APP_ROOT="${APP_ROOT}" bash "${REPO_ROOT}/deploy/ensure_canonical_venv.sh" ensure "${VENV_STATE_FILE}"

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

first_csv_value() {
  local raw="$1"
  printf '%s\n' "${raw}" | awk -F',' '{gsub(/^[[:space:]]+|[[:space:]]+$/, "", $1); print $1}'
}

trusted_hosts_are_localhost_only() {
  local trusted_hosts="$1"
  [[ "${trusted_hosts}" =~ ^(127\.0\.0\.1|localhost)(,(127\.0\.0\.1|localhost))*$ ]]
}

upsert_env_key() {
  local key="$1"
  local value="$2"
  local tmp_file

  tmp_file="$(mktemp)"
  sudo awk -v key="${key}" -v value="${value}" '
    BEGIN { updated = 0 }
    $0 ~ ("^" key "=") {
      if (updated == 0) {
        print key "=" value
        updated = 1
      }
      next
    }
    { print }
    END {
      if (updated == 0) {
        print key "=" value
      }
    }
  ' "${ENV_FILE}" > "${tmp_file}"
  sudo install -o root -g "${APP_GROUP}" -m 0660 "${tmp_file}" "${ENV_FILE}"
  rm -f "${tmp_file}"
  echo "✓ Set ${key}"
}

validate_saas_runtime_env() {
  local flask_env
  local flask_debug
  local database_url
  local trusted_hosts
  local trust_proxy
  local session_cookie_secure
  local remember_cookie_secure
  local session_cookie_samesite
  local twilio_validate_inbound_signature
  local security_headers_enabled
  local security_hsts_enabled
  local errors=()

  flask_env="$(current_env_value "FLASK_ENV")"
  flask_debug="$(current_env_value "FLASK_DEBUG")"
  database_url="$(current_env_value "DATABASE_URL")"
  trusted_hosts="$(current_env_value "TRUSTED_HOSTS")"
  trust_proxy="$(current_env_value "TRUST_PROXY")"
  session_cookie_secure="$(current_env_value "SESSION_COOKIE_SECURE")"
  remember_cookie_secure="$(current_env_value "REMEMBER_COOKIE_SECURE")"
  session_cookie_samesite="$(current_env_value "SESSION_COOKIE_SAMESITE")"
  twilio_validate_inbound_signature="$(current_env_value "TWILIO_VALIDATE_INBOUND_SIGNATURE")"
  security_headers_enabled="$(current_env_value "SECURITY_HEADERS_ENABLED")"
  security_hsts_enabled="$(current_env_value "SECURITY_HSTS_ENABLED")"

  if [[ "${flask_env}" != "production" ]]; then
    errors+=("FLASK_ENV must be set to production for live SaaS deploys.")
  fi
  if [[ -n "${flask_debug}" && "${flask_debug}" != "0" ]]; then
    errors+=("FLASK_DEBUG must be unset or 0 for live SaaS deploys.")
  fi
  if [[ "${database_url}" != postgresql* ]]; then
    errors+=("DATABASE_URL must use PostgreSQL for live SaaS deploys.")
  fi
  if [[ -z "${trusted_hosts}" ]]; then
    errors+=("TRUSTED_HOSTS must list the real public hostnames for this deployment.")
  elif trusted_hosts_are_localhost_only "${trusted_hosts}"; then
    echo "[warn] TRUSTED_HOSTS is localhost-only ('${trusted_hosts}')." >&2
    echo "       Public Host headers will be rejected with 400 even if the local health check passes." >&2
    errors+=("TRUSTED_HOSTS must not be localhost-only for live SaaS deploys.")
  fi
  if [[ "${trust_proxy}" != "1" ]]; then
    errors+=("TRUST_PROXY must be 1 when SaaS is served behind the public reverse proxy.")
  fi
  if [[ "${session_cookie_secure}" != "1" ]]; then
    errors+=("SESSION_COOKIE_SECURE must be 1 for live SaaS deploys.")
  fi
  if [[ "${remember_cookie_secure}" != "1" ]]; then
    errors+=("REMEMBER_COOKIE_SECURE must be 1 for live SaaS deploys.")
  fi
  if [[ "${session_cookie_samesite}" != "Lax" && "${session_cookie_samesite}" != "Strict" ]]; then
    errors+=("SESSION_COOKIE_SAMESITE must be Lax or Strict for live SaaS deploys.")
  fi
  if [[ "${twilio_validate_inbound_signature}" != "1" ]]; then
    errors+=("TWILIO_VALIDATE_INBOUND_SIGNATURE must be 1 for live SaaS deploys.")
  fi
  if [[ "${security_headers_enabled}" != "1" ]]; then
    errors+=("SECURITY_HEADERS_ENABLED must be 1 for live SaaS deploys.")
  fi
  if [[ "${security_hsts_enabled}" != "1" ]]; then
    errors+=("SECURITY_HSTS_ENABLED must be 1 for live SaaS deploys.")
  fi

  for flag_name in STRIPE_FAKE_CHECKOUT_ENABLED TWILIO_BROWSER_FAKE_SENDS TWILIO_A2P_FAKE_QUEUE; do
    if [[ "$(current_env_value "${flag_name}")" != "0" ]]; then
      errors+=("${flag_name} must be 0 for live SaaS deploys.")
    fi
  done

  if [[ ${#errors[@]} -gt 0 ]]; then
    printf 'ERROR: SaaS runtime configuration is unsafe for live deploys:\n' >&2
    printf ' - %s\n' "${errors[@]}" >&2
    exit 1
  fi
}

dump_service_diagnostics() {
  echo "==> Diagnostics: twinevia-saas service status"
  sudo systemctl status twinevia-saas --no-pager || true
  echo "==> Diagnostics: twinevia-saas-worker service status"
  sudo systemctl status twinevia-saas-worker --no-pager || true
  echo "==> Diagnostics: recent twinevia-saas journal logs"
  sudo journalctl -u twinevia-saas -n 200 --no-pager || true
  echo "==> Diagnostics: recent twinevia-saas-worker journal logs"
  sudo journalctl -u twinevia-saas-worker -n 120 --no-pager || true
}

ensure_env_key "FLASK_ENV" "production"
ensure_env_key "FLASK_DEBUG" "0"
upsert_env_key "SAAS_MODE" "1"
upsert_env_key "SCHEDULER_ENABLED" "0"
upsert_env_key "RQ_QUEUE_NAME" "twinevia-saas"
ensure_env_key "REDIS_URL" "redis://localhost:6379/0"
ensure_env_key "TRUST_PROXY" "1"
ensure_env_key "SESSION_COOKIE_SECURE" "1"
ensure_env_key "REMEMBER_COOKIE_SECURE" "1"
ensure_env_key "SESSION_COOKIE_SAMESITE" "Lax"
ensure_env_key "SECURITY_HEADERS_ENABLED" "1"
ensure_env_key "SECURITY_HSTS_ENABLED" "1"
ensure_env_key "SECURITY_HSTS_MAX_AGE" "31536000"
ensure_env_key "STRIPE_FAKE_CHECKOUT_ENABLED" "0"
ensure_env_key "BILLING_TRIAL_DAYS" "0"
ensure_env_key "TWILIO_BROWSER_FAKE_SENDS" "0"
ensure_env_key "TWILIO_A2P_FAKE_QUEUE" "0"
ensure_env_key "TWILIO_VALIDATE_INBOUND_SIGNATURE" "1"
ensure_env_key "PLATFORM_SERVICE_RESTART_ENABLED" "0"
ensure_env_key "PLATFORM_SERVICE_RESTART_SCRIPT" "${RESTART_HELPER_DEST}"
ensure_env_key "TWILIO_A2P_ONBOARDING_ENABLED" "0"

required_keys=(
  DATABASE_URL
  SAAS_BASE_URL
  STRIPE_SECRET_KEY
  STRIPE_WEBHOOK_SECRET
  STRIPE_PRICE_ID
  STRIPE_ANNUAL_PRICE_ID
  STRIPE_ACTIVATION_PRICE_ID
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

validate_saas_runtime_env

echo "==> Installing Python dependencies"
sudo -u "${APP_USER}" "${VENV_BIN}/python" -m pip install -r "${APP_ROOT}/requirements.txt"

echo "==> Applying SaaS schema"
sudo -u "${APP_USER}" bash -lc "set -euo pipefail; cd \"${APP_ROOT}\"; set -a; source \"${ENV_FILE}\"; set +a; \"${TWINEVIA_SAAS_DBDOCTOR_DEST}\" --apply && \"${TWINEVIA_SAAS_DBDOCTOR_DEST}\" --ensure-platform-admin && \"${TWINEVIA_SAAS_DBDOCTOR_DEST}\" --doctor"

echo "==> Validating SaaS app configuration startup path"
if ! sudo -u "${APP_USER}" bash -lc "set -euo pipefail; cd \"${APP_ROOT}\"; set -a; source \"${ENV_FILE}\"; set +a; \"${VENV_BIN}/python\" - <<'PY'
from app import create_app
create_app(run_startup_tasks=False, start_scheduler=False)
print('SaaS app config validation ok')
PY"; then
  echo "ERROR: SaaS app startup validation failed before services were enabled." >&2
  exit 1
fi

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
install_repo_file "${REPO_ROOT}/deploy/check_python_runtime.sh" "${APP_ROOT}/deploy/check_python_runtime.sh" 0755
install_repo_file "${REPO_ROOT}/deploy/ensure_canonical_venv.sh" "${APP_ROOT}/deploy/ensure_canonical_venv.sh" 0755
install_repo_file "${REPO_ROOT}/deploy/run_scheduler_once.sh" "${APP_ROOT}/deploy/run_scheduler_once.sh" 0755
install_repo_file "${REPO_ROOT}/deploy/run_worker.sh" "${APP_ROOT}/deploy/run_worker.sh" 0755
install_repo_file "${REPO_ROOT}/deploy/run_billing_reconcile_once.sh" "${APP_ROOT}/deploy/run_billing_reconcile_once.sh" 0755
install_repo_file "${REPO_ROOT}/deploy/run_platform_restart_queue_once.sh" "${APP_ROOT}/deploy/run_platform_restart_queue_once.sh" 0755
install_repo_file "${REPO_ROOT}/deploy/run_a2p_reconcile_once.sh" "${APP_ROOT}/deploy/run_a2p_reconcile_once.sh" 0755

sudo systemctl daemon-reload
sudo systemctl enable --now twinevia-saas twinevia-saas-worker twinevia-saas-scheduler.timer twinevia-saas-billing-reconcile.timer twinevia-saas-platform-restart-queue.timer twinevia-saas-a2p-reconcile.timer

echo "==> Verifying SaaS restart helper"
if ! sudo -u "${APP_USER}" sudo -n "${RESTART_HELPER_DEST}" --check >/dev/null; then
  echo "ERROR: ${APP_USER} cannot run ${RESTART_HELPER_DEST} with sudo -n." >&2
  exit 1
fi

echo "==> Verifying SaaS health"
HEALTH_HOST="$(resolve_health_host)"
if ! check_saas_health "${HEALTH_HOST}" 20; then
  echo "ERROR: SaaS health check failed after install (Host=${HEALTH_HOST})." >&2
  rollback_promoted_venv "${HEALTH_HOST}"
  dump_service_diagnostics
  exit 1
fi

echo "SaaS install completed."
