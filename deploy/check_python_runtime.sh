#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEFAULT_APP_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
APP_ROOT="${APP_ROOT:-${DEFAULT_APP_ROOT}}"
PYTHON_BIN="${PYTHON_BIN:-${APP_ROOT}/venv/bin/python}"
REQUIRED_PYTHON="${REQUIRED_PYTHON:-3.11}"
STALE_VENV_PATH="${STALE_VENV_PATH:-/opt/sms-saas/venv}"

if [[ ! -x "${PYTHON_BIN}" ]]; then
  echo "[python-runtime] ERROR: Python executable not found at ${PYTHON_BIN}" >&2
  exit 1
fi

DETECTED_PYTHON="$("${PYTHON_BIN}" -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')"
if [[ "${DETECTED_PYTHON}" != "${REQUIRED_PYTHON}" ]]; then
  echo "[python-runtime] ERROR: ${PYTHON_BIN} is Python ${DETECTED_PYTHON}; expected ${REQUIRED_PYTHON}." >&2
  echo "[python-runtime] Recreate ${APP_ROOT}/venv with python3.11 before starting services." >&2
  exit 1
fi

if [[ -f "${APP_ROOT}/venv/pyvenv.cfg" ]]; then
  if grep -qF "${STALE_VENV_PATH}" "${APP_ROOT}/venv/pyvenv.cfg"; then
    echo "[python-runtime] ERROR: ${APP_ROOT}/venv/pyvenv.cfg references stale venv path ${STALE_VENV_PATH}." >&2
    echo "[python-runtime] Rebuild ${APP_ROOT}/venv with deploy/ensure_canonical_venv.sh before starting services." >&2
    exit 1
  fi
  if grep -qF "${APP_ROOT}/venv.next-" "${APP_ROOT}/venv/pyvenv.cfg"; then
    echo "[python-runtime] ERROR: ${APP_ROOT}/venv/pyvenv.cfg references a temporary venv path." >&2
    echo "[python-runtime] Rebuild ${APP_ROOT}/venv with deploy/ensure_canonical_venv.sh before starting services." >&2
    exit 1
  fi
fi

for entrypoint in rq gunicorn pip flask; do
  entrypoint_path="${APP_ROOT}/venv/bin/${entrypoint}"
  if [[ ! -f "${entrypoint_path}" ]]; then
    continue
  fi
  if head -n 1 "${entrypoint_path}" | grep -qF "${STALE_VENV_PATH}"; then
    echo "[python-runtime] ERROR: ${entrypoint_path} shebang references stale venv path ${STALE_VENV_PATH}." >&2
    echo "[python-runtime] Rebuild ${APP_ROOT}/venv with deploy/ensure_canonical_venv.sh before starting services." >&2
    exit 1
  fi
  if head -n 1 "${entrypoint_path}" | grep -qF "${APP_ROOT}/venv.next-"; then
    echo "[python-runtime] ERROR: ${entrypoint_path} shebang references a temporary venv path." >&2
    echo "[python-runtime] Rebuild ${APP_ROOT}/venv with deploy/ensure_canonical_venv.sh before starting services." >&2
    exit 1
  fi
done
