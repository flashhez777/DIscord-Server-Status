# Discord Global Status Monitor (DGSM) 3.4.5

Cross-platform worldwide monitor for Discord's **public, global service health**. It does not monitor individual guilds, Discord accounts, or private bot state.

## Features

- Official Discord Statuspage summary: global indicator, components, incidents, updates, and scheduled maintenance.
- Local REST API, Gateway WebSocket handshake, Media CDN, DNS, TLS, and host-interface telemetry.
- Globalping community probes for worldwide ICMP, HTTP, DNS, CDN timing, and MTR/traceroute measurements.
- Integrated optional read-only RIPE Atlas measurements selected by the operator; results use the latest per-probe endpoint, a bounded lookback, a separate worker and a 15-minute default refresh. DGSM never creates Atlas measurements.
- p50/p95 latency, packet loss, probe counts, HTTP timing breakdown, historical JSONL samples, and event notifications.
- Verified TLS with OS trust roots, `certifi`, and an optional additional PEM trust bundle.
- Windows x64 EXE/DLL build pipeline with icon embedding, Authenticode signing, signature verification, and SHA-256 manifests.
- Kali/Linux source mode and portable Python zipapp build instructions.

## Install as a Python CLI

```bash
python -m pip install .
dgsm --help
```

Optional telemetry libraries are detected when already installed. Runtime
installation is disabled by default; use --auto-install to explicitly fetch
optional integration packages from HTTPS PyPI as binary wheels, or install
requirements-optional.txt yourself. The project has no mandatory third-party
runtime dependency.

## Quick start

### Windows

1. Extract the release ZIP.
2. Double-click `BUILD_AND_SIGN_WINDOWS.bat` to reuse/install supported CPython 3.11-3.15 x64, MSVC, Windows SDK resource compiler, Python build packages, build the EXE and DLL, sign/verify them, create a Desktop shortcut, and package the release.
3. Run `RUN_DISCORD_STATUS.bat` or `DiscordGlobalStatusMonitor.exe`. The launcher prefers the compiled EXE in the release package (or `dist\windows\exe` in a source checkout); if no EXE exists it probes Python and falls back from an unusable legacy `py.exe` to `python.exe`.

The DLL RC file uses the standard numeric resource identity `1 VERSIONINFO` for RT_VERSION, with self-contained numeric FILEOS/FILETYPE constants. After CFFI links the DLL, the builder applies the compiled resources through Win32 UpdateResourceW, validates RT_VERSION/ID 1 plus the icon records, and verifies ProductVersion/FileDescription before signing. The Windows EXE collects only Plyer’s Windows notification backend, not unrelated mobile/desktop platform backends. The builder first reuses an installed 64-bit CPython 3.11-3.15 runtime (newest supported version first), including Python 3.15 installations, so it does not install Python unnecessarily. It removes an old ZIP/checksum/signing report before starting so a failed run cannot leave a stale prior release beside partial new artifacts. PyInstaller 6.21+ supports CPython 3.15 and CFFI 2.1+ is tested on CPython 3.11-3.15. If no supported runtime exists, the builder downloads the official Python Manager MSIX from python.org, verifies its published SHA-256, installs it with Add-AppxPackage, and asks `pymanager.exe` to install a runtime. If AppX deployment is blocked, it falls back to the SHA-256 and Authenticode-verified Python.org CPython 3.13.17 installer. It never sends `install` or `list` subcommands to a legacy `py.exe`. Visual Studio Build Tools and the Windows SDK resource compiler can be installed from Microsoft-signed installers without WinGet. UAC elevation is limited to the Microsoft installer process: the main build and signing continue as the original user, so the certificate and Desktop shortcut are created in the correct profile. A timestamped build transcript is written to `.build/windows/logs/`.

### Kali Linux

```bash
python3 --version
python3 discord_server_global_status.py
```

Optional packages are not downloaded on startup by default. Use --auto-install to opt in to PyPI binary-wheel installation; ordinary monitoring works with standard-library fallbacks and needs no root privileges.

## Global telemetry backends

See [`BACKENDS.txt`](BACKENDS.txt) for backend capabilities, data-vantage limitations, environment variables, and setup details.

Default remote provider:

- Globalping public/community probes. Measurements are external vantage-point tests, not Discord-internal telemetry.

Optional independent provider:

- RIPE Atlas read-only results for existing measurement IDs, refreshed every 15 minutes by default. It does not create measurements or consume measurement credits.

Configure IDs using `.env.example` as a reference, then set `RIPE_ATLAS_MEASUREMENT_IDS` in your shell or pass `--ripe-atlas-ids`. Use `--no-ripe-atlas` to explicitly disable it. An optional API key is read from `RIPE_ATLAS_API_KEY`; credentials are never included in output. DGSM does not automatically load `.env` files.

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

# Existing read-only RIPE Atlas measurements
python3 discord_server_global_status.py --ripe-atlas-ids 12345678,12345679

# Disable RIPE Atlas even if the environment contains measurement IDs
python3 discord_server_global_status.py --no-ripe-atlas

# TLS and endpoint diagnostics
python3 discord_server_global_status.py --doctor

