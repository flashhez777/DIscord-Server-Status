#!/usr/bin/env bash
set -Eeuo pipefail

ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
BUILD_DIR="$ROOT/.build/linux"
VENV="$BUILD_DIR/venv"
STAGE="$BUILD_DIR/app"
DIST="$ROOT/dist/linux"
PYTHON_BIN="${PYTHON_BIN:-$(command -v python3 || true)}"
VERSION_MODULE="$ROOT/dgsm_version.py"
DLL_SMOKE_TEST="$ROOT/dll_smoke_test.py"

step() { printf '\n\033[1;36m==> %s\033[0m\n' "$1"; }
fail() { printf '\nBUILD FAILED: %s\n' "$1" >&2; exit 1; }
apt_run() {
  if [[ "${EUID:-$(id -u)}" -eq 0 ]]; then
    apt-get "$@"
  elif command -v sudo >/dev/null 2>&1; then
    sudo apt-get "$@"
  else
    fail 'Installing Linux build prerequisites needs root or sudo.'
  fi
}

[[ -f "$VERSION_MODULE" ]] || fail "Canonical version module missing: $VERSION_MODULE"
[[ -f "$DLL_SMOKE_TEST" ]] || fail "DLL smoke test missing: $DLL_SMOKE_TEST"
[[ -n "$PYTHON_BIN" ]] || fail 'python3 was not found. Install Python 3 and rerun.'
"$PYTHON_BIN" -c 'import sys; assert sys.version_info >= (3, 10), sys.version' \
  || fail 'Python 3.10 or newer is required for the native CFFI build.'
PY_MINOR="$($PYTHON_BIN -c 'import sys; print(f"python{sys.version_info.major}.{sys.version_info.minor}-dev")')"

NEED_COMPILER=0
if ! command -v gcc >/dev/null 2>&1 && ! command -v cc >/dev/null 2>&1; then
  NEED_COMPILER=1
fi
PY_INCLUDE="$($PYTHON_BIN -c 'import sysconfig; print(sysconfig.get_path("include") or "")')"
if [[ -z "$PY_INCLUDE" || ! -f "$PY_INCLUDE/Python.h" ]]; then
  NEED_COMPILER=1
fi
if [[ "$NEED_COMPILER" == '1' ]]; then
  command -v apt-get >/dev/null 2>&1 || fail "A C compiler and matching Python development headers are required. Install build-essential and $PY_MINOR."
  DEV_PACKAGE="$PY_MINOR"
  if ! command -v apt-cache >/dev/null 2>&1 || ! apt-cache show "$DEV_PACKAGE" >/dev/null 2>&1; then
    DEV_PACKAGE='python3-dev'
  fi
  step "Installing native Linux compiler and Python headers ($DEV_PACKAGE)"
  apt_run update
  apt_run install -y build-essential "$DEV_PACKAGE"
  PY_INCLUDE="$($PYTHON_BIN -c 'import sysconfig; print(sysconfig.get_path("include") or "")')"
  [[ -f "$PY_INCLUDE/Python.h" ]] || fail "Matching Python.h remains unavailable for $PYTHON_BIN ($PY_INCLUDE/Python.h). Install the development package matching this exact Python runtime: $PY_MINOR."
fi

step 'Creating isolated build virtual environment'
mkdir -p "$BUILD_DIR" "$DIST"
if [[ -x "$VENV/bin/python" ]]; then
  BASE_MINOR="$($PYTHON_BIN -c 'import sys; print("%d.%d" % sys.version_info[:2])')"
  VENV_MINOR="$($VENV/bin/python -c 'import sys; print("%d.%d" % sys.version_info[:2])')"
  if [[ "$BASE_MINOR" != "$VENV_MINOR" ]]; then
    echo "Existing build venv uses Python $VENV_MINOR; selected runtime is $BASE_MINOR. Recreating it."
    rm -rf "$VENV"
  fi
fi
if [[ ! -x "$VENV/bin/python" ]]; then
  "$PYTHON_BIN" -m venv "$VENV" || {
    if command -v apt-get >/dev/null 2>&1; then
      step 'Installing Python venv support'
      apt_run update
      apt_run install -y python3-venv python3-full
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
cp "$VERSION_MODULE" "$STAGE/"
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
cp "$VERSION_MODULE" "$DIST/"
cp "$ROOT/discord_global_status.h" "$DIST/"
if [[ ! -f "$ROOT/LICENSE" ]]; then fail "Project license file is missing: $ROOT/LICENSE"; fi
cp "$ROOT/LICENSE" "$DIST/"
if [[ -f "$ROOT/Discord_Global_Status_Monitor_v3.4.4_RELEASE_NOTES.txt" ]]; then
  cp "$ROOT/Discord_Global_Status_Monitor_v3.4.4_RELEASE_NOTES.txt" "$DIST/"
fi
rm -rf "$DIST/backends"
mkdir -p "$DIST/backends"
find "$ROOT/backends" -maxdepth 1 -type f -name '*.py' -exec cp -t "$DIST/backends" -- {} +

step 'Running artifact smoke tests'
file "$DIST/DiscordGlobalStatusMonitor.pyz" "$DIST"/*.so
"$VENV_PY" "$DIST/DiscordGlobalStatusMonitor.pyz" --help >/dev/null
"$VENV_PY" "$DIST/DiscordGlobalStatusMonitor.pyz" --version
"$VENV_PY" "$DLL_SMOKE_TEST" "$DIST/discord_global_status.so" --expected-version "$($VENV_PY -c 'from dgsm_version import __version__; print(__version__)')"
"$VENV_PY" - <<'PYABI' > "$DIST/BUILD_ABI.txt"
import cffi, platform, sys, sysconfig
print("DGSM Linux native build ABI")
print("Python:", sys.version.replace("\n", " "))
print("Implementation:", platform.python_implementation())
print("Machine:", platform.machine())
print("SOABI:", sysconfig.get_config_var("SOABI"))
print("CFFI:", cffi.__version__)
print("Platform:", sysconfig.get_platform())
PYABI

# Keep the release directory clean: the generated C source/object are build
# intermediates, not required by the runtime .so or its C ABI consumers.
rm -f "$DIST/_dgsm_embed.c" "$DIST/_dgsm_embed.o"
find "$DIST" -type d -name __pycache__ -prune -exec rm -rf {} +

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
printf 'ABI manifest: %s\n' "$DIST/BUILD_ABI.txt"
