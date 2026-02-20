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
echo "==> Security env sync summary: appended=${APPENDED_KEYS} existing=${EXISTING_KEYS} warnings=${WARNING_KEYS}"

echo "==> Installing dependencies"
sudo -u "${APP_USER}" "${VENV_BIN}/pip" install -r "${APP_ROOT}/requirements.txt"

echo "==> Applying database migrations"
sudo -u "${APP_USER}" bash -c "cd \"${APP_ROOT}\" && \"${DBDOCTOR_BIN}\" --apply"

echo "==> Restarting services"
sudo systemctl restart sms sms-worker
sudo systemctl restart sms-scheduler.timer

echo "==> Quick health check"
curl -fsS http://127.0.0.1:8000/health >/dev/null

echo "Deploy script completed successfully."
