# DGSM 3.4.4 — Release Engineering and CI Notes

This document describes how the repository currently builds and validates artifacts. It distinguishes source tests, target-native compilation, local development signing, and public publisher trust.

## 1. Version discipline

`dgsm_version.py` is the canonical version source. For DGSM 3.4.4 it exports:

- `__version__ = "3.4.4"`
- `__version_info__ = (3, 4, 4)`

Windows resource templates, the EXE, DLL, native build metadata, and package metadata must agree with this source. `pyproject.toml` also declares the package version; update it in the same versioned change and add matching release notes.

## 2. CI workflow as implemented

Workflow: `.github/workflows/ci.yml`

| Job | Environment | Work performed |
|---|---|---|
| Offline tests | Ubuntu and Windows; CPython 3.11, 3.12, 3.13, 3.14, 3.15 | Compile key Python modules, run unittest regression suite, build a wheel |
| Linux native build | Ubuntu; CPython 3.13 | Check shell syntax, build zipapp and C ABI shared object, smoke-test artifacts, upload Linux artifact |
| Windows native build | Windows; CPython 3.15 | Parse PowerShell scripts, build EXE and C ABI DLL, upload Windows artifact |

The test matrix contains ten OS/Python combinations (two operating systems multiplied by five Python versions). The native Linux and Windows build jobs depend on the offline test matrix.

### CI signing limitation

The Windows workflow runs:

```powershell
.\build_windows.ps1 -NoToolchainInstall -SkipSigning
```

The uploaded artifact is therefore named `DGSM-Windows-x64-unsigned-CI`. It is a useful native build output, not a publicly signed release. The Linux CI build also sets `DGSM_SKIP_SIGNING=1`. Never describe either CI artifact as CA-signed.

## 3. Windows developer build

Run from a complete source checkout on Windows x64:

```bat
BUILD_AND_SIGN_WINDOWS.bat
```

Equivalent PowerShell entry:

```powershell
.\build_windows.ps1
```

The script currently supports x64 CPython 3.11–3.15. It prefers a compatible installed runtime, creates an isolated build virtual environment, discovers/installs the required Microsoft native toolchain, generates version metadata from the canonical version, and builds:

- `dist\windows\exe\DiscordGlobalStatusMonitor.exe` — one-file console executable with a bundled Python runtime.
- `dist\windows\dll\discord_global_status.dll` — Python-backed C ABI library with the companion source/backend package.
- `dist\windows\DiscordGlobalStatusMonitor-Windows-x64.zip` and its SHA-256 sidecar.
- `dist\windows\signing\signing-report.txt` and timestamped logs under `.build\windows\logs\`.

The DLL must use a compatible CPython minor runtime and its companion source/backend files. It is not an alternative standalone EXE.

### Windows build gates

A proper local release build performs the following gates in order:

1. Resolve a supported x64 CPython runtime and the MSVC/Windows SDK toolchain.
2. Build the EXE and CFFI DLL in the isolated build environment.
3. Compile and apply DLL icon/version resources; validate the standard resource IDs.
4. Verify embedded file/product version metadata.
5. Run EXE help/version and DLL C ABI smoke tests.
6. Sign both PE artifacts, when signing is enabled.
7. Verify Authenticode signatures and repeat artifact smoke tests.
8. Generate the SHA-256 manifest, validate ZIP contents, and write the signing/build reports.

The Windows resource update must precede signing. Any post-sign modification to a PE file invalidates its Authenticode signature and requires signing and verification again.

## 4. Signing and trust model

DGSM supports two materially different certificate modes:

- **Self-signed development certificate:** created/reused in the current user's certificate store and locally trusted in that Windows profile. This is useful for local integrity/signature testing. It does not create a publicly trusted publisher identity, SmartScreen reputation, or trust on another computer.
- **CA-issued publisher PFX:** configure `DGSM_PFX_PATH` (or the supported build parameter) to use a code-signing certificate issued by a trusted CA. Provide the password securely at the prompt; avoid persistent environment variables or shell history.

The signing script resolves Microsoft's SignTool from an installed Windows SDK or uses the documented Microsoft SDK BuildTools fallback. It signs with SHA-256 Authenticode, attempts RFC 3161 timestamp services, verifies the signatures, and writes a signing report. The exact result—including whether timestamping succeeded—must be read from that report.

A valid local self-signed Authenticode signature and a publicly trusted publisher signature are not equivalent.

## 5. Linux build

Run on the intended Linux target:

```bash
chmod +x build_linux.sh
./build_linux.sh
```

The builder creates an isolated build environment, a portable Python zipapp, and a CFFI shared library. The native `.so` is tied to the selected CPython minor ABI and target platform; it is not a Windows DLL or a universal binary. `BUILD_ABI.txt` records the interpreter/platform ABI details. The build runs artifact smoke tests and creates local detached-signature/checksum outputs unless `DGSM_SKIP_SIGNING=1` is set.

The Linux native build requires CPython 3.10+ and matching development headers, plus a C compiler and CFFI build dependency. The Python package/CLI declares Python 3.9+ support; that does not lower the native Linux build's separate toolchain floor.

## 6. Release checklist

Before publishing a versioned GitHub Release:

- [ ] Confirm the canonical version, package version, and release-notes filename agree.
- [ ] Review the diff and run the offline regression suite.
- [ ] Confirm the current main-branch CI run succeeds.
- [ ] Build each native artifact on its target OS; do not infer Windows behavior from Linux or vice versa.
- [ ] On Windows, verify version resources, all C ABI exports, Authenticode signatures, post-sign smoke tests, and the signing report.
- [ ] On Linux, record the interpreter/ABI details and verify the zipapp/shared-library smoke tests and checksum/signature outputs.
- [ ] Compute SHA-256 hashes for the exact final files; do not reuse hashes from an earlier build.
- [ ] Attach the release ZIP, separately useful binaries/source support files, release notes, checksums, and signing report as appropriate.
- [ ] Label unsigned artifacts unambiguously. Never call CI's `-SkipSigning` output signed.
- [ ] Create a version tag that points at the exact reviewed commit and verify the tag/asset association after publishing.

An annotated Git tag can be created from the checked-out release commit with:

```bash
git tag -a v3.4.4 -m "DGSM 3.4.4"
git push origin v3.4.4
```

Use a new semantic version and matching tag for subsequent releases; do not move an existing published version tag to different source content.

## 7. Artifact provenance

Keep these identities attached to every published asset:

- Repository and exact source commit SHA.
- Release/tag name and canonical application version.
- Build workflow run URL (for CI artifacts).
- Target OS, architecture, and Python ABI where relevant.
- SHA-256 checksum of the exact distributed file.
- Signing mode and verification result.

CI artifacts are temporary workflow outputs. A GitHub Release asset is a separate, durable release attachment. Downloading a workflow artifact does not automatically publish a GitHub Release.

## 8. Current repository references

- Main build instructions: `README.md`
- Windows build/sign details: `BUILD_SIGNING_WINDOWS.txt` and `BUILD_TROUBLESHOOTING.txt`
- Linux artifact details: `README_Linux_Artifacts.txt`
- Data-source boundaries: `BACKENDS.txt`
- Security policy: `SECURITY.md`
- Current versioned notes: `Discord_Global_Status_Monitor_v3.4.4_RELEASE_NOTES.txt`
- CI definition: `.github/workflows/ci.yml`
