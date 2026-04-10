#!/usr/bin/env bash
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
RUN_ID=""

usage() {
  cat <<'EOF'
Usage: ./run/public_readiness_local.sh [--run-id RUN_ID]

Runs the deterministic local public-readiness gate and stores all evidence under:
  output/signoff/<run-id>/local/
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

if [[ -z "${RUN_ID}" ]]; then
  RUN_ID="$(date +%Y%m%d-%H%M%S)"
fi

RUN_DIR="${REPO_ROOT}/output/signoff/${RUN_ID}/local"
PLAYWRIGHT_DIR="${RUN_DIR}/playwright"
mkdir -p "${PLAYWRIGHT_DIR}"

run_and_capture() {
  local name="$1"
  shift
  local log_file="${RUN_DIR}/${name}.log"
  echo "==> ${name}" | tee "${log_file}"
  "$@" 2>&1 | tee -a "${log_file}"
}

run_and_capture browser env PLAYWRIGHT_ARTIFACT_DIR="${PLAYWRIGHT_DIR}" "${REPO_ROOT}/run/test_browser.sh"
run_and_capture backend "${REPO_ROOT}/run/test.sh"
run_and_capture verify "${REPO_ROOT}/run/verify.sh"

cat > "${RUN_DIR}/summary.txt" <<EOF
Public-readiness local signoff completed successfully.
Run ID: ${RUN_ID}
Browser artifacts: ${PLAYWRIGHT_DIR}
Backend log: ${RUN_DIR}/backend.log
Verify log: ${RUN_DIR}/verify.log
EOF

echo "Local signoff artifacts written to ${RUN_DIR}"
