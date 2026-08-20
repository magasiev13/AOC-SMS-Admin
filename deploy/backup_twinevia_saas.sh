#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP_ROOT="${APP_ROOT:-$(cd "${SCRIPT_DIR}/.." && pwd)}"
ENV_FILE="${TWINEVIA_ENV_FILE:-${APP_ROOT}/.env}"
APP_GROUP="${APP_GROUP:-twinevia}"
STAGING_DIR="$(mktemp -d)"
ARCHIVE_TAR="${STAGING_DIR}/twinevia-backup.tar"
VERIFY_TAR="${STAGING_DIR}/twinevia-backup-verify.tar"

cleanup() {
  rm -rf "${STAGING_DIR}"
}
trap cleanup EXIT

read_env_value() {
  local key="$1"
  awk -v key="${key}" '
    $0 ~ ("^" key "=") { value = substr($0, length(key) + 2) }
    END { print value }
  ' "${ENV_FILE}"
}

load_env_value() {
  local key="$1"
  if declare -p "${key}" >/dev/null 2>&1; then
    return
  fi
  if [[ -r "${ENV_FILE}" ]]; then
    printf -v "${key}" '%s' "$(read_env_value "${key}")"
  fi
}

for env_key in \
  DATABASE_URL \
  BACKUP_LOCAL_DIR \
  BACKUP_OFFSITE_MODE \
  BACKUP_OFFSITE_DESTINATION \
  BACKUP_ENCRYPTION_PASSPHRASE_FILE \
  BACKUP_STATUS_FILE \
  BACKUP_RETENTION_DAYS \
  BACKUP_MAX_AGE_HOURS \
  OPERATIONS_GITHUB_REPOSITORY \
  ALERT_WEBHOOK_URL; do
  load_env_value "${env_key}"
done

send_alert() {
  local message="$1"
  if [[ -z "${ALERT_WEBHOOK_URL:-}" ]]; then
    return
  fi
  ALERT_MESSAGE="${message}" python3 -c 'import json, os; print(json.dumps({"text": os.environ["ALERT_MESSAGE"]}))' \
    | curl --fail --silent --show-error --max-time 10 \
      -H 'Content-Type: application/json' \
      --data-binary @- \
      "${ALERT_WEBHOOK_URL}" >/dev/null
}

handle_error() {
  local exit_code=$?
  trap - ERR
  local message="Twinevia encrypted backup failed on $(hostname) for release ${APP_RELEASE_ID:-unknown}."
  logger -t twinevia-backup -- "${message}"
  send_alert "${message}" || true
  exit "${exit_code}"
}
trap handle_error ERR

require_command() {
  local command_name="$1"
  if ! command -v "${command_name}" >/dev/null 2>&1; then
    echo "Required command is unavailable: ${command_name}" >&2
    exit 1
  fi
}

