#!/usr/bin/env bash
set -euo pipefail

PATH=/usr/bin:/bin:/opt/sms-admin/venv/bin

if [ -f /opt/sms-admin/.env ]; then
  set -a
  . /opt/sms-admin/.env
  set +a
fi

: "${REDIS_URL:=redis://localhost:6379/0}"
: "${RQ_QUEUE_NAME:=sms}"

exec /opt/sms-admin/venv/bin/rq worker --url "$REDIS_URL" "$RQ_QUEUE_NAME"
