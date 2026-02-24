#!/usr/bin/env bash
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENV_DIR="${REPO_ROOT}/venv"
PYTHON_BIN="${VENV_DIR}/bin/python"
REQUIRED_PYTHON="3.11"

if [[ ! -x "${PYTHON_BIN}" ]]; then
  echo "Local venv is missing. Bootstrapping with ./run/setup.sh ..."
  "${REPO_ROOT}/run/setup.sh"
fi

if [[ ! -x "${PYTHON_BIN}" ]]; then
  echo "ERROR: venv not found at ${PYTHON_BIN} after setup." >&2
  exit 1
fi

VENV_PYTHON_VERSION="$("${PYTHON_BIN}" -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')"
if [[ "${VENV_PYTHON_VERSION}" != "${REQUIRED_PYTHON}" ]]; then
  echo "ERROR: venv uses Python ${VENV_PYTHON_VERSION}; expected ${REQUIRED_PYTHON}." >&2
  echo "Recreate it with: rm -rf ${VENV_DIR} && ./run/setup.sh" >&2
  exit 1
fi

if ! "${PYTHON_BIN}" - <<'PY' >/dev/null 2>&1
import flask
import flask_sqlalchemy
import flask_login
import flask_wtf
import sqlalchemy
import pytest
import pytest_cov
PY
then
  echo "Missing test/runtime dependencies in venv. Installing requirements + pytest tooling ..."
  "${PYTHON_BIN}" -m pip install -r "${REPO_ROOT}/requirements.txt"
  "${PYTHON_BIN}" -m pip install pytest pytest-cov
fi

unset PYTHONPATH
cd "${REPO_ROOT}"

has_target_path=0
for arg in "$@"; do
  if [[ "${arg}" != -* ]]; then
    has_target_path=1
    break
  fi
done

if [[ "$#" -eq 0 || "${has_target_path}" -eq 0 ]]; then
  exec "${PYTHON_BIN}" -m pytest --import-mode=importlib tests "$@"
fi

exec "${PYTHON_BIN}" -m pytest --import-mode=importlib "$@"
