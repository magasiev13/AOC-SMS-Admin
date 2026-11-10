#!/usr/bin/env bash
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PREVIEW_PORT="${PORT:-5011}"

if [[ ! "${PREVIEW_PORT}" =~ ^[0-9]+$ ]] || (( PREVIEW_PORT < 1 || PREVIEW_PORT > 65535 )); then
  echo "ERROR: PORT must be an integer from 1 to 65535." >&2
  exit 1
fi

# Desktop apps may not inherit the terminal's user-installed tool paths.
PREVIEW_PATH="${HOME}/.local/bin:${PATH:-/usr/bin:/bin}"

# Reuse the existing isolated browser stack. Drop inherited configuration and
# disable dotenv loading so a local preview cannot pick up live provider or DB settings.
exec env -i \
  HOME="${HOME}" \
  PATH="${PREVIEW_PATH}" \
  TMPDIR="${TMPDIR:-/tmp}" \
  LANG="${LANG:-en_US.UTF-8}" \
  PYTHON_DOTENV_DISABLED=1 \
  FLASK_SKIP_DOTENV=1 \
  PLAYWRIGHT_PORT="${PREVIEW_PORT}" \
  PLAYWRIGHT_BASE_URL="http://127.0.0.1:${PREVIEW_PORT}" \
  PLAYWRIGHT_DB_PATH="${REPO_ROOT}/.playwright/claude-preview.db" \
  PLAYWRIGHT_ARTIFACT_DIR="${REPO_ROOT}/.playwright/claude-preview-artifacts" \
  /bin/bash "${REPO_ROOT}/run/playwright_web.sh"
