#!/usr/bin/env bash
set -euo pipefail

APP_ROOT="${APP_ROOT:-/opt/twinevia-saas}"
APP_USER="${APP_USER:-}"
APP_GROUP="${APP_GROUP:-}"
ENV_TARGET_FILE="${APP_ROOT}/.env"
ENV_FILE="${ENV_TARGET_FILE}"
EXPECTED_GIT_BRANCH="${EXPECTED_GIT_BRANCH:-}"
EXPECTED_GIT_TRACKING_BRANCH="${EXPECTED_GIT_TRACKING_BRANCH:-}"
TWINEVIA_DEPLOY_REEXECED="${TWINEVIA_DEPLOY_REEXECED:-0}"
TWINEVIA_DEPLOY_PRE_PULL_HEAD="${TWINEVIA_DEPLOY_PRE_PULL_HEAD:-}"
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
  "twinevia-saas-a2p-reconcile.service"
  "twinevia-saas-a2p-reconcile.timer"
  "twinevia-saas-backup.service"
  "twinevia-saas-backup.timer"
  "twinevia-saas-readiness.service"
  "twinevia-saas-readiness.timer"
)
SAAS_RUNTIME_UNITS=(
  "twinevia-saas"
  "twinevia-saas-worker"
  "twinevia-saas-scheduler.timer"
  "twinevia-saas-billing-reconcile.timer"
  "twinevia-saas-platform-restart-queue.timer"
  "twinevia-saas-a2p-reconcile.timer"
  "twinevia-saas-backup.timer"
  "twinevia-saas-readiness.timer"
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
  if ! grep -qE "^${key}=" "${ENV_FILE}"; then
    printf '%s=%s\n' "${key}" "${value}" >> "${ENV_FILE}"
    echo "==> Appended missing key ${key}"
  fi
}

