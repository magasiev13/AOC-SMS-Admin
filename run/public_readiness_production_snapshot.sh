#!/usr/bin/env bash
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

resolve_compat_env() {
  local primary_key="$1"
  local legacy_key="$2"
  local default_value="${3:-}"
  local primary_value="${!primary_key:-}"
  local legacy_value="${!legacy_key:-}"

  if [[ -n "${primary_value}" ]]; then
    printf '%s\n' "${primary_value}"
    return
  fi
  if [[ -n "${legacy_value}" ]]; then
    echo "[warn] ${legacy_key} is deprecated; use ${primary_key} instead." >&2
    printf '%s\n' "${legacy_value}"
    return
  fi
  printf '%s\n' "${default_value}"
}

HOST="$(resolve_compat_env "TWINEVIA_PUBLIC_HOST" "BETA_SIGNOFF_HOST" "www.twinevia.com")"
SSH_TARGET="$(resolve_compat_env "TWINEVIA_SSH_TARGET" "BETA_SIGNOFF_SSH_TARGET" "")"
SSH_KEY="$(resolve_compat_env "TWINEVIA_SSH_KEY" "BETA_SIGNOFF_SSH_KEY" "$HOME/.ssh/itlab.key")"
SSH_PORT="$(resolve_compat_env "TWINEVIA_SSH_PORT" "BETA_SIGNOFF_SSH_PORT" "22")"
APP_ROOT="$(resolve_compat_env "TWINEVIA_APP_ROOT" "BETA_SIGNOFF_APP_ROOT" "")"
APP_USER="$(resolve_compat_env "TWINEVIA_APP_USER" "BETA_SIGNOFF_APP_USER" "")"
UNIT_PREFIX="$(resolve_compat_env "TWINEVIA_UNIT_PREFIX" "BETA_SIGNOFF_UNIT_PREFIX" "")"

RUN_ID=""
ORG_SLUG=""
LABEL=""
DAYS_BACK="$(resolve_compat_env "TWINEVIA_ACTIVITY_LOOKBACK_DAYS" "BETA_SIGNOFF_ACTIVITY_LOOKBACK_DAYS" "14")"

usage() {
  cat <<'EOF'
Usage: ./run/public_readiness_production_snapshot.sh --org-slug ORG_SLUG --label LABEL [--run-id RUN_ID] [--days-back DAYS]

Collects read-only production signoff evidence under:
  output/signoff/<run-id>/production/<label>/
EOF
}

sanitize_name() {
  printf '%s' "$1" \
    | tr '[:upper:]' '[:lower:]' \
    | tr -cs 'a-z0-9._-' '-' \
    | sed 's/^-*//; s/-*$//'
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --run-id)
      RUN_ID="${2:-}"
      shift 2
      ;;
    --org-slug)
      ORG_SLUG="${2:-}"
      shift 2
      ;;
    --label)
      LABEL="${2:-}"
      shift 2
      ;;
    --days-back)
      DAYS_BACK="${2:-}"
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

if [[ -z "${ORG_SLUG}" || -z "${LABEL}" ]]; then
  usage >&2
  exit 1
fi

if ! [[ "${DAYS_BACK}" =~ ^[0-9]+$ ]] || [[ "${DAYS_BACK}" -lt 1 ]]; then
  echo "ERROR: --days-back must be a positive integer." >&2
  exit 1
fi

if [[ -z "${SSH_TARGET}" ]]; then
  echo "ERROR: TWINEVIA_SSH_TARGET must be set for remote production snapshots." >&2
  exit 1
fi

if [[ -z "${RUN_ID}" ]]; then
  RUN_ID="$(date +%Y%m%d-%H%M%S)"
fi

SAFE_LABEL="$(sanitize_name "${LABEL}")"
if [[ -z "${SAFE_LABEL}" ]]; then
  SAFE_LABEL="snapshot"
fi

OUT_DIR="${REPO_ROOT}/output/signoff/${RUN_ID}/production/${SAFE_LABEL}"
mkdir -p "${OUT_DIR}"

SSH_OPTS=(
  -i "${SSH_KEY}"
  -p "${SSH_PORT}"
  -o BatchMode=yes
  -o StrictHostKeyChecking=yes
)
REQUIRED_UNITS=(
)

ssh_run() {
  ssh "${SSH_OPTS[@]}" "${SSH_TARGET}" "$@"
}

