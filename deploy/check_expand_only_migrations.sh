#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP_ROOT="${APP_ROOT:-$(cd "${SCRIPT_DIR}/.." && pwd)}"
MIGRATIONS_DIR="${APP_ROOT}/app/saas_migrations"

if [[ ! -d "${MIGRATIONS_DIR}" ]]; then
  echo "SaaS migrations directory is missing: ${MIGRATIONS_DIR}" >&2
  exit 1
fi

destructive_pattern='DROP[[:space:]]+(TABLE|COLUMN|INDEX|CONSTRAINT)|TRUNCATE[[:space:]]+TABLE|DELETE[[:space:]]+FROM|ALTER[[:space:]]+TABLE[^;]*(RENAME|DROP)'
matches="$(grep -ERni --include='[0-9][0-9][0-9]_*.py' "${destructive_pattern}" "${MIGRATIONS_DIR}" || true)"
if [[ -n "${matches}" ]]; then
  echo "Refusing release: SaaS migrations must remain expand-only during the managed pilot." >&2
  echo "${matches}" >&2
  exit 1
fi

echo "SaaS migration safety check passed: no contract-stage SQL detected."
