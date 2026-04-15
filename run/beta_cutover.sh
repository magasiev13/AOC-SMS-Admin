#!/usr/bin/env bash
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
HOST="${BETA_SIGNOFF_HOST:-beta.theitwingman.com}"
SSH_TARGET="${BETA_SIGNOFF_SSH_TARGET:-ubuntu@beta.theitwingman.com}"
SSH_KEY="${BETA_SIGNOFF_SSH_KEY:-$HOME/.ssh/itlab.key}"
SSH_PORT="${BETA_SIGNOFF_SSH_PORT:-22}"
APP_ROOT="${BETA_SIGNOFF_APP_ROOT:-/opt/twinevia-saas}"
APP_USER="${BETA_SIGNOFF_APP_USER:-}"
PROXY_CONFIG_PATH="${BETA_CUTOVER_PROXY_CONFIG:-}"
DEFAULT_BRANCH="$(git -C "${REPO_ROOT}" rev-parse --abbrev-ref HEAD 2>/dev/null || printf 'codex/saas-pilot-v2')"
DEPLOY_BRANCH="${BETA_DEPLOY_BRANCH:-${DEFAULT_BRANCH}}"
DEPLOY_TRACKING="${BETA_DEPLOY_TRACKING:-}"
RUN_ID=""
ORG_SLUG=""
FREEZE_NOTE=""
RUN_LOCAL_GATE=1
RUN_DEPLOY=0

usage() {
  cat <<'EOF'
Usage: ./run/beta_cutover.sh --org-slug ORG_SLUG [options]

Orchestrates the safe beta cutover workflow for the existing Twinevia SaaS host:
  1. optional local public-readiness gate
  2. pre-deploy read-only beta snapshot
  3. remote PostgreSQL/Redis/config backup bundle
  4. optional in-place deploy against the existing beta checkout
  5. post-deploy read-only beta snapshot

Options:
  --org-slug ORG_SLUG     Organization slug used for pre/post snapshot parity.
  --run-id RUN_ID         Artifact run id. Defaults to timestamp.
  --deploy-branch BRANCH  Expected beta checkout branch. Defaults to current local branch.
  --deploy-tracking REF   Expected beta tracking ref. Defaults to origin/<deploy-branch>.
  --freeze-note TEXT      Operator note saved with the cutover artifacts.
  --skip-local-gate       Skip ./run/public_readiness_local.sh.
  --deploy                Perform the live in-place deploy after backups.
  -h, --help              Show this help.

Environment:
  BETA_SIGNOFF_HOST         Public beta host. Default: beta.theitwingman.com
  BETA_SIGNOFF_SSH_TARGET   SSH target. Default: ubuntu@beta.theitwingman.com
  BETA_SIGNOFF_SSH_KEY      SSH identity file. Default: $HOME/.ssh/itlab.key
  BETA_SIGNOFF_SSH_PORT     SSH port. Default: 22
  BETA_SIGNOFF_APP_ROOT     Remote app root. Default: /opt/twinevia-saas
  BETA_SIGNOFF_APP_USER     Optional remote app user override.
  BETA_CUTOVER_PROXY_CONFIG Optional reverse-proxy config path to copy in addition to nginx -T output.
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --org-slug)
      ORG_SLUG="${2:-}"
      shift 2
      ;;
    --run-id)
      RUN_ID="${2:-}"
      shift 2
      ;;
    --deploy-branch)
      DEPLOY_BRANCH="${2:-}"
      shift 2
      ;;
    --deploy-tracking)
      DEPLOY_TRACKING="${2:-}"
      shift 2
      ;;
    --freeze-note)
      FREEZE_NOTE="${2:-}"
      shift 2
      ;;
    --skip-local-gate)
      RUN_LOCAL_GATE=0
      shift
      ;;
    --deploy)
      RUN_DEPLOY=1
      shift
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

if [[ -z "${ORG_SLUG}" ]]; then
  usage >&2
  exit 1
fi

if [[ -z "${RUN_ID}" ]]; then
  RUN_ID="$(date +%Y%m%d-%H%M%S)"
fi

if [[ -z "${DEPLOY_TRACKING}" ]]; then
  DEPLOY_TRACKING="origin/${DEPLOY_BRANCH}"
fi

RUN_DIR="${REPO_ROOT}/output/signoff/${RUN_ID}/beta-cutover"
mkdir -p "${RUN_DIR}"

