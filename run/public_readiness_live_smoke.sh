#!/usr/bin/env bash
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
RUN_ID=""

usage() {
  cat <<'EOF'
Usage: ./run/public_readiness_live_smoke.sh [--run-id RUN_ID]

Runs the authenticated read-only live smoke checks against the public Twinevia host.

Required environment:
  TWINEVIA_OWNER_USERNAME
  TWINEVIA_OWNER_PASSWORD
  TWINEVIA_PLATFORM_USERNAME
  TWINEVIA_PLATFORM_PASSWORD

Optional environment:
  TWINEVIA_LIVE_BASE_URL   Default: https://twinevia.com
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --run-id)
      RUN_ID="${2:-}"
      shift 2
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "Unknown argument: $1" >&2
      usage >&2
      exit 1
      ;;
  esac
done

required_env=(
  "TWINEVIA_OWNER_USERNAME"
  "TWINEVIA_OWNER_PASSWORD"
  "TWINEVIA_PLATFORM_USERNAME"
  "TWINEVIA_PLATFORM_PASSWORD"
)
missing_env=()
for key in "${required_env[@]}"; do
  if [[ -z "${!key:-}" ]]; then
    missing_env+=("${key}")
  fi
done

if [[ ${#missing_env[@]} -gt 0 ]]; then
  printf 'ERROR: Missing required live smoke environment: %s\n' "${missing_env[*]}" >&2
  exit 1
fi

if ! command -v npm >/dev/null 2>&1; then
  echo "ERROR: npm is required to run Playwright browser tests." >&2
  exit 1
fi

if [[ -z "${RUN_ID}" ]]; then
  RUN_ID="$(date +%Y%m%d-%H%M%S)"
fi

RUN_DIR="${REPO_ROOT}/output/signoff/${RUN_ID}/live-smoke"
mkdir -p "${RUN_DIR}"

if [[ ! -d "${REPO_ROOT}/node_modules" ]]; then
  echo "Installing browser test dependencies..."
  (cd "${REPO_ROOT}" && npm install)
fi

if [[ ! -d "${HOME}/Library/Caches/ms-playwright" ]] && [[ ! -d "${HOME}/.cache/ms-playwright" ]]; then
  echo "Installing Chromium for Playwright..."
  (cd "${REPO_ROOT}" && npx playwright install chromium)
fi

BASE_URL="${TWINEVIA_LIVE_BASE_URL:-https://twinevia.com}"
LOG_FILE="${RUN_DIR}/live-smoke.log"

printf '%s\n' "${BASE_URL}" > "${RUN_DIR}/base_url.txt"
echo "==> live_smoke" | tee "${LOG_FILE}"
(
  cd "${REPO_ROOT}"
  PLAYWRIGHT_ARTIFACT_DIR="${RUN_DIR}" \
  TWINEVIA_LIVE_BASE_URL="${BASE_URL}" \
  npx playwright test --config playwright.live.config.js tests/browser/live-production-smoke.spec.js
) 2>&1 | tee -a "${LOG_FILE}"

cat > "${RUN_DIR}/summary.txt" <<EOF
Live production smoke checks completed successfully.
Run ID: ${RUN_ID}
Base URL: ${BASE_URL}
Playwright report: ${RUN_DIR}/report
Log: ${LOG_FILE}
EOF

echo "Live smoke artifacts written to ${RUN_DIR}"
