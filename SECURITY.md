# Security policy

Do not submit secrets in issues or pull requests. Redact API tokens, signing
keys, private certificates, and user-specific paths from logs.

The monitor uses public Discord status data and optional third-party network
measurement providers. It does not need a Discord bot token or user account.

Report vulnerabilities privately through the repository's GitHub Security
Advisories feature when enabled by the repository owner.

TLS certificate and hostname verification must remain enabled. Do not add a
verify-disabled fallback for certificate errors.
