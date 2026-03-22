#!/usr/bin/env bash
set -euo pipefail

APP_ROOT="${APP_ROOT:-/opt/sms-saas}"
APP_USER="${APP_USER:-smsadmin}"
ENV_FILE="${APP_ROOT}/.env"
VENV_BIN="${APP_ROOT}/venv/bin"
SAAS_DBDOCTOR_BIN="${SAAS_DBDOCTOR_BIN:-/usr/local/bin/saas-dbdoctor}"

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

echo "==> Deploying SMS SaaS"

sudo -u "${APP_USER}" bash -c "cd \"${APP_ROOT}\" && git pull --ff-only"
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

sudo systemctl restart sms-saas sms-saas-worker sms-saas-scheduler.timer sms-saas-billing-reconcile.timer

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
