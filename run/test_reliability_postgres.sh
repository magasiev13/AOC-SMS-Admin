#!/usr/bin/env bash
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
: "${TWINEVIA_TEST_POSTGRES_ROOT:?Set the explicit disposable audit PostgreSQL root.}"
if [[ "${TWINEVIA_TEST_POSTGRES_ROOT}" != "postgresql://audit@127.0.0.1:55432/postgres" ]]; then
  echo "Reliability tests require the dedicated audit user/loopback port55432 root." >&2
  exit 1
fi
if ! command -v redis-server >/dev/null 2>&1; then
  echo "Reliability process tests require redis-server for private per-test instances." >&2
  exit 1
fi

export PYTHON_DOTENV_DISABLED=1
export SECRET_KEY=isolated-postgres-reliability-tests
export SCHEDULER_ENABLED=0
export DATABASE_URL=sqlite:////tmp/twinevia-reliability-placeholder.db
export REDIS_URL=redis://127.0.0.1:1/0
export OUTBOX_TEST_REDIS_SERVER="$(command -v redis-server)"
for key in OUTBOX_TEST_POSTGRES_ROOT AUDIT_AOC_DATABASE_ROOT \
  AUDIT_PERMISSION_DATABASE_ROOT AUDIT_REVIEW_DATABASE_ROOT AUDIT_UX_DATABASE_ROOT \
  INBOUND_TEST_POSTGRES_ROOT CANCELLATION_TEST_POSTGRES_ROOT REPLY_TEST_POSTGRES_ROOT \
  BILLING_TEST_POSTGRES_ROOT DELIVERY_TEST_POSTGRES_ROOT AUTOMATION_TEST_POSTGRES_ROOT \
  HISTORICAL_TEST_POSTGRES_ROOT AUDIENCE_TEST_POSTGRES_ROOT KEYWORD_TEST_POSTGRES_ROOT \
  FEEDBACK_TEST_POSTGRES_ROOT STAFF_PRIVACY_TEST_POSTGRES_ROOT; do
  export "${key}=${TWINEVIA_TEST_POSTGRES_ROOT}"
done

cd "${REPO_ROOT}"
./run/test.sh -q \
  tests/test_aoc_consent_contract.py tests/test_billing_transitions.py \
  tests/test_delivery_finality.py tests/test_automation_reply_outbox.py \
  tests/test_automation_outbox_rq_recovery.py tests/test_outbound_request_service.py \
  tests/test_outbound_rq_recovery.py tests/test_outbound_review.py \
  tests/test_durable_reply_races.py tests/test_inbound_inactive_consent.py \
  tests/test_scheduled_cancellation_safety.py tests/test_recipient_permissions.py \
  tests/test_ux_recovery.py tests/test_historical_recovery_safety.py tests/test_daily_audiences.py \
  tests/test_event_feedback.py tests/test_feedback_invitations.py tests/test_event_workspace.py tests/test_event_communication_schema.py tests/test_staff_contact_privacy.py \
  tests/test_booking_projection.py tests/test_booking_inventory.py tests/test_app_registration_sync.py \
  tests/test_keyword_legacy_editing.py tests/test_inbox_keyword_origin.py \
  tests/test_inbox_keyword_origin_ui.py tests/test_pr_comment_recovery.py "$@"
