#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SOURCE_CONFIG="${SCRIPT_DIR}/nginx/twinevia.conf"
AVAILABLE_CONFIG="/etc/nginx/sites-available/twinevia.conf"
ENABLED_CONFIG="/etc/nginx/sites-enabled/twinevia.conf"
CERTIFICATE_PATH="/etc/letsencrypt/live/twinevia.com/fullchain.pem"
PRIVATE_KEY_PATH="/etc/letsencrypt/live/twinevia.com/privkey.pem"
BACKUP_CONFIG=""

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

restore_config() {
  if [[ -n "${BACKUP_CONFIG}" && -f "${BACKUP_CONFIG}" ]]; then
    install -o root -g root -m 0644 "${BACKUP_CONFIG}" "${AVAILABLE_CONFIG}"
  else
    rm -f -- "${AVAILABLE_CONFIG}" "${ENABLED_CONFIG}"
  fi
  nginx -t >/dev/null 2>&1 && systemctl reload nginx || true
}
trap restore_config ERR

install -o root -g root -m 0644 "${SOURCE_CONFIG}" "${AVAILABLE_CONFIG}"
ln -sfn "${AVAILABLE_CONFIG}" "${ENABLED_CONFIG}"
nginx -t
systemctl reload nginx

trap - ERR
if [[ -n "${BACKUP_CONFIG}" ]]; then
  rm -f -- "${BACKUP_CONFIG}"
fi
echo "Twinevia Nginx hosts are active for twinevia.com, www.twinevia.com, and app.twinevia.com."
