#!/usr/bin/env bash
set -Eeuo pipefail

if [[ $# -lt 1 ]]; then
  echo "Usage: $0 FILE [FILE ...]" >&2
  exit 2
fi
if ! command -v openssl >/dev/null 2>&1; then
  echo "OpenSSL is required for detached signing." >&2
  exit 2
fi

ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
SIGN_HOME="${XDG_CONFIG_HOME:-$HOME/.config}/discord-global-status/signing"
mkdir -p "$SIGN_HOME"
chmod 700 "$SIGN_HOME"
KEY="${DGSM_SIGN_KEY:-$SIGN_HOME/dgsm-code-signing.key.pem}"
CERT="${DGSM_SIGN_CERT:-$SIGN_HOME/dgsm-code-signing-cert.pem}"

if [[ ! -f "$KEY" || ! -f "$CERT" ]]; then
  echo "Creating a local development signing key (file permissions restricted to the current user)."
  umask 077
  KEY_ARGS=(-nodes)
  if [[ -n "${DGSM_SIGN_PASSPHRASE:-}" ]]; then
    KEY_ARGS=(-passout env:DGSM_SIGN_PASSPHRASE)
  fi
  openssl req -x509 -newkey rsa:3072 -sha256 -days 825 \
    -keyout "$KEY" -out "$CERT" \
    -subj "/CN=Discord Global Status Monitor Local Developer/" \
    -addext "basicConstraints=critical,CA:FALSE" \
    -addext "keyUsage=critical,digitalSignature" \
    -addext "extendedKeyUsage=codeSigning" "${KEY_ARGS[@]}"
  chmod 600 "$KEY"
  chmod 644 "$CERT"
fi

if [[ ! -f "$KEY" || ! -f "$CERT" ]]; then
  echo "Signing key or certificate is missing." >&2
  exit 2
fi

# The private key is explicitly chmod 0600 above. Restore a normal umask so
# public detached signatures, certificates and checksum manifests are readable.
umask 022
chmod 600 "$KEY"
chmod 644 "$CERT"

OUT_DIR="${DGSM_SIGN_OUTPUT_DIR:-$ROOT/dist/linux}"
mkdir -p "$OUT_DIR"
cp "$CERT" "$OUT_DIR/DGSM-Development-CodeSigning-Certificate.pem"
PUBKEY="$SIGN_HOME/dgsm-code-signing-public.pem"
openssl x509 -in "$CERT" -pubkey -noout > "$PUBKEY"

SIGNED=0
for FILE in "$@"; do
  [[ -f "$FILE" ]] || { echo "Skip missing artifact: $FILE"; continue; }
  echo "Signing $FILE"
  if [[ -n "${DGSM_SIGN_PASSPHRASE:-}" ]]; then
    openssl dgst -sha256 -sign "$KEY" -passin env:DGSM_SIGN_PASSPHRASE \
      -out "${FILE}.sig" "$FILE"
  else
    openssl dgst -sha256 -sign "$KEY" -out "${FILE}.sig" "$FILE"
  fi
  openssl dgst -sha256 -verify "$PUBKEY" -signature "${FILE}.sig" "$FILE"
  chmod 644 "${FILE}.sig"
  echo "Signature verified: ${FILE}.sig"
  SIGNED=$((SIGNED + 1))
done

if [[ "$SIGNED" -eq 0 ]]; then
  echo "No files were signed." >&2
  exit 1
fi

if command -v sha256sum >/dev/null 2>&1; then
  : > "$OUT_DIR/SHA256SUMS.txt"
  for FILE in "$@"; do
    [[ -f "$FILE" ]] || continue
    HASH="$(sha256sum "$FILE" | awk '{print $1}')"
    case "$FILE" in
      "$ROOT"/*) RELATIVE="${FILE#"$ROOT"/}" ;;
      *) RELATIVE="$(basename "$FILE")" ;;
    esac
    printf '%s  %s\n' "$HASH" "$RELATIVE" >> "$OUT_DIR/SHA256SUMS.txt"
  done
fi

echo "Detached signatures use the local self-signed development certificate."
echo "They prove integrity against the included certificate, not public publisher identity."
echo "Certificate: $OUT_DIR/DGSM-Development-CodeSigning-Certificate.pem"
