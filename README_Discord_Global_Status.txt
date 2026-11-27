DISCORD GLOBAL STATUS MONITOR v3.2.1
====================================

Cross-platform worldwide Discord status and network telemetry monitor for
Windows 10/11, Kali Linux, Debian/Ubuntu and macOS. Python 3.9+ recommended.

SCOPE
-----
This application monitors GLOBAL Discord services only. It does not inspect
individual guilds, bots, accounts, user sessions, Discord's private data-center
telemetry, or internal server-to-server metrics.

WHAT IT MONITORS
----------------
1. Discord's official public Statuspage feed:
   - Overall worldwide operational state
   - API, Gateway, Media Proxy, Push, client platforms and third-party services
   - Voice services and regions exposed by Discord's status page
   - Active incidents, incident updates and scheduled maintenance

2. Worldwide remote measurements through Globalping community probes:
   - ICMP RTT to gateway.discord.gg, including min/average/max and packet loss
   - Distributed HTTPS checks against Discord's Gateway API
   - HTTP timing breakdown where provided (DNS, TCP, TLS, TTFB, total)
   - Distributed DNS resolution of gateway.discord.gg
   - Worldwide MTR route diagnostics and hop-level RTT/loss where returned
   - Discord Media CDN HTTPS timing
   - Probe country/city/continent, ASN and network where supplied
   - Median, p95, min/max, successful/failed probe counts and packet loss
   - Provider-reported rate limit, remaining allowance, reset time and credits

3. Local diagnostics, clearly separated from worldwide measurements:
   - Discord REST API availability and response time
   - Gateway TLS + WebSocket upgrade handshake
   - Discord Media CDN reachability
   - Local DNS resolution timing
   - Network interface RX/TX Mbps, packets/s, errors and drops (when psutil works)

4. Monitoring / reliability:
   - Continuous monitoring with automatic recovery on transient errors
   - Status, incident, maintenance, connectivity and telemetry change detection
   - Best-effort desktop notifications and optional terminal beep
   - Bounded retries for idempotent Globalping GET requests
   - No automatic retry of measurement-creation POSTs (avoids duplicate tests)
   - Rate-limit handling using HTTP 429 / Retry-After
   - Bounded response sizes and JSON/schema validation
   - Atomic state writes and rotating JSONL history
   - JSON output for external monitoring and automation

TLS / CERTIFICATE FIX IN v3.2.1
--------------------------------
All HTTPS calls (Discord Status, Discord REST, CDN, Globalping and the Gateway
TLS handshake) share one strict, verified TLS context. Certificate and hostname
verification are always enabled (CERT_REQUIRED + check_hostname). The monitor
never uses an insecure SSL context and never disables certificate checks.

The trust bundle combines:
  - Operating-system / Python trust roots
  - Mozilla roots from certifi (minimum 2026.11.20; stale certifi is upgraded)
  - An optional additional PEM trust bundle supplied by the user or environment

On first launch the script automatically checks optional package versions and
installs/upgrades them in a private per-user vendor directory. The certifi
upgrade check is important: an old globally-installed certifi package no
longer silently prevents a current app-local CA bundle from being installed.

Run diagnostics:
  python discord_server_global_status.py --doctor

Kali / Debian CA store repair (when system roots are missing or stale):
  sudo apt update
  sudo apt install --reinstall ca-certificates
  sudo update-ca-certificates
  python3 discord_server_global_status.py --doctor

If a managed network performs HTTPS/TLS inspection, ask the administrator for
the approved root CA in PEM format and add it without disabling verification:
  python3 discord_server_global_status.py --ca-bundle /path/to/approved-root.pem

PowerShell:
  py -3 .\discord_server_global_status.py --ca-bundle C:\certs\approved-root.pem

The --ca-bundle option ADDS a trust anchor. Only add a root CA you intentionally
trust. A corporate inspection certificate cannot be safely guessed or
manufactured by the monitor. Do not download random certificates or disable TLS.

DEPENDENCIES / AUTOMATIC INSTALLATION
--------------------------------------
The core monitor uses Python's standard library. Optional features use:
  certifi     Current Mozilla CA bundle; automatic minimum-version upgrade
  psutil      Per-interface throughput, packet, error and drop counters
  dnspython   Detailed local DNS resolver telemetry
  plyer       Desktop notifications
  colorama    Legacy Windows console color compatibility