SSH_OPTS=(
  -i "${SSH_KEY}"
  -p "${SSH_PORT}"
  -o BatchMode=yes
  -o StrictHostKeyChecking=yes
)

ssh_run() {
  ssh "${SSH_OPTS[@]}" "${SSH_TARGET}" "$@"
}

run_and_capture() {
  local name="$1"
  shift
  local log_file="${RUN_DIR}/${name}.log"
  echo "==> ${name}" | tee "${log_file}"
  "$@" 2>&1 | tee -a "${log_file}"
}

capture_remote_text_file() {
  local remote_path="$1"
  local local_path="$2"

  if ! ssh_run "sudo cat \"${remote_path}\"" > "${local_path}"; then
    echo "Failed to copy ${remote_path} from ${SSH_TARGET}" >&2
    exit 1
  fi
}

if [[ "${RUN_LOCAL_GATE}" -eq 1 ]]; then
  run_and_capture local_gate "${REPO_ROOT}/run/public_readiness_local.sh" --run-id "${RUN_ID}"
fi

run_and_capture pre_snapshot "${REPO_ROOT}/run/public_readiness_beta_snapshot.sh" \
  --run-id "${RUN_ID}" \
  --org-slug "${ORG_SLUG}" \
  --label pre-deploy

REMOTE_BACKUP_DIR="$(ssh "${SSH_OPTS[@]}" "${SSH_TARGET}" bash -s -- \
  "${APP_ROOT}" \
  "${APP_USER}" \
  "/var/backups/twinevia-beta-cutover/${RUN_ID}" \
  "${PROXY_CONFIG_PATH}" \
  "${DEPLOY_BRANCH}" \
  "${DEPLOY_TRACKING}" <<'REMOTE'
set -euo pipefail

APP_ROOT="$1"
APP_USER_OVERRIDE="$2"
BACKUP_DIR="$3"
PROXY_CONFIG_PATH="$4"
DEPLOY_BRANCH="$5"
DEPLOY_TRACKING="$6"

resolve_app_user() {
  if [[ -n "${APP_USER_OVERRIDE}" ]]; then
    printf '%s\n' "${APP_USER_OVERRIDE}"
    return
  fi
  if id -u twinevia >/dev/null 2>&1; then
    printf 'twinevia\n'
    return
  fi
  if id -u smsadmin >/dev/null 2>&1; then
    printf 'smsadmin\n'
    return
  fi
  printf 'twinevia\n'
}

resolve_app_group() {
  id -gn "${APP_USER}"
}

APP_USER="$(resolve_app_user)"
APP_GROUP="$(resolve_app_group)"

[ -d "${APP_ROOT}" ] || { echo "${APP_ROOT} missing" >&2; exit 1; }
[ -f "${APP_ROOT}/.env" ] || { echo "${APP_ROOT}/.env missing" >&2; exit 1; }

CURRENT_BRANCH="$(sudo -u "${APP_USER}" git -C "${APP_ROOT}" rev-parse --abbrev-ref HEAD)"
TRACKING_BRANCH="$(sudo -u "${APP_USER}" git -C "${APP_ROOT}" rev-parse --abbrev-ref --symbolic-full-name '@{u}' 2>/dev/null || true)"
[ "${CURRENT_BRANCH}" = "${DEPLOY_BRANCH}" ] || { echo "branch mismatch: ${CURRENT_BRANCH} != ${DEPLOY_BRANCH}" >&2; exit 1; }
[ "${TRACKING_BRANCH}" = "${DEPLOY_TRACKING}" ] || { echo "tracking mismatch: ${TRACKING_BRANCH:-<none>} != ${DEPLOY_TRACKING}" >&2; exit 1; }

sudo install -d -o "${APP_USER}" -g "${APP_GROUP}" -m 0700 "${BACKUP_DIR}"
sudo install -m 0600 "${APP_ROOT}/.env" "${BACKUP_DIR}/app.env"

