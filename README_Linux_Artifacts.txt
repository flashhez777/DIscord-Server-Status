DISCORD GLOBAL STATUS MONITOR 3.4.1 - LINUX ARTIFACTS
====================================================

Target build:
  OS: Linux x86-64
  Interpreter: CPython 3.13.5
  Native library: CFFI embedding ABI for CPython 3.13
  CFFI build version: 2.0.0 (local tested artifact; release build script installs CFFI >= 2.1.1)

FILES
-----
DiscordGlobalStatusMonitor.pyz
  Portable Python zipapp. Requires Python 3.9+ and bootstraps optional
  runtime telemetry dependencies into a private per-user application folder.

discord_global_status.so
  C ABI shared object, ABI-specific to the CPython minor version shown in
  BUILD_ABI.txt. It is not a Windows DLL. The C ABI exports the functions
  declared in discord_global_status.h.

discord_server_global_status.py, backends/
  Companion Python source required by the C ABI shared object. Place these
  next to the .so or set DGSM_HOME to their directory.

dgsm signatures
---------------
The .sig files are detached RSA/SHA-256 signatures. Verify them using the
included public development certificate:

  openssl x509 -in DGSM-Development-CodeSigning-Certificate.pem \
    -pubkey -noout > dgsm-public.pem
  openssl dgst -sha256 -verify dgsm-public.pem \
    -signature DiscordGlobalStatusMonitor.pyz.sig \
    DiscordGlobalStatusMonitor.pyz
  openssl dgst -sha256 -verify dgsm-public.pem \
    -signature discord_global_status.so.sig \
    discord_global_status.so

The development certificate is self-signed. It provides integrity checking
against this included public key, not a publicly trusted publisher identity.
