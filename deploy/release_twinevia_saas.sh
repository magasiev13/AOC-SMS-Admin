#!/usr/bin/env bash
set -euo pipefail

SOURCE_ROOT="${SOURCE_ROOT:-/opt/twinevia-saas}"
APP_ROOT="${APP_ROOT:-/opt/twinevia-saas}"
APP_USER="${APP_USER:-twinevia}"
APP_GROUP="${APP_GROUP:-twinevia}"
ENV_FILE="${TWINEVIA_ENV_FILE:-${APP_ROOT}/.env}"
RELEASES_DIR="${APP_ROOT}/releases"
CURRENT_LINK="${APP_ROOT}/current"
PREVIOUS_LINK="${APP_ROOT}/previous"
RELEASE_RETENTION_COUNT="${RELEASE_RETENTION_COUNT:-5}"
RUNTIME_UNITS=(
  twinevia-saas.service
  twinevia-saas-worker.service
  twinevia-saas-scheduler.timer
  twinevia-saas-billing-reconcile.timer
  twinevia-saas-platform-restart-queue.timer
  twinevia-saas-a2p-reconcile.timer
  twinevia-saas-backup.timer
  twinevia-saas-readiness.timer
)

if [[ ! "${RELEASE_RETENTION_COUNT}" =~ ^[0-9]+$ ]] || (( RELEASE_RETENTION_COUNT < 2 || RELEASE_RETENTION_COUNT > 20 )); then
  echo "RELEASE_RETENTION_COUNT must be between 2 and 20." >&2
  exit 1
fi
if [[ ! -d "${SOURCE_ROOT}/.git" ]]; then
  echo "Release source must be a Git checkout: ${SOURCE_ROOT}" >&2
  exit 1
fi
if [[ ! -r "${ENV_FILE}" ]]; then
  echo "Production environment file is missing or unreadable: ${ENV_FILE}" >&2
  exit 1
fi

source_status="$(sudo -u "${APP_USER}" git -C "${SOURCE_ROOT}" status --porcelain --untracked-files=all)"
if [[ -n "${source_status}" ]]; then
  echo "Refusing release: production source checkout is not clean." >&2
  echo "${source_status}" >&2
  exit 1
fi

release_sha="$(sudo -u "${APP_USER}" git -C "${SOURCE_ROOT}" rev-parse HEAD)"
release_id="$(date -u +%Y%m%dT%H%M%SZ)-${release_sha:0:12}"
pending_release="${RELEASES_DIR}/.pending-${release_id}"
release_dir="${RELEASES_DIR}/${release_id}"
old_release="$(readlink -f "${CURRENT_LINK}" 2>/dev/null || true)"
promoted=0

sudo install -d -o root -g "${APP_GROUP}" -m 0750 "${RELEASES_DIR}"
if [[ -e "${pending_release}" || -e "${release_dir}" ]]; then
  echo "Release path already exists: ${release_id}" >&2
  exit 1
fi
sudo install -d -o "${APP_USER}" -g "${APP_GROUP}" -m 0750 "${pending_release}"

cleanup_pending() {
  if [[ -d "${pending_release}" ]]; then
    sudo rm -rf -- "${pending_release}"
  fi
}
trap cleanup_pending EXIT

rollback_on_error() {
  local exit_code=$?
  trap - ERR
  if [[ "${promoted}" == "1" ]]; then
    if [[ -n "${old_release}" && -d "${old_release}" ]]; then
      local rollback_link="${CURRENT_LINK}.error-rollback-$$"
      sudo ln -s "${old_release}" "${rollback_link}" || true
      sudo mv -Tf "${rollback_link}" "${CURRENT_LINK}" || true
      sudo systemctl restart "${RUNTIME_UNITS[@]}" || true
    else
      sudo systemctl stop "${RUNTIME_UNITS[@]}" || true
      if [[ -L "${CURRENT_LINK}" ]]; then
        sudo rm -- "${CURRENT_LINK}" || true
      fi
    fi
  fi
  exit "${exit_code}"
}
trap rollback_on_error ERR

sudo -u "${APP_USER}" git -C "${SOURCE_ROOT}" archive --format=tar "${release_sha}" \
  | sudo -u "${APP_USER}" tar -xf - -C "${pending_release}"
sudo -u "${APP_USER}" ln -s "${ENV_FILE}" "${pending_release}/.env"

release_env_tmp="$(mktemp)"
printf 'APP_RELEASE_ID=%s\nAPP_RELEASE_SHA=%s\n' "${release_id}" "${release_sha}" > "${release_env_tmp}"
sudo install -o "${APP_USER}" -g "${APP_GROUP}" -m 0440 "${release_env_tmp}" "${pending_release}/.release.env"
rm -f "${release_env_tmp}"

