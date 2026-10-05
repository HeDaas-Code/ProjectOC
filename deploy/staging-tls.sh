#!/usr/bin/env bash
set -euo pipefail
ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
TLS_DIR="${STAGING_TLS_DIR:-$ROOT_DIR/.staging/tls}"
mkdir -p "$TLS_DIR"
if [[ -s "$TLS_DIR/fullchain.pem" && -s "$TLS_DIR/privkey.pem" ]]; then
  printf '%s\n' "$TLS_DIR"
  exit 0
fi
command -v openssl >/dev/null 2>&1 || { echo "openssl is required to create staging TLS" >&2; exit 1; }
NAME="${STAGING_TLS_NAME:-projectoc-staging.local}"
openssl req -x509 -newkey rsa:2048 -sha256 -nodes -days "${STAGING_TLS_DAYS:-7}" \
  -keyout "$TLS_DIR/privkey.pem" -out "$TLS_DIR/fullchain.pem" \
  -subj "/CN=$NAME" -addext "subjectAltName=DNS:$NAME,DNS:localhost,IP:127.0.0.1" >/dev/null 2>&1
chmod 600 "$TLS_DIR/privkey.pem"
printf '%s\n' "$TLS_DIR"
