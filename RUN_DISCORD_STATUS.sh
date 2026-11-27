#!/usr/bin/env bash
set -Eeuo pipefail
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"
if command -v python3 >/dev/null 2>&1; then
    exec python3 discord_server_global_status.py "$@"
fi
printf '%s\n' 'Python 3.9+ was not found. On Kali, install Python with:' '  sudo apt update && sudo apt install -y python3'
exit 127
