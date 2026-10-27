#!/usr/bin/env bash
# Shared by root-operated install, release, recovery and deployment entrypoints.
# The Python supervisor owns the lock and original snapshot across nested scripts.

_TWINEVIA_RUNTIME_HELPER="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)/runtime_control.py"

runtime_units_output="$(python3 "${_TWINEVIA_RUNTIME_HELPER}" units)" || exit 1
RUNTIME_UNITS=()
while IFS= read -r runtime_unit; do RUNTIME_UNITS+=("${runtime_unit}"); done <<< "${runtime_units_output}"

runtime_timer_csv() {
  python3 "${_TWINEVIA_RUNTIME_HELPER}" units --kind timers | paste -sd, -
}

runtime_transaction() {
  local kind="$1"
  local script="$2"
  shift 2
  if [[ -z "${TWINEVIA_RUNTIME_STATE_FILE:-}" ]]; then
    local options=(--app-root "${APP_ROOT}" --kind "${kind}")
    if [[ -n "${TWINEVIA_RUNTIME_STATE_ROOT:-}" ]]; then
      options+=(--state-root "${TWINEVIA_RUNTIME_STATE_ROOT}")
    fi
    if [[ "${TWINEVIA_INITIALIZE_RUNTIME:-0}" == "1" ]]; then
      options+=(--fresh-install)
    fi
    # Production entrypoints already run as root. A second sudo here would
    # discard reviewed deployment/fresh-install options through env_reset.
    exec python3 "${_TWINEVIA_RUNTIME_HELPER}" transaction "${options[@]}" -- /bin/bash "${script}" "$@"
  fi
}

runtime_ctl() {
  local operation="$1"
  shift
  sudo -n python3 "${_TWINEVIA_RUNTIME_HELPER}" "${operation}" --state "${TWINEVIA_RUNTIME_STATE_FILE:?Runtime capture is required}" "$@"
}

runtime_verify_readiness() {
  local token="${READINESS_TOKEN:-}"
  local base="${APP_BASE_URL:-}"
  if [[ -z "${token}" ]]; then token="$(read_env_value READINESS_TOKEN)"; fi
  if [[ -z "${base}" ]]; then base="$(read_env_value APP_BASE_URL)"; fi
  printf '%s' "${token}" | sudo -n env APP_BASE_URL="${base}" python3 "${_TWINEVIA_RUNTIME_HELPER}" verify-readiness \
    --state "${TWINEVIA_RUNTIME_STATE_FILE:?Runtime capture is required}" --readiness-token-stdin "$@"
}

runtime_restore_release() {
  local target="$1"
  # Contain candidate timers and an accidentally running held oneshot before
  # changing source. Do not claim restoration after a failed containment step.
  runtime_ctl quiesce --rollback || return 1
  runtime_ctl verify-target --target "${target}" || return 1
  atomic_link "${target}" "${CURRENT_LINK}" || return 1
  runtime_ctl restore || return 1
}