sudo -u "${APP_USER}" bash -lc "set -euo pipefail; git -C \"${APP_ROOT}\" rev-parse HEAD > \"${BACKUP_DIR}/live_commit.txt\""
sudo -u "${APP_USER}" bash -lc "set -euo pipefail; git -C \"${APP_ROOT}\" rev-parse --abbrev-ref HEAD > \"${BACKUP_DIR}/live_branch.txt\""
sudo -u "${APP_USER}" bash -lc "set -euo pipefail; git -C \"${APP_ROOT}\" rev-parse --abbrev-ref --symbolic-full-name '@{u}' > \"${BACKUP_DIR}/live_tracking_branch.txt\""
sudo bash -lc "systemctl status twinevia-saas twinevia-saas-worker twinevia-saas-scheduler.timer twinevia-saas-billing-reconcile.timer twinevia-saas-platform-restart-queue.timer twinevia-saas-a2p-reconcile.timer --no-pager > \"${BACKUP_DIR}/services.status.txt\""
sudo -u "${APP_USER}" env APP_ROOT="${APP_ROOT}" BACKUP_DIR="${BACKUP_DIR}" bash -lc '
  set -euo pipefail
  cd "${APP_ROOT}"
  set -a
  source .env
  set +a
  if [ -x /usr/local/bin/twinevia-saas-dbdoctor ]; then
    /usr/local/bin/twinevia-saas-dbdoctor --doctor > "${BACKUP_DIR}/saas_dbdoctor.txt"
  else
    /usr/local/bin/saas-dbdoctor --doctor > "${BACKUP_DIR}/saas_dbdoctor.txt"
  fi
  pg_dump "$DATABASE_URL" > "${BACKUP_DIR}/postgres.sql"
  if command -v redis-cli >/dev/null 2>&1; then
    redis-cli -u "$REDIS_URL" --rdb "${BACKUP_DIR}/redis.rdb" >/dev/null
  elif [ -f /var/lib/redis/dump.rdb ]; then
    cp /var/lib/redis/dump.rdb "${BACKUP_DIR}/redis.rdb"
  else
    printf "redis backup unavailable\n" > "${BACKUP_DIR}/redis.note.txt"
  fi
'

if command -v nginx >/dev/null 2>&1; then
  sudo bash -lc "nginx -T > \"${BACKUP_DIR}/nginx-T.txt\" 2>&1"
else
  sudo bash -lc "printf 'nginx not installed on host\n' > \"${BACKUP_DIR}/nginx-T.txt\""
fi

if [[ -n "${PROXY_CONFIG_PATH}" ]]; then
  if sudo test -f "${PROXY_CONFIG_PATH}"; then
    sudo install -m 0600 "${PROXY_CONFIG_PATH}" "${BACKUP_DIR}/proxy.conf"
  else
    sudo bash -lc "printf 'requested proxy config missing: %s\n' \"${PROXY_CONFIG_PATH}\" > \"${BACKUP_DIR}/proxy.note.txt\""
  fi
fi

printf '%s\n' "${BACKUP_DIR}"
REMOTE
)"

printf '%s\n' "${REMOTE_BACKUP_DIR}" > "${RUN_DIR}/remote_backup_dir.txt"
printf '%s\n' "${HOST}" > "${RUN_DIR}/host.txt"
printf '%s\n' "${SSH_TARGET}" > "${RUN_DIR}/ssh_target.txt"
printf '%s\n' "${APP_ROOT}" > "${RUN_DIR}/app_root.txt"
printf '%s\n' "${DEPLOY_BRANCH}" > "${RUN_DIR}/deploy_branch.txt"
printf '%s\n' "${DEPLOY_TRACKING}" > "${RUN_DIR}/deploy_tracking.txt"
printf '%s\n' "${FREEZE_NOTE}" > "${RUN_DIR}/freeze_note.txt"

capture_remote_text_file "${REMOTE_BACKUP_DIR}/app.env" "${RUN_DIR}/app.env"
capture_remote_text_file "${REMOTE_BACKUP_DIR}/nginx-T.txt" "${RUN_DIR}/nginx-T.txt"
capture_remote_text_file "${REMOTE_BACKUP_DIR}/saas_dbdoctor.txt" "${RUN_DIR}/saas_dbdoctor.txt"
capture_remote_text_file "${REMOTE_BACKUP_DIR}/services.status.txt" "${RUN_DIR}/services.status.txt"
capture_remote_text_file "${REMOTE_BACKUP_DIR}/live_commit.txt" "${RUN_DIR}/live_commit.txt"
capture_remote_text_file "${REMOTE_BACKUP_DIR}/live_branch.txt" "${RUN_DIR}/live_branch.txt"
capture_remote_text_file "${REMOTE_BACKUP_DIR}/live_tracking_branch.txt" "${RUN_DIR}/live_tracking_branch.txt"
if ssh_run "sudo test -f \"${REMOTE_BACKUP_DIR}/proxy.conf\""; then
  capture_remote_text_file "${REMOTE_BACKUP_DIR}/proxy.conf" "${RUN_DIR}/proxy.conf"
