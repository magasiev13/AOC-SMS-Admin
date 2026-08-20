#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP_ROOT="${APP_ROOT:-$(cd "${SCRIPT_DIR}/.." && pwd)}"
ENV_FILE="${TWINEVIA_ENV_FILE:-${APP_ROOT}/.env}"
APP_GROUP="${APP_GROUP:-twinevia}"
ARCHIVE_PATH=""
TARGET_DATABASE_URL="${RESTORE_DRILL_DATABASE_URL:-}"
PASSPHRASE_FILE=""
CONFIRMED_DATABASE_NAME="${RESTORE_DRILL_DATABASE_NAME:-}"
WORK_DIR="$(mktemp -d)"

cleanup() {
  rm -rf "${WORK_DIR}"
}
trap cleanup EXIT

usage() {
  echo "Usage: $0 --archive PATH --passphrase-file PATH [--target-database-url URL --confirm-isolated-database NAME]" >&2
  echo "RESTORE_DRILL_DATABASE_URL and RESTORE_DRILL_DATABASE_NAME may provide the target without exposing credentials in process arguments." >&2
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --archive)
      ARCHIVE_PATH="${2:-}"
      shift 2
      ;;
    --target-database-url)
      TARGET_DATABASE_URL="${2:-}"
      shift 2
      ;;
    --passphrase-file)
      PASSPHRASE_FILE="${2:-}"
      shift 2
      ;;
    --confirm-isolated-database)
      CONFIRMED_DATABASE_NAME="${2:-}"
      shift 2
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "Unknown argument: $1" >&2
      usage
      exit 1
      ;;
  esac
done

if [[ -f "${ENV_FILE}" ]]; then
  set -a
  source "${ENV_FILE}"
  set +a
fi
if [[ -z "${TARGET_DATABASE_URL}" ]]; then
  TARGET_DATABASE_URL="${RESTORE_DRILL_DATABASE_URL:-}"
fi
if [[ -z "${CONFIRMED_DATABASE_NAME}" ]]; then
  CONFIRMED_DATABASE_NAME="${RESTORE_DRILL_DATABASE_NAME:-}"
fi

if [[ -z "${ARCHIVE_PATH}" || -z "${TARGET_DATABASE_URL}" || -z "${PASSPHRASE_FILE}" || -z "${CONFIRMED_DATABASE_NAME}" ]]; then
  usage
  exit 1
fi
if [[ ! -r "${ARCHIVE_PATH}" || ! -s "${ARCHIVE_PATH}" ]]; then
  echo "Encrypted backup archive is missing or unreadable." >&2
  exit 1
fi
if [[ ! -r "${PASSPHRASE_FILE}" || ! -s "${PASSPHRASE_FILE}" ]]; then
  echo "Backup passphrase file is missing, unreadable, or empty." >&2
  exit 1
fi

for command_name in openssl tar sha256sum pg_restore psql; do
  if ! command -v "${command_name}" >/dev/null 2>&1; then
    echo "Required command is unavailable: ${command_name}" >&2
    exit 1
  fi
done

normalize_postgres_url() {
  local raw_url="$1"
  DATABASE_URL_INPUT="${raw_url}" python3 -c '
import os
from urllib.parse import urlsplit, urlunsplit

parsed = urlsplit(os.environ["DATABASE_URL_INPUT"])
if not parsed.scheme.startswith("postgresql"):
    raise SystemExit("database URL must use PostgreSQL")
print(urlunsplit(("postgresql", parsed.netloc, parsed.path, parsed.query, parsed.fragment)))
'
}

passwordless_postgres_url() {
  local raw_url="$1"
  DATABASE_URL_INPUT="${raw_url}" python3 -c '
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
'
}

postgres_password() {
  local raw_url="$1"
  DATABASE_URL_INPUT="${raw_url}" python3 -c '
import os
from urllib.parse import unquote, urlsplit

print(unquote(urlsplit(os.environ["DATABASE_URL_INPUT"]).password or ""))
'
}

target_pg_url="$(normalize_postgres_url "${TARGET_DATABASE_URL}")"
target_client_url="$(passwordless_postgres_url "${target_pg_url}")"
target_pg_password="$(postgres_password "${target_pg_url}")"
target_database_name="$(TARGET_DATABASE_URL="${target_pg_url}" python3 -c '
import os
from urllib.parse import unquote, urlsplit

name = unquote(urlsplit(os.environ["TARGET_DATABASE_URL"]).path.lstrip("/"))
print(name)
')"
if [[ -z "${target_database_name}" || "${target_database_name}" != "${CONFIRMED_DATABASE_NAME}" ]]; then
  echo "The confirmation name does not match the target database." >&2
  exit 1
