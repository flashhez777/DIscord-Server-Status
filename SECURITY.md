# Security policy

## Reporting

Do not submit secrets in issues or pull requests. Redact API tokens, signing
keys, private certificates, private-key material, passwords and user-specific
paths from logs and screenshots.

Report vulnerabilities privately through the repository's GitHub Security
Advisories feature when enabled by the repository owner. Include the affected
version/commit, operating system, Python version, reproduction steps and impact.
Do not include live credentials or private signing material in a report.

## Security boundaries

DGSM reads Discord's public Statuspage, makes unauthenticated public endpoint
probes, and optionally reads operator-selected existing RIPE Atlas measurements.
It does not need a Discord bot token or user account. Do not add guild
monitoring, bot login, account activity collection or private Discord data.

## Transport and credentials

TLS certificate and hostname verification must remain enabled. Do not add a
verify-disabled fallback for certificate errors. Requests to Discord,
Globalping and RIPE Atlas are HTTPS-only and hostname-allowlisted. Redirects
must remain on the same HTTPS origin; never forward credentials across origins
or downgrade to HTTP.

Keep GLOBALPING_API_TOKEN and RIPE_ATLAS_API_KEY in environment variables or
another secret store. Never log credential values or include them in JSON,
telemetry history, release notes, screenshots or artifacts. Measurement
creation POST requests must not be automatically retried.

## Runtime dependencies and local data

Automatic optional package installation is disabled by default. The explicit
--auto-install option uses HTTPS PyPI and binary wheels only. Core monitoring
must continue through standard-library fallbacks when optional packages are
not installed.

Local state and history files can contain provider-derived telemetry. Keep
POSIX data directories private (0700) and state files private (0600), refuse
symlink state files where supported, and write state snapshots using unique
atomic temporary files. Do not commit generated state/history files.

## Review and release

Before release, run the regression suite on Windows and Linux, audit the build
and optional dependency sets with pip-audit, inspect the Windows signing
report, verify checksums, and distinguish a self-signed development
certificate from a CA-issued publicly trusted publisher certificate.

See docs/SECURITY_AND_RELEASE_CHECKLIST.md for the current maintainer checklist.