fi
if ssh_run "sudo test -f \"${REMOTE_BACKUP_DIR}/proxy.note.txt\""; then
  capture_remote_text_file "${REMOTE_BACKUP_DIR}/proxy.note.txt" "${RUN_DIR}/proxy.note.txt"
fi
if ssh_run "sudo test -f \"${REMOTE_BACKUP_DIR}/redis.note.txt\""; then
  capture_remote_text_file "${REMOTE_BACKUP_DIR}/redis.note.txt" "${RUN_DIR}/redis.note.txt"
fi

if [[ "${RUN_DEPLOY}" -eq 1 ]]; then
  ssh "${SSH_OPTS[@]}" "${SSH_TARGET}" bash -s -- \
    "${APP_ROOT}" \
    "${APP_USER}" \
    "${DEPLOY_BRANCH}" \
    "${DEPLOY_TRACKING}" <<'REMOTE'
set -euo pipefail

APP_ROOT="$1"
APP_USER_OVERRIDE="$2"
DEPLOY_BRANCH="$3"
DEPLOY_TRACKING="$4"

resolve_app_user() {
  if [[ -n "${APP_USER_OVERRIDE}" ]]; then
    printf '%s\n' "${APP_USER_OVERRIDE}"
    return
  fi
  if id -u twinevia >/dev/null 2>&1; then
    printf 'twinevia\n'
    return
  fi
  if id -u smsadmin >/dev/null 2>&1; then
    printf 'smsadmin\n'
    return
  fi
  printf 'twinevia\n'
}

APP_USER="$(resolve_app_user)"
CURRENT_BRANCH="$(sudo -u "${APP_USER}" git -C "${APP_ROOT}" rev-parse --abbrev-ref HEAD)"
TRACKING_BRANCH="$(sudo -u "${APP_USER}" git -C "${APP_ROOT}" rev-parse --abbrev-ref --symbolic-full-name '@{u}' 2>/dev/null || true)"
[ "${CURRENT_BRANCH}" = "${DEPLOY_BRANCH}" ] || { echo "branch mismatch: ${CURRENT_BRANCH} != ${DEPLOY_BRANCH}" >&2; exit 1; }
[ "${TRACKING_BRANCH}" = "${DEPLOY_TRACKING}" ] || { echo "tracking mismatch: ${TRACKING_BRANCH:-<none>} != ${DEPLOY_TRACKING}" >&2; exit 1; }

sudo EXPECTED_GIT_BRANCH="${DEPLOY_BRANCH}" EXPECTED_GIT_TRACKING_BRANCH="${DEPLOY_TRACKING}" APP_ROOT="${APP_ROOT}" APP_USER="${APP_USER}" "${APP_ROOT}/deploy/deploy_twinevia_saas.sh"
REMOTE

  run_and_capture post_snapshot "${REPO_ROOT}/run/public_readiness_beta_snapshot.sh" \
    --run-id "${RUN_ID}" \
    --org-slug "${ORG_SLUG}" \
    --label post-deploy

  ssh_run "sudo cat \"${APP_ROOT}/.env\"" > "${RUN_DIR}/app.post.env"
  if ! diff -u "${RUN_DIR}/app.env" "${RUN_DIR}/app.post.env" > "${RUN_DIR}/app.env.diff"; then
    echo "WARNING: beta env changed during deploy. Review ${RUN_DIR}/app.env.diff" >&2
  fi
fi

cat > "${RUN_DIR}/summary.txt" <<EOF
Beta cutover preparation completed successfully.
Run ID: ${RUN_ID}
Host: ${HOST}
SSH target: ${SSH_TARGET}
App root: ${APP_ROOT}
Expected branch: ${DEPLOY_BRANCH}
Expected tracking: ${DEPLOY_TRACKING}
Remote backup dir: ${REMOTE_BACKUP_DIR}
Local gate run: ${RUN_LOCAL_GATE}
Deploy run: ${RUN_DEPLOY}
Freeze note: ${FREEZE_NOTE}
EOF

echo "Beta cutover artifacts written to ${RUN_DIR}"
