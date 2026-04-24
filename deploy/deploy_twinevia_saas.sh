#!/usr/bin/env bash
set -euo pipefail

APP_ROOT="${APP_ROOT:-/opt/twinevia-saas}"
APP_USER="${APP_USER:-}"
APP_GROUP="${APP_GROUP:-}"
ENV_FILE="${APP_ROOT}/.env"
VENV_BIN="${APP_ROOT}/venv/bin"
VENV_STATE_FILE=""
EXPECTED_GIT_BRANCH="${EXPECTED_GIT_BRANCH:-}"
EXPECTED_GIT_TRACKING_BRANCH="${EXPECTED_GIT_TRACKING_BRANCH:-}"
TWINEVIA_DEPLOY_REEXECED="${TWINEVIA_DEPLOY_REEXECED:-0}"
TWINEVIA_SAAS_DBDOCTOR_BIN="${TWINEVIA_SAAS_DBDOCTOR_BIN:-${SAAS_DBDOCTOR_BIN:-/usr/local/bin/twinevia-saas-dbdoctor}}"
TWINEVIA_SAAS_DBDOCTOR_ALIAS_BIN="${TWINEVIA_SAAS_DBDOCTOR_ALIAS_BIN:-${SAAS_DBDOCTOR_ALIAS_BIN:-/usr/local/bin/saas-dbdoctor}}"
RESTART_HELPER_SRC="${APP_ROOT}/deploy/restart_twinevia_saas_services.sh"
RESTART_HELPER_DEST="${RESTART_HELPER_DEST:-/usr/local/bin/restart-twinevia-saas-services}"
RESTART_SUDOERS_SRC="${APP_ROOT}/deploy/twinevia-saas-restart.sudoers"
RESTART_SUDOERS_DEST="${RESTART_SUDOERS_DEST:-/etc/sudoers.d/twinevia-saas-restart}"
SYSTEMD_UNIT_DIR="${SYSTEMD_UNIT_DIR:-/etc/systemd/system}"
VISUDO_BIN="${VISUDO_BIN:-/usr/sbin/visudo}"
LOG_DIR="${LOG_DIR:-/var/log/twinevia-saas}"
SAAS_SYSTEMD_UNITS=(
  "twinevia-saas.service"
  "twinevia-saas-worker.service"
  "twinevia-saas-scheduler.service"
  "twinevia-saas-scheduler.timer"
  "twinevia-saas-billing-reconcile.service"
  "twinevia-saas-billing-reconcile.timer"
  "twinevia-saas-platform-restart-queue.service"
  "twinevia-saas-platform-restart-queue.timer"
)
SAAS_RUNTIME_UNITS=(
  "twinevia-saas"
  "twinevia-saas-worker"
  "twinevia-saas-scheduler.timer"
  "twinevia-saas-billing-reconcile.timer"
  "twinevia-saas-platform-restart-queue.timer"
)
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

resolve_tracking_branch() {
  sudo -u "${APP_USER}" git -C "${APP_ROOT}" rev-parse --abbrev-ref --symbolic-full-name '@{u}' 2>/dev/null || true
}

