#!/usr/bin/env bash
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

if ! command -v npm >/dev/null 2>&1; then
  echo "ERROR: npm is required to run Playwright browser tests." >&2
  exit 1
fi

cd "${REPO_ROOT}"

if [[ ! -d node_modules ]]; then
  echo "Installing browser test dependencies..."
  npm install
fi

if [[ ! -d "${HOME}/Library/Caches/ms-playwright" ]] && [[ ! -d "${HOME}/.cache/ms-playwright" ]]; then
  echo "Installing Chromium for Playwright..."
  npx playwright install chromium
fi

exec npm run test:browser -- "$@"
