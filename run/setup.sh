#!/usr/bin/env bash
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENV_DIR="${REPO_ROOT}/venv"
ENV_FILE="${REPO_ROOT}/.env"
ENV_EXAMPLE="${REPO_ROOT}/.env.example"
REQUIRED_PYTHON="3.11"
PYTHON_CMD="${PYTHON_CMD:-python3.11}"

echo "== SMS Admin local setup =="

if ! command -v "${PYTHON_CMD}" >/dev/null 2>&1; then
  echo "ERROR: Python ${REQUIRED_PYTHON} is required but ${PYTHON_CMD} was not found in PATH." >&2
  echo "Install Python ${REQUIRED_PYTHON} and rerun, or set PYTHON_CMD to a Python ${REQUIRED_PYTHON} binary." >&2
  exit 1
fi

PYTHON_CMD_VERSION="$("${PYTHON_CMD}" -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')"
if [[ "${PYTHON_CMD_VERSION}" != "${REQUIRED_PYTHON}" ]]; then
  echo "ERROR: ${PYTHON_CMD} resolved to Python ${PYTHON_CMD_VERSION}; expected ${REQUIRED_PYTHON}." >&2
  exit 1
fi

PYTHON_BIN="${VENV_DIR}/bin/python"
if [[ ! -x "${PYTHON_BIN}" ]]; then
  "${PYTHON_CMD}" -m venv "${VENV_DIR}"
  echo "Created venv: ${VENV_DIR}"
else
  echo "Using existing venv: ${VENV_DIR}"
fi

if [[ ! -x "${PYTHON_BIN}" ]]; then
  echo "ERROR: venv python not found at ${PYTHON_BIN} after setup." >&2
  exit 1
fi

VENV_PYTHON_VERSION="$("${PYTHON_BIN}" -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')"
if [[ "${VENV_PYTHON_VERSION}" != "${REQUIRED_PYTHON}" ]]; then
  echo "ERROR: venv uses Python ${VENV_PYTHON_VERSION}; expected ${REQUIRED_PYTHON}." >&2
  echo "Recreate it with: rm -rf ${VENV_DIR} && ./run/setup.sh" >&2
  exit 1
fi

"${PYTHON_BIN}" -m pip install -r "${REPO_ROOT}/requirements.txt"
"${PYTHON_BIN}" -m pip install pytest pytest-cov
echo "Installed Python dependencies, pytest, and pytest-cov."

if [[ -f "${ENV_FILE}" ]]; then
  echo "Keeping existing .env file."
elif [[ -f "${ENV_EXAMPLE}" ]]; then
  cp "${ENV_EXAMPLE}" "${ENV_FILE}"
  echo "Created .env from .env.example. Update it with real credentials."
else
  echo "WARNING: .env.example not found. Create .env manually." >&2
fi

mkdir -p "${REPO_ROOT}/instance"
echo "Ensured instance directory exists."

cat <<'EOF'
Next steps:
1. Edit .env with your credentials.
2. Activate venv: source venv/bin/activate
3. Run tests: ./run/test.sh
4. Run the app: flask --app wsgi:app run --debug
EOF
