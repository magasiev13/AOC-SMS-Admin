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

cd "${REPO_ROOT}"

commands=(
  "./venv/bin/python -m app.dbdoctor --help"
  "./venv/bin/python -m app.saas_db --help"
  "./run/local_saas_stack.sh --help"
  "./run/public_readiness_local.sh --help"
  "./run/public_readiness_beta_snapshot.sh --help"
)

for command in "${commands[@]}"; do
  echo "==> ${command}"
  eval "${command}" >/dev/null
done

echo "Doc smoke checks passed."
