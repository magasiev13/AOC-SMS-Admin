#!/usr/bin/env bash
set -euo pipefail

APP_ROOT="/opt/sms-admin"
APP_USER="smsadmin"
ENV_FILE="${APP_ROOT}/.env"
VENV_BIN="${APP_ROOT}/venv/bin"
DBDOCTOR_BIN="/usr/local/bin/dbdoctor"
DEFAULT_TRUSTED_HOSTS="${DEFAULT_TRUSTED_HOSTS:-sms.theitwingman.com}"
APPENDED_KEYS=0
EXISTING_KEYS=0
WARNING_KEYS=0

ensure_env_key() {
  local key="$1"
  local value="$2"
  if ! sudo grep -qE "^${key}=" "${ENV_FILE}"; then
    echo "${key}=${value}" | sudo tee -a "${ENV_FILE}" >/dev/null
    APPENDED_KEYS=$((APPENDED_KEYS + 1))
    echo "[append] missing security key: ${key}"
    return
  fi
  EXISTING_KEYS=$((EXISTING_KEYS + 1))
  echo "[keep] security key already present: ${key}"
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

warn_if_non_recommended() {
  local key="$1"
  local recommended="$2"
  local current
  current="$(current_env_value "${key}")"
  if [[ -z "${current}" ]]; then
    return
  fi
  if [[ "${current}" != "${recommended}" ]]; then
    WARNING_KEYS=$((WARNING_KEYS + 1))
    echo "[warn] ${key} is set to '${current}'. Recommended value is '${recommended}'."
  fi
}

first_csv_value() {
  local raw="$1"
  echo "${raw}" | awk -F',' '{gsub(/^[[:space:]]+|[[:space:]]+$/, "", $1); print $1}'
}

dump_service_diagnostics() {
  echo "==> Diagnostics: sms service status"
  sudo systemctl status sms --no-pager || true
  echo "==> Diagnostics: sms-worker service status"
  sudo systemctl status sms-worker --no-pager || true
  echo "==> Diagnostics: recent sms journal logs"
  sudo journalctl -u sms -n 200 --no-pager || true
  echo "==> Diagnostics: recent sms-worker journal logs"
  sudo journalctl -u sms-worker -n 120 --no-pager || true
}

echo "==> Deploy script: update code + ensure security hardening config + restart"

sudo touch "${ENV_FILE}"
sudo chown root:smsadmin "${ENV_FILE}"
sudo chmod 660 "${ENV_FILE}"

echo "==> Updating repository"
sudo -u "${APP_USER}" bash -c "cd \"${APP_ROOT}\" && git pull --ff-only"

echo "==> Ensuring hardening env keys (append-only)"
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
ensure_env_key "SESSION_COOKIE_SECURE" "1"
ensure_env_key "SESSION_COOKIE_SAMESITE" "Lax"
ensure_env_key "REMEMBER_COOKIE_SECURE" "1"

echo "==> Warning check for non-recommended hardening values"
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
warn_if_non_recommended "SESSION_COOKIE_SECURE" "1"
warn_if_non_recommended "SESSION_COOKIE_SAMESITE" "Lax"
warn_if_non_recommended "REMEMBER_COOKIE_SECURE" "1"

trusted_hosts_value="$(current_env_value TRUSTED_HOSTS)"
if [[ "${trusted_hosts_value}" =~ ^(127\.0\.0\.1|localhost)(,(127\.0\.0\.1|localhost))*$ ]]; then
  WARNING_KEYS=$((WARNING_KEYS + 1))
  echo "[warn] TRUSTED_HOSTS is localhost-only ('${trusted_hosts_value}')."
  echo "       Nginx Host headers for your public domain will be rejected with 400."
fi
echo "==> Security env sync summary: appended=${APPENDED_KEYS} existing=${EXISTING_KEYS} warnings=${WARNING_KEYS}"

echo "==> Installing dependencies"
sudo -u "${APP_USER}" "${VENV_BIN}/pip" install -r "${APP_ROOT}/requirements.txt"

echo "==> Applying database migrations"
sudo -u "${APP_USER}" bash -c "cd \"${APP_ROOT}\" && \"${DBDOCTOR_BIN}\" --apply"

echo "==> Validating app configuration startup path"
if ! sudo -u "${APP_USER}" bash -lc "set -euo pipefail; cd \"${APP_ROOT}\"; set -a; source \"${ENV_FILE}\"; set +a; \"${VENV_BIN}/python\" - <<'PY'
from app import create_app
create_app(run_startup_tasks=False, start_scheduler=False)
print('App config validation ok')
PY"; then
  echo "==> App startup validation failed before restart."
  exit 1
fi

echo "==> Restarting services"
sudo systemctl restart sms sms-worker
sudo systemctl restart sms-scheduler.timer

if ! sudo systemctl is-active --quiet sms; then
  echo "==> sms service is not active after restart."
  dump_service_diagnostics
  exit 1
fi

echo "==> Quick health check (with trusted host header + retries)"
HEALTH_HOST="$(first_csv_value "${trusted_hosts_value}")"
if [[ -z "${HEALTH_HOST}" ]]; then
  HEALTH_HOST="127.0.0.1"
fi

health_ok=0
for attempt in $(seq 1 20); do
  if curl -fsS --connect-timeout 2 --max-time 5 -H "Host: ${HEALTH_HOST}" "http://127.0.0.1:8000/health" >/dev/null; then
    health_ok=1
    break
  fi
  echo "Health check attempt ${attempt}/20 failed; retrying..."
  sleep 2
done

if [[ "${health_ok}" -ne 1 ]]; then
  echo "==> Health check failed after retries."
  dump_service_diagnostics
  exit 1
fi

echo "Deploy script completed successfully."
