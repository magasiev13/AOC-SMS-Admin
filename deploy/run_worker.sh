#!/usr/bin/env bash
set -euo pipefail

VENV_PY="/opt/sms-admin/venv/bin/python"

: "${REDIS_URL:=redis://localhost:6379/0}"
: "${RQ_QUEUE_NAME:=sms}"

exec "${VENV_PY}" -m rq worker --url "${REDIS_URL}" "${RQ_QUEUE_NAME}"
