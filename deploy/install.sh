#!/usr/bin/env bash
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DBDOCTOR_SRC="${REPO_ROOT}/bin/dbdoctor"
DBDOCTOR_DEST="${DBDOCTOR_DEST:-/usr/local/bin/dbdoctor}"

if [[ ! -f "${DBDOCTOR_SRC}" ]]; then
  echo "ERROR: ${DBDOCTOR_SRC} not found. Did you clone the repo to ${REPO_ROOT}?" >&2
  exit 1
fi

sudo install -m 0755 "${DBDOCTOR_SRC}" "${DBDOCTOR_DEST}"

echo "Installed dbdoctor to ${DBDOCTOR_DEST}"