fi
if [[ "${target_database_name}" == "postgres" || "${target_database_name}" == "template0" || "${target_database_name}" == "template1" ]]; then
  echo "Refusing to restore into a PostgreSQL administrative database." >&2
  exit 1
fi

target_identity="$(PGPASSWORD="${target_pg_password}" psql "${target_client_url}" -v ON_ERROR_STOP=1 -Atc "SELECT current_database() || '|' || COALESCE(inet_server_addr()::text, 'local') || '|' || inet_server_port()::text")"
if [[ -n "${DATABASE_URL:-}" ]]; then
  production_pg_url="$(normalize_postgres_url "${DATABASE_URL}")"
  production_client_url="$(passwordless_postgres_url "${production_pg_url}")"
  production_pg_password="$(postgres_password "${production_pg_url}")"
  production_identity="$(PGPASSWORD="${production_pg_password}" psql "${production_client_url}" -v ON_ERROR_STOP=1 -Atc "SELECT current_database() || '|' || COALESCE(inet_server_addr()::text, 'local') || '|' || inet_server_port()::text")"
  unset production_pg_password
  if [[ "${target_identity}" == "${production_identity}" ]]; then
    echo "Refusing to restore into the configured production database." >&2
    exit 1
  fi
fi

decrypted_tar="${WORK_DIR}/backup.tar"
restore_dir="${WORK_DIR}/restore"
install -d -m 0700 "${restore_dir}"
openssl enc \
  -d \
  -aes-256-cbc \
  -pbkdf2 \
  -iter 250000 \
  -pass "file:${PASSPHRASE_FILE}" \
  -in "${ARCHIVE_PATH}" \
  -out "${decrypted_tar}"
tar --no-same-owner --no-same-permissions -xf "${decrypted_tar}" -C "${restore_dir}"
(
  cd "${restore_dir}"
  sha256sum -c manifest.sha256
)

PGPASSWORD="${target_pg_password}" pg_restore \
  --exit-on-error \
  --clean \
  --if-exists \
  --no-owner \
  --no-acl \
  --dbname="${target_client_url}" \
  "${restore_dir}/database/postgresql.dump"

DATABASE_URL="${target_pg_url}" \
SAAS_MODE=1 \
FLASK_ENV=development \
FLASK_DEBUG=1 \
"${APP_ROOT}/venv/bin/python" -m app.saas_db --apply

DATABASE_URL="${target_pg_url}" \
SAAS_MODE=1 \
FLASK_ENV=development \
FLASK_DEBUG=1 \
"${APP_ROOT}/venv/bin/python" -m app.saas_db --doctor

required_table_count="$(PGPASSWORD="${target_pg_password}" psql "${target_client_url}" -v ON_ERROR_STOP=1 -Atc "SELECT COUNT(*) FROM information_schema.tables WHERE table_schema = 'public' AND table_name IN ('organizations','organization_subscriptions','organization_messaging_profiles','message_dispatch_attempts','external_webhook_deliveries')")"
unset target_pg_password
if [[ "${required_table_count}" != "5" ]]; then
  echo "Restored database is missing one or more managed-pilot tables." >&2
  exit 1
fi

: "${RESTORE_DRILL_STATUS_FILE:?RESTORE_DRILL_STATUS_FILE is required}"
status_dir="$(dirname "${RESTORE_DRILL_STATUS_FILE}")"
install -d -o root -g "${APP_GROUP}" -m 0750 "${status_dir}"
archive_sha256="$(sha256sum "${ARCHIVE_PATH}" | awk '{print $1}')"
status_tmp="${WORK_DIR}/restore-drill-status.json"
RESTORE_COMPLETED_AT="$(date -u +%Y-%m-%dT%H:%M:%S%z)" \
RESTORE_ARCHIVE_SHA256="${archive_sha256}" \
RESTORE_TARGET_IDENTITY="${target_identity}" \
RESTORE_STATUS_OUTPUT="${status_tmp}" \
python3 -c '
import json
import os
from pathlib import Path

Path(os.environ["RESTORE_STATUS_OUTPUT"]).write_text(
    json.dumps(
        {
            "archive_sha256": os.environ["RESTORE_ARCHIVE_SHA256"],
            "completed_at": os.environ["RESTORE_COMPLETED_AT"],
            "schema_ready": True,
            "target_identity": os.environ["RESTORE_TARGET_IDENTITY"],
        },
        indent=2,
        sort_keys=True,
    ) + "\n",
    encoding="utf-8",
)
'
install -o root -g "${APP_GROUP}" -m 0640 "${status_tmp}" "${RESTORE_DRILL_STATUS_FILE}"

echo "Isolated restore drill succeeded for ${target_identity}; archive SHA-256 ${archive_sha256}."
