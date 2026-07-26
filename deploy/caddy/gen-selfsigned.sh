#!/usr/bin/env bash
# Generate a self-signed TLS cert for the GuestOS reverse proxy (lab / internal).
set -euo pipefail
HOST="${1:-192.168.123.197}"
DIR="$(cd "$(dirname "$0")" && pwd)/certs"
mkdir -p "$DIR"
umask 077
openssl req -x509 -newkey rsa:2048 -sha256 -days 825 -nodes \
  -keyout "$DIR/key.pem" \
  -out "$DIR/cert.pem" \
  -subj "/CN=${HOST}/O=GuestOS Lab" \
  -addext "subjectAltName=IP:${HOST},DNS:${HOST}"
chmod 640 "$DIR/key.pem" "$DIR/cert.pem"
echo "Wrote $DIR/cert.pem and $DIR/key.pem for ${HOST}"