current_env_value() {
  local key="$1"
  local line
  line="$(grep -E "^${key}=" "${ENV_FILE}" | tail -n1 || true)"
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
  local twilio_validate_inbound_signature
  local security_headers_enabled
  local security_hsts_enabled
  local public_base_url
  local app_base_url
  local stripe_secret_key
  local stripe_publishable_key
  local operations_monitoring_mode
  local operations_github_repository
  local backup_offsite_mode
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
  public_base_url="$(current_env_value "PUBLIC_BASE_URL")"
  app_base_url="$(current_env_value "APP_BASE_URL")"
  stripe_secret_key="$(current_env_value "STRIPE_SECRET_KEY")"
  stripe_publishable_key="$(current_env_value "STRIPE_PUBLISHABLE_KEY")"
  operations_monitoring_mode="$(current_env_value "OPERATIONS_MONITORING_MODE")"
  operations_github_repository="$(current_env_value "OPERATIONS_GITHUB_REPOSITORY")"
  backup_offsite_mode="$(current_env_value "BACKUP_OFFSITE_MODE")"

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
  if [[ "${public_base_url}" != "https://twinevia.com" ]]; then
    errors+=("PUBLIC_BASE_URL must equal https://twinevia.com.")
  fi
  if [[ "${app_base_url}" != "https://app.twinevia.com" ]]; then
    errors+=("APP_BASE_URL must equal https://app.twinevia.com.")
  fi
  if [[ "$(current_env_value "MANAGED_PILOT_ENABLED")" != "1" ]]; then
    errors+=("MANAGED_PILOT_ENABLED must be 1.")
  fi
  if [[ "${stripe_secret_key}" != sk_live_* && "${stripe_secret_key}" != rk_live_* ]]; then
    errors+=("STRIPE_SECRET_KEY must be a live-mode secret or restricted key.")
  fi
  if [[ "${stripe_publishable_key}" != pk_live_* ]]; then
    errors+=("STRIPE_PUBLISHABLE_KEY must be a live-mode key.")
  fi
  if [[ "${operations_monitoring_mode}" != "github_actions" ]]; then
    errors+=("OPERATIONS_MONITORING_MODE must equal github_actions for this production deploy.")
  fi
  if [[ "${operations_github_repository}" != "magasiev13/AOC-SMS-Admin" ]]; then
    errors+=("OPERATIONS_GITHUB_REPOSITORY must equal magasiev13/AOC-SMS-Admin.")
  fi
  if [[ "${backup_offsite_mode}" != "github_actions" ]]; then
    errors+=("BACKUP_OFFSITE_MODE must equal github_actions for this production deploy.")
  fi

  local exact_values=(
    "STRIPE_EXPECTED_ACCOUNT_ID=acct_1TCY8xEksbf3Q3Fg"
    "STRIPE_ACTIVATION_PRICE_ID=price_1TPq4KEksbf3Q3FgwATaTJ7h"
    "STRIPE_MONTHLY_PRICE_ID=price_1TYtNuEksbf3Q3FgN2B1VqGN"
    "STRIPE_ANNUAL_PRICE_ID=price_1TYtO4Eksbf3Q3FgHzXB9S5b"
    "BILLING_ACTIVATION_FEE_USD=149.00"
    "BILLING_MONTHLY_PRICE_USD=59.99"
    "BILLING_ANNUAL_PRICE_USD=600.00"
    "BILLING_MONTHLY_INCLUDED_OUTBOUND_SEGMENTS=1000"
    "BILLING_ANNUAL_INCLUDED_OUTBOUND_SEGMENTS=1000"
    "BILLING_OUTBOUND_SEGMENT_RATE_USD=0.0300"
  )
  local exact_pair
  local exact_key
  local exact_value
  for exact_pair in "${exact_values[@]}"; do
    exact_key="${exact_pair%%=*}"
    exact_value="${exact_pair#*=}"
    if [[ "$(current_env_value "${exact_key}")" != "${exact_value}" ]]; then
      errors+=("${exact_key} must equal ${exact_value}.")
    fi
  done

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
  awk -v key="${key}" -v value="${value}" '
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
  install -m 0600 "${tmp_file}" "${ENV_FILE}"
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

echo "==> Deploying Twinevia SaaS"

APP_USER="$(resolve_app_user)"
APP_GROUP="$(resolve_app_group "${APP_USER}")"
PRE_PULL_HEAD="$(sudo -u "${APP_USER}" git -C "${APP_ROOT}" rev-parse HEAD 2>/dev/null || true)"
if [[ -z "${TWINEVIA_DEPLOY_PRE_PULL_HEAD}" ]]; then
  TWINEVIA_DEPLOY_PRE_PULL_HEAD="${PRE_PULL_HEAD}"
fi

if ! id -u "${APP_USER}" >/dev/null 2>&1; then
  echo "==> Missing SaaS app user ${APP_USER}." >&2
  exit 1
fi

assert_git_source
source_status="$(sudo -u "${APP_USER}" git -C "${APP_ROOT}" status --porcelain --untracked-files=all)"
if [[ -n "${source_status}" ]]; then
  echo "==> Refusing deploy: production source checkout is not clean." >&2
  echo "${source_status}" >&2
  exit 1
fi
sudo -u "${APP_USER}" bash -c "cd \"${APP_ROOT}\" && git pull --ff-only"
assert_git_source
POST_PULL_HEAD="$(sudo -u "${APP_USER}" git -C "${APP_ROOT}" rev-parse HEAD 2>/dev/null || true)"
if [[ "${TWINEVIA_DEPLOY_REEXECED}" != "1" && -n "${PRE_PULL_HEAD}" && -n "${POST_PULL_HEAD}" && "${PRE_PULL_HEAD}" != "${POST_PULL_HEAD}" ]]; then
  echo "==> Re-executing deploy script after checkout update"
  export TWINEVIA_DEPLOY_REEXECED=1
  export TWINEVIA_DEPLOY_PRE_PULL_HEAD
  exec bash "${APP_ROOT}/deploy/deploy_twinevia_saas.sh"
fi
if [[ ! -L "${APP_ROOT}/current" && "${TWINEVIA_DEPLOY_PRE_PULL_HEAD}" == "${POST_PULL_HEAD}" ]]; then
  reflog_previous_head="$(sudo -u "${APP_USER}" git -C "${APP_ROOT}" rev-parse 'HEAD@{1}' 2>/dev/null || true)"
  if [[ "${reflog_previous_head}" =~ ^[0-9a-f]{40}$ && "${reflog_previous_head}" != "${POST_PULL_HEAD}" ]]; then
    TWINEVIA_DEPLOY_PRE_PULL_HEAD="${reflog_previous_head}"
  fi
fi

ENV_STAGE_FILE="$(mktemp)"
ENV_ORIGINAL_FILE="$(mktemp)"
env_installed=0
release_started=0
deployment_completed=0
cleanup_env_stage() {
  local exit_code=$?
  trap - EXIT
  if [[ "${exit_code}" != "0" && "${env_installed}" == "1" && "${deployment_completed}" != "1" ]]; then
    sudo install -o root -g "${APP_GROUP}" -m 0640 "${ENV_ORIGINAL_FILE}" "${ENV_TARGET_FILE}" || true
    if [[ "${release_started}" == "1" && -L "${APP_ROOT}/current" ]]; then
      sudo systemctl restart "${SAAS_RUNTIME_UNITS[@]}" || true
    fi
  fi
  rm -f "${ENV_STAGE_FILE}" "${ENV_ORIGINAL_FILE}"
  exit "${exit_code}"
}
trap cleanup_env_stage EXIT
sudo cat "${ENV_TARGET_FILE}" > "${ENV_STAGE_FILE}"
sudo cat "${ENV_TARGET_FILE}" > "${ENV_ORIGINAL_FILE}"
chmod 0600 "${ENV_STAGE_FILE}"
chmod 0600 "${ENV_ORIGINAL_FILE}"
ENV_FILE="${ENV_STAGE_FILE}"

ensure_env_key "FLASK_ENV" "production"
ensure_env_key "FLASK_DEBUG" "0"
upsert_env_key "SAAS_MODE" "1"
upsert_env_key "SCHEDULER_ENABLED" "0"
upsert_env_key "RQ_QUEUE_NAME" "twinevia-saas"
upsert_env_key "MANAGED_PILOT_ENABLED" "1"
upsert_env_key "SAAS_BASE_URL" "https://app.twinevia.com"
upsert_env_key "PUBLIC_BASE_URL" "https://twinevia.com"
upsert_env_key "APP_BASE_URL" "https://app.twinevia.com"
upsert_env_key "TRUSTED_HOSTS" "twinevia.com,www.twinevia.com,app.twinevia.com"
ensure_env_key "PILOT_APPLICATION_RATE_LIMIT_COUNT" "5"
ensure_env_key "PILOT_APPLICATION_RATE_LIMIT_WINDOW_SECONDS" "3600"
ensure_env_key "CUSTOMER_POLICY_VERSION" "2026-08-18-managed-pilot-v1"
ensure_env_key "TERMS_POLICY_VERSION" "2026-08-18-managed-pilot-v1"
ensure_env_key "PRIVACY_POLICY_VERSION" "2026-08-18-managed-pilot-v1"
ensure_env_key "ACCEPTABLE_USE_POLICY_VERSION" "2026-08-18-managed-pilot-v1"
ensure_env_key "SMS_POLICY_VERSION" "2026-08-18-managed-pilot-v1"
ensure_env_key "BILLING_POLICY_VERSION" "2026-08-18-managed-pilot-v1"
ensure_env_key "MAX_CONTENT_LENGTH" "2097152"
ensure_env_key "MAX_FORM_MEMORY_SIZE" "262144"
ensure_env_key "MAX_FORM_PARTS" "100"
ensure_env_key "WEBHOOK_MAX_BYTES" "262144"
ensure_env_key "CSV_IMPORT_MAX_BYTES" "1048576"
ensure_env_key "CSV_IMPORT_MAX_ROWS" "5000"
ensure_env_key "CSV_IMPORT_MAX_COLUMNS" "25"
ensure_env_key "CSV_IMPORT_MAX_CELL_CHARS" "2000"
ensure_env_key "CSV_EXPORT_MAX_ROWS" "25000"
ensure_env_key "SEND_MAX_RECIPIENTS" "5000"
ensure_env_key "SEND_MAX_SEGMENTS" "15000"
ensure_env_key "RECIPIENT_SNAPSHOT_MAX_BYTES" "1048576"
ensure_env_key "TENANT_MAX_PROCESSING_MESSAGE_LOGS" "5"
ensure_env_key "SCHEDULED_MAX_PENDING_PER_ORGANIZATION" "25"
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
upsert_env_key "STRIPE_EXPECTED_ACCOUNT_ID" "acct_1TCY8xEksbf3Q3Fg"
upsert_env_key "STRIPE_EXPECTED_ACTIVATION_PRICE_ID" "price_1TPq4KEksbf3Q3FgwATaTJ7h"
upsert_env_key "STRIPE_EXPECTED_MONTHLY_PRICE_ID" "price_1TYtNuEksbf3Q3FgN2B1VqGN"
upsert_env_key "STRIPE_EXPECTED_ANNUAL_PRICE_ID" "price_1TYtO4Eksbf3Q3FgHzXB9S5b"
upsert_env_key "STRIPE_PRICE_ID" "price_1TYtNuEksbf3Q3FgN2B1VqGN"
upsert_env_key "STRIPE_MONTHLY_PRICE_ID" "price_1TYtNuEksbf3Q3FgN2B1VqGN"
upsert_env_key "STRIPE_ANNUAL_PRICE_ID" "price_1TYtO4Eksbf3Q3FgHzXB9S5b"
upsert_env_key "STRIPE_ACTIVATION_PRICE_ID" "price_1TPq4KEksbf3Q3FgwATaTJ7h"
upsert_env_key "STRIPE_LIVE_CONFIGURATION_REQUIRED" "1"
upsert_env_key "BILLING_ACTIVATION_FEE_USD" "149.00"
upsert_env_key "BILLING_MONTHLY_PRICE_USD" "59.99"
upsert_env_key "BILLING_ANNUAL_PRICE_USD" "600.00"
upsert_env_key "BILLING_MONTHLY_INCLUDED_OUTBOUND_SEGMENTS" "1000"
upsert_env_key "BILLING_ANNUAL_INCLUDED_OUTBOUND_SEGMENTS" "1000"
upsert_env_key "BILLING_OUTBOUND_SEGMENT_RATE_USD" "0.0300"
ensure_env_key "BILLING_OFFER_VERSION" "2026-08-managed-pilot-v1"
ensure_env_key "TWILIO_BROWSER_FAKE_SENDS" "0"
ensure_env_key "TWILIO_A2P_FAKE_QUEUE" "0"
ensure_env_key "TWILIO_VALIDATE_INBOUND_SIGNATURE" "1"
ensure_env_key "PLATFORM_SERVICE_RESTART_ENABLED" "0"
ensure_env_key "PLATFORM_SERVICE_RESTART_SCRIPT" "${RESTART_HELPER_DEST}"
ensure_env_key "READINESS_WORKER_MAX_AGE_SECONDS" "120"
ensure_env_key "READINESS_SYSTEMCTL_TIMEOUT_SECONDS" "5"
upsert_env_key "READINESS_REQUIRED_SYSTEMD_TIMERS" "twinevia-saas-scheduler.timer,twinevia-saas-billing-reconcile.timer,twinevia-saas-a2p-reconcile.timer,twinevia-saas-backup.timer,twinevia-saas-readiness.timer"
upsert_env_key "OPERATIONS_MONITORING_MODE" "github_actions"
upsert_env_key "OPERATIONS_GITHUB_REPOSITORY" "magasiev13/AOC-SMS-Admin"
ensure_env_key "BACKUP_LOCAL_DIR" "/var/backups/twinevia-saas"
upsert_env_key "BACKUP_OFFSITE_MODE" "github_actions"
upsert_env_key "BACKUP_OFFSITE_DESTINATION" ""
ensure_env_key "BACKUP_ENCRYPTION_PASSPHRASE_FILE" "/etc/twinevia-saas/backup-passphrase"
ensure_env_key "BACKUP_RETENTION_DAYS" "35"
ensure_env_key "BACKUP_STATUS_FILE" "/var/lib/twinevia-saas/backup-status.json"
ensure_env_key "BACKUP_MAX_AGE_HOURS" "30"
ensure_env_key "RESTORE_DRILL_STATUS_FILE" "/var/lib/twinevia-saas/restore-drill-status.json"
ensure_env_key "RESTORE_DRILL_MAX_AGE_DAYS" "90"
ensure_env_key "AOC_SCHEDULED_CANCELLATION_RECORD_FILE" "/var/lib/twinevia-saas/aoc-scheduled-cancellations/managed-pilot-launch.json"

required_keys=(
  DATABASE_URL
  SECRET_KEY
  REDIS_URL
  STRIPE_SECRET_KEY
  STRIPE_PUBLISHABLE_KEY
  STRIPE_WEBHOOK_SECRET
  STRIPE_WEBHOOK_ENDPOINT_ID
  STRIPE_PORTAL_CONFIGURATION_ID
  TWILIO_ACCOUNT_SID
  TWILIO_AUTH_TOKEN
  TWILIO_CREDENTIAL_ENCRYPTION_KEY
  READINESS_TOKEN
  OPERATIONS_MONITORING_MODE
  OPERATIONS_GITHUB_REPOSITORY
  BACKUP_LOCAL_DIR
  BACKUP_OFFSITE_MODE
  BACKUP_ENCRYPTION_PASSPHRASE_FILE
  BACKUP_STATUS_FILE
  RESTORE_DRILL_STATUS_FILE
  RESTORE_DRILL_DATABASE_URL
  RESTORE_DRILL_DATABASE_NAME
  AOC_SCHEDULED_CANCELLATION_RECORD_FILE
)
missing_required=()
for key in "${required_keys[@]}"; do
  if [[ -z "$(current_env_value "${key}")" ]]; then
    missing_required+=("${key}")
  fi
done
if [[ "$(current_env_value "OPERATIONS_MONITORING_MODE")" == "webhook" ]]; then
  for key in ALERT_WEBHOOK_URL UPTIME_MONITOR_HEARTBEAT_URL; do
    if [[ -z "$(current_env_value "${key}")" ]]; then
      missing_required+=("${key}")
    fi
  done
fi
if [[ "$(current_env_value "BACKUP_OFFSITE_MODE")" == "mounted" && -z "$(current_env_value "BACKUP_OFFSITE_DESTINATION")" ]]; then
  missing_required+=("BACKUP_OFFSITE_DESTINATION")
fi
if [[ ${#missing_required[@]} -gt 0 ]]; then
  echo "==> Refusing deploy: missing required production keys: ${missing_required[*]}" >&2
  exit 1
fi

validate_saas_runtime_env

sudo install -o root -g "${APP_GROUP}" -m 0640 "${ENV_STAGE_FILE}" "${ENV_TARGET_FILE}"
env_installed=1
ENV_FILE="${ENV_TARGET_FILE}"

if [[ ! -L "${APP_ROOT}/current" ]]; then
  echo "==> Creating a recoverable bootstrap release before systemd conversion"
  SOURCE_ROOT="${APP_ROOT}" \
  APP_ROOT="${APP_ROOT}" \
  APP_USER="${APP_USER}" \
  APP_GROUP="${APP_GROUP}" \
  TWINEVIA_ENV_FILE="${ENV_FILE}" \
  BOOTSTRAP_RELEASE_ONLY=1 \
  BOOTSTRAP_RELEASE_SHA="${TWINEVIA_DEPLOY_PRE_PULL_HEAD}" \
  bash "${APP_ROOT}/deploy/release_twinevia_saas.sh"
fi

sync_deploy_artifacts

echo "==> Building and promoting an immutable release"
release_started=1
SOURCE_ROOT="${APP_ROOT}" \
APP_ROOT="${APP_ROOT}" \
APP_USER="${APP_USER}" \
APP_GROUP="${APP_GROUP}" \
TWINEVIA_ENV_FILE="${ENV_FILE}" \
bash "${APP_ROOT}/deploy/release_twinevia_saas.sh"
deployment_completed=1

if ! sudo -u "${APP_USER}" sudo -n "${RESTART_HELPER_DEST}" --check >/dev/null; then
  echo "==> SaaS restart helper validation failed." >&2
  exit 1
fi

echo "SaaS versioned deploy completed successfully."