resolve_remote_app_user() {
  if [[ -n "${APP_USER}" ]]; then
    printf '%s\n' "${APP_USER}"
    return
  fi
  if [[ -n "${APP_ROOT}" ]]; then
    local owner
    owner="$(ssh_run "if [ -d \"${APP_ROOT}\" ]; then stat -c '%U' \"${APP_ROOT}\" 2>/dev/null; fi" || true)"
    if [[ -n "${owner}" && "${owner}" != "root" ]]; then
      printf '%s\n' "${owner}"
      return
    fi
  fi
  ssh_run "if id -u twinevia >/dev/null 2>&1; then printf twinevia; elif id -u smsadmin >/dev/null 2>&1; then printf smsadmin; else printf twinevia; fi"
}

resolve_remote_app_root() {
  if [[ -n "${APP_ROOT}" ]]; then
    printf '%s\n' "${APP_ROOT}"
    return
  fi
  ssh_run "
    active_root=\"\$(systemctl show twinevia-saas.service -p WorkingDirectory --value 2>/dev/null || true)\"
    if [ -n \"\${active_root}\" ] && [ -d \"\${active_root}/.git\" ]; then
      printf '%s' \"\${active_root}\"
    elif [ -d /opt/twinevia-saas/.git ]; then
      printf /opt/twinevia-saas
    elif [ -d /opt/sms-saas/.git ]; then
      printf /opt/sms-saas
    else
      printf /opt/twinevia-saas
    fi
  "
}

resolve_remote_unit_prefix() {
  if [[ -n "${UNIT_PREFIX}" ]]; then
    printf '%s\n' "${UNIT_PREFIX}"
    return
  fi
  ssh_run "if systemctl list-unit-files twinevia-saas.service --no-legend | grep -q '^twinevia-saas.service[[:space:]]'; then printf twinevia-saas; elif systemctl list-unit-files sms-saas.service --no-legend | grep -q '^sms-saas.service[[:space:]]'; then printf sms-saas; else printf twinevia-saas; fi"
}

capture_remote_file() {
  local output_name="$1"
  shift
  ssh_run "$@" > "${OUT_DIR}/${output_name}" 2>&1
}

command_failures=0
APP_ROOT="$(resolve_remote_app_root)"
APP_USER="$(resolve_remote_app_user)"
UNIT_PREFIX="$(resolve_remote_unit_prefix)"
REQUIRED_UNITS=(
  "${UNIT_PREFIX}"
  "${UNIT_PREFIX}-worker"
  "${UNIT_PREFIX}-scheduler.timer"
  "${UNIT_PREFIX}-billing-reconcile.timer"
  "${UNIT_PREFIX}-platform-restart-queue.timer"
  "${UNIT_PREFIX}-a2p-reconcile.timer"
)

printf '%s\n' "${APP_ROOT}" > "${OUT_DIR}/app_root.txt"
printf '%s\n' "${APP_USER}" > "${OUT_DIR}/app_user.txt"
printf '%s\n' "${UNIT_PREFIX}" > "${OUT_DIR}/unit_prefix.txt"

commit_sha="unavailable"
if commit_sha="$(ssh_run "sudo -u ${APP_USER} git -C ${APP_ROOT} rev-parse HEAD")"; then
  printf '%s\n' "${commit_sha}" > "${OUT_DIR}/live_commit.txt"
else
  printf '%s\n' "${commit_sha}" > "${OUT_DIR}/live_commit.txt"
  command_failures=1
fi

live_branch="unavailable"
if live_branch="$(ssh_run "sudo -u ${APP_USER} git -C ${APP_ROOT} rev-parse --abbrev-ref HEAD")"; then
  printf '%s\n' "${live_branch}" > "${OUT_DIR}/live_branch.txt"
else
  printf '%s\n' "${live_branch}" > "${OUT_DIR}/live_branch.txt"
  command_failures=1
fi

tracking_branch="unavailable"
if tracking_branch="$(ssh_run "sudo -u ${APP_USER} git -C ${APP_ROOT} rev-parse --abbrev-ref --symbolic-full-name '@{u}' 2>/dev/null || true")"; then
  printf '%s\n' "${tracking_branch}" > "${OUT_DIR}/live_tracking_branch.txt"
else
  printf '%s\n' "${tracking_branch}" > "${OUT_DIR}/live_tracking_branch.txt"
  command_failures=1
fi

health_code="$(curl -sS -D "${OUT_DIR}/health.headers" -o "${OUT_DIR}/health.body" -w '%{http_code}' "https://${HOST}/health" || true)"
printf '%s\n' "${health_code}" > "${OUT_DIR}/health.status"

if ! capture_remote_file saas_dbdoctor.txt \
  "sudo -u ${APP_USER} bash -lc 'cd ${APP_ROOT} && set -a && source .env && set +a && if [ -x /usr/local/bin/twinevia-saas-dbdoctor ]; then TWINEVIA_SAAS_APP_ROOT=\"${APP_ROOT}\" TWINEVIA_SAAS_PYTHON=\"${APP_ROOT}/venv/bin/python\" /usr/local/bin/twinevia-saas-dbdoctor --doctor; else TWINEVIA_SAAS_APP_ROOT=\"${APP_ROOT}\" TWINEVIA_SAAS_PYTHON=\"${APP_ROOT}/venv/bin/python\" /usr/local/bin/saas-dbdoctor --doctor; fi'"; then
  command_failures=1
fi

if ! capture_remote_file services.status.txt \
  "sudo systemctl status ${REQUIRED_UNITS[*]} --no-pager"; then
  command_failures=1
fi

service_activity="$(ssh_run "sudo bash -lc 'for unit in ${REQUIRED_UNITS[*]}; do printf \"%s %s\\n\" \"\$unit\" \"\$(systemctl is-active \"\$unit\" 2>/dev/null || echo missing)\"; done'" || true)"
printf '%s\n' "${service_activity}" > "${OUT_DIR}/services.activity.txt"
if [[ -z "${service_activity}" ]]; then
  command_failures=1
fi

if ! capture_remote_file worker.log.txt \
  "sudo journalctl -u ${UNIT_PREFIX}-worker -n 120 --no-pager"; then
  command_failures=1
fi

if ! capture_remote_file org_state.json \
  "sudo -u ${APP_USER} bash -lc 'cd ${APP_ROOT} && set -a && source .env && set +a && ORG_SLUG=\"${ORG_SLUG}\" ./venv/bin/python - <<'\"'\"'PY'\"'\"'
import json
import os

from app import create_app
from app.models import Organization

app = create_app(run_startup_tasks=False, start_scheduler=False)
slug = os.environ[\"ORG_SLUG\"]

with app.app_context():
    organization = Organization.query.filter_by(slug=slug).first()
    if organization is None:
        print(json.dumps({
            \"organization_slug\": slug,
            \"missing\": True,
            \"error\": f\"Organization not found: {slug}\",
        }, indent=2, sort_keys=True))
        raise SystemExit(0)

    membership_rows = [
        {
            \"email\": membership.user.email if membership.user else None,
            \"role\": membership.role,
            \"user_id\": membership.user_id,
        }
        for membership in organization.memberships
    ]
    invitation_rows = [
        {
            \"accepted_at\": invitation.accepted_at,
            \"email\": invitation.email,
            \"expires_at\": invitation.expires_at,
            \"role\": invitation.role,
            \"status\": invitation.status,
            \"token\": invitation.token,
        }
        for invitation in organization.invitations
    ]

    subscription = organization.subscription
    messaging = organization.messaging_profile
    onboarding = organization.a2p_onboarding
    payload = {
        \"organization\": {
            \"id\": organization.id,
            \"name\": organization.name,
            \"slug\": organization.slug,
            \"status\": organization.status,
        },
        \"subscription\": {
            \"status\": subscription.status if subscription else None,
            \"stripe_customer_id\": subscription.stripe_customer_id if subscription else None,
            \"stripe_subscription_id\": subscription.stripe_subscription_id if subscription else None,
            \"current_period_end\": subscription.current_period_end if subscription else None,
        },
        \"messaging\": {
            \"provider_status\": messaging.provider_status if messaging else None,
            \"status\": messaging.status if messaging else None,
            \"twilio_subaccount_sid\": messaging.twilio_subaccount_sid if messaging else None,
            \"messaging_service_sid\": messaging.messaging_service_sid if messaging else None,
            \"phone_number_sid\": messaging.phone_number_sid if messaging else None,
            \"from_number\": messaging.from_number if messaging else None,
            \"sender_review_status\": messaging.sender_review_status if messaging else None,
            \"can_send\": messaging.can_send if messaging else False,
            \"last_provision_error\": messaging.last_provision_error if messaging else None,
        },
        \"a2p_onboarding\": {
            \"onboarding_status\": onboarding.onboarding_status if onboarding else None,
            \"brand_status\": onboarding.brand_status if onboarding else None,
            \"campaign_status\": onboarding.campaign_status if onboarding else None,
            \"verification_status\": onboarding.verification_status if onboarding else None,
            \"submitted_at\": onboarding.submitted_at if onboarding else None,
            \"last_synced_at\": onboarding.last_synced_at if onboarding else None,
            \"last_error\": onboarding.last_error if onboarding else None,
        },
        \"memberships\": membership_rows,
        \"pending_invitations\": invitation_rows,
    }
    print(json.dumps(payload, indent=2, sort_keys=True, default=str))
