#!/usr/bin/env bash
set -euo pipefail

STATUS_FILE=""
REPOSITORY=""
RUN_ID=""
ARTIFACT_NAME=""
APP_GROUP="${APP_GROUP:-twinevia}"

usage() {
  echo "Usage: $0 --status-file PATH --repository OWNER/REPOSITORY --run-id ID --artifact-name NAME" >&2
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --status-file)
      STATUS_FILE="${2:-}"
      shift 2
      ;;
    --repository)
      REPOSITORY="${2:-}"
      shift 2
      ;;
    --run-id)
      RUN_ID="${2:-}"
      shift 2
      ;;
    --artifact-name)
      ARTIFACT_NAME="${2:-}"
      shift 2
      ;;
    *)
      usage
      exit 1
      ;;
  esac
done

if [[ "${STATUS_FILE}" != /* || "${STATUS_FILE}" == "/" ]]; then
  echo "--status-file must be a dedicated absolute file path." >&2
  exit 1
fi
if [[ ! "${REPOSITORY}" =~ ^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$ ]]; then
  echo "--repository must use the owner/repository format." >&2
  exit 1
fi
if [[ ! "${RUN_ID}" =~ ^[0-9]+$ ]]; then
  echo "--run-id must be numeric." >&2
  exit 1
fi
if [[ ! "${ARTIFACT_NAME}" =~ ^[A-Za-z0-9_.-]+$ ]]; then
  echo "--artifact-name contains unsupported characters." >&2
  exit 1
fi
if [[ ! -r "${STATUS_FILE}" ]]; then
  echo "Backup status is unavailable: ${STATUS_FILE}" >&2
  exit 1
fi

status_directory="$(dirname "${STATUS_FILE}")"
status_temporary="$(mktemp "${status_directory}/.backup-status.XXXXXX")"
cleanup() {
  rm -f "${status_temporary}"
}
trap cleanup EXIT

BACKUP_STATUS_INPUT="${STATUS_FILE}" \
BACKUP_STATUS_OUTPUT="${status_temporary}" \
BACKUP_GITHUB_REPOSITORY="${REPOSITORY}" \
BACKUP_GITHUB_RUN_ID="${RUN_ID}" \
BACKUP_GITHUB_ARTIFACT_NAME="${ARTIFACT_NAME}" \
python3 -c '
import hashlib
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path

input_path = Path(os.environ["BACKUP_STATUS_INPUT"])
output_path = Path(os.environ["BACKUP_STATUS_OUTPUT"])
payload = json.loads(input_path.read_text(encoding="utf-8"))
if payload.get("offsite_mode") != "github_actions":
    raise SystemExit("backup status is not awaiting a GitHub Actions export")
if payload.get("offsite_verified") is True:
    raise SystemExit("backup status is already marked off-host verified")

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
if re.fullmatch(r"[0-9a-f]{64}\n?", hmac_path.read_text(encoding="ascii")) is None:
    raise SystemExit("backup HMAC sidecar is malformed")

repository = os.environ["BACKUP_GITHUB_REPOSITORY"]
run_id = os.environ["BACKUP_GITHUB_RUN_ID"]
payload["offsite_artifact_name"] = os.environ["BACKUP_GITHUB_ARTIFACT_NAME"]
payload["offsite_reference"] = f"https://github.com/{repository}/actions/runs/{run_id}#artifacts"
payload["offsite_verified"] = True
payload["offsite_verified_at"] = datetime.now(timezone.utc).isoformat()
output_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
'

install -o root -g "${APP_GROUP}" -m 0640 "${status_temporary}" "${STATUS_FILE}"
rm -f "${status_temporary}"
trap - EXIT
logger -t twinevia-backup -- "Confirmed GitHub Actions off-host backup run=${RUN_ID} artifact=${ARTIFACT_NAME}."
