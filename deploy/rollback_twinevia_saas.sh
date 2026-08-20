#!/usr/bin/env bash
set -euo pipefail

APP_ROOT="${APP_ROOT:-/opt/twinevia-saas}"
APP_USER="${APP_USER:-twinevia}"
ENV_FILE="${TWINEVIA_ENV_FILE:-${APP_ROOT}/.env}"
RELEASES_DIR="${APP_ROOT}/releases"
CURRENT_LINK="${APP_ROOT}/current"
PREVIOUS_LINK="${APP_ROOT}/previous"
TARGET_RELEASE_ID="${1:-}"
RUNTIME_UNITS=(
  twinevia-saas.service
  twinevia-saas-worker.service
  twinevia-saas-scheduler.timer
  twinevia-saas-billing-reconcile.timer
  twinevia-saas-platform-restart-queue.timer
  twinevia-saas-a2p-reconcile.timer
  twinevia-saas-backup.timer
  twinevia-saas-readiness.timer
)

current_release="$(readlink -f "${CURRENT_LINK}" 2>/dev/null || true)"
original_previous="$(readlink -f "${PREVIOUS_LINK}" 2>/dev/null || true)"
if [[ -z "${current_release}" ]]; then
  echo "Current release link is missing." >&2
  exit 1
fi
if [[ -n "${TARGET_RELEASE_ID}" ]]; then
  target_release="$(readlink -f "${RELEASES_DIR}/${TARGET_RELEASE_ID}" 2>/dev/null || true)"
else
  target_release="$(readlink -f "${PREVIOUS_LINK}" 2>/dev/null || true)"
fi
if [[ -z "${target_release}" || ! -d "${target_release}" ]]; then
  echo "Rollback target does not exist." >&2
  exit 1
fi
case "${target_release}" in
  "${RELEASES_DIR}"/*) ;;
  *)
    echo "Rollback target is outside the release directory." >&2
    exit 1
    ;;
esac
if [[ "${target_release}" == "${current_release}" ]]; then
  echo "Rollback target is already active." >&2
  exit 1
fi

sudo -u "${APP_USER}" bash -lc "set -euo pipefail; cd \"${target_release}\"; set -a; source \"${ENV_FILE}\"; source \"${target_release}/.release.env\"; set +a; ./venv/bin/python -m app.saas_db --doctor"

atomic_link() {
  local target="$1"
  local link_path="$2"
  local temporary_link="${link_path}.rollback-$$"
  sudo ln -s "${target}" "${temporary_link}"
  sudo mv -Tf "${temporary_link}" "${link_path}"
}

read_env_value() {
  local key="$1"
  sudo awk -v key="${key}" '
    $0 ~ ("^" key "=") { value = substr($0, length(key) + 2) }
    END { print value }
  ' "${ENV_FILE}"
}

restore_original_release() {
  local exit_code=$?
  trap - ERR
  atomic_link "${current_release}" "${CURRENT_LINK}" || true
  if [[ -n "${original_previous}" && -d "${original_previous}" ]]; then
    atomic_link "${original_previous}" "${PREVIOUS_LINK}" || true
  elif [[ -L "${PREVIOUS_LINK}" ]]; then
    sudo rm -- "${PREVIOUS_LINK}" || true
  fi
  sudo systemctl restart "${RUNTIME_UNITS[@]}" || true
  echo "Rollback target failed verification; restored original release $(basename "${current_release}")." >&2
  exit "${exit_code}"
}

atomic_link "${target_release}" "${CURRENT_LINK}"
trap restore_original_release ERR
sudo systemctl restart "${RUNTIME_UNITS[@]}"

app_base_url="$(read_env_value "APP_BASE_URL")"
readiness_token="$(read_env_value "READINESS_TOKEN")"
health_host="$(APP_URL="${app_base_url}" python3 -c 'import os; from urllib.parse import urlsplit; print(urlsplit(os.environ["APP_URL"]).hostname or "")')"
if [[ -z "${health_host}" || -z "${readiness_token}" ]]; then
  echo "Rollback verification requires APP_BASE_URL and READINESS_TOKEN." >&2
  false
fi
for attempt in $(seq 1 20); do
  units_ready=1
  for runtime_unit in "${RUNTIME_UNITS[@]}"; do
    if ! sudo systemctl is-active --quiet "${runtime_unit}"; then
      units_ready=0
      break
    fi
  done
  health_response="$(curl --silent --show-error --connect-timeout 2 --max-time 5 -H "Host: ${health_host}" http://127.0.0.1:8100/health || true)"
  readiness_response="$(curl --silent --show-error --connect-timeout 2 --max-time 10 -H "Host: ${health_host}" -H "X-Twinevia-Readiness-Token: ${readiness_token}" http://127.0.0.1:8100/ready || true)"
  if [[ "${units_ready}" == "1" && "${health_response}" == "OK" && "${readiness_response}" == "READY" ]]; then
    atomic_link "${current_release}" "${PREVIOUS_LINK}"
    trap - ERR
    echo "Rollback completed. Active release: $(basename "${target_release}")."
    exit 0
  fi
  sleep 2
done

echo "Rollback target did not pass service, health, and readiness verification." >&2
false
