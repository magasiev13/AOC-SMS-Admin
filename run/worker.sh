#!/usr/bin/env bash
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENV_DIR="${REPO_ROOT}/venv"
PYTHON_BIN="${VENV_DIR}/bin/python"

if [[ ! -x "${PYTHON_BIN}" ]]; then
  echo "ERROR: venv not found at ${PYTHON_BIN}." >&2
  echo "Run: ./run/setup.sh" >&2
  exit 1
fi

if [[ -f "${REPO_ROOT}/.env" ]]; then
  # rq CLI does not load .env automatically.
  set -a
  # shellcheck disable=SC1091
  source "${REPO_ROOT}/.env"
  set +a
else
  echo "WARNING: .env not found; using defaults where possible." >&2
fi

REDIS_URL="${REDIS_URL:-redis://localhost:6379/0}"
RQ_QUEUE_NAME="${RQ_QUEUE_NAME:-sms}"

RQ_BIN="${VENV_DIR}/bin/rq"
if [[ -x "${RQ_BIN}" ]]; then
  exec "${RQ_BIN}" worker --url "${REDIS_URL}" "${RQ_QUEUE_NAME}"
fi

exec "${PYTHON_BIN}" -m rq worker --url "${REDIS_URL}" "${RQ_QUEUE_NAME}"
