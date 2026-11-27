#!/usr/bin/env bash
set -Eeuo pipefail

if [[ $# -lt 1 ]]; then
  echo "Usage: $0 FILE [FILE ...]" >&2
  exit 2
fi
ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
CERT="${DGSM_SIGN_CERT:-$ROOT/dist/linux/DGSM-Development-CodeSigning-Certificate.pem}"
if [[ ! -f "$CERT" ]]; then
  CERT="$ROOT/DGSM-Development-CodeSigning-Certificate.pem"
fi
if [[ ! -f "$CERT" ]]; then
  echo "Signing certificate not found. Set DGSM_SIGN_CERT or put the PEM certificate next to this script." >&2
  exit 2
fi
TMP_DIR="$(mktemp -d)"
trap 'rm -rf "$TMP_DIR"' EXIT
PUBKEY="$TMP_DIR/public-key.pem"
openssl x509 -in "$CERT" -pubkey -noout > "$PUBKEY"
for FILE in "$@"; do
  [[ -f "$FILE" ]] || { echo "Missing file: $FILE" >&2; exit 1; }
  [[ -f "${FILE}.sig" ]] || { echo "Missing signature: ${FILE}.sig" >&2; exit 1; }
  openssl dgst -sha256 -verify "$PUBKEY" -signature "${FILE}.sig" "$FILE"
done
