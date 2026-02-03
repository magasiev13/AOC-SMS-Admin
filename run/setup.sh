#!/usr/bin/env bash
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENV_DIR="${REPO_ROOT}/venv"
ENV_FILE="${REPO_ROOT}/.env"
ENV_EXAMPLE="${REPO_ROOT}/.env.example"

echo "== SMS Admin local setup =="

if ! command -v python3 >/dev/null 2>&1; then
  echo "ERROR: python3 is required but was not found in PATH." >&2
  exit 1
fi

if [[ ! -d "${VENV_DIR}" ]]; then
  python3 -m venv "${VENV_DIR}"
  echo "Created venv: ${VENV_DIR}"
else
  echo "Using existing venv: ${VENV_DIR}"
fi

PYTHON_BIN="${VENV_DIR}/bin/python"
if [[ ! -x "${PYTHON_BIN}" ]]; then
  echo "ERROR: venv python not found at ${PYTHON_BIN}." >&2
  exit 1
fi

"${PYTHON_BIN}" -m pip install -r "${REPO_ROOT}/requirements.txt"
echo "Installed Python dependencies."

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
3. Run the app: flask --app wsgi:app run --debug
EOF
