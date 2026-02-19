#!/usr/bin/env bash
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENV_DIR="${REPO_ROOT}/venv"
PYTHON_BIN="${VENV_DIR}/bin/python"
REQUIRED_PYTHON="3.11"

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

if [[ -f "${REPO_ROOT}/.env" ]]; then
  set -a
  # shellcheck disable=SC1091
  source "${REPO_ROOT}/.env"
  set +a
else
  echo "WARNING: .env not found; using defaults where possible." >&2
fi

REDIS_URL="${REDIS_URL:-redis://localhost:6379/0}"
RQ_QUEUE_NAME="${RQ_QUEUE_NAME:-sms}"

check_redis() {
  if command -v redis-cli >/dev/null 2>&1; then
    redis-cli -u "${REDIS_URL}" ping >/dev/null 2>&1
    return $?
  fi

  "${PYTHON_BIN}" - <<'PY' >/dev/null 2>&1
import os
import redis

url = os.environ.get("REDIS_URL", "redis://localhost:6379/0")
client = redis.Redis.from_url(url)
client.ping()
PY
}

if ! check_redis; then
  echo "ERROR: Redis is not reachable at REDIS_URL=${REDIS_URL}" >&2
  echo "Start Redis locally, then retry." >&2
  echo "Examples:" >&2
  echo "  brew services start redis" >&2
  echo "  redis-server" >&2
  exit 1
fi

worker_pid=""

cleanup() {
  if [[ -n "${worker_pid}" ]] && kill -0 "${worker_pid}" >/dev/null 2>&1; then
    kill "${worker_pid}" >/dev/null 2>&1 || true
    wait "${worker_pid}" >/dev/null 2>&1 || true
  fi
}
trap cleanup EXIT INT TERM

echo "Starting RQ worker (queue=${RQ_QUEUE_NAME}, redis=${REDIS_URL})..."
"${REPO_ROOT}/run/worker.sh" &
worker_pid="$!"

echo "Starting Flask dev server..."
"${PYTHON_BIN}" -m flask --app wsgi:app run --debug
