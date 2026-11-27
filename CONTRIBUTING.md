# Contributing to DGSM

DGSM monitors Discord's published worldwide service health. Contributions
must retain that scope; do not add guild membership collection, bot login,
Discord account tracking, or private user telemetry.

## Local checks

```bash
python -m py_compile discord_server_global_status.py backends/*.py discord_monitor_dll_build.py
python -m unittest -v test_discord_server_global_status.py
```

## Backend guidelines

New remote telemetry providers belong in `backends/` and should:

- Use HTTPS with normal certificate and hostname verification.
- Have explicit request timeouts and bounded response sizes.
- Respect provider rate limits and `Retry-After` responses.
- Prefer latest-result endpoints over downloading unbounded history.
- Label the vantage point (local, remote probe, country/region) and provider.
- Never claim external measurements are Discord-internal telemetry.
- Avoid logging API keys, cookies, or bearer tokens.
- Be optional: provider failures must not hide the official Discord status feed.

## Pull requests

Include offline unit tests for payload normalization, error handling, and
rate-limit behavior. Do not commit credentials, private signing keys, runtime
logs, build artifacts, or generated `__pycache__` directories.
