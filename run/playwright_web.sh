#!/usr/bin/env bash
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENV_DIR="${REPO_ROOT}/venv"
PYTHON_BIN="${VENV_DIR}/bin/python"
REQUIRED_PYTHON="3.11"
PLAYWRIGHT_PORT="${PLAYWRIGHT_PORT:-5010}"
PLAYWRIGHT_BASE_URL="${PLAYWRIGHT_BASE_URL:-http://127.0.0.1:${PLAYWRIGHT_PORT}}"
PLAYWRIGHT_PUBLIC_BASE_URL="${PLAYWRIGHT_PUBLIC_BASE_URL:-${PLAYWRIGHT_BASE_URL}}"
PLAYWRIGHT_APP_BASE_URL="${PLAYWRIGHT_APP_BASE_URL:-${PLAYWRIGHT_BASE_URL}}"
PLAYWRIGHT_DB_DIR="${REPO_ROOT}/.playwright"
PLAYWRIGHT_DB_PATH="${PLAYWRIGHT_DB_PATH:-${PLAYWRIGHT_DB_DIR}/browser-tests.db}"
PLAYWRIGHT_ARTIFACT_DIR="${PLAYWRIGHT_ARTIFACT_DIR:-${REPO_ROOT}/output/playwright}"
PLAYWRIGHT_INFRA_ROOT="$(mktemp -d)"
PLAYWRIGHT_REDIS_PID=""
PLAYWRIGHT_WORKER_PID=""

cleanup_playwright_infrastructure() {
  if [[ -n "${PLAYWRIGHT_WORKER_PID}" ]]; then
    kill "${PLAYWRIGHT_WORKER_PID}" >/dev/null 2>&1 || true
    wait "${PLAYWRIGHT_WORKER_PID}" >/dev/null 2>&1 || true
  fi
  if [[ -n "${PLAYWRIGHT_REDIS_PID}" ]]; then
    kill "${PLAYWRIGHT_REDIS_PID}" >/dev/null 2>&1 || true
    wait "${PLAYWRIGHT_REDIS_PID}" >/dev/null 2>&1 || true
  fi
  rm -rf -- "${PLAYWRIGHT_INFRA_ROOT}"
}

terminate_playwright_web() {
  exit 143
}

trap cleanup_playwright_infrastructure EXIT
trap terminate_playwright_web INT TERM

if [[ ! -x "${PYTHON_BIN}" ]]; then
  echo "ERROR: venv not found at ${PYTHON_BIN}." >&2
  echo "Run: ./run/setup.sh" >&2
  exit 1
fi

VENV_PYTHON_VERSION="$("${PYTHON_BIN}" -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')"
if [[ "${VENV_PYTHON_VERSION}" != "${REQUIRED_PYTHON}" ]]; then
  echo "ERROR: venv uses Python ${VENV_PYTHON_VERSION}; expected ${REQUIRED_PYTHON}." >&2
  echo "Recreate it with: rm -rf ${VENV_DIR} && ./run/setup.sh" >&2
  exit 1
fi
if ! command -v redis-server >/dev/null 2>&1; then
  echo "ERROR: redis-server is required for isolated browser tests." >&2
  exit 1
fi

mkdir -p \
  "${PLAYWRIGHT_DB_DIR}" \
  "${PLAYWRIGHT_ARTIFACT_DIR}/report" \
  "${PLAYWRIGHT_ARTIFACT_DIR}/test-results" \
  "${PLAYWRIGHT_ARTIFACT_DIR}/infrastructure"

PLAYWRIGHT_REDIS_PORT="$("${PYTHON_BIN}" -c '
import socket

with socket.socket() as listener:
    listener.bind(("127.0.0.1", 0))
    print(listener.getsockname()[1])
')"
export REDIS_URL="redis://127.0.0.1:${PLAYWRIGHT_REDIS_PORT}/0"
export RQ_QUEUE_NAME="twinevia-saas"
export RQ_WORKER_NAME="twinevia-playwright-${PLAYWRIGHT_REDIS_PORT}"

redis-server \
  --bind 127.0.0.1 \
  --port "${PLAYWRIGHT_REDIS_PORT}" \
  --save "" \
  --appendonly no \
  --dir "${PLAYWRIGHT_INFRA_ROOT}" \
  >"${PLAYWRIGHT_ARTIFACT_DIR}/infrastructure/redis.log" 2>&1 &