release_manifest_tmp="$(mktemp)"
RELEASE_ID="${release_id}" RELEASE_SHA="${release_sha}" RELEASE_MANIFEST="${release_manifest_tmp}" python3 -c '
import json
import os
from datetime import datetime, timezone
from pathlib import Path

Path(os.environ["RELEASE_MANIFEST"]).write_text(
    json.dumps(
        {
            "created_at": datetime.now(timezone.utc).isoformat(),
            "release_id": os.environ["RELEASE_ID"],
            "source_sha": os.environ["RELEASE_SHA"],
        },
        indent=2,
        sort_keys=True,
    ) + "\n",
    encoding="utf-8",
)
'
sudo install -o "${APP_USER}" -g "${APP_GROUP}" -m 0440 "${release_manifest_tmp}" "${pending_release}/release.json"
rm -f "${release_manifest_tmp}"

sudo -u "${APP_USER}" python3.11 -m venv "${pending_release}/venv"
sudo -u "${APP_USER}" "${pending_release}/venv/bin/python" -m pip install --disable-pip-version-check -r "${pending_release}/requirements.txt"
sudo -u "${APP_USER}" "${pending_release}/venv/bin/python" -m pip check
sudo -u "${APP_USER}" "${pending_release}/venv/bin/python" -m pip freeze \
  > "${pending_release}/requirements.resolved.txt"
sudo -u "${APP_USER}" env APP_ROOT="${pending_release}" "${pending_release}/deploy/check_expand_only_migrations.sh"
sudo -u "${APP_USER}" bash -lc "set -euo pipefail; cd \"${pending_release}\"; set -a; source \"${ENV_FILE}\"; source \"${pending_release}/.release.env\"; set +a; ./run/verify.sh"

set -a
source "${ENV_FILE}"
set +a
: "${BACKUP_STATUS_FILE:?BACKUP_STATUS_FILE is required before a release can migrate production}"
: "${BACKUP_ENCRYPTION_PASSPHRASE_FILE:?BACKUP_ENCRYPTION_PASSPHRASE_FILE is required}"
: "${RESTORE_DRILL_DATABASE_URL:?RESTORE_DRILL_DATABASE_URL is required}"
: "${RESTORE_DRILL_DATABASE_NAME:?RESTORE_DRILL_DATABASE_NAME is required}"

backup_application_root="${SOURCE_ROOT}"
if [[ -n "${old_release}" && -d "${old_release}" ]]; then
  backup_application_root="${old_release}"
fi
echo "Creating and verifying the encrypted pre-migration backup."
APP_ROOT="${backup_application_root}" \
APP_GROUP="${APP_GROUP}" \
APP_RELEASE_ID="pre-migration-${release_id}" \
TWINEVIA_ENV_FILE="${ENV_FILE}" \
bash "${pending_release}/deploy/backup_twinevia_saas.sh"

backup_archive="$(BACKUP_STATUS_PATH="${BACKUP_STATUS_FILE}" python3 -c '
import json
import os
from pathlib import Path

payload = json.loads(Path(os.environ["BACKUP_STATUS_PATH"]).read_text(encoding="utf-8"))
print(str(payload["local_path"]))
')"
if [[ -z "${backup_archive}" ]]; then
  echo "Pre-migration backup status did not contain a local archive path." >&2
  exit 1
fi

APP_ROOT="${pending_release}" \
APP_GROUP="${APP_GROUP}" \
TWINEVIA_ENV_FILE="${ENV_FILE}" \
bash "${pending_release}/deploy/restore_twinevia_saas_backup.sh" \
  --archive "${backup_archive}" \
  --passphrase-file "${BACKUP_ENCRYPTION_PASSPHRASE_FILE}"

sudo -u "${APP_USER}" bash -lc "set -euo pipefail; cd \"${pending_release}\"; set -a; source \"${ENV_FILE}\"; source \"${pending_release}/.release.env\"; set +a; ./venv/bin/python -m app.aoc_scheduled_guard --organization-slug \"${AOC_EVENTS_ORGANIZATION_SLUG:-armenians-of-colorado}\" --expect-dispatchable-count 0"

