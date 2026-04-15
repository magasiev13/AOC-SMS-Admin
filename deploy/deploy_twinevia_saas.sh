#!/usr/bin/env bash
set -euo pipefail

APP_ROOT="${APP_ROOT:-/opt/twinevia-saas}"
APP_USER="${APP_USER:-}"
APP_GROUP="${APP_GROUP:-}"
ENV_FILE="${APP_ROOT}/.env"
VENV_BIN="${APP_ROOT}/venv/bin"
EXPECTED_GIT_BRANCH="${EXPECTED_GIT_BRANCH:-}"
EXPECTED_GIT_TRACKING_BRANCH="${EXPECTED_GIT_TRACKING_BRANCH:-}"
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

echo "==> Deploying Twinevia SaaS"

APP_USER="$(resolve_app_user)"
APP_GROUP="$(resolve_app_group "${APP_USER}")"

if ! id -u "${APP_USER}" >/dev/null 2>&1; then
  echo "==> Missing SaaS app user ${APP_USER}." >&2
  exit 1
fi

assert_git_source
sudo -u "${APP_USER}" bash -c "cd \"${APP_ROOT}\" && git pull --ff-only"
assert_git_source
upsert_env_key "SAAS_MODE" "1"
upsert_env_key "SCHEDULER_ENABLED" "0"
upsert_env_key "RQ_QUEUE_NAME" "twinevia-saas"
upsert_env_key "REDIS_URL" "redis://localhost:6379/0"
upsert_env_key "PLATFORM_SERVICE_RESTART_ENABLED" "0"
upsert_env_key "PLATFORM_SERVICE_RESTART_SCRIPT" "${RESTART_HELPER_DEST}"
sync_deploy_artifacts
sudo -u "${APP_USER}" "${VENV_BIN}/pip" install -r "${APP_ROOT}/requirements.txt"
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
HEALTH_OK=0
for attempt in $(seq 1 20); do
  if curl -fsS --connect-timeout 2 --max-time 5 -H "Host: ${HEALTH_HOST}" http://127.0.0.1:8100/health >/dev/null; then
    HEALTH_OK=1
    break
  fi
  echo "Health check attempt ${attempt}/20 failed for host ${HEALTH_HOST}; retrying..."
  sleep 2
done

if [[ "${HEALTH_OK}" -ne 1 ]]; then
  echo "==> SaaS health check failed after deploy (Host=${HEALTH_HOST})." >&2
  sudo systemctl status twinevia-saas --no-pager || true
  sudo journalctl -u twinevia-saas -n 120 --no-pager || true
  exit 1
fi

echo "SaaS deploy completed successfully."
