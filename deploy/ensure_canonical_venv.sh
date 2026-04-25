#!/usr/bin/env bash
set -euo pipefail

ACTION="${1:-ensure}"
STATE_FILE="${2:-}"
APP_ROOT="${APP_ROOT:-/opt/twinevia-saas}"
APP_USER="${APP_USER:-twinevia}"
APP_GROUP="${APP_GROUP:-${APP_USER}}"
REQUIRED_PYTHON="${REQUIRED_PYTHON:-3.11}"
VENV_DIR="${APP_ROOT}/venv"
REQUIREMENTS_FILE="${REQUIREMENTS_FILE:-${APP_ROOT}/requirements.txt}"

write_state() {
  local promoted="$1"
  local backup_path="${2:-}"
  if [[ -z "${STATE_FILE}" ]]; then
    return
  fi
  {
    printf 'VENV_PROMOTED=%s\n' "${promoted}"
    printf 'VENV_BACKUP_PATH=%s\n' "${backup_path}"
  } > "${STATE_FILE}"
}

run_as_app_user() {
  sudo -u "${APP_USER}" "$@"
}

python_version_for() {
  local python_bin="$1"
  "${python_bin}" -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")'
}

text_file_contains() {
  local file="$1"
  local pattern="$2"
  [[ -f "${file}" ]] && grep -Iq . "${file}" && grep -qF "${pattern}" "${file}"
}

rewrite_text_reference() {
  local root="$1"
  local from_path="$2"
  local to_path="$3"
  local file

  if [[ -f "${root}/pyvenv.cfg" ]] && text_file_contains "${root}/pyvenv.cfg" "${from_path}"; then
    sudo sed -i "s|${from_path}|${to_path}|g" "${root}/pyvenv.cfg"
  fi

  if [[ -d "${root}/bin" ]]; then
    while IFS= read -r -d '' file; do
      if text_file_contains "${file}" "${from_path}"; then
        sudo sed -i "s|${from_path}|${to_path}|g" "${file}"
      fi
    done < <(find "${root}/bin" -maxdepth 1 -type f -print0)
  fi
}

validate_basic_venv() {
  local root="$1"
  local version

  if [[ ! -x "${root}/bin/python" ]]; then
    echo "[venv] ERROR: Python executable not found at ${root}/bin/python" >&2
    return 1
  fi

  version="$(python_version_for "${root}/bin/python")"
  if [[ "${version}" != "${REQUIRED_PYTHON}" ]]; then
    echo "[venv] ERROR: ${root}/bin/python is Python ${version}; expected ${REQUIRED_PYTHON}." >&2
    return 1
  fi
}

validate_canonical_venv() {
  local root="$1"
  local script

  validate_basic_venv "${root}"

  if [[ -f "${root}/pyvenv.cfg" ]]; then
    if grep -qF "${APP_ROOT}/venv.next-" "${root}/pyvenv.cfg"; then
      echo "[venv] ERROR: ${root}/pyvenv.cfg still references a temporary venv path." >&2
      return 1
    fi
    if ! grep -qF "${VENV_DIR}" "${root}/pyvenv.cfg"; then
      echo "[venv] ERROR: ${root}/pyvenv.cfg does not reference ${VENV_DIR}." >&2
      return 1
    fi
  fi

  for script in rq gunicorn pip flask; do
    if [[ ! -f "${root}/bin/${script}" ]]; then
      continue
    fi
    if head -n 1 "${root}/bin/${script}" | grep -qF "${APP_ROOT}/venv.next-"; then
      echo "[venv] ERROR: ${root}/bin/${script} shebang still references a temporary venv path." >&2
      return 1
    fi
  done
}

venv_needs_rebuild() {
  if [[ ! -x "${VENV_DIR}/bin/python" ]]; then
    return 0
  fi
  if [[ "$(python_version_for "${VENV_DIR}/bin/python")" != "${REQUIRED_PYTHON}" ]]; then
    return 0
  fi
  if [[ -f "${VENV_DIR}/pyvenv.cfg" ]] && grep -qF "${APP_ROOT}/venv.next-" "${VENV_DIR}/pyvenv.cfg"; then
    return 0
  fi
  if [[ -d "${VENV_DIR}/bin" ]] && grep -RIlF "${APP_ROOT}/venv.next-" "${VENV_DIR}/bin" >/dev/null 2>&1; then
    return 0
  fi
  return 1
}

