#!/usr/bin/env bash
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
HOST="${BETA_SIGNOFF_HOST:-beta.theitwingman.com}"
SSH_TARGET="${BETA_SIGNOFF_SSH_TARGET:-ubuntu@beta.theitwingman.com}"
SSH_KEY="${BETA_SIGNOFF_SSH_KEY:-$HOME/.ssh/itlab.key}"
APP_ROOT="${BETA_SIGNOFF_APP_ROOT:-/opt/sms-saas}"
APP_USER="${BETA_SIGNOFF_APP_USER:-smsadmin}"

RUN_ID=""
ORG_SLUG=""
LABEL=""

usage() {
  cat <<'EOF'
Usage: ./run/public_readiness_beta_snapshot.sh --org-slug ORG_SLUG --label LABEL [--run-id RUN_ID]

Collects read-only beta signoff evidence under:
  output/signoff/<run-id>/beta/<label>/
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

if [[ -z "${RUN_ID}" ]]; then
  RUN_ID="$(date +%Y%m%d-%H%M%S)"
fi

SAFE_LABEL="$(sanitize_name "${LABEL}")"
if [[ -z "${SAFE_LABEL}" ]]; then
  SAFE_LABEL="snapshot"
fi

OUT_DIR="${REPO_ROOT}/output/signoff/${RUN_ID}/beta/${SAFE_LABEL}"
mkdir -p "${OUT_DIR}"

SSH_OPTS=(
  -i "${SSH_KEY}"
  -o BatchMode=yes
  -o StrictHostKeyChecking=yes
)
REQUIRED_UNITS=(
  "sms-saas"
  "sms-saas-worker"
  "sms-saas-scheduler.timer"
  "sms-saas-billing-reconcile.timer"
  "sms-saas-a2p-reconcile.timer"
)

ssh_run() {
  ssh "${SSH_OPTS[@]}" "${SSH_TARGET}" "$@"
}

capture_remote_file() {
  local output_name="$1"
  shift
  ssh_run "$@" > "${OUT_DIR}/${output_name}" 2>&1
}

command_failures=0

commit_sha="unavailable"
if commit_sha="$(ssh_run "sudo -u ${APP_USER} git -C ${APP_ROOT} rev-parse HEAD")"; then
  printf '%s\n' "${commit_sha}" > "${OUT_DIR}/live_commit.txt"
else
  printf '%s\n' "${commit_sha}" > "${OUT_DIR}/live_commit.txt"
  command_failures=1
fi

health_code="$(curl -sS -D "${OUT_DIR}/health.headers" -o "${OUT_DIR}/health.body" -w '%{http_code}' "https://${HOST}/health" || true)"
printf '%s\n' "${health_code}" > "${OUT_DIR}/health.status"

if ! capture_remote_file saas_dbdoctor.txt \
  "sudo -u ${APP_USER} bash -lc 'cd ${APP_ROOT} && set -a && source .env && set +a && /usr/local/bin/saas-dbdoctor --doctor'"; then
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
  "sudo journalctl -u sms-saas-worker -n 120 --no-pager"; then
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
    result = {
        \"organization_slug\": slug,
        \"twilio_subaccount_sid\": profile.twilio_subaccount_sid if profile else None,
        \"messaging_service_sid\": profile.messaging_service_sid if profile else None,
        \"phone_number_sid\": profile.phone_number_sid if profile else None,
    }

    if profile is None or not profile.twilio_subaccount_sid:
        result[\"skipped\"] = \"no_twilio_subaccount_sid\"
        print(json.dumps(result, indent=2, sort_keys=True))
        raise SystemExit(0)

    try:
        from app.services.twilio_service import _build_subaccount_client

        client = _build_subaccount_client(profile)

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

        result[\"ownership_matches_subaccount\"] = (
            (result[\"phone_number_account_sid\"] in {None, profile.twilio_subaccount_sid})
            and (result[\"messaging_service_account_sid\"] in {None, profile.twilio_subaccount_sid})
        )
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

cat > "${OUT_DIR}/summary.md" <<EOF
# Beta Snapshot

- Host: ${HOST}
- Label: ${LABEL}
- Org slug: ${ORG_SLUG}
- Live commit: ${commit_sha}
- Health HTTP status: ${health_code}
- Required unit activity: see \`services.activity.txt\`
- Doctor output: \`saas_dbdoctor.txt\`
- Worker log: \`worker.log.txt\`
- Org state: \`org_state.json\`
- Twilio ownership: \`twilio_ownership.json\`
EOF

if [[ "${command_failures}" -ne 0 || "${health_failed}" -ne 0 || "${services_failed}" -ne 0 || "${org_state_failed}" -ne 0 || "${ownership_failed}" -ne 0 ]]; then
  echo "Beta snapshot captured, but one or more release checks failed. See ${OUT_DIR}" >&2
  exit 1
fi

echo "Beta snapshot written to ${OUT_DIR}"
