#!/usr/bin/env bash
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${REPO_ROOT}"

status=0

check_clean() {
  local label="$1"
  local pattern="$2"
  shift 2
  local output=""

  output="$(
    rg -n \
      --hidden \
      --glob '!venv/**' \
      --glob '!output/**' \
      --glob '!node_modules/**' \
      --glob '!run/naming_audit.sh' \
      "${pattern}" "$@" || true
  )"
  if [[ -n "${output}" ]]; then
    echo "ERROR: ${label}" >&2
    echo "${output}" >&2
    echo >&2
    status=1
  fi
}

primary_paths=(
  README.md
  AGENTS.md
  .env.example
  app
  bin
  deploy
  docs
  run
  tests
  .github
  package.json
  package-lock.json
)

operator_paths=(
  README.md
  .env.example
  deploy
  docs
  run
  .github
)

check_clean "Found old product-brand references in primary surfaces" "Relayn" "${primary_paths[@]}"
check_clean "Found stale SaaS queue samples in primary surfaces" "RQ_QUEUE_NAME=sms-saas" "${primary_paths[@]}"
check_clean "Found stale Twilio friendly-name samples in primary surfaces" "TWILIO_PLATFORM_FRIENDLY_NAME=Relayn" "${primary_paths[@]}"
check_clean "Found stale SaaS restart helper references in primary surfaces" "restart-sms-saas-services" "${primary_paths[@]}"
check_clean "Found retired public-host references in operator surfaces" "sms\\.theitwingman\\.com|beta\\.theitwingman\\.com" "${operator_paths[@]}"
check_clean "Found retired beta script or artifact names in operator surfaces" "public_readiness_beta_snapshot\\.sh|beta_cutover\\.sh|beta-cutover|output/signoff/<run-id>/beta/" "${operator_paths[@]}"
check_clean "Found retired beta-environment wording in operator surfaces" "\\bbeta (snapshot|cutover|host|deploy|signoff)\\b" "${operator_paths[@]}"

if [[ "${status}" -eq 0 ]]; then
  echo "Naming audit passed."
fi

exit "${status}"