The script attempts to bootstrap pip with ensurepip when needed, then installs
packages into a private application directory. It does not modify global
Python or require sudo/admin permissions. Kali/Debian externally-managed Python
installations are left untouched. If package installation or internet access
fails, built-in standard-library fallbacks remain active.

Private package/data locations:
  Windows: %LOCALAPPDATA%\DiscordGlobalStatusMonitor\
  Linux:   ~/.local/state/DiscordGlobalStatusMonitor/
  macOS:   ~/Library/Application Support/DiscordGlobalStatusMonitor/

RUN — WINDOWS
-------------
1. Extract the release ZIP.
2. Double-click RUN_DISCORD_STATUS.bat.

If a Windows executable has been compiled, the launcher uses it. Otherwise it
runs the Python source. Source mode requires Python 3.9+:
  py -3 discord_server_global_status.py

RUN — KALI LINUX
----------------
  chmod +x RUN_DISCORD_STATUS.sh discord_server_global_status.py
  ./RUN_DISCORD_STATUS.sh

The launcher uses the Linux PyInstaller executable when built; otherwise it
uses the portable Python zipapp or the source. Python 3.9+ is required for the
zipapp/source mode. Ctrl+C stops continuous monitoring.

COMMON OPTIONS
--------------
  --interval 30          Official Statuspage refresh interval, seconds (min 10)
  --timeout 8            Network request timeout, seconds
  --once                 Fetch one snapshot and wait for initial telemetry
  --json                 One-shot machine-readable JSON snapshot
  --history              Show status / incident change history
  --telemetry-history    Show saved worldwide telemetry samples
  --doctor               Diagnose TLS trust, dependency versions and endpoints
  --ca-bundle PATH       Add an approved PEM CA without disabling verification
  --no-globalping        Disable remote worldwide measurements
  --global-probes 10     Worldwide ping probe count (1-50)
  --global-interval 600  Worldwide ping period (seconds)
  --http-interval 900    Worldwide HTTPS test period (seconds)
  --dns-interval 1800    Worldwide DNS period (seconds)
  --cdn-interval 1800    Worldwide CDN period (seconds)
  --trace-interval 3600  Worldwide MTR period (seconds)
  --trace-probes 3       Worldwide MTR probe count (1-10)
  --trace-now            Start MTR immediately
  --no-probes            Disable local REST/Gateway/CDN/DNS checks
  --no-notifications     Disable desktop notifications
  --no-auto-install      Disable automatic optional dependency installation
  --no-save              Disable persistent event / telemetry history
  --no-color             Disable ANSI colors
  --no-clear             Do not clear the terminal between refreshes
  --beep                 Terminal beep on meaningful changes
  --data-dir PATH        Override the local data/vendor directory

EXAMPLES
--------
  # Continuous global monitor
  python3 discord_server_global_status.py

  # Run diagnostics first (recommended after a TLS error)
  python3 discord_server_global_status.py --doctor

  # Run worldwide MTR immediately
  python3 discord_server_global_status.py --trace-now

  # Increase global ping probe count without changing the schedule
  python3 discord_server_global_status.py --global-probes 20

  # JSON snapshot for an external monitoring pipeline
  python3 discord_server_global_status.py --json

  # Disable third-party worldwide probes, keep Discord's official status feed
  python3 discord_server_global_status.py --no-globalping

OPTIONAL GLOBALPING TOKEN
-------------------------
No token is needed for standard public measurements. An optional Globalping
bearer token may provide higher quota/account credits, subject to the live
provider account limits. Set it in the environment; never hard-code it.

PowerShell:
  $env:GLOBALPING_API_TOKEN = "YOUR_TOKEN"
  py -3 discord_server_global_status.py

Kali:
  export GLOBALPING_API_TOKEN='YOUR_TOKEN'
  python3 discord_server_global_status.py

BUILD WINDOWS EXE + DLL
-----------------------
Build on Windows itself (PyInstaller does not cross-compile Windows binaries
from Linux). Install:
  - Python 3.9+ with Python Launcher, or set -PythonLauncher to a command
  - Visual Studio Build Tools with "Desktop development with C++"
  - Windows 10/11 SDK including SignTool

Open PowerShell in the extracted project directory:
  Set-ExecutionPolicy -Scope Process Bypass
  .\build_windows.ps1

