#!/usr/bin/env bash
set -Eeuo pipefail
ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
if [[ -x "$ROOT/dist/linux/discord-global-status" ]]; then
  exec "$ROOT/dist/linux/discord-global-status" "$@"
elif [[ -f "$ROOT/dist/linux/DiscordGlobalStatusMonitor.pyz" ]]; then
  exec python3 "$ROOT/dist/linux/DiscordGlobalStatusMonitor.pyz" "$@"
elif [[ -f "$ROOT/discord_server_global_status.py" ]]; then
  exec python3 "$ROOT/discord_server_global_status.py" "$@"
else
  echo "Discord Global Status Monitor files not found. Extract the full ZIP first." >&2
  exit 1
fi
