#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SOURCE_CONFIG="${SCRIPT_DIR}/nginx/twinevia.conf"
AVAILABLE_CONFIG="/etc/nginx/sites-available/twinevia.conf"
ENABLED_CONFIG="/etc/nginx/sites-enabled/twinevia.conf"
LEGACY_ENABLED_CONFIG="/etc/nginx/sites-enabled/twinevia.com"
CERTIFICATE_PATH="/etc/letsencrypt/live/twinevia.com/fullchain.pem"
PRIVATE_KEY_PATH="/etc/letsencrypt/live/twinevia.com/privkey.pem"
BACKUP_CONFIG=""
LEGACY_ENABLED_BACKUP=""
LEGACY_ENABLED_KIND="absent"
LEGACY_ENABLED_TARGET=""

if [[ "${1:-}" != "--confirm-dns-and-certificate-ready" || $# -ne 1 ]]; then
  echo "Usage: sudo $0 --confirm-dns-and-certificate-ready" >&2
  exit 1
fi
for command_name in nginx openssl; do
  if ! command -v "${command_name}" >/dev/null 2>&1; then
    echo "Required command is unavailable: ${command_name}" >&2
    exit 1
  fi
done
if [[ ! -r "${CERTIFICATE_PATH}" || ! -r "${PRIVATE_KEY_PATH}" ]]; then
  echo "The Twinevia certificate or private key is missing." >&2
  exit 1
fi

certificate_text="$(openssl x509 -in "${CERTIFICATE_PATH}" -noout -text)"
for hostname in twinevia.com www.twinevia.com app.twinevia.com; do
  if [[ "${certificate_text}" != *"DNS:${hostname}"* ]]; then
    echo "The certificate does not contain SAN ${hostname}." >&2
    exit 1
  fi
done

if [[ -f "${AVAILABLE_CONFIG}" ]]; then
  BACKUP_CONFIG="$(mktemp)"
  install -m 0644 "${AVAILABLE_CONFIG}" "${BACKUP_CONFIG}"
fi
if [[ -L "${LEGACY_ENABLED_CONFIG}" ]]; then
  LEGACY_ENABLED_KIND="symlink"
  LEGACY_ENABLED_TARGET="$(readlink "${LEGACY_ENABLED_CONFIG}")"
elif [[ -f "${LEGACY_ENABLED_CONFIG}" ]]; then
  LEGACY_ENABLED_KIND="file"
  LEGACY_ENABLED_BACKUP="$(mktemp)"
  install -m 0644 "${LEGACY_ENABLED_CONFIG}" "${LEGACY_ENABLED_BACKUP}"
elif [[ -e "${LEGACY_ENABLED_CONFIG}" ]]; then
  echo "Legacy Twinevia Nginx path is not a file or symlink: ${LEGACY_ENABLED_CONFIG}" >&2
  exit 1
fi

restore_config() {
  if [[ -n "${BACKUP_CONFIG}" && -f "${BACKUP_CONFIG}" ]]; then
    install -o root -g root -m 0644 "${BACKUP_CONFIG}" "${AVAILABLE_CONFIG}"
  else
    rm -f -- "${AVAILABLE_CONFIG}" "${ENABLED_CONFIG}"
  fi
  case "${LEGACY_ENABLED_KIND}" in
    symlink)
      ln -sfn -- "${LEGACY_ENABLED_TARGET}" "${LEGACY_ENABLED_CONFIG}"
      ;;
    file)
      install -o root -g root -m 0644 "${LEGACY_ENABLED_BACKUP}" "${LEGACY_ENABLED_CONFIG}"
      ;;
    absent)
      rm -f -- "${LEGACY_ENABLED_CONFIG}"
      ;;
    *)
      echo "Unsupported legacy Nginx backup state: ${LEGACY_ENABLED_KIND}" >&2
      ;;
  esac
  nginx -t >/dev/null 2>&1 && systemctl reload nginx || true
}
trap restore_config ERR

# The original production install used twinevia.com as the enabled filename.
# Disable that exact legacy entry before enabling the canonical config so the
# shared upstream and server blocks are never loaded twice.
rm -f -- "${LEGACY_ENABLED_CONFIG}"
install -o root -g root -m 0644 "${SOURCE_CONFIG}" "${AVAILABLE_CONFIG}"
ln -sfn "${AVAILABLE_CONFIG}" "${ENABLED_CONFIG}"
nginx -t
systemctl reload nginx

trap - ERR
if [[ -n "${BACKUP_CONFIG}" ]]; then
  rm -f -- "${BACKUP_CONFIG}"
fi
if [[ -n "${LEGACY_ENABLED_BACKUP}" ]]; then
  rm -f -- "${LEGACY_ENABLED_BACKUP}"
fi
echo "Twinevia Nginx hosts are active for twinevia.com, www.twinevia.com, and app.twinevia.com."