For a private development machine where you explicitly want to trust its
self-signed certificate for the current Windows user:
  .\build_windows.ps1 -TrustSelfSignedForCurrentUser

The script creates a private venv, installs build/runtime dependencies,
builds a standalone x64 EXE with PyInstaller, compiles a CFFI DLL, then signs
EXE/DLL using a local development certificate unless -SkipSigning is used.
The EXE bundles Python/runtime dependencies; it does not require Python.

The CFFI DLL is an integration library, not a standalone executable. It exports
a small C ABI but requires a compatible CPython runtime and the monitor Python
source beside the DLL (or DGSM_HOME pointing to that source). It is not a
renamed .pyd file.

For a real publisher-trusted Windows release, install a CA-issued code-signing
certificate (OV/EV or an approved cloud signing service) in CurrentUser\My and
set DGSM_CERT_THUMBPRINT to its thumbprint before running build_windows.ps1.
A locally-created self-signed development cert is only suitable for private
lab/testing; it is not publicly trusted and may trigger SmartScreen warnings.

BUILD LINUX PYTHON PROGRAM + OPTIONAL .SO
-----------------------------------------
Run on the target Linux architecture/distribution:
  chmod +x build_linux.sh
  ./build_linux.sh

The build always creates dist/linux/DiscordGlobalStatusMonitor.pyz, a
self-contained Python zipapp containing the monitor source (requires Python 3.9+).
If dependencies can be downloaded, the script also uses PyInstaller to produce
an ELF executable. If CFFI and a C compiler are available, it creates
 discord_global_status.so, a native C ABI wrapper for the matching CPython ABI.

The CFFI .so is not a standalone ELF application; it links to libpython and
needs the matching CPython runtime and the monitor source. Rebuild it on the
target environment for ABI portability. The zipapp is the portable Python
program when no one-file freezer is available.

SIGNING
-------
Windows:
  build_windows.ps1 signs EXE/DLL using SignTool. For public distribution use
a CA-issued signing certificate. A self-signed cert can validate the build
locally but does not establish a trusted publisher identity on other systems.

Linux:
  bash sign_linux.sh dist/linux/DiscordGlobalStatusMonitor.pyz \
       dist/linux/discord_server_global_status.py

The Linux script creates a local self-signed RSA code-signing certificate and
produces detached SHA-256 signatures (.sig). The private key is encrypted and
stored under ~/.config/discord-global-status/signing/, outside the release ZIP.
Verification example:
  openssl x509 -in dist/linux/DGSM-Development-CodeSigning-Certificate.pem \
      -pubkey -noout > public-key.pem
  openssl dgst -sha256 -verify public-key.pem \
      -signature dist/linux/DiscordGlobalStatusMonitor.pyz.sig \
      dist/linux/DiscordGlobalStatusMonitor.pyz

Self-signed signatures prove a file matches the certificate you trust; they do
not provide public publisher trust. Compare certificate fingerprints through
an independent trusted channel. Do not distribute private signing keys.

DATA / HISTORY
--------------
State, event history and telemetry are written into the per-user directory
above. Use --data-dir to change this location or --no-save to disable saves.

IMPORTANT MEASUREMENT INTERPRETATION
------------------------------------
- Official Statuspage is the source for Discord's published incident status.
- Globalping RTT, packet loss, HTTP, DNS and MTR are observations from remote
  community probes reaching public Discord endpoints. They are NOT Discord's
  own internal data-center metrics and cannot guarantee country/ISP coverage.
- Local REST/Gateway/CDN/DNS latency and host throughput belong to the computer
  running the monitor and are shown separately.
- ICMP can be filtered/rate-limited. A failed ping alone does not prove outage;
  correlate it with HTTPS, DNS, MTR and Discord's official incident feed.
- A DNS resolution failure is different from a certificate trust failure.

REFERENCES
----------
Discord Status API:
  https://discordstatus.com/api
  https://discordstatus.com/api/v2/summary.json

Globalping API and usage limits:
  https://globalping.io/docs/api.globalping.io
  https://github.com/jsdelivr/globalping/blob/master/public/v1/spec.yaml

Python packaging / CFFI:
  https://pyinstaller.org/en/stable/
  https://cffi.readthedocs.io/en/latest/embedding.html

Windows signing:
  https://learn.microsoft.com/en-us/windows/win32/seccrypto/signtool

Version: 3.2.1