validate_backup_directory() {
  local directory="$1"
  local label="$2"
  if [[ "${directory}" != /* || "${directory}" == "/" || "${directory}" == "/var" || "${directory}" == "/var/backups" ]]; then
    echo "${label} must be a dedicated absolute directory, got '${directory}'." >&2
    exit 1
  fi
}

for command_name in pg_dump openssl tar sha256sum; do
  require_command "${command_name}"
done

: "${DATABASE_URL:?DATABASE_URL is required for PostgreSQL backup}"
: "${BACKUP_LOCAL_DIR:?BACKUP_LOCAL_DIR is required}"
: "${BACKUP_OFFSITE_MODE:?BACKUP_OFFSITE_MODE is required}"
: "${BACKUP_ENCRYPTION_PASSPHRASE_FILE:?BACKUP_ENCRYPTION_PASSPHRASE_FILE is required}"
: "${BACKUP_STATUS_FILE:?BACKUP_STATUS_FILE is required}"
: "${BACKUP_RETENTION_DAYS:?BACKUP_RETENTION_DAYS is required}"
: "${BACKUP_MAX_AGE_HOURS:?BACKUP_MAX_AGE_HOURS is required}"

case "${BACKUP_OFFSITE_MODE}" in
  github_actions)
    : "${OPERATIONS_GITHUB_REPOSITORY:?OPERATIONS_GITHUB_REPOSITORY is required for GitHub Actions backups}"
    if [[ -n "${BACKUP_OFFSITE_DESTINATION:-}" ]]; then
      echo "BACKUP_OFFSITE_DESTINATION must be empty when BACKUP_OFFSITE_MODE=github_actions." >&2
      exit 1
    fi
    ;;
  mounted)
    : "${BACKUP_OFFSITE_DESTINATION:?BACKUP_OFFSITE_DESTINATION is required for mounted backups}"
    require_command findmnt
    ;;
  *)
    echo "BACKUP_OFFSITE_MODE must be github_actions or mounted." >&2
    exit 1
    ;;
esac

if [[ "${DATABASE_URL}" != postgresql* ]]; then
  echo "DATABASE_URL must reference PostgreSQL." >&2
  exit 1
fi
pg_database_url="$(DATABASE_URL_INPUT="${DATABASE_URL}" python3 -c '
import os
from urllib.parse import quote, unquote, urlsplit, urlunsplit

parsed = urlsplit(os.environ["DATABASE_URL_INPUT"])
host = parsed.hostname or ""
if ":" in host:
    host = f"[{host}]"
user = quote(unquote(parsed.username or ""), safe="")
netloc = f"{user}@{host}" if user else host
if parsed.port is not None:
    netloc = f"{netloc}:{parsed.port}"
print(urlunsplit(("postgresql", netloc, parsed.path, parsed.query, parsed.fragment)))
')"
pg_password="$(DATABASE_URL_INPUT="${DATABASE_URL}" python3 -c '
import os
from urllib.parse import unquote, urlsplit

print(unquote(urlsplit(os.environ["DATABASE_URL_INPUT"]).password or ""))
')"
if [[ ! -r "${BACKUP_ENCRYPTION_PASSPHRASE_FILE}" || ! -s "${BACKUP_ENCRYPTION_PASSPHRASE_FILE}" ]]; then
  echo "Backup passphrase file is missing, unreadable, or empty." >&2
  exit 1
fi
if [[ ! "${BACKUP_RETENTION_DAYS}" =~ ^[0-9]+$ ]] || (( BACKUP_RETENTION_DAYS < 7 || BACKUP_RETENTION_DAYS > 365 )); then
  echo "BACKUP_RETENTION_DAYS must be between 7 and 365." >&2
  exit 1
fi
if [[ ! "${BACKUP_MAX_AGE_HOURS}" =~ ^[0-9]+$ ]] || (( BACKUP_MAX_AGE_HOURS < 1 || BACKUP_MAX_AGE_HOURS > 168 )); then
  echo "BACKUP_MAX_AGE_HOURS must be between 1 and 168." >&2
  exit 1
fi

validate_backup_directory "${BACKUP_LOCAL_DIR}" "BACKUP_LOCAL_DIR"
install -d -m 0700 "${BACKUP_LOCAL_DIR}"
if [[ "${BACKUP_OFFSITE_MODE}" == "mounted" ]]; then
  validate_backup_directory "${BACKUP_OFFSITE_DESTINATION}" "BACKUP_OFFSITE_DESTINATION"
  if [[ "${BACKUP_LOCAL_DIR}" == "${BACKUP_OFFSITE_DESTINATION}" ]]; then
    echo "Local and off-host backup directories must be different." >&2
    exit 1
  fi
  install -d -m 0700 "${BACKUP_OFFSITE_DESTINATION}"
  offsite_filesystem="$(findmnt -n -o FSTYPE -T "${BACKUP_OFFSITE_DESTINATION}")"
  offsite_source="$(findmnt -n -o SOURCE -T "${BACKUP_OFFSITE_DESTINATION}")"
  case "${offsite_filesystem}" in
    nfs|nfs4|cifs|smb3|fuse.sshfs|fuse.rclone) ;;
    *)
      echo "BACKUP_OFFSITE_DESTINATION must be a supported remote filesystem, got '${offsite_filesystem}'." >&2
      exit 1
      ;;
  esac
  if [[ "${offsite_source}" != *:* && "${offsite_source}" != //* ]]; then
    echo "BACKUP_OFFSITE_DESTINATION does not identify a remote filesystem source." >&2
    exit 1
  fi
fi

if [[ "${BACKUP_OFFSITE_MODE}" == "github_actions" && "${BACKUP_ARTIFACT_EXPORT:-0}" != "1" ]]; then
  BACKUP_STATUS_PATH="${BACKUP_STATUS_FILE}" \
  BACKUP_MAX_AGE="${BACKUP_MAX_AGE_HOURS}" \
  python3 -c '
import json
import os
from datetime import datetime, timezone
from pathlib import Path

payload = json.loads(Path(os.environ["BACKUP_STATUS_PATH"]).read_text(encoding="utf-8"))
completed_at = datetime.fromisoformat(str(payload["completed_at"]))
if completed_at.tzinfo is None:
    completed_at = completed_at.replace(tzinfo=timezone.utc)
age_hours = (datetime.now(timezone.utc) - completed_at).total_seconds() / 3600
if payload.get("offsite_mode") != "github_actions" or payload.get("offsite_verified") is not True:
    raise SystemExit("latest backup lacks verified GitHub Actions off-host proof")
if not str(payload.get("offsite_reference") or "").startswith("https://github.com/"):
    raise SystemExit("latest backup has an invalid GitHub Actions reference")
if age_hours < 0 or age_hours > int(os.environ["BACKUP_MAX_AGE"]):
    raise SystemExit(f"latest verified off-host backup is {age_hours:.1f} hours old")
'
  logger -t twinevia-backup -- "Verified the latest GitHub Actions off-host backup status."
  exit 0
fi

timestamp="$(date -u +%Y%m%dT%H%M%SZ)"
release_id="${APP_RELEASE_ID:-$(basename "$(readlink -f "${APP_ROOT}")")}"
release_id="$(printf '%s' "${release_id}" | tr -c 'A-Za-z0-9._-' '_')"
archive_name="twinevia-${timestamp}-${release_id}.tar.enc"
local_archive="${BACKUP_LOCAL_DIR}/${archive_name}"
offsite_archive=""
local_hmac="${local_archive}.hmac"
offsite_hmac=""
if [[ "${BACKUP_OFFSITE_MODE}" == "mounted" ]]; then
  offsite_archive="${BACKUP_OFFSITE_DESTINATION}/${archive_name}"
  offsite_hmac="${offsite_archive}.hmac"
fi
payload_dir="${STAGING_DIR}/payload"
install -d -m 0700 "${payload_dir}/database" "${payload_dir}/config" "${payload_dir}/host"

PGPASSWORD="${pg_password}" pg_dump \
  --format=custom \
  --compress=9 \
  --no-owner \
  --no-acl \
  --file="${payload_dir}/database/postgresql.dump" \
  "${pg_database_url}"
unset pg_password

install -m 0600 "${ENV_FILE}" "${payload_dir}/config/app.env"
if [[ -d /etc/nginx ]]; then
  tar -cf "${payload_dir}/host/nginx.tar" -C /etc nginx
fi
systemd_units=(/etc/systemd/system/twinevia-saas*)
if [[ -e "${systemd_units[0]}" ]]; then
  systemd_unit_names=()
  for systemd_unit_path in "${systemd_units[@]}"; do
    systemd_unit_names+=("$(basename "${systemd_unit_path}")")
  done
  tar -cf "${payload_dir}/host/systemd-units.tar" -C /etc/systemd/system "${systemd_unit_names[@]}"
fi

release_root="$(readlink -f "${APP_ROOT}")"
tar \
  --exclude='./venv' \
  --exclude='./.git' \
  --exclude='./releases' \
  --exclude='./current' \
  --exclude='./previous' \
  --exclude='./instance' \
  --exclude='./node_modules' \
  --exclude='./output' \
  --exclude='./.env' \
  -cf "${payload_dir}/deployed-application.tar" \
  -C "${release_root}" .

BACKUP_RELEASE_ID="${release_id}" \
BACKUP_CREATED_AT="${timestamp}" \
BACKUP_METADATA_PATH="${payload_dir}/metadata.json" \
python3 -c '
import json
import os
from pathlib import Path

Path(os.environ["BACKUP_METADATA_PATH"]).write_text(
    json.dumps(
        {
            "created_at": os.environ["BACKUP_CREATED_AT"],
            "release_id": os.environ["BACKUP_RELEASE_ID"],
            "database_format": "PostgreSQL custom",
            "provider_secrets": "encrypted values are contained in the PostgreSQL dump",
        },
        indent=2,
        sort_keys=True,
    ) + "\n",
    encoding="utf-8",
)
'

(
  cd "${payload_dir}"
  find . -type f ! -name manifest.sha256 -print0 \
    | sort -z \
    | xargs -0 sha256sum > manifest.sha256
)
tar -cf "${ARCHIVE_TAR}" -C "${payload_dir}" .
openssl enc \
  -aes-256-cbc \
  -pbkdf2 \
  -iter 250000 \
  -salt \
  -pass "file:${BACKUP_ENCRYPTION_PASSPHRASE_FILE}" \
  -in "${ARCHIVE_TAR}" \
  -out "${local_archive}"
chmod 0600 "${local_archive}"

BACKUP_ARCHIVE_PATH="${local_archive}" \
BACKUP_HMAC_PATH="${local_hmac}" \
BACKUP_PASSPHRASE_PATH="${BACKUP_ENCRYPTION_PASSPHRASE_FILE}" \
python3 -c '
import hashlib
import hmac
import os
from pathlib import Path

archive_path = Path(os.environ["BACKUP_ARCHIVE_PATH"])
passphrase = Path(os.environ["BACKUP_PASSPHRASE_PATH"]).read_bytes()
authentication_key = hashlib.pbkdf2_hmac(
    "sha256",
    passphrase,
    b"twinevia-backup-authentication-v1",
    250000,
    dklen=32,
)
digest = hmac.new(authentication_key, digestmod=hashlib.sha256)
with archive_path.open("rb") as archive_handle:
    for block in iter(lambda: archive_handle.read(1024 * 1024), b""):
        digest.update(block)
Path(os.environ["BACKUP_HMAC_PATH"]).write_text(digest.hexdigest() + "\n", encoding="ascii")
'
chmod 0600 "${local_hmac}"

openssl enc \
  -d \
  -aes-256-cbc \
  -pbkdf2 \
  -iter 250000 \
  -pass "file:${BACKUP_ENCRYPTION_PASSPHRASE_FILE}" \
  -in "${local_archive}" \
  -out "${VERIFY_TAR}"
tar -tf "${VERIFY_TAR}" >/dev/null

offsite_verified="false"
offsite_reference=""
if [[ "${BACKUP_OFFSITE_MODE}" == "mounted" ]]; then
  install -m 0600 "${local_archive}" "${offsite_archive}"
  install -m 0600 "${local_hmac}" "${offsite_hmac}"
  cmp --silent "${local_archive}" "${offsite_archive}"
  cmp --silent "${local_hmac}" "${offsite_hmac}"
  offsite_verified="true"
  offsite_reference="${offsite_archive}"
fi
encrypted_sha256="$(sha256sum "${local_archive}" | awk '{print $1}')"

status_dir="$(dirname "${BACKUP_STATUS_FILE}")"
install -d -o root -g "${APP_GROUP}" -m 0750 "${status_dir}"
status_tmp="${STAGING_DIR}/backup-status.json"
BACKUP_COMPLETED_AT="$(date -u +%Y-%m-%dT%H:%M:%S%z)" \
BACKUP_LOCAL_PATH="${local_archive}" \
BACKUP_OFFSITE_PATH="${offsite_archive}" \
BACKUP_LOCAL_HMAC_PATH="${local_hmac}" \
BACKUP_OFFSITE_HMAC_PATH="${offsite_hmac}" \
BACKUP_OFFSITE_MODE_VALUE="${BACKUP_OFFSITE_MODE}" \
BACKUP_OFFSITE_REFERENCE="${offsite_reference}" \
BACKUP_OFFSITE_VERIFIED="${offsite_verified}" \
BACKUP_SHA256="${encrypted_sha256}" \
BACKUP_RELEASE_ID="${release_id}" \
BACKUP_STATUS_OUTPUT="${status_tmp}" \
python3 -c '
import json
import os
from pathlib import Path

Path(os.environ["BACKUP_STATUS_OUTPUT"]).write_text(
    json.dumps(
        {
            "completed_at": os.environ["BACKUP_COMPLETED_AT"],
            "encrypted_sha256": os.environ["BACKUP_SHA256"],
            "local_path": os.environ["BACKUP_LOCAL_PATH"],
            "local_hmac_path": os.environ["BACKUP_LOCAL_HMAC_PATH"],
            "offsite_mode": os.environ["BACKUP_OFFSITE_MODE_VALUE"],
            "offsite_path": os.environ["BACKUP_OFFSITE_PATH"],
            "offsite_hmac_path": os.environ["BACKUP_OFFSITE_HMAC_PATH"],
            "offsite_reference": os.environ["BACKUP_OFFSITE_REFERENCE"],
            "offsite_verified": os.environ["BACKUP_OFFSITE_VERIFIED"] == "true",
            "release_id": os.environ["BACKUP_RELEASE_ID"],
        },
        indent=2,
        sort_keys=True,
    ) + "\n",
    encoding="utf-8",
)
'
install -o root -g "${APP_GROUP}" -m 0640 "${status_tmp}" "${BACKUP_STATUS_FILE}"
rm -f "${status_tmp}"

find "${BACKUP_LOCAL_DIR}" -maxdepth 1 -type f -name 'twinevia-*.tar.enc' -mtime "+${BACKUP_RETENTION_DAYS}" -delete
find "${BACKUP_LOCAL_DIR}" -maxdepth 1 -type f -name 'twinevia-*.tar.enc.hmac' -mtime "+${BACKUP_RETENTION_DAYS}" -delete
if [[ "${BACKUP_OFFSITE_MODE}" == "mounted" ]]; then
  find "${BACKUP_OFFSITE_DESTINATION}" -maxdepth 1 -type f -name 'twinevia-*.tar.enc' -mtime "+${BACKUP_RETENTION_DAYS}" -delete
  find "${BACKUP_OFFSITE_DESTINATION}" -maxdepth 1 -type f -name 'twinevia-*.tar.enc.hmac' -mtime "+${BACKUP_RETENTION_DAYS}" -delete
fi

logger -t twinevia-backup -- "Encrypted PostgreSQL backup completed release=${release_id} sha256=${encrypted_sha256} offsite_mode=${BACKUP_OFFSITE_MODE}."