PY'"; then
  command_failures=1
fi

if ! capture_remote_file twilio_ownership.json \
  "sudo -u ${APP_USER} bash -lc 'cd ${APP_ROOT} && set -a && source .env && set +a && ORG_SLUG=\"${ORG_SLUG}\" ./venv/bin/python - <<'\"'\"'PY'\"'\"'
import json
import os

from app import create_app
from app.models import Organization


def _normalized_sid(value):
    return (value or \"\").strip().lower() or None

app = create_app(run_startup_tasks=False, start_scheduler=False)
slug = os.environ[\"ORG_SLUG\"]

with app.app_context():
    organization = Organization.query.filter_by(slug=slug).first()
    if organization is None:
        print(json.dumps({
            \"organization_slug\": slug,
            \"missing\": True,
            \"error\": f\"Organization not found: {slug}\",
        }, indent=2, sort_keys=True))
        raise SystemExit(0)

    profile = organization.messaging_profile
    provider_mode = profile.provider_mode if profile else None
    ownership_account_sid = None
    result = {
        \"organization_slug\": slug,
        \"provider_mode\": provider_mode,
        \"twilio_account_sid\": profile.twilio_account_sid if profile else None,
        \"twilio_subaccount_sid\": profile.twilio_subaccount_sid if profile else None,
        \"messaging_service_sid\": profile.messaging_service_sid if profile else None,
        \"phone_number_sid\": profile.phone_number_sid if profile else None,
    }

    if profile is None:
        result[\"skipped\"] = \"no_messaging_profile\"
        print(json.dumps(result, indent=2, sort_keys=True))
        raise SystemExit(0)

    if provider_mode == \"customer_managed\" and profile.twilio_account_sid:
        from app.services.twilio_service import _client_for_profile

        client = _client_for_profile(profile)
        ownership_account_sid = profile.twilio_account_sid
    elif profile.twilio_subaccount_sid:
        from app.services.twilio_service import _build_subaccount_client

        client = _build_subaccount_client(profile)
        ownership_account_sid = profile.twilio_subaccount_sid
    else:
        result[\"skipped\"] = \"no_twilio_read_account_sid\"
        print(json.dumps(result, indent=2, sort_keys=True))
        raise SystemExit(0)

    try:
        if profile.phone_number_sid:
            phone_number = client.incoming_phone_numbers(profile.phone_number_sid).fetch()
            result[\"phone_number\"] = phone_number.phone_number
            result[\"phone_number_account_sid\"] = phone_number.account_sid
        else:
            result[\"phone_number\"] = None
            result[\"phone_number_account_sid\"] = None

        if profile.messaging_service_sid:
            messaging_service = client.messaging.v1.services(profile.messaging_service_sid).fetch()
            result[\"messaging_service_account_sid\"] = getattr(messaging_service, \"account_sid\", None)
        else:
            result[\"messaging_service_account_sid\"] = None

        result[\"twilio_read_account_sid\"] = ownership_account_sid
        result[\"ownership_matches_subaccount\"] = (
            (_normalized_sid(result[\"phone_number_account_sid\"]) in {None, _normalized_sid(ownership_account_sid)})
            and (_normalized_sid(result[\"messaging_service_account_sid\"]) in {None, _normalized_sid(ownership_account_sid)})
        )
    except Exception as exc:
        result[\"error\"] = str(exc)

    print(json.dumps(result, indent=2, sort_keys=True, default=str))
PY'"; then
  command_failures=1
fi

if ! capture_remote_file org_messaging_audit.json \
  "sudo -u ${APP_USER} bash -lc 'cd ${APP_ROOT} && set -a && source .env && set +a && ORG_SLUG=\"${ORG_SLUG}\" DAYS_BACK=\"${DAYS_BACK}\" ./venv/bin/python - <<'\"'\"'PY'\"'\"'
import json
import os
from datetime import datetime, timedelta, timezone

from sqlalchemy import func

from app import create_app
from app.models import AuthEvent, InboxMessage, MessageLog, MessagingUsageRecord, Organization, ScheduledMessage

app = create_app(run_startup_tasks=False, start_scheduler=False)
slug = os.environ[\"ORG_SLUG\"]
days_back = int(os.environ[\"DAYS_BACK\"])
window_end = datetime.now(timezone.utc)
window_start = window_end - timedelta(days=days_back)
window_start_naive = window_start.replace(tzinfo=None)


def _message_log_sids(raw_details):
    try:
        payload = json.loads(raw_details or \"[]\")
    except json.JSONDecodeError:
        return []
    if isinstance(payload, dict):
        payload = payload.get(\"details\") or payload.get(\"results\") or []
    if not isinstance(payload, list):
        return []
    seen = []
    for item in payload:
        if not isinstance(item, dict):
            continue
        sid = str(item.get(\"sid\") or \"\").strip()
        if sid and sid not in seen:
            seen.append(sid)
    return seen

with app.app_context():
    organization = Organization.query.filter_by(slug=slug).first()
    if organization is None:
        print(json.dumps({
            \"organization_slug\": slug,
            \"missing\": True,
            \"error\": f\"Organization not found: {slug}\",
        }, indent=2, sort_keys=True))
        raise SystemExit(0)

    user_ids = [membership.user_id for membership in organization.memberships]
    message_logs = (
        MessageLog.query
        .filter(MessageLog.organization_id == organization.id)
        .filter(MessageLog.created_at >= window_start_naive)
        .order_by(MessageLog.created_at.desc(), MessageLog.id.desc())
        .all()
    )
    scheduled_messages = (
        ScheduledMessage.query
        .filter(ScheduledMessage.organization_id == organization.id)
        .filter(ScheduledMessage.created_at >= window_start_naive)
        .order_by(ScheduledMessage.created_at.desc(), ScheduledMessage.id.desc())
        .all()
    )
    outbound_inbox_messages = (
        InboxMessage.query
        .filter(InboxMessage.organization_id == organization.id)
        .filter(InboxMessage.direction == \"outbound\")
        .filter(InboxMessage.created_at >= window_start_naive)
        .order_by(InboxMessage.created_at.desc(), InboxMessage.id.desc())
        .all()
    )
    usage_rows = (
        MessagingUsageRecord.query
        .filter(MessagingUsageRecord.organization_id == organization.id)
        .filter(MessagingUsageRecord.created_at >= window_start_naive)
        .order_by(MessagingUsageRecord.created_at.desc(), MessagingUsageRecord.id.desc())
        .all()
    )
    usage_by_source = (
        MessagingUsageRecord.query
        .with_entities(
            MessagingUsageRecord.source,
            func.count(MessagingUsageRecord.id),
            func.coalesce(func.sum(MessagingUsageRecord.billable_units), 0),
            func.coalesce(func.sum(MessagingUsageRecord.provider_cost), 0),
            func.coalesce(func.sum(MessagingUsageRecord.sell_amount), 0),
        )
        .filter(MessagingUsageRecord.organization_id == organization.id)
        .filter(MessagingUsageRecord.created_at >= window_start_naive)
        .group_by(MessagingUsageRecord.source)
        .order_by(MessagingUsageRecord.source.asc())
        .all()
    )
    auth_events = []
    if user_ids:
        auth_events = (
            AuthEvent.query
            .filter(AuthEvent.user_id.in_(user_ids))
            .filter(AuthEvent.created_at >= window_start_naive)
            .order_by(AuthEvent.created_at.desc(), AuthEvent.id.desc())
            .all()
        )

    message_log_sids = sorted(
        {
            sid
            for log in message_logs
            for sid in _message_log_sids(log.details)
            if sid
        }
    )

    payload = {
        \"organization\": {
            \"id\": organization.id,
            \"slug\": organization.slug,
            \"name\": organization.name,
        },
        \"window\": {
            \"days_back\": days_back,
            \"start\": window_start.isoformat(),
            \"end\": window_end.isoformat(),
        },
        \"message_logs_summary\": {
            \"count\": len(message_logs),
            \"total_recipients\": sum(int(log.total_recipients or 0) for log in message_logs),
            \"total_success\": sum(int(log.success_count or 0) for log in message_logs),
            \"total_failure\": sum(int(log.failure_count or 0) for log in message_logs),
        },
        \"message_logs\": [
            {
                \"id\": log.id,
                \"created_at\": log.created_at,
                \"target\": log.target,
                \"event_id\": log.event_id,
                \"status\": log.status,
                \"test_mode\": bool(log.test_mode),
                \"total_recipients\": log.total_recipients,
                \"success_count\": log.success_count,
                \"failure_count\": log.failure_count,
                \"detail_sids\": _message_log_sids(log.details),
            }
            for log in message_logs
        ],
        \"message_log_sids\": message_log_sids,
        \"scheduled_messages\": [
            {
                \"id\": message.id,
                \"created_at\": message.created_at,
                \"scheduled_at\": message.scheduled_at,
                \"target\": message.target,
                \"event_id\": message.event_id,
                \"status\": message.status,
                \"test_mode\": bool(message.test_mode),
                \"attempt_count\": message.attempt_count,
                \"message_log_id\": message.message_log_id,
                \"error_message\": message.error_message,
            }
            for message in scheduled_messages
        ],
        \"outbound_inbox_messages\": [
            {
                \"id\": message.id,
                \"created_at\": message.created_at,
                \"thread_id\": message.thread_id,
                \"phone\": message.phone,
                \"automation_source\": message.automation_source,
                \"message_sid\": message.message_sid,
                \"delivery_status\": message.delivery_status,
                \"delivery_error\": message.delivery_error,
            }
            for message in outbound_inbox_messages
        ],
        \"messaging_usage_by_source\": [
            {
                \"source\": row[0],
                \"count\": int(row[1] or 0),
                \"billable_units\": int(row[2] or 0),
                \"provider_cost\": row[3],
                \"sell_amount\": row[4],
            }
            for row in usage_by_source
        ],
        \"messaging_usage_records\": [
            {
                \"id\": row.id,
                \"created_at\": row.created_at,
                \"message_sid\": row.message_sid,
                \"source\": row.source,
                \"twilio_subaccount_sid\": row.twilio_subaccount_sid,
                \"twilio_message_status\": row.twilio_message_status,
                \"billable_units\": row.billable_units,
                \"provider_cost\": row.provider_cost,
                \"sell_amount\": row.sell_amount,
                \"reconciliation_status\": row.reconciliation_status,
            }
            for row in usage_rows
        ],
        \"auth_events\": [
            {
                \"id\": event.id,
                \"created_at\": event.created_at,
                \"event_type\": event.event_type,
                \"outcome\": event.outcome,
                \"user_id\": event.user_id,
                \"username\": event.username,
                \"metadata\": event.metadata_payload,
            }
            for event in auth_events
        ],
    }
    print(json.dumps(payload, indent=2, sort_keys=True, default=str))
PY'"; then
  command_failures=1
fi

if ! capture_remote_file twilio_recent_messages.json \
  "sudo -u ${APP_USER} bash -lc 'cd ${APP_ROOT} && set -a && source .env && set +a && ORG_SLUG=\"${ORG_SLUG}\" DAYS_BACK=\"${DAYS_BACK}\" ./venv/bin/python - <<'\"'\"'PY'\"'\"'
import json
import os
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation

from app import create_app
from app.models import Organization


def _as_utc(value):
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _absolute_decimal(value):
    try:
        if value in {None, \"\"}:
            return Decimal(\"0\")
        return abs(Decimal(str(value)))
    except (InvalidOperation, TypeError, ValueError):
        return Decimal(\"0\")


app = create_app(run_startup_tasks=False, start_scheduler=False)
slug = os.environ[\"ORG_SLUG\"]
days_back = int(os.environ[\"DAYS_BACK\"])
window_end = datetime.now(timezone.utc)
window_start = window_end - timedelta(days=days_back)
message_limit = 2000

with app.app_context():
    organization = Organization.query.filter_by(slug=slug).first()
    if organization is None:
        print(json.dumps({
            \"organization_slug\": slug,
            \"missing\": True,
            \"error\": f\"Organization not found: {slug}\",
        }, indent=2, sort_keys=True))
        raise SystemExit(0)

    profile = organization.messaging_profile
    provider_mode = profile.provider_mode if profile else None
    read_account_sid = None
    result = {
        \"organization_slug\": slug,
        \"window\": {
            \"days_back\": days_back,
            \"start\": window_start.isoformat(),
            \"end\": window_end.isoformat(),
        },
        \"provider_mode\": provider_mode,
        \"twilio_account_sid\": profile.twilio_account_sid if profile else None,
        \"twilio_subaccount_sid\": profile.twilio_subaccount_sid if profile else None,
        \"messaging_service_sid\": profile.messaging_service_sid if profile else None,
        \"sender_number\": profile.from_number if profile else None,
        \"message_limit\": message_limit,
    }

    if profile is None:
        result[\"skipped\"] = \"no_messaging_profile\"
        print(json.dumps(result, indent=2, sort_keys=True))
        raise SystemExit(0)

    try:
        if provider_mode == \"customer_managed\" and profile.twilio_account_sid:
            from app.services.twilio_service import _client_for_profile

            client = _client_for_profile(profile)
            read_account_sid = profile.twilio_account_sid
        elif profile.twilio_subaccount_sid:
            from app.services.twilio_service import _build_subaccount_client

            client = _build_subaccount_client(profile)
            read_account_sid = profile.twilio_subaccount_sid
        else:
            result[\"skipped\"] = \"no_twilio_read_account_sid\"
            print(json.dumps(result, indent=2, sort_keys=True))
            raise SystemExit(0)

        raw_messages = client.messages.list(limit=message_limit)
        filtered_messages = []
        grouped = defaultdict(lambda: {\"count\": 0, \"segments\": 0, \"provider_cost\": Decimal(\"0\")})

        for message in raw_messages:
            created_at = _as_utc(getattr(message, \"date_created\", None) or getattr(message, \"date_sent\", None))
            if created_at is not None and created_at < window_start:
                continue
            direction = (getattr(message, \"direction\", None) or \"\").strip().lower()
            if direction and not direction.startswith(\"outbound\"):
                continue

            phone = getattr(message, \"to\", None)
            try:
                segments = max(0, int(getattr(message, \"num_segments\", None) or 0))
            except (TypeError, ValueError):
                segments = 0
            provider_cost = _absolute_decimal(getattr(message, \"price\", None))

            filtered_messages.append(
                {
                    \"sid\": getattr(message, \"sid\", None),
                    \"to\": phone,
                    \"from\": getattr(message, \"from_\", None),
                    \"status\": getattr(message, \"status\", None),
                    \"direction\": direction or None,
                    \"num_segments\": segments,
                    \"price\": str(provider_cost),
                    \"price_unit\": getattr(message, \"price_unit\", None),
                    \"account_sid\": getattr(message, \"account_sid\", None),
                    \"messaging_service_sid\": getattr(message, \"messaging_service_sid\", None),
                    \"date_created\": created_at.isoformat() if created_at is not None else None,
                    \"error_code\": getattr(message, \"error_code\", None),
                }
            )
            if phone:
                grouped_entry = grouped[phone]
                grouped_entry[\"count\"] += 1
                grouped_entry[\"segments\"] += segments
                grouped_entry[\"provider_cost\"] += provider_cost

        result[\"messages\"] = filtered_messages
        result[\"summary\"] = {
            \"total_messages\": len(filtered_messages),
            \"unique_destinations\": len(grouped),
            \"truncated_to_limit\": len(raw_messages) >= message_limit,
        }
        result[\"twilio_read_account_sid\"] = read_account_sid
        result[\"by_destination\"] = [
            {
                \"to\": phone,
                \"count\": values[\"count\"],
                \"segments\": values[\"segments\"],
                \"provider_cost\": str(values[\"provider_cost\"]),
            }
            for phone, values in sorted(grouped.items(), key=lambda item: (-item[1][\"count\"], item[0] or \"\"))
        ]
    except Exception as exc:
        result[\"error\"] = str(exc)

    print(json.dumps(result, indent=2, sort_keys=True, default=str))
PY'"; then
  command_failures=1
fi

health_failed=0
if [[ "${health_code}" != "200" ]]; then
  health_failed=1
fi

services_failed=0
while read -r unit state; do
  if [[ "${state}" != "active" ]]; then
    services_failed=1
  fi
done <<< "${service_activity}"

org_state_failed=0
if ! /usr/bin/python3 - "${OUT_DIR}/org_state.json" <<'PY'; then
import json
import sys

path = sys.argv[1]
with open(path, "r", encoding="utf-8") as handle:
    payload = json.load(handle)

if payload.get("missing"):
    raise SystemExit(1)

if payload.get("error"):
    raise SystemExit(1)
PY
  org_state_failed=1
fi

ownership_failed=0
if ! /usr/bin/python3 - "${OUT_DIR}/twilio_ownership.json" <<'PY'; then
import json
import sys

path = sys.argv[1]
with open(path, "r", encoding="utf-8") as handle:
    payload = json.load(handle)

if payload.get("skipped"):
    raise SystemExit(0)

if payload.get("error"):
    raise SystemExit(1)

if payload.get("ownership_matches_subaccount") is False:
    raise SystemExit(1)
PY
  ownership_failed=1
fi

if ! /usr/bin/python3 - \
  "${OUT_DIR}" \
  "${HOST}" \
  "${LABEL}" \
  "${ORG_SLUG}" \
  "${DAYS_BACK}" \
  "${APP_ROOT}" \
  "${APP_USER}" \
  "${UNIT_PREFIX}" \
  "${commit_sha}" \
  "${health_code}" <<'PY'; then
import json
import sys
from collections import Counter
from pathlib import Path

out_dir = Path(sys.argv[1])
host = sys.argv[2]
label = sys.argv[3]
org_slug = sys.argv[4]
days_back = sys.argv[5]
app_root = sys.argv[6]
app_user = sys.argv[7]
unit_prefix = sys.argv[8]
commit_sha = sys.argv[9]
health_code = sys.argv[10]


def _load_artifact(name):
    path = out_dir / name
    if not path.exists():
        return {"_artifact_error": f"missing artifact: {name}"}
    try:
        with path.open("r", encoding="utf-8") as handle:
            return json.load(handle)
    except Exception as exc:  # noqa: BLE001
        return {"_artifact_error": str(exc)}


def _normalized_sid(value):
    return (value or "").strip().lower()


ownership = _load_artifact("twilio_ownership.json")
audit = _load_artifact("org_messaging_audit.json")
messages = _load_artifact("twilio_recent_messages.json")

known_sids = set()
if isinstance(audit, dict):
    for sid in audit.get("message_log_sids", []):
        normalized = _normalized_sid(sid)
        if normalized:
            known_sids.add(normalized)
    for row in audit.get("messaging_usage_records", []):
        normalized = _normalized_sid(row.get("message_sid"))
        if normalized:
            known_sids.add(normalized)
    for row in audit.get("outbound_inbox_messages", []):
        normalized = _normalized_sid(row.get("message_sid"))
        if normalized:
            known_sids.add(normalized)

twilio_messages = messages.get("messages", []) if isinstance(messages, dict) else []
out_of_band_messages = []
for message in twilio_messages:
    if not isinstance(message, dict):
        continue
    normalized_sid = _normalized_sid(message.get("sid"))
    if normalized_sid and normalized_sid in known_sids:
        continue
    out_of_band_messages.append(message)

out_of_band_by_destination = Counter(
    (message.get("to") or "unknown")
    for message in out_of_band_messages
)
out_of_band_payload = {
    "count": len(out_of_band_messages),
    "by_destination": [
        {"to": destination, "count": count}
        for destination, count in out_of_band_by_destination.most_common()
    ],
    "messages": out_of_band_messages,
}
with (out_dir / "twilio_out_of_band.json").open("w", encoding="utf-8") as handle:
    json.dump(out_of_band_payload, handle, indent=2, sort_keys=True)

summary_lines = [
    "# Production Snapshot",
    "",
    f"- Host: {host}",
    f"- Label: {label}",
    f"- Org slug: {org_slug}",
    f"- Audit window: last {days_back} day(s)",
    f"- App root: {app_root}",
    f"- App user: {app_user}",
    f"- Unit prefix: {unit_prefix}",
    f"- Live commit: {commit_sha}",
    f"- Health HTTP status: {health_code}",
    "- Required unit activity: see `services.activity.txt`",
    "- Doctor output: `saas_dbdoctor.txt`",
    "- Worker log: `worker.log.txt`",
    "- Org state: `org_state.json`",
    "- Twilio ownership: `twilio_ownership.json`",
    "- Org messaging audit: `org_messaging_audit.json`",
    "- Recent Twilio messages: `twilio_recent_messages.json`",
    "- Out-of-band Twilio messages: `twilio_out_of_band.json`",
]

if isinstance(ownership, dict):
    if ownership.get("_artifact_error"):
        summary_lines.append(f"- Ownership artifact note: {ownership['_artifact_error']}")
    elif ownership.get("error"):
        summary_lines.append(f"- Twilio ownership status: error ({ownership['error']})")
    elif ownership.get("skipped"):
        summary_lines.append(f"- Twilio ownership status: skipped ({ownership['skipped']})")
    else:
        summary_lines.append(
            "- Twilio ownership matches expected account: "
            f"{ownership.get('ownership_matches_subaccount')}"
        )

if isinstance(messages, dict):
    if messages.get("_artifact_error"):
        summary_lines.append(f"- Recent-message artifact note: {messages['_artifact_error']}")
    elif messages.get("error"):
        summary_lines.append(f"- Twilio message read status: error ({messages['error']})")
    elif messages.get("skipped"):
        summary_lines.append(f"- Twilio message read status: skipped ({messages['skipped']})")
    else:
        summary_lines.append(
            "- Twilio outbound messages in window: "
            f"{messages.get('summary', {}).get('total_messages', 'n/a')}"
        )
        summary_lines.append(
            f"- Out-of-band outbound messages in window: {len(out_of_band_messages)}"
        )
        if out_of_band_by_destination:
            top_destinations = ", ".join(
                f"{destination} ({count})"
                for destination, count in out_of_band_by_destination.most_common(5)
            )
            summary_lines.append(f"- Top out-of-band destinations: {top_destinations}")

if isinstance(audit, dict):
    if audit.get("_artifact_error"):
        summary_lines.append(f"- Org audit artifact note: {audit['_artifact_error']}")
    elif audit.get("error"):
        summary_lines.append(f"- Org audit status: error ({audit['error']})")
    else:
        summary_lines.append(
            "- App-linked outbound usage rows: "
            f"{len(audit.get('messaging_usage_records', []))}"
        )
        sources = audit.get("messaging_usage_by_source", [])
        if sources:
            summary_lines.append(
                "- Usage by source: "
                + ", ".join(
                    f"{row.get('source')}={row.get('count')}"
                    for row in sources
                    if isinstance(row, dict)
                )
            )

with (out_dir / "summary.md").open("w", encoding="utf-8") as handle:
    handle.write("\n".join(summary_lines) + "\n")
PY
  command_failures=1
fi

if [[ "${command_failures}" -ne 0 || "${health_failed}" -ne 0 || "${services_failed}" -ne 0 || "${org_state_failed}" -ne 0 || "${ownership_failed}" -ne 0 ]]; then
  echo "Production snapshot captured, but one or more release checks failed. See ${OUT_DIR}" >&2
  exit 1
fi

echo "Production snapshot written to ${OUT_DIR}"
