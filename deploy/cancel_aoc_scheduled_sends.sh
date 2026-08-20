#!/usr/bin/env bash
set -euo pipefail

APP_ROOT="${APP_ROOT:-/opt/twinevia-saas/current}"
ENV_FILE="${TWINEVIA_ENV_FILE:-/opt/twinevia-saas/.env}"
EXPECTED_COUNT=""
CONFIRMED_SLUG=""

usage() {
  echo "Usage: $0 --expected-count 2 --confirm-organization-slug armenians-of-colorado" >&2
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --expected-count)
      EXPECTED_COUNT="${2:-}"
      shift 2
      ;;
    --confirm-organization-slug)
      CONFIRMED_SLUG="${2:-}"
      shift 2
      ;;
    *)
      usage
      exit 1
      ;;
  esac
done

if [[ "${EXPECTED_COUNT}" != "2" || "${CONFIRMED_SLUG}" != "armenians-of-colorado" ]]; then
  echo "This launch operation requires the exact AOC slug and expected count of 2." >&2
  exit 1
fi
if [[ ! -r "${ENV_FILE}" || ! -x "${APP_ROOT}/venv/bin/python" ]]; then
  echo "The production environment or current release runtime is unavailable." >&2
  exit 1
fi

set -a
source "${ENV_FILE}"
source "${APP_ROOT}/.release.env"
set +a

sudo systemctl stop twinevia-saas-scheduler.timer
sudo systemctl stop twinevia-saas-scheduler.service || true
if sudo systemctl is-active --quiet twinevia-saas-scheduler.service; then
  echo "The scheduler service is still active; cancellation was not attempted." >&2
  exit 1
fi

"${APP_ROOT}/venv/bin/python" -m app.aoc_scheduled_cancel \
  --expected-count "${EXPECTED_COUNT}" \
  --confirm-organization-slug "${CONFIRMED_SLUG}"
"${APP_ROOT}/venv/bin/python" -m app.aoc_scheduled_guard \
  --organization-slug "${CONFIRMED_SLUG}" \
  --expect-dispatchable-count 0

echo "AOC scheduled sends were recorded and cancelled. The scheduler timer remains stopped."
