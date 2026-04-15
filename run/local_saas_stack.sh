#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENV_DIR="${ROOT}/venv"
PYTHON_BIN="${VENV_DIR}/bin/python"
REQUIRED_PYTHON="3.11"

PORT="${LOCAL_SAAS_PORT:-5000}"
BASE_URL="${LOCAL_SAAS_BASE_URL:-http://127.0.0.1:${PORT}}"
WEBHOOK_FORWARD_URL="${LOCAL_SAAS_STRIPE_FORWARD_URL:-${BASE_URL}/webhooks/stripe}"
OPEN_BROWSER=1
SEED_DEMO=1
RESET_DEMO=1

SEED_ARGS=()
APP_PID=""
WORKER_PID=""
STRIPE_PID=""
STRIPE_TAIL_PID=""
SCHEDULER_PID=""
STRIPE_LOG=""

usage() {
  cat <<'EOF'
Usage: ./run/local_saas_stack.sh [options]

Options:
  --no-seed                      Skip demo seeding entirely.
  --keep-data                    Seed without resetting the SQLite database first.
  --no-open                      Do not open the login page automatically.
  --live-from-number NUMBER      Assign one live Twilio sender to Twinevia Internal.
  --live-messaging-service-sid SID
                                 Assign the paired MG... Messaging Service SID.
  -h, --help                     Show this help.

Environment overrides:
  LOCAL_SAAS_PORT                Local Flask port (default: 5000)
  LOCAL_SAAS_BASE_URL            Base URL used by the app (default: http://127.0.0.1:$LOCAL_SAAS_PORT)
  LOCAL_SAAS_STRIPE_FORWARD_URL  Stripe forward target (default: $LOCAL_SAAS_BASE_URL/webhooks/stripe)
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --no-seed)
      SEED_DEMO=0
      shift
      ;;
    --keep-data)
      RESET_DEMO=0
      shift
      ;;
    --no-open)
      OPEN_BROWSER=0
      shift
      ;;
    --live-from-number)
      if [[ $# -lt 2 ]]; then
        echo "ERROR: --live-from-number requires a value." >&2
        exit 1
      fi
      SEED_ARGS+=("--live-from-number" "$2")
      shift 2
      ;;
    --live-messaging-service-sid)
      if [[ $# -lt 2 ]]; then
        echo "ERROR: --live-messaging-service-sid requires a value." >&2
        exit 1
      fi
      SEED_ARGS+=("--live-messaging-service-sid" "$2")
      shift 2
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "ERROR: Unknown option: $1" >&2
      usage >&2
      exit 1
      ;;
  esac
done

if [[ ! -x "${PYTHON_BIN}" ]]; then
  echo "ERROR: venv not found at ${PYTHON_BIN}." >&2
  echo "Run: ./run/setup.sh" >&2
  exit 1
fi

VENV_PYTHON_VERSION="$("${PYTHON_BIN}" -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')"
if [[ "${VENV_PYTHON_VERSION}" != "${REQUIRED_PYTHON}" ]]; then
  echo "ERROR: venv uses Python ${VENV_PYTHON_VERSION}; expected ${REQUIRED_PYTHON}." >&2
  echo "Recreate it with: rm -rf ${VENV_DIR} && ./run/setup.sh" >&2
  exit 1
fi

require_command() {
  if ! command -v "$1" >/dev/null 2>&1; then
    echo "ERROR: required command not found: $1" >&2
    exit 1
  fi
}

check_redis() {
  if command -v redis-cli >/dev/null 2>&1; then
    redis-cli -u "${REDIS_URL:-redis://localhost:6379/0}" ping >/dev/null 2>&1
    return $?
  fi

  "${PYTHON_BIN}" - <<'PY' >/dev/null 2>&1
import os
import redis

url = os.environ.get("REDIS_URL", "redis://localhost:6379/0")
redis.Redis.from_url(url).ping()
PY
}

cleanup_pid() {
  local pid="$1"
  if [[ -n "${pid}" ]] && kill -0 "${pid}" >/dev/null 2>&1; then
    kill "${pid}" >/dev/null 2>&1 || true
    wait "${pid}" >/dev/null 2>&1 || true
  fi
}

cleanup() {
  echo
  echo "Shutting down local SaaS stack..."
  cleanup_pid "${SCHEDULER_PID}"
  cleanup_pid "${STRIPE_TAIL_PID}"
  cleanup_pid "${STRIPE_PID}"
  cleanup_pid "${APP_PID}"
  cleanup_pid "${WORKER_PID}"
  if [[ -n "${STRIPE_LOG}" && -f "${STRIPE_LOG}" ]]; then
    rm -f "${STRIPE_LOG}"
  fi
}
trap cleanup EXIT INT TERM

require_command stripe

cd "${ROOT}"

if [[ -f "${ROOT}/.env" ]]; then
  set -a
  # shellcheck disable=SC1091
  source "${ROOT}/.env"
  set +a
fi

export SAAS_BASE_URL="${BASE_URL}"

echo "Starting Redis..."
brew services start redis >/dev/null 2>&1 || true

if ! check_redis; then
  echo "ERROR: Redis is not reachable at REDIS_URL=${REDIS_URL:-redis://localhost:6379/0}" >&2
  exit 1
fi

if [[ "${SEED_DEMO}" == "1" ]]; then
  echo "Seeding demo data..."
  seed_command=("${ROOT}/run/seed_demo_saas.sh")
  if [[ "${RESET_DEMO}" == "1" ]]; then
    seed_command+=("--reset")
  fi
  if [[ ${#SEED_ARGS[@]} -gt 0 ]]; then
    seed_command+=("${SEED_ARGS[@]}")
  fi
  "${seed_command[@]}"
fi

echo "Starting Stripe webhook forwarding..."
STRIPE_LOG="$(mktemp -t local-saas-stripe.XXXXXX.log)"
stripe listen --forward-to "${WEBHOOK_FORWARD_URL}" >"${STRIPE_LOG}" 2>&1 &
STRIPE_PID="$!"
tail -n +1 -f "${STRIPE_LOG}" &
STRIPE_TAIL_PID="$!"

echo "Waiting for Stripe signing secret..."
for _ in $(seq 1 60); do
  if ! kill -0 "${STRIPE_PID}" >/dev/null 2>&1; then
    echo "ERROR: stripe listen exited before a signing secret was captured." >&2
    exit 1
  fi
  STRIPE_SECRET="$(grep -Eo 'whsec_[A-Za-z0-9]+' "${STRIPE_LOG}" | tail -n 1 || true)"
  if [[ -n "${STRIPE_SECRET}" ]]; then
    export STRIPE_WEBHOOK_SECRET="${STRIPE_SECRET}"
    break
  fi
  sleep 1
done

if [[ -z "${STRIPE_WEBHOOK_SECRET:-}" ]]; then
  echo "ERROR: could not capture Stripe webhook signing secret from stripe listen output." >&2
  exit 1
fi

echo "Captured Stripe webhook secret for this session."

echo "Starting RQ worker..."
"${ROOT}/run/worker.sh" &
WORKER_PID="$!"

echo "Starting app server..."
"${PYTHON_BIN}" -m flask --app wsgi:app run --debug --host 127.0.0.1 --port "${PORT}" &
APP_PID="$!"

echo "Waiting for app to boot..."
for _ in $(seq 1 60); do
  if curl -fsS "${BASE_URL}/login" >/dev/null 2>&1; then
    break
  fi
  sleep 1
done

if ! curl -fsS "${BASE_URL}/login" >/dev/null 2>&1; then
  echo "ERROR: app did not become ready at ${BASE_URL}/login" >&2
  exit 1
fi

echo "Starting scheduler..."
SCHEDULER_ENABLED=1 SCHEDULER_RUNNER=1 "${PYTHON_BIN}" -m app.scheduler_runner &
SCHEDULER_PID="$!"

if [[ "${OPEN_BROWSER}" == "1" ]] && command -v open >/dev/null 2>&1; then
  echo "Opening app..."
  open "${BASE_URL}/login"
fi

echo
echo "Local SaaS stack is running."
echo "App:       ${BASE_URL}"
echo "Webhook:   ${WEBHOOK_FORWARD_URL}"
echo "App PID:   ${APP_PID}"
echo "Worker PID:${WORKER_PID}"
echo "Stripe PID:${STRIPE_PID}"
echo "Sched PID: ${SCHEDULER_PID}"
echo
echo "Press Ctrl+C to stop everything."

wait
