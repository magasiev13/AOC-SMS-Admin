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
PROXY_CONFIG_PATH="$(resolve_compat_env "TWINEVIA_PROXY_CONFIG_PATH" "BETA_CUTOVER_PROXY_CONFIG" "")"
resolve_default_deploy_branch() {
  local symbolic_branch
  local head_sha
  local preferred_branch="saas-pilot-v2"
  local preferred_local_ref="refs/heads/${preferred_branch}"
  local preferred_remote_ref="refs/remotes/origin/${preferred_branch}"
  local candidate
  local candidates=()

  symbolic_branch="$(git -C "${REPO_ROOT}" symbolic-ref --quiet --short HEAD 2>/dev/null || true)"
  if [[ -n "${symbolic_branch}" ]]; then
    printf '%s\n' "${symbolic_branch}"
    return
  fi

  head_sha="$(git -C "${REPO_ROOT}" rev-parse HEAD 2>/dev/null || true)"
  if [[ -z "${head_sha}" ]]; then
    return
  fi

  if git -C "${REPO_ROOT}" show-ref --verify --quiet "${preferred_local_ref}" && [[ "$(git -C "${REPO_ROOT}" rev-parse "${preferred_local_ref}")" == "${head_sha}" ]]; then
    printf '%s\n' "${preferred_branch}"
    return
  fi
  if git -C "${REPO_ROOT}" show-ref --verify --quiet "${preferred_remote_ref}" && [[ "$(git -C "${REPO_ROOT}" rev-parse "${preferred_remote_ref}")" == "${head_sha}" ]]; then
    printf '%s\n' "${preferred_branch}"
    return
  fi

  while IFS= read -r candidate; do
    [[ -z "${candidate}" ]] && continue
    candidate="${candidate#origin/}"
    if [[ " ${candidates[*]} " != *" ${candidate} "* ]]; then
      candidates+=("${candidate}")
    fi
  done < <(git -C "${REPO_ROOT}" for-each-ref --format='%(refname:short)' --points-at HEAD refs/heads refs/remotes/origin 2>/dev/null)

  if [[ ${#candidates[@]} -eq 1 ]]; then
    printf '%s\n' "${candidates[0]}"
  fi
}

DEFAULT_BRANCH="$(resolve_default_deploy_branch)"
DEPLOY_BRANCH="$(resolve_compat_env "TWINEVIA_DEPLOY_BRANCH" "BETA_DEPLOY_BRANCH" "${DEFAULT_BRANCH}")"
DEPLOY_TRACKING="$(resolve_compat_env "TWINEVIA_DEPLOY_TRACKING" "BETA_DEPLOY_TRACKING" "")"
RUN_ID=""
ORG_SLUG=""
FREEZE_NOTE=""
RUN_LOCAL_GATE=1
RUN_DEPLOY=0
CANONICALIZE_HOST=0
EMPTY_ARG_PLACEHOLDER="__TWINEVIA_EMPTY__"

usage() {
  cat <<'EOF'
Usage: ./run/production_cutover.sh --org-slug ORG_SLUG [options]

Orchestrates the safe production cutover workflow for the live Twinevia SaaS host:
  1. optional local public-readiness gate
  2. pre-deploy read-only production snapshot
  3. remote PostgreSQL/Redis/config backup bundle
  4. optional in-place deploy against the existing live checkout
  5. post-deploy read-only production snapshot

Options:
  --org-slug ORG_SLUG     Organization slug used for pre/post snapshot parity.
  --run-id RUN_ID         Artifact run id. Defaults to timestamp.
  --deploy-branch BRANCH  Expected live checkout branch. Defaults to the current local branch when resolvable.
  --deploy-tracking REF   Expected live tracking ref. Defaults to origin/<deploy-branch>.
  --canonicalize-host     Migrate a legacy /opt/sms-saas runtime to /opt/twinevia-saas once before deploy.
  --freeze-note TEXT      Operator note saved with the cutover artifacts.
  --skip-local-gate       Skip ./run/public_readiness_local.sh.
  --deploy                Perform the live in-place deploy after backups.
  -h, --help              Show this help.

Environment:
  TWINEVIA_PUBLIC_HOST      Public host for HTTPS health checks. Default: www.twinevia.com
  TWINEVIA_SSH_TARGET       Required SSH target for remote cutover work.
  TWINEVIA_SSH_KEY          SSH identity file. Default: $HOME/.ssh/itlab.key
  TWINEVIA_SSH_PORT         SSH port. Default: 22
  TWINEVIA_APP_ROOT         Optional remote app root override. Auto-detects /opt/twinevia-saas then /opt/sms-saas.
  TWINEVIA_APP_USER         Optional remote app user override.
  TWINEVIA_UNIT_PREFIX      Optional remote unit prefix override. Auto-detects twinevia-saas then sms-saas.
  TWINEVIA_PROXY_CONFIG_PATH Optional reverse-proxy config path to copy in addition to nginx -T output.
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
    --canonicalize-host)
      CANONICALIZE_HOST=1
      shift
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

if [[ -z "${SSH_TARGET}" ]]; then
  echo "ERROR: TWINEVIA_SSH_TARGET must be set for remote production cutovers." >&2
  exit 1
fi

if [[ -z "${DEPLOY_BRANCH}" ]]; then
  echo "ERROR: Could not infer a deploy branch from this detached checkout." >&2
  echo "Pass --deploy-branch explicitly (for example: --deploy-branch saas-pilot-v2)." >&2
  exit 1
fi

if [[ -z "${RUN_ID}" ]]; then
  RUN_ID="$(date +%Y%m%d-%H%M%S)"
fi

if [[ -z "${DEPLOY_TRACKING}" ]]; then
  DEPLOY_TRACKING="origin/${DEPLOY_BRANCH}"
fi

RUN_DIR="${REPO_ROOT}/output/signoff/${RUN_ID}/production-cutover"
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

systemd_show_value() {
  local key="$1"
  local file_path="$2"

  awk -F= -v key="${key}" '$1 == key { print substr($0, index($0, "=") + 1); exit }' "${file_path}"
}

assert_runtime_layout() {
  local file_path="$1"
  local expected_user="$2"
  local expected_root="$3"
  local context="$4"
  local actual_user
  local actual_root

  actual_user="$(systemd_show_value "User" "${file_path}")"
  actual_root="$(systemd_show_value "WorkingDirectory" "${file_path}")"

  if [[ "${actual_user}" != "${expected_user}" || "${actual_root}" != "${expected_root}" ]]; then
    echo "ERROR: ${context} did not leave twinevia-saas.service on the canonical runtime layout." >&2
    echo "Expected User=${expected_user} WorkingDirectory=${expected_root}" >&2
    echo "Actual   User=${actual_user:-<missing>} WorkingDirectory=${actual_root:-<missing>}" >&2
    exit 1
  fi
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

resolve_remote_unit_prefix() {
  if [[ -n "${UNIT_PREFIX}" ]]; then
    printf '%s\n' "${UNIT_PREFIX}"
    return
  fi
  ssh_run "if systemctl list-unit-files twinevia-saas.service --no-legend | grep -q '^twinevia-saas.service[[:space:]]'; then printf twinevia-saas; elif systemctl list-unit-files sms-saas.service --no-legend | grep -q '^sms-saas.service[[:space:]]'; then printf sms-saas; else printf twinevia-saas; fi"
}

APP_ROOT="$(resolve_remote_app_root)"
APP_USER="$(resolve_remote_app_user)"
UNIT_PREFIX="$(resolve_remote_unit_prefix)"

APP_USER_ARG="${APP_USER:-${EMPTY_ARG_PLACEHOLDER}}"
PROXY_CONFIG_ARG="${PROXY_CONFIG_PATH:-${EMPTY_ARG_PLACEHOLDER}}"

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

run_and_capture pre_snapshot "${REPO_ROOT}/run/public_readiness_production_snapshot.sh" \
  --run-id "${RUN_ID}" \
  --org-slug "${ORG_SLUG}" \
  --label pre-deploy

REMOTE_BACKUP_DIR="$(ssh "${SSH_OPTS[@]}" "${SSH_TARGET}" bash -s -- \
  "${APP_ROOT}" \
  "${APP_USER_ARG}" \
  "${UNIT_PREFIX}" \
  "/var/backups/twinevia-production-cutover/${RUN_ID}" \
  "${PROXY_CONFIG_ARG}" \
  "${DEPLOY_BRANCH}" \
  "${DEPLOY_TRACKING}" <<'REMOTE'
set -euo pipefail

APP_ROOT="$1"
APP_USER_OVERRIDE="$2"
UNIT_PREFIX="$3"
BACKUP_DIR="$4"
PROXY_CONFIG_PATH="$5"
DEPLOY_BRANCH="$6"
DEPLOY_TRACKING="$7"

if [[ "${APP_USER_OVERRIDE}" == "__TWINEVIA_EMPTY__" ]]; then
  APP_USER_OVERRIDE=""
fi
if [[ "${PROXY_CONFIG_PATH}" == "__TWINEVIA_EMPTY__" ]]; then
  PROXY_CONFIG_PATH=""
fi

resolve_app_user() {
  if [[ -n "${APP_USER_OVERRIDE}" ]]; then
    printf '%s\n' "${APP_USER_OVERRIDE}"
    return
  fi
  if [[ -d "${APP_ROOT}" ]]; then
    owner="$(stat -c '%U' "${APP_ROOT}" 2>/dev/null || true)"
    if [[ -n "${owner}" && "${owner}" != "root" ]]; then
      printf '%s\n' "${owner}"
      return
    fi
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
[ -z "$(sudo -u "${APP_USER}" git -C "${APP_ROOT}" status --short --untracked-files=all)" ] || { echo "${APP_ROOT} has uncommitted or untracked changes" >&2; exit 1; }
[ "${CURRENT_BRANCH}" = "${DEPLOY_BRANCH}" ] || { echo "branch mismatch: ${CURRENT_BRANCH} != ${DEPLOY_BRANCH}" >&2; exit 1; }
[ "${TRACKING_BRANCH}" = "${DEPLOY_TRACKING}" ] || { echo "tracking mismatch: ${TRACKING_BRANCH:-<none>} != ${DEPLOY_TRACKING}" >&2; exit 1; }

sudo install -d -o "${APP_USER}" -g "${APP_GROUP}" -m 0700 "${BACKUP_DIR}"
sudo install -m 0600 "${APP_ROOT}/.env" "${BACKUP_DIR}/app.env"

sudo -u "${APP_USER}" bash -lc "set -euo pipefail; git -C \"${APP_ROOT}\" rev-parse HEAD > \"${BACKUP_DIR}/live_commit.txt\""
sudo -u "${APP_USER}" bash -lc "set -euo pipefail; git -C \"${APP_ROOT}\" rev-parse --abbrev-ref HEAD > \"${BACKUP_DIR}/live_branch.txt\""
sudo -u "${APP_USER}" bash -lc "set -euo pipefail; git -C \"${APP_ROOT}\" rev-parse --abbrev-ref --symbolic-full-name '@{u}' > \"${BACKUP_DIR}/live_tracking_branch.txt\""
sudo bash -lc "systemctl status ${UNIT_PREFIX} ${UNIT_PREFIX}-worker ${UNIT_PREFIX}-scheduler.timer ${UNIT_PREFIX}-billing-reconcile.timer ${UNIT_PREFIX}-platform-restart-queue.timer ${UNIT_PREFIX}-a2p-reconcile.timer --no-pager > \"${BACKUP_DIR}/services.status.txt\""
sudo -u "${APP_USER}" env APP_ROOT="${APP_ROOT}" BACKUP_DIR="${BACKUP_DIR}" bash -lc '
  set -euo pipefail
  cd "${APP_ROOT}"
  set -a
  source .env
  set +a
  if [ -x /usr/local/bin/twinevia-saas-dbdoctor ]; then
    TWINEVIA_SAAS_APP_ROOT="${APP_ROOT}" TWINEVIA_SAAS_PYTHON="${APP_ROOT}/venv/bin/python" /usr/local/bin/twinevia-saas-dbdoctor --doctor > "${BACKUP_DIR}/saas_dbdoctor.txt"
  else
    TWINEVIA_SAAS_APP_ROOT="${APP_ROOT}" TWINEVIA_SAAS_PYTHON="${APP_ROOT}/venv/bin/python" /usr/local/bin/saas-dbdoctor --doctor > "${BACKUP_DIR}/saas_dbdoctor.txt"
  fi
  PG_DUMP_URL="$("./venv/bin/python" - <<'"'"'PY'"'"'
import os

database_url = os.environ["DATABASE_URL"]
if database_url.startswith("postgresql+psycopg://"):
    database_url = "postgresql://" + database_url[len("postgresql+psycopg://"):]
print(database_url)
PY
)"
  pg_dump "$PG_DUMP_URL" > "${BACKUP_DIR}/postgres.sql"
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
printf '%s\n' "${APP_ROOT}" > "${RUN_DIR}/pre_app_root.txt"
printf '%s\n' "${APP_USER}" > "${RUN_DIR}/app_user.txt"
printf '%s\n' "${APP_USER}" > "${RUN_DIR}/pre_app_user.txt"
printf '%s\n' "${UNIT_PREFIX}" > "${RUN_DIR}/unit_prefix.txt"
printf '%s\n' "${DEPLOY_BRANCH}" > "${RUN_DIR}/deploy_branch.txt"
printf '%s\n' "${DEPLOY_TRACKING}" > "${RUN_DIR}/deploy_tracking.txt"
printf '%s\n' "${FREEZE_NOTE}" > "${RUN_DIR}/freeze_note.txt"
printf '%s\n' "0" > "${RUN_DIR}/canonicalized.txt"
ssh_run "sudo systemctl show twinevia-saas.service -p User -p Group -p WorkingDirectory" > "${RUN_DIR}/runtime_layout.pre.txt"

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

if [[ "${CANONICALIZE_HOST}" -eq 1 ]]; then
  CANONICALIZATION_RESULT="$(ssh "${SSH_OPTS[@]}" "${SSH_TARGET}" bash -s -- \
    "${APP_ROOT}" \
    "${APP_USER_ARG}" <<'REMOTE'
set -euo pipefail

APP_ROOT="$1"
APP_USER_OVERRIDE="$2"
TARGET_ROOT="/opt/twinevia-saas"

if [[ "${APP_USER_OVERRIDE}" == "__TWINEVIA_EMPTY__" ]]; then
  APP_USER_OVERRIDE=""
fi

resolve_app_user() {
  if [[ -n "${APP_USER_OVERRIDE}" ]]; then
    printf '%s\n' "${APP_USER_OVERRIDE}"
    return
  fi
  if [[ -d "${APP_ROOT}" ]]; then
    owner="$(stat -c '%U' "${APP_ROOT}" 2>/dev/null || true)"
    if [[ -n "${owner}" && "${owner}" != "root" ]]; then
      printf '%s\n' "${owner}"
      return
    fi
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

CURRENT_APP_USER="$(resolve_app_user)"

if [[ "${APP_ROOT}" != "/opt/sms-saas" ]]; then
  printf 'canonicalized=0\n'
  printf 'app_root=%s\n' "${APP_ROOT}"
  printf 'app_user=%s\n' "${CURRENT_APP_USER}"
  exit 0
fi

if ! getent group twinevia >/dev/null 2>&1; then
  sudo addgroup --system twinevia
fi
if ! id -u twinevia >/dev/null 2>&1; then
  sudo adduser --system --ingroup twinevia --home "${TARGET_ROOT}" --shell /bin/bash twinevia
fi

sudo install -d -o twinevia -g twinevia "${TARGET_ROOT}"
if command -v rsync >/dev/null 2>&1; then
  sudo rsync -a --delete "${APP_ROOT}/" "${TARGET_ROOT}/"
else
  sudo bash -lc "shopt -s dotglob nullglob; cp -a \"${APP_ROOT}\"/* \"${TARGET_ROOT}\"/"
fi
sudo chown -R twinevia:twinevia "${TARGET_ROOT}"
sudo chown root:twinevia "${TARGET_ROOT}/.env"
sudo chmod 660 "${TARGET_ROOT}/.env"

sudo APP_ROOT="${TARGET_ROOT}" APP_USER="twinevia" APP_GROUP="twinevia" "${TARGET_ROOT}/deploy/install_saas.sh"

printf 'canonicalized=1\n'
printf 'app_root=%s\n' "${TARGET_ROOT}"
printf 'app_user=twinevia\n'
REMOTE
)"

  CANONICALIZED_VALUE="$(printf '%s\n' "${CANONICALIZATION_RESULT}" | awk -F= '/^canonicalized=/{print $2}' | tail -n1)"
  POST_CANONICAL_APP_ROOT="$(printf '%s\n' "${CANONICALIZATION_RESULT}" | awk -F= '/^app_root=/{print $2}' | tail -n1)"
  POST_CANONICAL_APP_USER="$(printf '%s\n' "${CANONICALIZATION_RESULT}" | awk -F= '/^app_user=/{print $2}' | tail -n1)"

  if [[ -z "${CANONICALIZED_VALUE}" || -z "${POST_CANONICAL_APP_ROOT}" || -z "${POST_CANONICAL_APP_USER}" ]]; then
    echo "ERROR: canonical host migration did not return the expected metadata." >&2
    exit 1
  fi

  printf '%s\n' "${CANONICALIZED_VALUE}" > "${RUN_DIR}/canonicalized.txt"
  APP_ROOT="${POST_CANONICAL_APP_ROOT}"
  APP_USER="${POST_CANONICAL_APP_USER}"
  APP_USER_ARG="${APP_USER}"
  UNIT_PREFIX="$(resolve_remote_unit_prefix)"
fi

printf '%s\n' "${APP_ROOT}" > "${RUN_DIR}/post_app_root.txt"
printf '%s\n' "${APP_USER}" > "${RUN_DIR}/post_app_user.txt"
printf '%s\n' "${APP_ROOT}" > "${RUN_DIR}/app_root.txt"
printf '%s\n' "${APP_USER}" > "${RUN_DIR}/app_user.txt"
printf '%s\n' "${UNIT_PREFIX}" > "${RUN_DIR}/unit_prefix.txt"
ssh_run "sudo systemctl show twinevia-saas.service -p User -p Group -p WorkingDirectory" > "${RUN_DIR}/runtime_layout.post.txt"
if [[ "${CANONICALIZE_HOST}" -eq 1 ]]; then
  assert_runtime_layout "${RUN_DIR}/runtime_layout.post.txt" "twinevia" "/opt/twinevia-saas" "Canonical host migration"
fi

if [[ "${RUN_DEPLOY}" -eq 1 ]]; then
  ssh "${SSH_OPTS[@]}" "${SSH_TARGET}" bash -s -- \
    "${APP_ROOT}" \
    "${APP_USER_ARG}" \
    "${DEPLOY_BRANCH}" \
    "${DEPLOY_TRACKING}" <<'REMOTE'
set -euo pipefail

APP_ROOT="$1"
APP_USER_OVERRIDE="$2"
DEPLOY_BRANCH="$3"
DEPLOY_TRACKING="$4"

if [[ "${APP_USER_OVERRIDE}" == "__TWINEVIA_EMPTY__" ]]; then
  APP_USER_OVERRIDE=""
fi

resolve_app_user() {
  if [[ -n "${APP_USER_OVERRIDE}" ]]; then
    printf '%s\n' "${APP_USER_OVERRIDE}"
    return
  fi
  if [[ -d "${APP_ROOT}" ]]; then
    owner="$(stat -c '%U' "${APP_ROOT}" 2>/dev/null || true)"
    if [[ -n "${owner}" && "${owner}" != "root" ]]; then
      printf '%s\n' "${owner}"
      return
    fi
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

  sudo -u "${APP_USER}" git -C "${APP_ROOT}" pull --ff-only
  sudo EXPECTED_GIT_BRANCH="${DEPLOY_BRANCH}" EXPECTED_GIT_TRACKING_BRANCH="${DEPLOY_TRACKING}" APP_ROOT="${APP_ROOT}" APP_USER="${APP_USER}" "${APP_ROOT}/deploy/deploy_twinevia_saas.sh"
REMOTE

fi

if [[ "${RUN_DEPLOY}" -eq 1 || "${CANONICALIZE_HOST}" -eq 1 ]]; then
  run_and_capture post_snapshot "${REPO_ROOT}/run/public_readiness_production_snapshot.sh" \
    --run-id "${RUN_ID}" \
    --org-slug "${ORG_SLUG}" \
    --label post-deploy

  ssh_run "sudo cat \"${APP_ROOT}/.env\"" > "${RUN_DIR}/app.post.env"
  if ! diff -u "${RUN_DIR}/app.env" "${RUN_DIR}/app.post.env" > "${RUN_DIR}/app.env.diff"; then
    echo "WARNING: production env changed during cutover. Review ${RUN_DIR}/app.env.diff" >&2
  fi
fi

cat > "${RUN_DIR}/summary.txt" <<EOF
Production cutover completed successfully.
Run ID: ${RUN_ID}
Host: ${HOST}
SSH target: ${SSH_TARGET}
App root: ${APP_ROOT}
App user: ${APP_USER}
Expected branch: ${DEPLOY_BRANCH}
Expected tracking: ${DEPLOY_TRACKING}
Remote backup dir: ${REMOTE_BACKUP_DIR}
Local gate run: ${RUN_LOCAL_GATE}
Deploy run: ${RUN_DEPLOY}
Canonicalize host requested: ${CANONICALIZE_HOST}
Canonicalize host ran: $(cat "${RUN_DIR}/canonicalized.txt")
Freeze note: ${FREEZE_NOTE}
EOF

echo "Production cutover artifacts written to ${RUN_DIR}"
