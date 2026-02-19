#!/usr/bin/env bash
set -euo pipefail

APP_ROOT="${APP_ROOT:-/opt/sms-admin}"
PYTHON_BIN="${PYTHON_BIN:-${APP_ROOT}/venv/bin/python}"
REQUIRED_PYTHON="${REQUIRED_PYTHON:-3.11}"

if [[ ! -x "${PYTHON_BIN}" ]]; then
  echo "[python-runtime] ERROR: Python executable not found at ${PYTHON_BIN}" >&2
  exit 1
fi

DETECTED_PYTHON="$("${PYTHON_BIN}" -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')"
if [[ "${DETECTED_PYTHON}" != "${REQUIRED_PYTHON}" ]]; then
  echo "[python-runtime] ERROR: ${PYTHON_BIN} is Python ${DETECTED_PYTHON}; expected ${REQUIRED_PYTHON}." >&2
  echo "[python-runtime] Recreate /opt/sms-admin/venv with python3.11 before starting services." >&2
  exit 1
fi
