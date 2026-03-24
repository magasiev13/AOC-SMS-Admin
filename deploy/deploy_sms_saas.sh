#!/usr/bin/env bash
set -euo pipefail

APP_ROOT="${APP_ROOT:-/opt/sms-saas}"
APP_USER="${APP_USER:-smsadmin}"
ENV_FILE="${APP_ROOT}/.env"
VENV_BIN="${APP_ROOT}/venv/bin"
SAAS_DBDOCTOR_BIN="${SAAS_DBDOCTOR_BIN:-/usr/local/bin/saas-dbdoctor}"
RESTART_HELPER_SRC="${APP_ROOT}/deploy/restart_sms_saas_services.sh"
RESTART_HELPER_DEST="${RESTART_HELPER_DEST:-/usr/local/bin/restart-sms-saas-services}"
RESTART_SUDOERS_SRC="${APP_ROOT}/deploy/sms-saas-restart.sudoers"
RESTART_SUDOERS_DEST="${RESTART_SUDOERS_DEST:-/etc/sudoers.d/sms-saas-restart}"
SYSTEMD_UNIT_DIR="${SYSTEMD_UNIT_DIR:-/etc/systemd/system}"
VISUDO_BIN="${VISUDO_BIN:-/usr/sbin/visudo}"
SAAS_SYSTEMD_UNITS=(
  "sms-saas.service"
  "sms-saas-worker.service"
  "sms-saas-scheduler.service"
  "sms-saas-scheduler.timer"
  "sms-saas-billing-reconcile.service"
  "sms-saas-billing-reconcile.timer"
  "sms-saas-platform-restart-queue.service"
  "sms-saas-platform-restart-queue.timer"
)
SAAS_RUNTIME_UNITS=(
  "sms-saas"
  "sms-saas-worker"
  "sms-saas-scheduler.timer"
  "sms-saas-billing-reconcile.timer"
  "sms-saas-platform-restart-queue.timer"
)

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

sync_deploy_artifacts() {
  local unit
  local tmp_sudoers

  echo "==> Syncing SaaS deploy artifacts"
  sudo install -o root -g root -m 0755 "${RESTART_HELPER_SRC}" "${RESTART_HELPER_DEST}"
  tmp_sudoers="$(mktemp)"
  trap 'rm -f "${tmp_sudoers}"' RETURN
  sed "s|__RESTART_HELPER_DEST__|${RESTART_HELPER_DEST}|g" "${RESTART_SUDOERS_SRC}" > "${tmp_sudoers}"
  sudo install -o root -g root -m 0440 "${tmp_sudoers}" "${RESTART_SUDOERS_DEST}"
  sudo "${VISUDO_BIN}" -cf "${RESTART_SUDOERS_DEST}" >/dev/null
  for unit in "${SAAS_SYSTEMD_UNITS[@]}"; do
    sudo install -m 0644 "${APP_ROOT}/deploy/${unit}" "${SYSTEMD_UNIT_DIR}/${unit}"
  done
  sudo systemctl daemon-reload
}

echo "==> Deploying SMS SaaS"

sudo -u "${APP_USER}" bash -c "cd \"${APP_ROOT}\" && git pull --ff-only"
sync_deploy_artifacts
sudo -u "${APP_USER}" "${VENV_BIN}/pip" install -r "${APP_ROOT}/requirements.txt"
sudo -u "${APP_USER}" bash -lc "set -euo pipefail; cd \"${APP_ROOT}\"; set -a; source \"${ENV_FILE}\"; set +a; \"${SAAS_DBDOCTOR_BIN}\" --apply && \"${SAAS_DBDOCTOR_BIN}\" --ensure-platform-admin && \"${SAAS_DBDOCTOR_BIN}\" --doctor"

if ! sudo -u "${APP_USER}" bash -lc "set -euo pipefail; cd \"${APP_ROOT}\"; set -a; source \"${ENV_FILE}\"; set +a; \"${VENV_BIN}/python\" - <<'PY'
from app import create_app
create_app(run_startup_tasks=False, start_scheduler=False)
print('SaaS app config validation ok')
PY"; then
  echo "==> SaaS app validation failed before restart." >&2
  exit 1
fi

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
  sudo systemctl status sms-saas --no-pager || true
  sudo journalctl -u sms-saas -n 120 --no-pager || true
  exit 1
fi

echo "SaaS deploy completed successfully."
