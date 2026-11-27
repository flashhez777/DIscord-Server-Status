DISCORD GLOBAL STATUS MONITOR (DGSM) 3.3.3
==========================================

PROJECT PURPOSE
---------------
DGSM monitors Discord's public worldwide service health. It is not a guild
monitor, user tracker, Discord bot, or private service telemetry collector.

FEATURES
--------
* Official Discord Statuspage: overall state, components, incidents,
  incident updates, and scheduled maintenance.
* Local probes: Discord REST API, Gateway WebSocket handshake, CDN, DNS,
  TLS reachability, and host network interface rates/errors.
* Globalping backend: remote community probes for ICMP, HTTP, DNS, CDN
  timings, and MTR/traceroute.
* RIPE Atlas backend: optional, read-only collection from existing public
  measurement IDs supplied by the operator.
* Statistics: median/p50, p95, min/max, packet loss, probe counts, HTTP
  timing breakdown, event log, and JSON telemetry history.
* Verified TLS: OS trust roots plus certifi and an explicit optional PEM CA.
* Windows build: installs/checks Python and the C++/Windows SDK toolchain,
  compiles an EXE and Python-backed C ABI DLL, embeds the DGSM network icon,
  Authenticode-signs both binaries, verifies signatures, and packages output.

WINDOWS BUILD FIX
-----------------
The previous build called `py.exe -3.12` directly. On systems where py.exe
exists but Python 3.12 is not installed, the launcher writes "No suitable
Python runtime found" to stderr. Windows PowerShell 5.1 can promote native
stderr into NativeCommandError when ErrorActionPreference is Stop, aborting
before the builder can install Python.

Version 3.3.3 replaces direct launcher invocation with a .NET
ProcessStartInfo-based process runner. stdout, stderr, and exit codes are
captured as ordinary data. A missing interpreter is now a discovery result.
The builder searches registry registrations, known install directories,
PATH aliases, py.exe, and pymanager.exe. If needed, it installs the official
Python install manager from WinGet's msstore source, provisions CPython 3.12
with `py install 3.12`, obtains the exact path using
`py list -1 --format=exe 3.12`, refreshes `%LocalAppData%\Python\bin`, and
finally falls back to the standalone Python.Python.3.12 WinGet package.

BACKENDS
--------
1. Discord Statuspage backend (official global incident/component source).
2. Globalping backend (third-party worldwide probe network).
3. RIPE Atlas backend (optional public results for operator-supplied IDs).
4. Local connectivity backend (REST/Gateway/CDN/DNS/TLS from this PC).
5. Host network backend (local RX/TX, packet rates, errors and drops).

GitHub automation:
  .github/workflows/ci.yml runs the offline suite on Windows and Linux, builds
  the portable Linux zipapp/shared library, and compiles Windows artifacts in
  the hosted Windows environment. CI Windows artifacts are deliberately
  unsigned; run BUILD_AND_SIGN_WINDOWS.bat locally for development signing or
  configure a trusted signing certificate in a protected signing workflow.

QUICK START
-----------
Install CLI package:
  python -m pip install .
  dgsm --help

Windows:
  Extract the ZIP and double-click BUILD_AND_SIGN_WINDOWS.bat.
  Run RUN_DISCORD_STATUS.bat or DiscordGlobalStatusMonitor.exe.

Kali/Linux:
  python3 discord_server_global_status.py

Tests:
  python3 -m unittest -v test_discord_server_global_status.py

OPTIONAL RIPE ATLAS BACKEND
---------------------------
DGSM uses the `/latest/` results endpoint with a seven-day lookback so it reads
recent per-probe results rather than repeatedly downloading full histories.
Set RIPE_ATLAS_MEASUREMENT_IDS to one or more comma-separated existing public
measurement IDs. Or pass --ripe-atlas-ids 12345678,12345679. The backend reads
existing measurement results only; it does not create measurements or spend
RIPE Atlas credits. RIPE_ATLAS_API_KEY may be provided for authorized reads of
measurements that are not public. Keep API keys out of source control.

GLOBALPING AUTHENTICATION
------------------------
Set GLOBALPING_API_TOKEN in the process environment to use an optional
Globalping token. The live provider /v1/limits response remains authoritative.
Do not commit tokens to GitHub.

SECURITY / MEASUREMENT NOTES
---------------------------
* TLS verification is always enabled.
* Third-party worldwide probes do not represent Discord internal telemetry.
* ICMP can be filtered; correlate ping with HTTP, DNS, TLS, Gateway and the
  official Statuspage before calling it an outage.
* The generated Windows development certificate is self-signed. It can
  verify local integrity but is not a publicly trusted publisher certificate.
* The CFFI DLL requires compatible CPython 3.12 and companion source/backend
  files at runtime; it is not a self-contained implementation of Python.

DOCUMENTATION
-------------
README.md                       GitHub's normal rendered project page
GITHUB_README.txt               Plain-text GitHub/project overview
BACKENDS.txt                    Backend architecture and configuration
BUILD_TROUBLESHOOTING.txt       Windows/Python/Visual Studio build fixes
README_Discord_Global_Status.txt Full operating guide

Official references:
  https://discordstatus.com/
  https://globalping.io/docs/api
  https://atlas.ripe.net/docs/apis/rest-api-manual/
  https://docs.python.org/3/using/windows.html
