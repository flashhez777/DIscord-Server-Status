#!/usr/bin/env bash
set -Eeuo pipefail

ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
BUILD_DIR="$ROOT/.build/linux"
VENV="$BUILD_DIR/venv"
STAGE="$BUILD_DIR/app"
DIST="$ROOT/dist/linux"
PYTHON_BIN="${PYTHON_BIN:-$(command -v python3 || true)}"

step() { printf '\n\033[1;36m==> %s\033[0m\n' "$1"; }
fail() { printf '\nBUILD FAILED: %s\n' "$1" >&2; exit 1; }

[[ -n "$PYTHON_BIN" ]] || fail 'python3 was not found. Install Python 3 and rerun.'
"$PYTHON_BIN" -c 'import sys; assert sys.version_info >= (3, 9), sys.version' \
  || fail 'Python 3.9 or newer is required.'

if ! command -v gcc >/dev/null 2>&1 && ! command -v cc >/dev/null 2>&1; then
  if command -v apt-get >/dev/null 2>&1 && command -v sudo >/dev/null 2>&1; then
    step 'Installing native Linux compiler prerequisites'
    sudo apt-get update
    sudo apt-get install -y build-essential python3-dev
  else
    fail 'A C compiler is required for the .so. Install build-essential and Python development headers.'
  fi
fi

step 'Creating isolated build virtual environment'
mkdir -p "$BUILD_DIR" "$DIST"
if [[ ! -x "$VENV/bin/python" ]]; then
  "$PYTHON_BIN" -m venv "$VENV" || {
    if command -v apt-get >/dev/null 2>&1 && command -v sudo >/dev/null 2>&1; then
      step 'Installing Python venv support'
      sudo apt-get update
      sudo apt-get install -y python3-venv python3-full
      "$PYTHON_BIN" -m venv "$VENV"
    else
      fail 'venv module is unavailable. Install python3-venv/python3-full and rerun.'
    fi
  }
fi
VENV_PY="$VENV/bin/python"

step 'Installing Linux build dependencies into the private venv'
"$VENV_PY" -m pip install --upgrade pip setuptools wheel
"$VENV_PY" -m pip install --upgrade -r "$ROOT/requirements-linux-build.txt"
"$VENV_PY" -c 'import cffi, setuptools; print("CFFI and setuptools are ready.")'

step 'Creating portable Python zipapp'
rm -rf "$STAGE"
mkdir -p "$STAGE" "$DIST"
cp "$ROOT/discord_server_global_status.py" "$STAGE/"
mkdir -p "$STAGE/backends"
find "$ROOT/backends" -maxdepth 1 -type f -name '*.py' -exec cp -t "$STAGE/backends" -- {} +
"$VENV_PY" -m zipapp "$STAGE" \
  --output "$DIST/DiscordGlobalStatusMonitor.pyz" \
  --python '/usr/bin/env python3' \
  --main 'discord_server_global_status:main'
chmod 755 "$DIST/DiscordGlobalStatusMonitor.pyz"
"$VENV_PY" "$DIST/DiscordGlobalStatusMonitor.pyz" --version

step 'Compiling the Linux C ABI shared library'
"$VENV_PY" "$ROOT/discord_monitor_dll_build.py" --output-dir "$DIST"
cp "$ROOT/discord_server_global_status.py" "$DIST/"
rm -rf "$DIST/backends"
mkdir -p "$DIST/backends"
find "$ROOT/backends" -maxdepth 1 -type f -name '*.py' -exec cp -t "$DIST/backends" -- {} +
cp "$ROOT/discord_global_status.h" "$DIST/"

step 'Running artifact smoke tests'
file "$DIST/DiscordGlobalStatusMonitor.pyz" "$DIST"/*.so
"$VENV_PY" "$DIST/DiscordGlobalStatusMonitor.pyz" --help >/dev/null

step 'Signing Linux artifacts (local development certificate)'
if [[ "${DGSM_SKIP_SIGNING:-0}" != '1' ]]; then
  chmod +x "$ROOT/sign_linux.sh"
  "$ROOT/sign_linux.sh" "$DIST/DiscordGlobalStatusMonitor.pyz" "$DIST"/*.so
else
  echo 'Signing skipped because DGSM_SKIP_SIGNING=1.'
fi

step 'Linux build completed'
printf 'Zipapp: %s\n' "$DIST/DiscordGlobalStatusMonitor.pyz"
printf 'Library: %s\n' "$DIST"/*.so
