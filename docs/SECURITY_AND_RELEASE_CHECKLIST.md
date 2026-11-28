# DGSM 3.4.5 Security and Release Checklist

This checklist is the maintained release gate for the current 3.4.5 security
and correctness change. Check each item against the exact commit/tag being
released; do not reuse results or hashes from an earlier build.

## 1. Source and version integrity

- [ ] dgsm_version.py is the canonical version source (3.4.5).
- [ ] pyproject.toml, README title, release notes, Windows version resources,
  Linux package metadata and CI artifact paths match the canonical version.
- [ ] The feature branch diff is reviewed; no secrets, tokens, private keys,
  local user data, build caches or generated binaries are committed.
- [ ] The project scope remains worldwide public Discord status and independent
  network telemetry only. No guild, bot-login, account or private Discord data.

## 2. DNS correctness (Globalping)

- [ ] Re-run the regression fixture representing Buffalo, Los Angeles, Sydney,
  Lagos and Sao Paulo with the same four A records in different orders.
- [ ] Confirm sorted/de-duplicated RRsets compare equal even when each resolver
  reports a different remaining TTL (139, 300, 300, 175 and 86 seconds).
- [ ] Confirm TTL remains visible per answer and is not included in the RRset
  identity comparison.
- [ ] Confirm NOERROR plus zero A/AAAA answers is NODATA/WARN, not UP.
- [ ] Confirm NXDOMAIN is WARN and SERVFAIL is DOWN.
- [ ] Confirm malformed A/AAAA data, TTL conflicts and truncated provider
  result sets are surfaced without crashing or being marked healthy.
- [ ] Confirm global answer-set variance is informational; geographically
  different CDN answers are not automatically declared an outage.

## 3. Network and credential safety

- [ ] TLS certificate and hostname verification are required everywhere.
- [ ] API hosts are allowlisted: discordstatus.com, discord.com,
  cdn.discordapp.com, api.globalping.io and atlas.ripe.net.
- [ ] Plain HTTP, cross-origin redirects, HTTPS downgrade redirects,
  embedded credentials and non-standard HTTPS ports are rejected.
- [ ] Globalping only accepts the supported measurement/limits endpoints and
  safe measurement IDs. Measurement-creation POST is never auto-retried.
- [ ] Globalping and RIPE Atlas bearer/API keys never appear in dashboard,
  JSON output, diagnostics, logs, release notes or artifacts.
- [ ] Provider JSON is size/count bounded; numeric metrics reject NaN,
  infinity, booleans and negative values.
- [ ] Optional packages are not installed on first launch without the explicit
  --auto-install opt-in. The opt-in installer is restricted to HTTPS PyPI
  and binary wheels.

## 4. Local state and output safety

- [ ] POSIX data/vendor directories are private (0700) and state/log files
  are private (0600).
- [ ] State reads/writes refuse symlink files where the platform supports
  O_NOFOLLOW; Windows behavior is verified on a real Windows runner.
- [ ] State snapshots use unique temporary files, flush/fsync and atomic
  replacement. No fixed .tmp collision remains.
- [ ] Remote strings are sanitized before terminal output; ANSI/OSC and
  bidirectional controls cannot alter the terminal display.
- [ ] Event/telemetry history remains bounded and malformed lines do not crash
  history viewing.

## 5. Automated validation gates

- [ ] Run python -m py_compile on all application/backend/build modules.
- [ ] Run python -m unittest -v test_discord_server_global_status.py.
- [ ] Run pip-audit against build and optional dependency requirement files.
- [ ] Pass the Windows + Ubuntu CPython 3.11, 3.12, 3.13, 3.14 and 3.15 tests.
- [ ] Build the Python wheel.
- [ ] Build the Linux zipapp and C ABI shared object on Linux; run smoke tests.
- [ ] Build the Windows EXE and C ABI DLL on Windows; validate resource IDs,
  version metadata, exported symbols and pre-/post-sign smoke tests.
- [ ] Inspect every CI job, including the dependency audit; a queued or
  in-progress job is not a pass.

## 6. Windows signature and release artifact gates

- [ ] Verify DiscordGlobalStatusMonitor.exe and discord_global_status.dll
  with signtool verify /pa /all /v or Get-AuthenticodeSignature.
- [ ] Record the signing mode and signer identity in signing-report.txt.
- [ ] Development-signed artifacts must be labelled development-signed /
  self-signed — not publicly trusted. Include only the public .cer, never
  the signing private key.
- [ ] For a publicly trusted release, configure a CA-issued code-signing PFX
  through the protected release secret, use a secure password prompt/secret,
  timestamp the signatures and verify the resulting certificate chain.
- [ ] Run the EXE --version/--help smoke tests and DLL ABI smoke test after
  signing. Any post-sign file change requires re-signing.
- [ ] Validate ZIP contents, compare package version/resource versions, and
  generate SHA-256 from the exact final ZIP and binaries.
- [ ] Release notes, commit SHA, tag, artifact checksums and signing report are
  attached or linked to the published release.

## 7. Post-release verification

- [ ] Download the published ZIP from the GitHub Release page and verify its
  SHA-256 against the published manifest.
- [ ] Test installation and launch on a clean Windows x64 system.
- [ ] Confirm the signing state is accurately represented by Windows and by
  the included signing report/certificate.
- [ ] Run one live --once --json check and one continuous dashboard session.
- [ ] Confirm the worldwide DNS output shows the same normalized four-address
  RRset for the supplied example, while preserving TTL differences.
- [ ] Record any live-provider or target-host issue as a follow-up; offline
  tests do not establish that every region/provider is currently reachable.

## Security scope note

A completed checklist is evidence about the tested commit and environments,
not a mathematical proof that every vulnerability has been removed. Keep
reviewing dependency advisories, provider schema changes, Windows/Linux build
behavior and newly reported vulnerabilities for future maintenance releases.
