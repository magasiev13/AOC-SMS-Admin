#!/usr/bin/env bash
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON_BIN="${REPO_ROOT}/venv/bin/python"
TEMP_ROOT="$(mktemp -d)"
REDIS_PID=""
WORKER_PID=""

cleanup() {
  if [[ -n "${WORKER_PID}" ]]; then
    kill "${WORKER_PID}" >/dev/null 2>&1 || true
    wait "${WORKER_PID}" >/dev/null 2>&1 || true
  fi
  if [[ -n "${REDIS_PID}" ]]; then
    kill "${REDIS_PID}" >/dev/null 2>&1 || true
    wait "${REDIS_PID}" >/dev/null 2>&1 || true
  fi
  rm -rf -- "${TEMP_ROOT}"
}
trap cleanup EXIT

if [[ ! -x "${PYTHON_BIN}" ]]; then
  echo "Local Python runtime is missing. Run ./run/setup.sh first." >&2
  exit 1
fi
if ! command -v redis-server >/dev/null 2>&1; then
  echo "redis-server is required for the isolated readiness check." >&2
  exit 1
fi

REDIS_PORT="$("${PYTHON_BIN}" -c '
import socket

with socket.socket() as listener:
    listener.bind(("127.0.0.1", 0))
    print(listener.getsockname()[1])
')"
REDIS_URL="redis://127.0.0.1:${REDIS_PORT}/0"
DATABASE_URL="sqlite:///${TEMP_ROOT}/readiness.db"
RQ_QUEUE_NAME="twinevia-saas-readiness"

redis-server \
  --bind 127.0.0.1 \
  --port "${REDIS_PORT}" \
  --save "" \
  --appendonly no \
  --dir "${TEMP_ROOT}" \
  >"${TEMP_ROOT}/redis.log" 2>&1 &
REDIS_PID=$!

redis_ready=0
for attempt in $(seq 1 40); do
  if REDIS_URL="${REDIS_URL}" "${PYTHON_BIN}" -c 'from redis import Redis; import os; raise SystemExit(0 if Redis.from_url(os.environ["REDIS_URL"]).ping() else 1)' >/dev/null 2>&1; then
    redis_ready=1
    break
  fi
  sleep 0.1
done
if [[ "${redis_ready}" != "1" ]]; then
  echo "The isolated Redis instance did not become ready." >&2
  sed -n '1,120p' "${TEMP_ROOT}/redis.log" >&2
  exit 1
fi

export DATABASE_URL
export REDIS_URL
export RQ_QUEUE_NAME
export RQ_WORKER_NAME="twinevia-readiness-${REDIS_PORT}"
export SAAS_MODE=1
export SCHEDULER_ENABLED=0
export FLASK_ENV=development
export FLASK_DEBUG=1
export APP_RELEASE_ID="local-readiness"
export STRIPE_SECRET_KEY="sk_test_readiness"
export STRIPE_WEBHOOK_SECRET="whsec_readiness"
export STRIPE_MONTHLY_PRICE_ID="price_monthly_readiness"
export STRIPE_ANNUAL_PRICE_ID="price_annual_readiness"
export STRIPE_ACTIVATION_PRICE_ID="price_activation_readiness"
export PUBLIC_BASE_URL="https://public.readiness.test"
export APP_BASE_URL="https://app.readiness.test"
export TWILIO_CREDENTIAL_ENCRYPTION_KEY="readiness-placeholder"

if ! "${PYTHON_BIN}" -m app.saas_db --apply >"${TEMP_ROOT}/migrations.log" 2>&1; then
  echo "The isolated readiness database could not be migrated." >&2
  sed -n '1,200p' "${TEMP_ROOT}/migrations.log" >&2
  exit 1
fi
"${PYTHON_BIN}" -m app.rq_worker >"${TEMP_ROOT}/worker.log" 2>&1 &
WORKER_PID=$!

worker_ready=0
for attempt in $(seq 1 40); do
  if "${PYTHON_BIN}" -c '
import os
from redis import Redis
from rq import Worker

workers = Worker.all(connection=Redis.from_url(os.environ["REDIS_URL"]))
raise SystemExit(0 if workers else 1)
' >/dev/null 2>&1; then
    worker_ready=1
    break
  fi
  sleep 0.1
done
if [[ "${worker_ready}" != "1" ]]; then
  echo "The isolated RQ worker did not register." >&2
  sed -n '1,160p' "${TEMP_ROOT}/worker.log" >&2
  exit 1
fi

"${PYTHON_BIN}" -m app.readiness --infrastructure-only
