#!/usr/bin/env bash
set -Eeuo pipefail
ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT"
PYTHON_BIN="${PYTHON:-python3}"

"$PYTHON_BIN" -m py_compile \
  discord_server_global_status.py \
  discord_monitor_dll_build.py \
  create_python_zipapp.py \
  test_discord_server_global_status.py
"$PYTHON_BIN" test_discord_server_global_status.py
bash -n build_linux.sh sign_linux.sh verify_linux.sh RUN_DISCORD_STATUS.sh validate_release.sh
"$PYTHON_BIN" discord_server_global_status.py --version
"$PYTHON_BIN" discord_server_global_status.py --help >/dev/null
printf '\nStatic validation passed. Live endpoint access is not tested by this script.\n'
