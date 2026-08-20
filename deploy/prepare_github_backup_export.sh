#!/usr/bin/env bash
set -euo pipefail

APP_ROOT="${APP_ROOT:-/opt/twinevia-saas}"
ENV_FILE="${TWINEVIA_ENV_FILE:-${APP_ROOT}/.env}"
APP_GROUP="${APP_GROUP:-twinevia}"
BACKUP_SCRIPT=""
EXPORT_DIR=""
EXPORT_OWNER=""

usage() {
  echo "Usage: $0 --backup-script PATH --export-dir /tmp/twinevia-backup-export-ID --export-owner USER" >&2
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --backup-script)
      BACKUP_SCRIPT="${2:-}"
      shift 2
      ;;
    --export-dir)
      EXPORT_DIR="${2:-}"
      shift 2
      ;;
    --export-owner)
      EXPORT_OWNER="${2:-}"
      shift 2
      ;;
    *)
      usage
      exit 1
      ;;
  esac
done

if [[ "${BACKUP_SCRIPT}" != /* || ! -r "${BACKUP_SCRIPT}" ]]; then
  echo "--backup-script must be a readable absolute path." >&2
  exit 1
fi
if [[ ! "${EXPORT_DIR}" =~ ^/tmp/twinevia-backup-export-[0-9]+$ ]]; then
  echo "--export-dir must be a run-specific Twinevia path under /tmp." >&2
  exit 1
fi
if [[ ! "${EXPORT_OWNER}" =~ ^[a-z_][a-z0-9_-]*$ ]] || ! id -u "${EXPORT_OWNER}" >/dev/null 2>&1; then
  echo "--export-owner must identify an existing local user." >&2
  exit 1
fi
if [[ ! -r "${ENV_FILE}" ]]; then
  echo "Production environment file is unavailable: ${ENV_FILE}" >&2
  exit 1
fi
if [[ -e "${EXPORT_DIR}" ]]; then
  echo "Export directory already exists: ${EXPORT_DIR}" >&2
  exit 1
fi

read_env_value() {
  local key="$1"
  awk -v key="${key}" '
    $0 ~ ("^" key "=") { value = substr($0, length(key) + 2) }
    END { print value }
  ' "${ENV_FILE}"
}

backup_status_file="$(read_env_value "BACKUP_STATUS_FILE")"
if [[ "${backup_status_file}" != /* || "${backup_status_file}" == "/" ]]; then
  echo "BACKUP_STATUS_FILE must be a dedicated absolute file path." >&2
  exit 1
fi

BACKUP_ARTIFACT_EXPORT=1 \
APP_ROOT="${APP_ROOT}" \
APP_GROUP="${APP_GROUP}" \
TWINEVIA_ENV_FILE="${ENV_FILE}" \
bash "${BACKUP_SCRIPT}"

export_owner_uid="$(id -u "${EXPORT_OWNER}")"
export_owner_gid="$(id -g "${EXPORT_OWNER}")"
install -d -o "${export_owner_uid}" -g "${export_owner_gid}" -m 0700 "${EXPORT_DIR}"
BACKUP_STATUS_INPUT="${backup_status_file}" \
BACKUP_EXPORT_DIRECTORY="${EXPORT_DIR}" \
BACKUP_EXPORT_UID="${export_owner_uid}" \
BACKUP_EXPORT_GID="${export_owner_gid}" \
python3 -c '
import hashlib
import json
import os
import shutil
from pathlib import Path

status_path = Path(os.environ["BACKUP_STATUS_INPUT"])
export_directory = Path(os.environ["BACKUP_EXPORT_DIRECTORY"])
uid = int(os.environ["BACKUP_EXPORT_UID"])
gid = int(os.environ["BACKUP_EXPORT_GID"])
payload = json.loads(status_path.read_text(encoding="utf-8"))
if payload.get("offsite_mode") != "github_actions" or payload.get("offsite_verified") is True:
    raise SystemExit("backup status is not awaiting a GitHub Actions export")

archive_path = Path(str(payload["local_path"]))
hmac_path = Path(str(payload["local_hmac_path"]))
if not archive_path.is_file() or not hmac_path.is_file():
    raise SystemExit("local encrypted archive or HMAC sidecar is missing")

digest = hashlib.sha256()
with archive_path.open("rb") as archive_handle:
    for block in iter(lambda: archive_handle.read(1024 * 1024), b""):
        digest.update(block)
if digest.hexdigest() != str(payload.get("encrypted_sha256") or ""):
    raise SystemExit("local encrypted archive SHA-256 does not match backup status")

copied_paths = []
for source_path in (archive_path, hmac_path):
    destination_path = export_directory / source_path.name
    shutil.copy2(source_path, destination_path)
    copied_paths.append(destination_path)

status_copy = export_directory / "backup-status.json"
shutil.copy2(status_path, status_copy)
copied_paths.append(status_copy)
manifest_path = export_directory / "artifact-manifest.json"
manifest_path.write_text(
    json.dumps(
        {
            "archive": archive_path.name,
            "encrypted_sha256": payload["encrypted_sha256"],
            "hmac": hmac_path.name,
            "offsite_mode": payload["offsite_mode"],
        },
        indent=2,
        sort_keys=True,
    )
    + "\n",
    encoding="utf-8",
)
copied_paths.append(manifest_path)
for copied_path in copied_paths:
    copied_path.chmod(0o600)
    os.chown(copied_path, uid, gid)
'

logger -t twinevia-backup -- "Prepared encrypted backup export at ${EXPORT_DIR}."