# Local history
python3 discord_server_global_status.py --history
python3 discord_server_global_status.py --telemetry-history
```

## Windows build, signing and release flow

The Windows builder follows the same operational model as the Destiny 2 Damage Tracker build pipeline, extended to sign both DGSM artifacts:

1. Discover an installed x64 CPython 3.11-3.15 runtime; bootstrap one only when needed.
2. Create an isolated `.build/windows/venv` and install `requirements-build.txt`.
3. Generate EXE and DLL version resources from the single version in `dgsm_version.py`.
4. Build the one-file console EXE with PyInstaller, DGSM ICO and Windows version metadata.
5. Compile the DLL icon/version resource (`assets/DGSM.rc`) using Windows SDK `rc.exe`; RT_VERSION must use numeric name ID 1 (`1 VERSIONINFO`).
6. Link the `.res` into the CFFI DLL, then apply/validate resources with Win32 UpdateResourceW before checking DLL metadata.
7. Smoke-test the EXE and DLL before signing.
8. Locate a Microsoft-signed SignTool in the installed SDK; otherwise download Microsoft's `Microsoft.Windows.SDK.BuildTools` NuGet package to a temporary directory, verify the embedded SignTool Authenticode signature, and remove temporary files after signing.
9. Sign both artifacts with SHA-256 Authenticode and try DigiCert then Sectigo RFC 3161 timestamp services. When both timestamp services are unavailable, sign without a timestamp and record that fact.
10. Verify both signatures using SignTool and Windows Authenticode, then re-run EXE/DLL smoke tests.
11. Validate the embedded EXE/DLL ProductVersion and FileDescription resources, create a Desktop shortcut, record the exact build dependency versions, generate a SHA-256 file manifest, validate required ZIP entries, and remove build intermediates unless `-KeepBuildCache` is requested.

By default, the builder creates/reuses a user-scoped self-signed development code-signing certificate in `Cert:\CurrentUser\My`, exports its public `.cer`, and trusts it only in the current user's Root and TrustedPublisher stores. This makes signatures verifiable for local development on that Windows profile; it does not provide public publisher trust or SmartScreen reputation. For production distribution set `DGSM_PFX_PATH` to a CA-issued code-signing PFX. The password is prompted securely, or may be supplied in `DGSM_PFX_PASSWORD` for automation (avoid persistent environment variables and shell history).

Useful commands:

```powershell
# Default: build, sign, verify and package EXE + DLL
.\build_windows.ps1

# Retain the Python venv and intermediates for iterative development
.\build_windows.ps1 -KeepBuildCache

# Require all toolchains to be preinstalled (CI mode); still installs Python dependencies
.\build_windows.ps1 -NoToolchainInstall -SkipSigning

# Use a public publisher certificate
$env:DGSM_PFX_PATH = 'C:\Certificates\publisher-codesign.pfx'
.\build_windows.ps1
```

Windows outputs are under `dist\windows\`: `exe\DiscordGlobalStatusMonitor.exe`, `dll\discord_global_status.dll`, `signing\signing-report.txt`, `DiscordGlobalStatusMonitor-Windows-x64.zip`, and the `.sha256` sidecar. The DLL is a Python-backed C ABI library; unlike the EXE it requires a compatible CPython runtime and companion source/backend files.

## Security and interpretation

- TLS certificate and hostname verification remain enabled. Do not solve certificate errors by disabling verification.
- HTTPS requests are restricted to the documented Discord/Globalping/RIPE API hosts. Redirects must stay on the same HTTPS origin.
- Globalping DNS answers are compared as sorted RRsets. TTL differences describe resolver cache age and are not, by themselves, evidence of different DNS answers. NOERROR with no A/AAAA address answer is reported as NODATA/WARN.
- Never commit `GLOBALPING_API_TOKEN` or `RIPE_ATLAS_API_KEY` to GitHub.
- Globalping and RIPE Atlas probe results are measurements from third-party networks. They do not expose Discord's private data-center telemetry.
- A failed ping can be caused by ICMP filtering. Correlate it with HTTP, DNS, TLS, Gateway, and Discord's official status feed before interpreting it as an outage.
- A self-signed development certificate provides local integrity/signature verification, not a publicly trusted publisher identity.
- The CFFI DLL is a Python-backed C ABI wrapper. It requires the same CPython 3.11-3.15 minor runtime used to build the DLL and the companion Python source/backend package.

## GitHub Actions

`.github/workflows/ci.yml` runs the offline regression suite on Windows and Linux
using CPython 3.11-3.15, runs pip-audit over build and optional runtime
dependencies, builds the Linux zipapp/C ABI library and builds/development-signs
the Windows EXE/DLL. Windows CI uses an ephemeral self-signed certificate: it is
not publicly trusted and does not provide SmartScreen reputation. Linux CI
skips signing.

The `.github/workflows/release.yml` workflow rebuilds target artifacts and
publishes a GitHub Release with ZIPs, binaries, checksums, signing reports and
release notes when a `vX.Y.Z` tag or `release/vX.Y.Z` branch is pushed.
Unrelated branch pushes skip its release jobs. Configure the protected
`DGSM_PFX_BASE64` and `DGSM_PFX_PASSWORD` secrets to use a CA-issued Windows
publisher certificate; without them, Windows uses the self-signed development
certificate.

The workflow uses the current major releases of the official checkout,
setup-python, and upload-artifact actions. <https://github.com/actions/setup-python>

## Tests

```bash
python3 -m unittest -v test_discord_server_global_status.py
python3 -m py_compile discord_server_global_status.py backends/http_transport.py backends/ripe_atlas.py discord_monitor_dll_build.py
```

## Developer documentation

- [Annotated codebase guide](docs/CODEBASE_GUIDE.md) — module responsibilities, runtime flow, data-vantage model, persistence, native ABI, and extension rules.
- [Release engineering and CI](docs/RELEASE_ENGINEERING.md) — platform build gates, signing/trust distinctions, CI matrix, and versioned release checklist.
- [Security and release checklist](docs/SECURITY_AND_RELEASE_CHECKLIST.md) — DNS, redirect, credential, state-file, automated test, signing and publication gates.
- [Historical 3.2.1 build record](docs/archive/BUILD_INFO-v3.2.1.txt) — retained for provenance; not current build validation.

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
├── dgsm_version.py
├── assets/version_info.txt
├── dll_smoke_test.py
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