sudo -u "${APP_USER}" bash -lc "set -euo pipefail; cd \"${pending_release}\"; set -a; source \"${ENV_FILE}\"; source \"${pending_release}/.release.env\"; set +a; ./venv/bin/python -m app.saas_db --apply; ./venv/bin/python -m app.saas_db --ensure-platform-admin; ./venv/bin/python -m app.saas_db --doctor"
sudo -u "${APP_USER}" bash -lc "set -euo pipefail; cd \"${pending_release}\"; set -a; source \"${ENV_FILE}\"; source \"${pending_release}/.release.env\"; set +a; ./venv/bin/python - <<'PY'
from app import create_runtime_app

create_runtime_app(start_scheduler=False)
print('Release startup and live provider configuration validation passed.')
PY"

sudo chown -R root:"${APP_GROUP}" "${pending_release}"
sudo chmod -R u=rwX,g=rX,o= "${pending_release}"
sudo mv "${pending_release}" "${release_dir}"
pending_release=""

atomic_link() {
  local target="$1"
  local link_path="$2"
  local temporary_link="${link_path}.next-${release_id}"
  sudo ln -s "${target}" "${temporary_link}"
  sudo mv -Tf "${temporary_link}" "${link_path}"
}

if [[ -n "${old_release}" ]]; then
  atomic_link "${old_release}" "${PREVIOUS_LINK}"
fi
atomic_link "${release_dir}" "${CURRENT_LINK}"
promoted=1
sudo systemctl daemon-reload
sudo systemctl enable --now "${RUNTIME_UNITS[@]}"
sudo systemctl restart "${RUNTIME_UNITS[@]}"

health_host="$(APP_URL="${APP_BASE_URL:-}" python3 -c 'import os; from urllib.parse import urlsplit; print(urlsplit(os.environ["APP_URL"]).hostname or "")')"
if [[ -z "${health_host}" ]]; then
  echo "APP_BASE_URL must provide the health-check host." >&2
  exit 1
fi

health_ready=0
for attempt in $(seq 1 30); do
  if [[ "$(curl --silent --show-error --connect-timeout 2 --max-time 5 -H "Host: ${health_host}" http://127.0.0.1:8100/health || true)" == "OK" ]]; then
    health_ready=1
    break
  fi
  sleep 2
done
if [[ "${health_ready}" != "1" ]]; then
  if [[ -n "${old_release}" ]]; then
    atomic_link "${old_release}" "${CURRENT_LINK}"
    sudo systemctl restart "${RUNTIME_UNITS[@]}" || true
  else
    sudo systemctl stop "${RUNTIME_UNITS[@]}" || true
    if [[ -L "${CURRENT_LINK}" ]]; then
      sudo rm -- "${CURRENT_LINK}"
    fi
  fi
  promoted=0
  echo "Release ${release_id} failed health verification and was rolled back." >&2
  exit 1
fi

readiness_ready=0
for attempt in $(seq 1 15); do
  if [[ "$(curl --silent --show-error --connect-timeout 2 --max-time 10 -H "Host: ${health_host}" -H "X-Twinevia-Readiness-Token: ${READINESS_TOKEN:-}" http://127.0.0.1:8100/ready || true)" == "READY" ]]; then
    readiness_ready=1
    break
  fi
  sleep 2
done
if [[ "${readiness_ready}" != "1" ]]; then
  if [[ -n "${old_release}" ]]; then
    atomic_link "${old_release}" "${CURRENT_LINK}"
    sudo systemctl restart "${RUNTIME_UNITS[@]}" || true
  else
    sudo systemctl stop "${RUNTIME_UNITS[@]}" || true
    if [[ -L "${CURRENT_LINK}" ]]; then
      sudo rm -- "${CURRENT_LINK}"
    fi
  fi
  promoted=0
  echo "Release ${release_id} failed readiness verification and was rolled back." >&2
  exit 1
fi

current_real="$(readlink -f "${CURRENT_LINK}")"
previous_real="$(readlink -f "${PREVIOUS_LINK}" 2>/dev/null || true)"
release_paths=()
while IFS= read -r release_path; do
  release_paths+=("${release_path}")
done < <(find "${RELEASES_DIR}" -mindepth 1 -maxdepth 1 -type d ! -name '.pending-*' -printf '%T@ %p\n' | sort -rn | awk '{print $2}')
retained=0
for candidate in "${release_paths[@]}"; do
  candidate_real="$(readlink -f "${candidate}")"
  if [[ "${candidate_real}" == "${current_real}" || "${candidate_real}" == "${previous_real}" ]]; then
    continue
  fi
  retained=$((retained + 1))
  if (( retained > RELEASE_RETENTION_COUNT - 2 )); then
    sudo rm -rf -- "${candidate_real}"
  fi
done

trap - ERR
trap - EXIT
echo "Release ${release_id} is active and ready. Previous release: ${old_release:-none}."
