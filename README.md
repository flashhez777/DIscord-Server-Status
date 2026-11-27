# Discord Global Status Monitor (DGSM) 3.3.3

Cross-platform worldwide monitor for Discord's **public, global service health**. It does not monitor individual guilds, Discord accounts, or private bot state.

## Features

- Official Discord Statuspage summary: global indicator, components, incidents, updates, and scheduled maintenance.
- Local REST API, Gateway WebSocket handshake, Media CDN, DNS, TLS, and host-interface telemetry.
- Globalping community probes for worldwide ICMP, HTTP, DNS, CDN timing, and MTR/traceroute measurements.
- Optional read-only RIPE Atlas backend for existing public measurements selected by the operator; it uses the latest per-probe results endpoint with a bounded lookback rather than downloading full measurement history.
- p50/p95 latency, packet loss, probe counts, HTTP timing breakdown, historical JSONL samples, and event notifications.
- Verified TLS with OS trust roots, `certifi`, and an optional additional PEM trust bundle.
- Windows x64 EXE/DLL build pipeline with icon embedding, Authenticode signing, signature verification, and SHA-256 manifests.
- Kali/Linux source mode and portable Python zipapp build instructions.

## Install as a Python CLI

```bash
python -m pip install .
dgsm --help
```

Optional telemetry libraries are installed into a private runtime directory
when the monitor starts. The project itself has no mandatory third-party
runtime dependency.

## Quick start

### Windows

1. Extract the release ZIP.
2. Double-click `BUILD_AND_SIGN_WINDOWS.bat` to install/check Python 3.12, MSVC, Windows SDK, Python build packages, build the EXE and DLL, sign them, and create the release ZIP.
3. Run `RUN_DISCORD_STATUS.bat` or `DiscordGlobalStatusMonitor.exe`.

The builder uses `ProcessStartInfo` to capture stdout, stderr, and exit codes for Python, WinGet, Visual Studio, resource compilation, and SignTool. A missing `py -3.12` runtime is a discovery result rather than a terminating PowerShell error. It installs the official Python install manager when needed, provisions CPython 3.12, queries `py list -1 --format=exe 3.12` to obtain the exact executable path, refreshes the manager's `%LocalAppData%\Python\bin` aliases, and falls back to the standalone CPython 3.12 WinGet package. A GitHub Actions workflow runs the offline tests on Windows and Linux and builds CI artifacts.

### Kali Linux

```bash
python3 --version
python3 discord_server_global_status.py
```

Optional packages are installed into a private application state directory. No root privileges are required for ordinary monitoring.

## Global telemetry backends

See [`BACKENDS.txt`](BACKENDS.txt) for backend capabilities, data-vantage limitations, environment variables, and setup details.

Default remote provider:

- Globalping public/community probes. Measurements are external vantage-point tests, not Discord-internal telemetry.

Optional independent provider:

- RIPE Atlas read-only results for existing public measurement IDs. It does not create paid measurements.

Configure IDs using `.env.example` as a reference, then set the variables in your shell. DGSM does not automatically load `.env` files; this avoids unexpectedly loading secrets from a working directory.

## Common commands

```bash
# Continuous monitoring (default)
python3 discord_server_global_status.py

# One cycle
python3 discord_server_global_status.py --once

# JSON snapshot
python3 discord_server_global_status.py --json

# Disable Globalping but retain optional RIPE Atlas
python3 discord_server_global_status.py --no-globalping

# Existing public RIPE Atlas measurements
python3 discord_server_global_status.py --ripe-atlas-ids 12345678,12345679

# TLS and endpoint diagnostics
python3 discord_server_global_status.py --doctor

# Local history
python3 discord_server_global_status.py --history
python3 discord_server_global_status.py --telemetry-history
```

## Security and interpretation

- TLS certificate and hostname verification remain enabled. Do not solve certificate errors by disabling verification.
- Never commit `GLOBALPING_API_TOKEN` or `RIPE_ATLAS_API_KEY` to GitHub.
- Globalping and RIPE Atlas probe results are measurements from third-party networks. They do not expose Discord's private data-center telemetry.
- A failed ping can be caused by ICMP filtering. Correlate it with HTTP, DNS, TLS, Gateway, and Discord's official status feed before interpreting it as an outage.
- A self-signed development certificate provides local integrity/signature verification, not a publicly trusted publisher identity.
- The CFFI DLL is a Python-backed C ABI wrapper. It requires compatible CPython 3.12 at runtime and the companion Python source/backend package.

## GitHub Actions

`.github/workflows/ci.yml` runs the offline regression suite on Windows and
Linux using Python 3.12 and 3.13. It also builds the Linux zipapp/C ABI library
and Windows EXE/DLL as CI artifacts. Windows CI artifacts are unsigned; the
local `BUILD_AND_SIGN_WINDOWS.bat` performs the development signing flow.

The workflow uses the current major releases of the official checkout,
setup-python, and upload-artifact actions. <https://github.com/actions/setup-python>

## Tests

```bash
python3 -m unittest -v test_discord_server_global_status.py
python3 -m py_compile discord_server_global_status.py backends/ripe_atlas.py discord_monitor_dll_build.py
```

## Repository layout

```text
.
├── discord_server_global_status.py
├── backends/
│   └── ripe_atlas.py
├── assets/
│   ├── DGSM.ico
│   ├── DGSM-icon.png
│   └── DGSM.rc
├── build_windows.ps1
├── BUILD_AND_SIGN_WINDOWS.bat
├── sign_windows.ps1
├── discord_monitor_dll_build.py
├── discord_global_status.h
├── test_discord_server_global_status.py
├── BACKENDS.txt
├── BUILD_TROUBLESHOOTING.txt
├── CONTRIBUTING.md
├── SECURITY.md
├── .github/workflows/ci.yml
└── .env.example
```

## Sources

- Discord Statuspage: https://discordstatus.com/
- Globalping API: https://globalping.io/docs/api
- RIPE Atlas latest-results API: https://atlas.ripe.net/docs/apis/rest-api-reference/measurements/measurements_latest
- RIPE Atlas API: https://atlas.ripe.net/docs/apis/rest-api-manual/
- Python on Windows: https://docs.python.org/3/using/windows.html
