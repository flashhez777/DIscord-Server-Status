DISCORD GLOBAL STATUS MONITOR (DGSM) 3.3.3
==========================================

PURPOSE
-------
Worldwide monitoring of Discord's public global service health. This is not
a guild monitor and does not log in to Discord as a bot or user.

WINDOWS QUICK BUILD
-------------------
1. Extract the full ZIP directory.
2. Double-click BUILD_AND_SIGN_WINDOWS.bat.
3. Approve Windows permission prompts when installing MSVC/Windows SDK.
4. The builder installs/checks Python 3.12, build dependencies, MSVC x64,
   Windows SDK resource compiler and SignTool.
5. It builds DiscordGlobalStatusMonitor.exe and discord_global_status.dll,
   embeds the DGSM network icon, signs both with Authenticode, verifies the
   signatures, and creates a Windows release ZIP.
6. Launch RUN_DISCORD_STATUS.bat or DiscordGlobalStatusMonitor.exe.

PYTHON RUNTIME DISCOVERY FIX
----------------------------
Earlier builds directly invoked `py.exe -3.12`. When the launcher existed but
Python 3.12 did not, it emitted "No suitable Python runtime found" to stderr.
Windows PowerShell 5.1 could promote that text to NativeCommandError and stop
the build before installing Python. Version 3.3.3 uses a captured .NET process
runner, distinguishes stderr from exit code, discovers registered/known Python
paths, installs the official Python install manager via WinGet msstore, runs
`py install 3.12`, obtains the exact path with `py list -1 --format=exe 3.12`,
refreshes `%LocalAppData%\Python\bin`, checks the runtime, and uses the
standalone WinGet CPython 3.12 package as a fallback.

WINDOWS BUILD OUTPUT
--------------------
dist\windows\exe\DiscordGlobalStatusMonitor.exe
dist\windows\dll\discord_global_status.dll
dist\windows\DiscordGlobalStatusMonitor-Windows-x64.zip

KALI / LINUX
------------
Run the Python source directly:
  python3 discord_server_global_status.py

Build Linux zipapp and C ABI shared library:
  chmod +x build_linux.sh
  ./build_linux.sh

Run portable zipapp:
  python3 dist/linux/DiscordGlobalStatusMonitor.pyz

BACKENDS
--------
1. Discord Statuspage: official worldwide incidents/components/maintenance.
2. Globalping: third-party worldwide ICMP/HTTP/DNS/CDN/MTR probes.
3. RIPE Atlas: optional read-only latest-result backend for existing public
   measurement IDs supplied by the operator. Uses a 7-day lookback and does
   not download full measurement history.
4. Local connectivity: Discord REST, Gateway WebSocket, CDN, DNS and TLS.
5. Host telemetry: local interface throughput, packet rates, errors and drops.

RIPE ATLAS OPTIONAL BACKEND
---------------------------
Set RIPE_ATLAS_MEASUREMENT_IDS to comma-separated existing public measurement
IDs, or pass --ripe-atlas-ids 12345678,12345679. Results are read-only; DGSM
does not create/schedule measurements or spend credits. The selected IDs need
to target Discord endpoints for the results to be meaningful. Optional
RIPE_ATLAS_API_KEY may be set for authorized reads of private measurements.

GLOBALPING
----------
Optional bearer token environment variable: GLOBALPING_API_TOKEN
DGSM queries the provider's /v1/limits endpoint and respects rate limits.

USEFUL COMMANDS
---------------
  python3 discord_server_global_status.py --once
  python3 discord_server_global_status.py --json
  python3 discord_server_global_status.py --doctor
  python3 discord_server_global_status.py --history
  python3 discord_server_global_status.py --telemetry-history
  python3 discord_server_global_status.py --no-globalping
  python3 discord_server_global_status.py --ripe-atlas-ids 12345678

TESTS
-----
  python3 -m unittest -v test_discord_server_global_status.py

SECURITY AND MEASUREMENT LIMITATIONS
------------------------------------
* TLS verification remains enabled; do not disable certificate validation.
* Third-party probes do not expose Discord's private data-center telemetry.
* ICMP may be filtered; correlate it with HTTP/DNS/TLS/Gateway and the
  official Discord Statuspage before interpreting it as an outage.
* Self-signed development certificates provide local signature/integrity
  verification, not a publicly trusted publisher identity or SmartScreen
  reputation. Public distribution requires a CA-issued code-signing cert.
* The Windows DLL is a Python-backed C ABI wrapper. It requires compatible
  CPython 3.12 and companion source/backend files at runtime.

REPOSITORY DOCUMENTATION
------------------------
README.md                       GitHub rendered documentation
GITHUB_README.txt               Plain-text project overview
BACKENDS.txt                    Backend design and configuration
BUILD_TROUBLESHOOTING.txt       Python/WinGet/MSVC build troubleshooting
.env.example                    Safe environment-variable template
