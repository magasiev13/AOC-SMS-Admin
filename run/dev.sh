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

if [[ ! -f "${REPO_ROOT}/.env" ]]; then
  echo "WARNING: .env not found. The app may not start without required variables." >&2
  echo "Tip: cp .env.example .env" >&2
fi

exec "${PYTHON_BIN}" -m flask --app wsgi:app run --debug
