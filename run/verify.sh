#!/usr/bin/env bash
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENV_PYTHON="${REPO_ROOT}/venv/bin/python"
REQUIRED_PYTHON="3.11"

if [[ -x "${VENV_PYTHON}" ]]; then
  PYTHON_BIN="${VENV_PYTHON}"
elif command -v python3.11 >/dev/null 2>&1; then
  PYTHON_BIN="$(command -v python3.11)"
elif command -v python3 >/dev/null 2>&1; then
  PYTHON_BIN="$(command -v python3)"
else
  echo "ERROR: Python ${REQUIRED_PYTHON} is required to run static verification." >&2
  exit 1
fi

PYTHON_VERSION="$(${PYTHON_BIN} -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')"
if [[ "${PYTHON_VERSION}" != "${REQUIRED_PYTHON}" ]]; then
  echo "ERROR: verification must run on Python ${REQUIRED_PYTHON}; found ${PYTHON_VERSION}." >&2
  echo "Use ./run/setup.sh to create the project venv, then rerun ./run/verify.sh" >&2
  exit 1
fi

unset PYTHONPATH
cd "${REPO_ROOT}"

"${PYTHON_BIN}" -m compileall -q app tests