unique_backup_path() {
  local timestamp
  local candidate
  timestamp="$(date -u +%Y%m%d%H%M%S)"
  candidate="${VENV_DIR}.backup-${timestamp}"
  if [[ -e "${candidate}" ]]; then
    candidate="${candidate}-$$"
  fi
  printf '%s\n' "${candidate}"
}

build_next_venv() {
  local next_venv="$1"

  if ! command -v python3.11 >/dev/null 2>&1; then
    echo "[venv] ERROR: python3.11 is unavailable." >&2
    return 1
  fi
  if [[ ! -f "${REQUIREMENTS_FILE}" ]]; then
    echo "[venv] ERROR: requirements file not found at ${REQUIREMENTS_FILE}." >&2
    return 1
  fi

  run_as_app_user python3.11 -m venv "${next_venv}"
  run_as_app_user "${next_venv}/bin/python" -m pip install --upgrade pip
  run_as_app_user "${next_venv}/bin/python" -m pip install -r "${REQUIREMENTS_FILE}"
  validate_basic_venv "${next_venv}"

}

promote_next_venv() {
  local next_venv="$1"
  local backup_path=""

  if [[ -d "${VENV_DIR}" ]]; then
    backup_path="$(unique_backup_path)"
    sudo mv "${VENV_DIR}" "${backup_path}"
  fi
  sudo mv "${next_venv}" "${VENV_DIR}"
  rewrite_text_reference "${VENV_DIR}" "${next_venv}" "${VENV_DIR}"
  sudo chown -R "${APP_USER}:${APP_GROUP}" "${VENV_DIR}"

  if ! validate_canonical_venv "${VENV_DIR}"; then
    echo "[venv] ERROR: promoted venv failed canonical validation." >&2
    if [[ -n "${backup_path}" && -d "${backup_path}" ]]; then
      sudo mv "${VENV_DIR}" "${VENV_DIR}.failed-$(date -u +%Y%m%d%H%M%S)"
      sudo mv "${backup_path}" "${VENV_DIR}"
      sudo chown -R "${APP_USER}:${APP_GROUP}" "${VENV_DIR}"
    fi
    return 1
  fi

  write_state "1" "${backup_path}"
  echo "[venv] Promoted canonical virtualenv at ${VENV_DIR}."
  if [[ -n "${backup_path}" ]]; then
    echo "[venv] Previous virtualenv preserved at ${backup_path}."
  fi
}

ensure_canonical_venv() {
  local next_venv

  write_state "0" ""
  if ! venv_needs_rebuild; then
    validate_canonical_venv "${VENV_DIR}"
    echo "[venv] Virtualenv already canonical at ${VENV_DIR}."
    return
  fi

  next_venv="${APP_ROOT}/venv.next-$(date -u +%Y%m%d%H%M%S)-$$"
  echo "[venv] Building replacement virtualenv at ${next_venv}."
  build_next_venv "${next_venv}"
  promote_next_venv "${next_venv}"
}

rollback_from_state() {
  local state_file="$1"
  local promoted=""
  local backup_path=""
  local failed_path

  if [[ -z "${state_file}" || ! -f "${state_file}" ]]; then
    echo "[venv] No venv state file found for rollback." >&2
    return 1
  fi

  promoted="$(grep -E '^VENV_PROMOTED=' "${state_file}" | tail -n1 | cut -d= -f2- || true)"
  backup_path="$(grep -E '^VENV_BACKUP_PATH=' "${state_file}" | tail -n1 | cut -d= -f2- || true)"
  if [[ "${promoted}" != "1" || -z "${backup_path}" || ! -d "${backup_path}" ]]; then
    echo "[venv] No promoted venv backup available for rollback."
    return 0
  fi

  failed_path="${VENV_DIR}.failed-$(date -u +%Y%m%d%H%M%S)"
  if [[ -d "${VENV_DIR}" ]]; then
    sudo mv "${VENV_DIR}" "${failed_path}"
    echo "[venv] Failed virtualenv preserved at ${failed_path}."
  fi
  sudo mv "${backup_path}" "${VENV_DIR}"
  rewrite_text_reference "${VENV_DIR}" "${backup_path}" "${VENV_DIR}"
  sudo chown -R "${APP_USER}:${APP_GROUP}" "${VENV_DIR}"
  validate_canonical_venv "${VENV_DIR}"
  echo "[venv] Rolled back virtualenv to ${VENV_DIR}."
}

case "${ACTION}" in
  ensure)
    ensure_canonical_venv
    ;;
  rollback)
    rollback_from_state "${STATE_FILE}"
    ;;
  *)
    echo "Usage: $0 [ensure [state-file]|rollback state-file]" >&2
    exit 64
    ;;
esac
