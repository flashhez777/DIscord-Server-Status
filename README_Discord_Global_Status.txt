DISCORD GLOBAL STATUS MONITOR (DGSM) 3.4.4
==========================================

Cross-platform monitor for Discord GLOBAL public service health only. It does
not inspect guilds, bots, accounts or private Discord data-center telemetry.

GLOBAL STATUS AND TELEMETRY
---------------------------
* Official Discord Statuspage global state, components, incidents and
  scheduled maintenance.
* Local REST API, Gateway, CDN, DNS, TLS and host network interface metrics.
* Globalping distributed ICMP, HTTP/HTTPS, DNS and MTR probes.
* Integrated optional read-only RIPE Atlas results for operator-selected
  existing measurements; separate worker, 15-minute default refresh.
* RTT median/p95, packet loss, HTTP timing breakdown, quota and historical
  telemetry. Each sample carries its measurement vantage/source.
* Verified TLS using OS trust roots, certifi and optional approved CA PEM.

RUN THE MONITOR
---------------
Windows:
  Double-click RUN_DISCORD_STATUS.bat

Kali/Linux:
  chmod +x RUN_DISCORD_STATUS.sh
  ./RUN_DISCORD_STATUS.sh

The runtime bootstraps optional telemetry/notification packages into a private
per-user package directory when needed. It does not globally modify Python.

WINDOWS AUTOMATIC EXE + DLL BUILD AND SIGNING
---------------------------------------------
Extract the complete source package. Double-click:
  BUILD_AND_SIGN_WINDOWS.bat

The builder mirrors the Destiny 2 Damage Tracker build/sign workflow and
extends it to both DGSM.exe and the native C ABI DLL:

  1. Find/reuse 64-bit CPython 3.11-3.15; use the official Python Manager or
     hash-verified Python.org runtime only when none is already installed.
     WinGet is not required and legacy py.exe is never used for install/list.
  2. Create an isolated .build\windows\venv and install requirements-build.txt.
  3. Derive EXE/DLL version resources from dgsm_version.py.
  4. Build the one-file console EXE with PyInstaller, DGSM.ico and version info.
  5. Compile the DLL's icon and version resource with Windows SDK rc.exe.
     RT_VERSION uses standard numeric resource name ID 1 (1 VERSIONINFO).
  6. Link the .res into the CFFI DLL, then apply and validate resource records
     with Win32 UpdateResourceW before DLL version verification.
  7. Smoke-test the EXE and DLL before signing.
  8. Use installed Microsoft SignTool; if missing, download Microsoft's SDK
     BuildTools NuGet package to a temporary directory, validate its embedded
     Authenticode-signed SignTool, and delete temporary files after signing.
  9. Reuse/create a CurrentUser code-signing cert or use a PFX. Sign both files
     with SHA-256 Authenticode, try DigiCert/Sectigo timestamping, fall back
     to signing without a timestamp if both timestamp authorities are down.
 10. Verify signatures and run the post-sign EXE/DLL smoke tests.
 11. Create a Desktop shortcut, package binaries and a SHA-256 manifest, then
     clean the venv and build intermediates by default.

Only Microsoft toolchain installers that need elevation receive UAC. The build
and signing continue as the launching user, so the local certificate, log and
Desktop shortcut are created for the correct user profile.

Signing defaults to a self-signed development certificate in CurrentUser\My,
with the public certificate trusted in CurrentUser\Root and CurrentUser\TrustedPublisher.
This does not create public publisher trust. For a CA-issued production PFX:

PowerShell:
  $env:DGSM_PFX_PATH = 'C:\Certificates\publisher-codesign.pfx'
  .\build_windows.ps1

The signer prompts for the PFX password securely. For CI, DGSM_PFX_PASSWORD may
be supplied as a protected secret environment variable.

Useful build switches:
  .\build_windows.ps1 -KeepBuildCache
  .\build_windows.ps1 -NoToolchainInstall -SkipSigning

WINDOWS OUTPUTS
---------------
  dist\windows\exe\DiscordGlobalStatusMonitor.exe
  dist\windows\dll\discord_global_status.dll
  dist\windows\signing\signing-report.txt
  dist\windows\DiscordGlobalStatusMonitor-Windows-x64.zip
  dist\windows\DiscordGlobalStatusMonitor-Windows-x64.zip.sha256

The EXE bundles CPython and its Python dependencies. The CFFI DLL is a
Python-backed C ABI library and requires the matching CPython minor runtime
and companion source/backend directory. It is not a standalone executable.

LINUX BUILD
-----------
  chmod +x build_linux.sh
  ./build_linux.sh

Outputs include a Python zipapp, CPython-ABI-specific C shared library, ABI
manifest, detached RSA/SHA-256 signatures and SHA-256 sums. Detached local
signatures prove integrity using the supplied development certificate; they
do not establish public publisher identity.

DIAGNOSTICS
-----------
  python3 discord_server_global_status.py --doctor
  python3 discord_server_global_status.py --once
  python3 discord_server_global_status.py --history
  python3 discord_server_global_status.py --telemetry-history
  python3 discord_server_global_status.py --ripe-atlas-ids 12345678,12345679
  python3 discord_server_global_status.py --no-ripe-atlas
  python3 -m unittest -v test_discord_server_global_status.py

TLS verification remains enabled. Do not disable certificate checks to work
around a proxy or trust-store problem. Use --ca-bundle only with an approved
CA certificate.
