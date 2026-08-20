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

if [[ -f "${ENV_FILE}" ]]; then
  set -a
  source "${ENV_FILE}"
  set +a
fi

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

for command_name in pg_dump openssl tar sha256sum findmnt; do
  require_command "${command_name}"
done

: "${DATABASE_URL:?DATABASE_URL is required for PostgreSQL backup}"
: "${BACKUP_LOCAL_DIR:?BACKUP_LOCAL_DIR is required}"
: "${BACKUP_OFFSITE_DESTINATION:?BACKUP_OFFSITE_DESTINATION is required}"
: "${BACKUP_ENCRYPTION_PASSPHRASE_FILE:?BACKUP_ENCRYPTION_PASSPHRASE_FILE is required}"
: "${BACKUP_STATUS_FILE:?BACKUP_STATUS_FILE is required}"
: "${BACKUP_RETENTION_DAYS:?BACKUP_RETENTION_DAYS is required}"

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

validate_backup_directory "${BACKUP_LOCAL_DIR}" "BACKUP_LOCAL_DIR"
validate_backup_directory "${BACKUP_OFFSITE_DESTINATION}" "BACKUP_OFFSITE_DESTINATION"
if [[ "${BACKUP_LOCAL_DIR}" == "${BACKUP_OFFSITE_DESTINATION}" ]]; then
  echo "Local and off-host backup directories must be different." >&2
  exit 1
fi

install -d -m 0700 "${BACKUP_LOCAL_DIR}" "${BACKUP_OFFSITE_DESTINATION}"
local_device="$(stat -c '%d' "${BACKUP_LOCAL_DIR}")"
offsite_device="$(stat -c '%d' "${BACKUP_OFFSITE_DESTINATION}")"
offsite_mount="$(findmnt -n -o TARGET -T "${BACKUP_OFFSITE_DESTINATION}")"
if [[ "${local_device}" == "${offsite_device}" || "${offsite_mount}" == "/" ]]; then
  echo "BACKUP_OFFSITE_DESTINATION must be on a separately mounted filesystem." >&2
  exit 1
fi

timestamp="$(date -u +%Y%m%dT%H%M%SZ)"
release_id="${APP_RELEASE_ID:-$(basename "$(readlink -f "${APP_ROOT}")")}"
release_id="$(printf '%s' "${release_id}" | tr -c 'A-Za-z0-9._-' '_')"
archive_name="twinevia-${timestamp}-${release_id}.tar.enc"
local_archive="${BACKUP_LOCAL_DIR}/${archive_name}"
offsite_archive="${BACKUP_OFFSITE_DESTINATION}/${archive_name}"
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
  tar -cf "${payload_dir}/host/systemd-units.tar" -C /etc/systemd/system twinevia-saas*
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

openssl enc \
  -d \
  -aes-256-cbc \
  -pbkdf2 \
  -iter 250000 \
  -pass "file:${BACKUP_ENCRYPTION_PASSPHRASE_FILE}" \
  -in "${local_archive}" \
  -out "${VERIFY_TAR}"
tar -tf "${VERIFY_TAR}" >/dev/null

install -m 0600 "${local_archive}" "${offsite_archive}"
cmp --silent "${local_archive}" "${offsite_archive}"
encrypted_sha256="$(sha256sum "${local_archive}" | awk '{print $1}')"

status_dir="$(dirname "${BACKUP_STATUS_FILE}")"
install -d -o root -g "${APP_GROUP}" -m 0750 "${status_dir}"
status_tmp="${STAGING_DIR}/backup-status.json"
BACKUP_COMPLETED_AT="$(date -u +%Y-%m-%dT%H:%M:%S%z)" \
BACKUP_LOCAL_PATH="${local_archive}" \
BACKUP_OFFSITE_PATH="${offsite_archive}" \
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
            "offsite_path": os.environ["BACKUP_OFFSITE_PATH"],
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
find "${BACKUP_OFFSITE_DESTINATION}" -maxdepth 1 -type f -name 'twinevia-*.tar.enc' -mtime "+${BACKUP_RETENTION_DAYS}" -delete

logger -t twinevia-backup -- "Encrypted PostgreSQL backup completed release=${release_id} sha256=${encrypted_sha256} offsite=${offsite_archive}."
