#!/usr/bin/env bash
set -Eeuo pipefail

ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
PYTHON_BIN="${PYTHON:-python3}"
BUILD_DIR="$ROOT/.build/linux"
VENV="$BUILD_DIR/venv"
DIST="$ROOT/dist/linux"

if ! command -v "$PYTHON_BIN" >/dev/null 2>&1; then
  echo "Python 3 is required. Install python3 first." >&2
  exit 1
fi

mkdir -p "$BUILD_DIR" "$DIST"
if [[ ! -x "$VENV/bin/python" ]]; then
  "$PYTHON_BIN" -m venv "$VENV" || {
    echo "Could not create a virtual environment. On Kali/Debian install: sudo apt install python3-venv" >&2
    exit 1
  }
fi

PY="$VENV/bin/python"
HAS_PYINSTALLER=0
if "$PY" -m pip install --upgrade pip && \
   "$PY" -m pip install --upgrade pyinstaller cffi certifi psutil dnspython plyer; then
  HAS_PYINSTALLER=1
else
  echo "WARNING: Build dependency download failed (often DNS/TLS/proxy related)." >&2
  echo "The standard-library Python zipapp will still be created." >&2
fi

rm -rf "$BUILD_DIR/pyinstaller-work" "$BUILD_DIR/spec"
if [[ "$HAS_PYINSTALLER" -eq 1 ]]; then
  "$PY" -m PyInstaller \
    --clean --noconfirm --onefile --console \
    --name discord-global-status \
    --distpath "$DIST" \
    --workpath "$BUILD_DIR/pyinstaller-work" \
    --specpath "$BUILD_DIR/spec" \
    --collect-all certifi \
    --collect-all psutil \
    --collect-all dns \
    --collect-all plyer \
    --hidden-import colorama \
    "$ROOT/discord_server_global_status.py"
else
  echo "Skipping ELF freezer build because PyInstaller is unavailable." >&2
fi

"$PYTHON_BIN" "$ROOT/create_python_zipapp.py" --output "$DIST/DiscordGlobalStatusMonitor.pyz"
cp "$ROOT/discord_server_global_status.py" "$DIST/"
cp "$ROOT/discord_global_status.h" "$DIST/"

# Build a Python-backed native shared library as an optional C integration API.
# Use the build venv if CFFI was downloaded, otherwise reuse system CFFI if present.
if "$PY" -c 'import cffi' >/dev/null 2>&1; then
  "$PY" "$ROOT/discord_monitor_dll_build.py" --output-dir "$DIST" || \
    echo "WARNING: native .so wrapper failed; Python zipapp remains available." >&2
elif "$PYTHON_BIN" -c 'import cffi' >/dev/null 2>&1; then
  "$PYTHON_BIN" "$ROOT/discord_monitor_dll_build.py" --output-dir "$DIST" || \
    echo "WARNING: native .so wrapper failed; Python zipapp remains available." >&2
else
  echo "CFFI/compiler not available; skipping optional .so wrapper." >&2
fi
# CFFI emits intermediate C/object files beside the library; they are not
# runtime deliverables and are kept out of the release directory.
rm -f "$DIST/_dgsm_embed.c" "$DIST/_dgsm_embed.o"

SIGN_TARGETS=("$DIST/DiscordGlobalStatusMonitor.pyz" "$DIST/discord_server_global_status.py")
[[ -f "$DIST/discord-global-status" ]] && SIGN_TARGETS+=("$DIST/discord-global-status")
[[ -f "$DIST/discord_global_status.so" ]] && SIGN_TARGETS+=("$DIST/discord_global_status.so")
if [[ -f "$ROOT/sign_linux.sh" && -t 0 ]]; then
  bash "$ROOT/sign_linux.sh" "${SIGN_TARGETS[@]}" || \
    echo "WARNING: detached signing was skipped or incomplete; see sign_linux.sh." >&2
else
  echo "Signing is available with: bash sign_linux.sh <artifact files>" >&2
fi

printf '\nBuild complete. Outputs:\n'
find "$DIST" -maxdepth 1 -type f -printf '  %f (%s bytes)\n' | sort
printf '\nRun: %s\n' "$ROOT/RUN_DISCORD_STATUS.sh"