PLAYWRIGHT_REDIS_PID=$!

playwright_redis_ready=0
for attempt in $(seq 1 40); do
  if "${PYTHON_BIN}" -c '
import os
from redis import Redis

raise SystemExit(0 if Redis.from_url(os.environ["REDIS_URL"]).ping() else 1)
' >/dev/null 2>&1; then
    playwright_redis_ready=1
    break
  fi
  sleep 0.1
done
if [[ "${playwright_redis_ready}" != "1" ]]; then
  echo "ERROR: isolated browser Redis did not become ready." >&2
  sed -n '1,160p' "${PLAYWRIGHT_ARTIFACT_DIR}/infrastructure/redis.log" >&2
  exit 1
fi

export DATABASE_URL="sqlite:///${PLAYWRIGHT_DB_PATH}"
export FLASK_DEBUG=0
export SAAS_MODE=1
export SCHEDULER_ENABLED=0
export SECRET_KEY="playwright-browser-secret"
export SAAS_BASE_URL="${PLAYWRIGHT_APP_BASE_URL}"
export PUBLIC_BASE_URL="${PLAYWRIGHT_PUBLIC_BASE_URL}"
export APP_BASE_URL="${PLAYWRIGHT_APP_BASE_URL}"
export PLATFORM_SERVICE_RESTART_ENABLED=1
export STRIPE_SECRET_KEY="sk_test_browser"
export STRIPE_WEBHOOK_SECRET="whsec_browser"
export STRIPE_PRICE_ID="price_browser"
export STRIPE_MONTHLY_PRICE_ID="price_browser"
export STRIPE_ANNUAL_PRICE_ID="price_browser_annual"
export STRIPE_ACTIVATION_PRICE_ID="price_browser_activation"
export STRIPE_FAKE_CHECKOUT_ENABLED=1
export BILLING_TRIAL_DAYS=14
export TWILIO_CREDENTIAL_ENCRYPTION_KEY="4jHh8g7UFD3rjpWrW0zLPRenSn7bmG5qd73PRoSaD0o="
export TWILIO_BROWSER_FAKE_SENDS=1
export TWILIO_ACCOUNT_SID="${TWILIO_ACCOUNT_SID:-ACplaywrightplatform}"
export TWILIO_AUTH_TOKEN="${TWILIO_AUTH_TOKEN:-playwright-platform-token}"
export TWILIO_A2P_ONBOARDING_ENABLED=1
export TWILIO_PRIMARY_CUSTOMER_PROFILE_SID="BUbrowserprimary123"
export TWILIO_A2P_FAKE_QUEUE=1
export PLAYWRIGHT_ARTIFACT_DIR
export ADMIN_USERNAME="platform-admin"
export ADMIN_EMAIL="platform@browser.test"
export ADMIN_PASSWORD="Platform-pass1!"
export PYTHONPATH="${REPO_ROOT}:${PYTHONPATH:-}"

cd "${REPO_ROOT}"
"${PYTHON_BIN}" "${REPO_ROOT}/tests/browser/seed_saas_browser_db.py" "${PLAYWRIGHT_DB_PATH}" "${PLAYWRIGHT_BASE_URL}"

"${PYTHON_BIN}" -m app.rq_worker \
  >"${PLAYWRIGHT_ARTIFACT_DIR}/infrastructure/worker.log" 2>&1 &
PLAYWRIGHT_WORKER_PID=$!

playwright_worker_ready=0
for attempt in $(seq 1 40); do
  if "${PYTHON_BIN}" -c '
import os
from redis import Redis
from rq import Worker

workers = Worker.all(connection=Redis.from_url(os.environ["REDIS_URL"]))
raise SystemExit(0 if any(worker.name == os.environ["RQ_WORKER_NAME"] for worker in workers) else 1)
' >/dev/null 2>&1; then
    playwright_worker_ready=1
    break
  fi
  sleep 0.1
done
if [[ "${playwright_worker_ready}" != "1" ]]; then
  echo "ERROR: isolated browser RQ worker did not register." >&2
  sed -n '1,200p' "${PLAYWRIGHT_ARTIFACT_DIR}/infrastructure/worker.log" >&2
  exit 1
fi

"${PYTHON_BIN}" -m flask --app wsgi:app run --host 127.0.0.1 --port "${PLAYWRIGHT_PORT}" --no-debugger --no-reload
