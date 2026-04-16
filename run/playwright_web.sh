#!/usr/bin/env bash
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENV_DIR="${REPO_ROOT}/venv"
PYTHON_BIN="${VENV_DIR}/bin/python"
REQUIRED_PYTHON="3.11"
PLAYWRIGHT_PORT="${PLAYWRIGHT_PORT:-5010}"
PLAYWRIGHT_BASE_URL="${PLAYWRIGHT_BASE_URL:-http://127.0.0.1:${PLAYWRIGHT_PORT}}"
PLAYWRIGHT_DB_DIR="${REPO_ROOT}/.playwright"
PLAYWRIGHT_DB_PATH="${PLAYWRIGHT_DB_PATH:-${PLAYWRIGHT_DB_DIR}/browser-tests.db}"
PLAYWRIGHT_ARTIFACT_DIR="${PLAYWRIGHT_ARTIFACT_DIR:-${REPO_ROOT}/output/playwright}"

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

mkdir -p "${PLAYWRIGHT_DB_DIR}" "${PLAYWRIGHT_ARTIFACT_DIR}/report" "${PLAYWRIGHT_ARTIFACT_DIR}/test-results"

export DATABASE_URL="sqlite:///${PLAYWRIGHT_DB_PATH}"
export FLASK_DEBUG=0
export SAAS_MODE=1
export SCHEDULER_ENABLED=0
export SECRET_KEY="playwright-browser-secret"
export SAAS_BASE_URL="${PLAYWRIGHT_BASE_URL}"
export PLATFORM_SERVICE_RESTART_ENABLED=1
export STRIPE_SECRET_KEY="sk_test_browser"
export STRIPE_WEBHOOK_SECRET="whsec_browser"
export STRIPE_PRICE_ID="price_browser"
export STRIPE_FAKE_CHECKOUT_ENABLED=1
export TWILIO_CREDENTIAL_ENCRYPTION_KEY="4jHh8g7UFD3rjpWrW0zLPRenSn7bmG5qd73PRoSaD0o="
export TWILIO_BROWSER_FAKE_SENDS=1
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

exec "${PYTHON_BIN}" -m flask --app wsgi:app run --host 127.0.0.1 --port "${PLAYWRIGHT_PORT}" --no-debugger --no-reload
