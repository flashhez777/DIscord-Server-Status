DISCORD GLOBAL STATUS MONITOR 3.4.4 - LINUX ARTIFACTS
====================================================

BUILD ON THE TARGET HOST
------------------------
Run:
  chmod +x build_linux.sh
  ./build_linux.sh

The builder creates:
  * DiscordGlobalStatusMonitor.pyz — portable Python zipapp (Python 3.9+).
  * discord_global_status.so — native C ABI library compiled for the selected
    CPython minor ABI. It is not a Windows DLL or a universal Linux binary.
  * discord_global_status.h and companion monitor/backend source files.
  * BUILD_ABI.txt recording the interpreter, SOABI, CFFI and platform details.
  * LICENSE and the matching 3.4.4 release notes.
  * Detached RSA/SHA-256 signatures and checksums for local signed builds.

BUILD REQUIREMENTS
------------------
The native build requires:
  * Linux x86-64.
  * CPython 3.10+ and matching development headers.
  * A C compiler (GCC or Clang).
  * CFFI >= 2.1.1 and setuptools in a private build environment.

The .so is tied to the CPython minor ABI recorded in BUILD_ABI.txt. It requires
the same compatible Python runtime and the companion source/backend directory.
Place these files beside the shared object or set DGSM_HOME.

SIGNING
-------
Local Linux builds use the project's development certificate unless signing is
skipped with DGSM_SKIP_SIGNING=1. The certificate is self-signed: detached
signatures prove integrity relative to that included public key, not a publicly
trusted publisher identity.

GitHub Actions uploads target-native Linux build artifacts for each successful
workflow run. Prebuilt binaries are not maintained as source-controlled files;
build them on the target host or retrieve the matching CI artifact.
