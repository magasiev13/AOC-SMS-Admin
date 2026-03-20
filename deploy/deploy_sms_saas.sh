#!/usr/bin/env bash
set -euo pipefail

APP_ROOT="${APP_ROOT:-/opt/sms-saas}"
APP_USER="${APP_USER:-smsadmin}"
ENV_FILE="${APP_ROOT}/.env"
VENV_BIN="${APP_ROOT}/venv/bin"
SAAS_DBDOCTOR_BIN="${SAAS_DBDOCTOR_BIN:-/usr/local/bin/saas-dbdoctor}"

echo "==> Deploying SMS SaaS"

sudo -u "${APP_USER}" bash -c "cd \"${APP_ROOT}\" && git pull --ff-only"
sudo -u "${APP_USER}" "${VENV_BIN}/pip" install -r "${APP_ROOT}/requirements.txt"
sudo -u "${APP_USER}" bash -c "cd \"${APP_ROOT}\" && \"${SAAS_DBDOCTOR_BIN}\" --apply && \"${SAAS_DBDOCTOR_BIN}\" --doctor"

if ! sudo -u "${APP_USER}" bash -lc "set -euo pipefail; cd \"${APP_ROOT}\"; set -a; source \"${ENV_FILE}\"; set +a; \"${VENV_BIN}/python\" - <<'PY'
from app import create_app
create_app(run_startup_tasks=False, start_scheduler=False)
print('SaaS app config validation ok')
PY"; then
  echo "==> SaaS app validation failed before restart." >&2
  exit 1
fi

sudo systemctl restart sms-saas sms-saas-worker sms-saas-scheduler.timer sms-saas-billing-reconcile.timer

if ! curl -fsS --connect-timeout 2 --max-time 5 http://127.0.0.1:8100/health >/dev/null; then
  echo "==> SaaS health check failed after deploy." >&2
  sudo systemctl status sms-saas --no-pager || true
  sudo journalctl -u sms-saas -n 120 --no-pager || true
  exit 1
fi

echo "SaaS deploy completed successfully."
