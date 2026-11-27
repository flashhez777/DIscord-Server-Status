DISCORD GLOBAL STATUS MONITOR (DGSM) 3.4.1
==========================================

Cross-platform CLI for Discord worldwide public service health and global
network telemetry. It does not monitor individual guilds or bots.

FEATURES
--------
* Official Discord Statuspage global incidents, components and maintenance.
* Local REST API, Gateway, CDN, DNS, TLS and host network telemetry.
* Globalping worldwide probes: ping, HTTP, DNS and MTR.
* Optional read-only RIPE Atlas existing-measurement backend.
* RTT median/p95, packet loss, DNS/TLS/HTTP breakdown and history.
* Verified TLS; OS CA roots, certifi and optional extra PEM trust bundle.

WINDOWS ONE-CLICK BUILD
-----------------------
Double-click BUILD_AND_SIGN_WINDOWS.bat.

The builder:
  1. Reuses an existing 64-bit CPython 3.11-3.15 runtime (newest first).
  2. Detects Python through registry, common installation directories, PATH,
     Python Manager and safe legacy-launcher version probes.
  3. Only if no compatible runtime exists, downloads and verifies Python
     Manager 26.4 MSIX from python.org and calls `pymanager.exe install`.
  4. If MSIX deployment is blocked, tries the SHA-256 and Authenticode-verified Python 3.13.17 installer.
  5. Installs/checks MSVC x64, Windows SDK resource compiler and SignTool
     directly from Microsoft-signed installers. WinGet is optional.
  6. Creates a private virtual environment and installs build dependencies.
  7. Compiles EXE and CFFI DLL, embeds the DGSM network icon and signs them.
  8. Verifies signatures, writes SHA256 hashes and packages the release.

The builder never sends `install` or `list` to a legacy `py.exe`. It captures
native stdout/stderr/exit codes using .NET ProcessStartInfo. Full logs are in
.build/windows/logs/.

RUNTIME COMPATIBILITY
---------------------
PyInstaller 6.21+ supports CPython 3.15. CFFI 2.1+ is tested with CPython
3.11-3.15. The chosen interpreter version is recorded in the build manifest.
The CFFI DLL requires the same CPython minor version used at build time.

LINUX
-----
Run `./build_linux.sh` to build the portable zipapp and C ABI .so. A Linux
shared object is not a Windows DLL; builds are native to each target OS.

SECURITY
--------
TLS verification remains enabled. Do not commit .env files, API keys,
private signing keys, or certificates with private key material.
Self-signed development Authenticode signatures are locally verifiable but
not publicly trusted. Public release signing needs a trusted code-signing CA.

TESTS
-----
python -m unittest -v test_discord_server_global_status.py
python -m py_compile discord_server_global_status.py backends/ripe_atlas.py discord_monitor_dll_build.py

See README.md, BACKENDS.txt, SECURITY.md and BUILD_TROUBLESHOOTING.txt.