assert_git_source() {
  local current_branch
  local expected_tracking
  local tracking_branch

  if [[ -z "${EXPECTED_GIT_BRANCH}" ]]; then
    return
  fi

  current_branch="$(sudo -u "${APP_USER}" git -C "${APP_ROOT}" rev-parse --abbrev-ref HEAD)"
  if [[ "${current_branch}" != "${EXPECTED_GIT_BRANCH}" ]]; then
    echo "==> Refusing deploy: ${APP_ROOT} is on ${current_branch}, expected ${EXPECTED_GIT_BRANCH}." >&2
    exit 1
  fi

  expected_tracking="${EXPECTED_GIT_TRACKING_BRANCH:-origin/${EXPECTED_GIT_BRANCH}}"
  tracking_branch="$(resolve_tracking_branch)"
  if [[ "${tracking_branch}" != "${expected_tracking}" ]]; then
    echo "==> Refusing deploy: ${APP_ROOT} tracks ${tracking_branch:-<none>}, expected ${expected_tracking}." >&2
    exit 1
  fi
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
    -e "s|__TWINEVIA_SAAS_DBDOCTOR_DEST__|${TWINEVIA_SAAS_DBDOCTOR_BIN}|g" \
    -e "s|__RESTART_HELPER_DEST__|${RESTART_HELPER_DEST}|g" \
    "${src}" > "${tmp_file}"
  sudo install -m "${mode}" "${tmp_file}" "${dest}"
  rm -f "${tmp_file}"
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

trusted_hosts_are_localhost_only() {
  local trusted_hosts="$1"
  [[ "${trusted_hosts}" =~ ^(127\.0\.0\.1|localhost)(,(127\.0\.0\.1|localhost))*$ ]]
}

ensure_env_key() {
  local key="$1"
  local value="$2"
  if ! sudo grep -qE "^${key}=" "${ENV_FILE}"; then
    echo "${key}=${value}" | sudo tee -a "${ENV_FILE}" >/dev/null
    echo "==> Appended missing key ${key}"
  fi
}

current_env_value() {
  local key="$1"
  local line
  line="$(sudo grep -E "^${key}=" "${ENV_FILE}" | tail -n1 || true)"
  if [[ -z "${line}" ]]; then
    echo ""
    return
  fi
  echo "${line#*=}"
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
  local errors=()

  flask_env="$(current_env_value "FLASK_ENV")"
  flask_debug="$(current_env_value "FLASK_DEBUG")"
  database_url="$(current_env_value "DATABASE_URL")"
  trusted_hosts="$(current_env_value "TRUSTED_HOSTS")"
  trust_proxy="$(current_env_value "TRUST_PROXY")"
  session_cookie_secure="$(current_env_value "SESSION_COOKIE_SECURE")"
  remember_cookie_secure="$(current_env_value "REMEMBER_COOKIE_SECURE")"
  session_cookie_samesite="$(current_env_value "SESSION_COOKIE_SAMESITE")"

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

  for flag_name in STRIPE_FAKE_CHECKOUT_ENABLED TWILIO_BROWSER_FAKE_SENDS TWILIO_A2P_FAKE_QUEUE; do
    if [[ "$(current_env_value "${flag_name}")" != "0" ]]; then
      errors+=("${flag_name} must be 0 for live SaaS deploys.")
    fi
  done

  if [[ ${#errors[@]} -gt 0 ]]; then
    printf '==> Refusing deploy due to unsafe SaaS runtime configuration:\n' >&2
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
}

sync_deploy_artifacts() {
  local unit
  local tmp_sudoers

  echo "==> Syncing SaaS deploy artifacts"
  sudo install -o root -g root -m 0755 "${APP_ROOT}/bin/twinevia-saas-dbdoctor" "${TWINEVIA_SAAS_DBDOCTOR_BIN}"
  sudo install -o root -g root -m 0755 "${APP_ROOT}/bin/saas-dbdoctor" "${TWINEVIA_SAAS_DBDOCTOR_ALIAS_BIN}"
  sudo install -o root -g root -m 0755 "${RESTART_HELPER_SRC}" "${RESTART_HELPER_DEST}"
  tmp_sudoers="$(mktemp)"
  trap 'rm -f "${tmp_sudoers}"' RETURN
  sed \
    -e "s|__APP_USER__|${APP_USER}|g" \
    -e "s|__RESTART_HELPER_DEST__|${RESTART_HELPER_DEST}|g" \
    "${RESTART_SUDOERS_SRC}" > "${tmp_sudoers}"
  sudo install -o root -g root -m 0440 "${tmp_sudoers}" "${RESTART_SUDOERS_DEST}"
  sudo "${VISUDO_BIN}" -cf "${RESTART_SUDOERS_DEST}" >/dev/null
  sudo mkdir -p "${LOG_DIR}"
  sudo chown -R "${APP_USER}:${APP_GROUP}" "${LOG_DIR}"
  for unit in "${SAAS_SYSTEMD_UNITS[@]}"; do
    render_template "${APP_ROOT}/deploy/${unit}" "${SYSTEMD_UNIT_DIR}/${unit}" 0644
  done
  sudo systemctl daemon-reload
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
  for unit in "${legacy_units[@]}"; do
    sudo systemctl stop "${unit}" || true
    sudo systemctl disable "${unit}" || true
  done
  sudo systemctl reset-failed "${legacy_units[@]}" || true
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
  if APP_USER="${APP_USER}" APP_GROUP="${APP_GROUP}" APP_ROOT="${APP_ROOT}" bash "${APP_ROOT}/deploy/ensure_canonical_venv.sh" rollback "${VENV_STATE_FILE}"; then
    sudo systemctl restart "${SAAS_RUNTIME_UNITS[@]}" || true
    if check_saas_health "${health_host}" 10; then
      echo "==> Health recovered after virtualenv rollback."
    fi
  fi
}

echo "==> Deploying Twinevia SaaS"

APP_USER="$(resolve_app_user)"
APP_GROUP="$(resolve_app_group "${APP_USER}")"
PRE_PULL_HEAD="$(sudo -u "${APP_USER}" git -C "${APP_ROOT}" rev-parse HEAD 2>/dev/null || true)"

if ! id -u "${APP_USER}" >/dev/null 2>&1; then
  echo "==> Missing SaaS app user ${APP_USER}." >&2
  exit 1
fi

assert_git_source
sudo -u "${APP_USER}" bash -c "cd \"${APP_ROOT}\" && git pull --ff-only"
assert_git_source
POST_PULL_HEAD="$(sudo -u "${APP_USER}" git -C "${APP_ROOT}" rev-parse HEAD 2>/dev/null || true)"
if [[ "${TWINEVIA_DEPLOY_REEXECED}" != "1" && -n "${PRE_PULL_HEAD}" && -n "${POST_PULL_HEAD}" && "${PRE_PULL_HEAD}" != "${POST_PULL_HEAD}" ]]; then
  echo "==> Re-executing deploy script after checkout update"
  export TWINEVIA_DEPLOY_REEXECED=1
  exec bash "${APP_ROOT}/deploy/deploy_twinevia_saas.sh"
fi
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
ensure_env_key "STRIPE_FAKE_CHECKOUT_ENABLED" "0"
ensure_env_key "TWILIO_BROWSER_FAKE_SENDS" "0"
ensure_env_key "TWILIO_A2P_FAKE_QUEUE" "0"
ensure_env_key "PLATFORM_SERVICE_RESTART_ENABLED" "0"
ensure_env_key "PLATFORM_SERVICE_RESTART_SCRIPT" "${RESTART_HELPER_DEST}"
validate_saas_runtime_env
sync_deploy_artifacts
VENV_STATE_FILE="$(mktemp)"
echo "==> Ensuring canonical Python virtualenv"
APP_USER="${APP_USER}" APP_GROUP="${APP_GROUP}" APP_ROOT="${APP_ROOT}" bash "${APP_ROOT}/deploy/ensure_canonical_venv.sh" ensure "${VENV_STATE_FILE}"
sudo -u "${APP_USER}" "${VENV_BIN}/python" -m pip install -r "${APP_ROOT}/requirements.txt"
sudo -u "${APP_USER}" bash -lc "set -euo pipefail; cd \"${APP_ROOT}\"; set -a; source \"${ENV_FILE}\"; set +a; \"${TWINEVIA_SAAS_DBDOCTOR_BIN}\" --apply && \"${TWINEVIA_SAAS_DBDOCTOR_BIN}\" --ensure-platform-admin && \"${TWINEVIA_SAAS_DBDOCTOR_BIN}\" --doctor"

echo "==> Refreshing systemd units and helper scripts"
render_template "${APP_ROOT}/deploy/twinevia-saas.service" /etc/systemd/system/twinevia-saas.service 0644
render_template "${APP_ROOT}/deploy/twinevia-saas-worker.service" /etc/systemd/system/twinevia-saas-worker.service 0644
render_template "${APP_ROOT}/deploy/twinevia-saas-scheduler.service" /etc/systemd/system/twinevia-saas-scheduler.service 0644
render_template "${APP_ROOT}/deploy/twinevia-saas-scheduler.timer" /etc/systemd/system/twinevia-saas-scheduler.timer 0644
render_template "${APP_ROOT}/deploy/twinevia-saas-billing-reconcile.service" /etc/systemd/system/twinevia-saas-billing-reconcile.service 0644
render_template "${APP_ROOT}/deploy/twinevia-saas-billing-reconcile.timer" /etc/systemd/system/twinevia-saas-billing-reconcile.timer 0644
render_template "${APP_ROOT}/deploy/twinevia-saas-a2p-reconcile.service" /etc/systemd/system/twinevia-saas-a2p-reconcile.service 0644
render_template "${APP_ROOT}/deploy/twinevia-saas-a2p-reconcile.timer" /etc/systemd/system/twinevia-saas-a2p-reconcile.timer 0644
sudo systemctl daemon-reload
sudo systemctl enable --now twinevia-saas-a2p-reconcile.timer

if ! sudo -u "${APP_USER}" bash -lc "set -euo pipefail; cd \"${APP_ROOT}\"; set -a; source \"${ENV_FILE}\"; set +a; \"${VENV_BIN}/python\" - <<'PY'
from app import create_app
create_app(run_startup_tasks=False, start_scheduler=False)
print('SaaS app config validation ok')
PY"; then
  echo "==> SaaS app validation failed before restart." >&2
  exit 1
fi

retire_legacy_saas_runtime
sudo systemctl enable --now "${SAAS_RUNTIME_UNITS[@]}"

if ! sudo -u "${APP_USER}" sudo -n "${RESTART_HELPER_DEST}" --check >/dev/null; then
  echo "==> SaaS restart helper validation failed." >&2
  exit 1
fi

sudo systemctl restart "${SAAS_RUNTIME_UNITS[@]}"

HEALTH_HOST="$(resolve_health_host)"
if ! check_saas_health "${HEALTH_HOST}" 20; then
  echo "==> SaaS health check failed after deploy (Host=${HEALTH_HOST})." >&2
  rollback_promoted_venv "${HEALTH_HOST}"
  dump_service_diagnostics
  exit 1
fi

echo "SaaS deploy completed successfully."
