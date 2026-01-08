#!/usr/bin/env bash
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DBDOCTOR_SRC="${REPO_ROOT}/bin/dbdoctor"
DBDOCTOR_DEST="${DBDOCTOR_DEST:-/usr/local/bin/dbdoctor}"
ENV_FILE="/opt/sms-admin/.env"
DEFAULT_REDIS_URL="redis://localhost:6379/0"
DEFAULT_RQ_QUEUE_NAME="sms"

if [[ ! -f "${DBDOCTOR_SRC}" ]]; then
  echo "ERROR: ${DBDOCTOR_SRC} not found. Did you clone the repo to ${REPO_ROOT}?" >&2
  exit 1
fi

sudo install -m 0755 "${DBDOCTOR_SRC}" "${DBDOCTOR_DEST}"

echo "Installed dbdoctor to ${DBDOCTOR_DEST}"

sudo touch "${ENV_FILE}"
sudo chown root:smsadmin "${ENV_FILE}"
sudo chmod 640 "${ENV_FILE}"

ensure_env_key() {
  local key="$1"
  local value="$2"
  if ! sudo grep -qE "^${key}=" "${ENV_FILE}"; then
    echo "${key}=${value}" | sudo tee -a "${ENV_FILE}" >/dev/null
  fi
}

ensure_env_key "REDIS_URL" "${DEFAULT_REDIS_URL}"
ensure_env_key "RQ_QUEUE_NAME" "${DEFAULT_RQ_QUEUE_NAME}"

echo "Running dbdoctor --apply"
sudo "${DBDOCTOR_DEST}" --apply

sudo install -m 0644 "${REPO_ROOT}/deploy/sms.service" /etc/systemd/system/sms.service
sudo install -m 0644 "${REPO_ROOT}/deploy/sms-worker.service" /etc/systemd/system/sms-worker.service
sudo install -m 0644 "${REPO_ROOT}/deploy/sms-scheduler.service" /etc/systemd/system/sms-scheduler.service

sudo systemctl daemon-reload
sudo systemctl enable --now sms sms-worker sms-scheduler

echo "Deploy report:"
set +e
if command -v redis-cli >/dev/null 2>&1; then
  redis-cli ping
else
  echo "redis-cli not found"
fi
systemctl status --no-pager sms sms-worker sms-scheduler
journalctl -u sms-worker -n 30 --no-pager
set -e
