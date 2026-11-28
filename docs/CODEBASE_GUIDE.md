# DGSM 3.4.4 — Annotated Codebase Guide

This guide maps the implementation that exists in the 3.4.4 source tree. It is intended for maintainers reviewing changes, diagnosing failures, or adding a new global telemetry provider.

## 1. Project contract and measurement model

DGSM monitors **Discord's public, worldwide service health**. It does not log in to Discord, use a bot token, inspect guilds, collect account activity, or read private Discord infrastructure telemetry.

Keep these data planes separate when interpreting or displaying results:

| Data plane | Source / vantage | What it can establish |
|---|---|---|
| Official service status | Discord Statuspage API | Discord's published global indicator, components, incidents, updates, and maintenance |
| Local connectivity | The host running DGSM | Whether that host can reach Discord REST, Gateway, CDN, DNS, and TLS endpoints |
| Globalping | Independent community probes | External ICMP, HTTP, DNS, CDN, and route observations from selected remote probes |
| RIPE Atlas | Operator-selected existing measurements | Independent remote probe observations for those measurement IDs |
| Host network counters | Local operating system | Interface throughput, packet rates, errors, and drops on the DGSM host |

The Statuspage is the authoritative *published service-health feed*. Globalping and RIPE Atlas are independent network vantage points. A failed ICMP probe alone is not proof of a Discord outage; ICMP can be filtered. Correlate symptoms with HTTP, DNS, TLS, Gateway, and the official status feed.

## 2. Runtime lifecycle

The primary entry point is `discord_server_global_status.py:main()`.

1. `parse_args()` validates the CLI, interval floors, probe counts, RIPE Atlas IDs, and a conservative estimate of Globalping usage.
2. DGSM resolves a per-user state directory (or `--data-dir` override), then optionally bootstraps quality-of-life packages into a private `vendor/` directory. It does not modify system Python packages.
3. `initialize_tls_context()` builds a verified TLS context. Hostname checks and certificate verification remain enabled; an additional PEM bundle may extend trust.
4. The official Statuspage feed and local probes are collected concurrently. Changes are compared with the prior snapshot, logged, and optionally notified.
5. Periodic Globalping jobs run in a bounded executor. They do not block the normal dashboard while remote probes are pending.
6. If measurement IDs are configured, RIPE Atlas uses its own worker. It reads existing latest results only and is independent of Globalping scheduling.
7. Completed telemetry is normalized, stored as JSONL history, and rendered in the dashboard or one-shot JSON output.
8. Executors are shut down in the `finally` block. Ctrl+C is handled as a normal monitor stop.

For `--once` and `--json`, the monitor waits for the measurements explicitly requested for that invocation, subject to bounded timeouts. Continuous mode keeps the dashboard responsive while remote work is pending.

## 3. Main application: `discord_server_global_status.py`

This module is intentionally the coordinator and user interface; provider-specific RIPE Atlas HTTP/result handling lives in `backends/ripe_atlas.py`.

| Area | Important functions | Responsibility |
|---|---|---|
| CLI and paths | `parse_args()`, `state_directory()` | Options, validation, per-user state location |
| Optional runtime | `bootstrap_optional_dependencies()`, `_module_satisfies()` | Private optional package installation and graceful fallbacks |
| TLS and diagnostics | `initialize_tls_context()`, `tls_context()`, `tls_failure_message()`, `run_doctor()` | Verified trust context and actionable diagnostics |
| HTTP/status source | `request_json()`, `fetch_official_summary()` | Bounded JSON HTTP access and official Statuspage summary |
| Local connectivity | `probe_rest_api()`, `probe_gateway_websocket()`, `probe_cdn()`, `probe_local_dns()`, `run_local_probes()` | Host-local Discord endpoint checks |
| Globalping | `globalping_request()`, `fetch_globalping_limits()`, `build_globalping_measurement_payload()`, `create_globalping_measurement()`, `normalize_globalping_result()`, `run_global_telemetry()` | Remote probe requests, quota awareness, payload construction, normalization |
| RIPE Atlas bridge | `run_ripe_atlas()` | Calls the optional backend and keeps it separate from Globalping |
| Host metrics | `local_network_counters()`, `calculate_local_network_rates()` | Local interface counters and rate deltas |
| Status model | `build_snapshot()`, `detect_changes()`, `component_groups()` | Normalized status snapshot and change detection |
| Persistence | `load_json_file()`, `save_json_atomic()`, `append_event_log()`, `append_telemetry_log()` | Atomic state snapshot and bounded event/telemetry history |
| Presentation | `render_dashboard()`, `output_json()`, `show_history()`, `show_telemetry_history()` | Human-readable dashboard and machine-readable output |
| Orchestration | `main()` | Worker scheduling, lifecycle, persistence, notifications, and exit status |

The main state files are:

- `last_state.json` — last normalized official status snapshot.
- `events.jsonl` — status, probe, and provider transition events.
- `worldwide_telemetry.jsonl` — completed telemetry samples.

Default per-user state locations are:

- Windows: `%LOCALAPPDATA%\DiscordGlobalStatusMonitor`
- macOS: `~/Library/Application Support/DiscordGlobalStatusMonitor`
- Linux: `$XDG_STATE_HOME/DiscordGlobalStatusMonitor`, or `~/.local/state/DiscordGlobalStatusMonitor` when XDG_STATE_HOME is unset.

## 4. Backend: `backends/ripe_atlas.py`

This module is a read-only adapter for **existing** RIPE Atlas measurements.

- `parse_measurement_ids()` accepts positive, comma-separated numeric IDs, removes duplicates, and caps a cycle at eight IDs.
- `_get_json()` calls the latest-results endpoint, requests the latest result version per probe with a seven-day lookback, applies a response-size cap, and handles common HTTP/rate-limit/TLS failures.
- `_extract_rtts()` extracts packet RTT samples or supported aggregate RTT values without inventing packet-loss data.
- `normalize_results()` converts provider records into probe rows and summary statistics.
- `collect()` fetches configured measurements concurrently and reports partial failures separately.

The optional key is read from the configured environment-variable name (default `RIPE_ATLAS_API_KEY`). It is not printed in normal telemetry output. DGSM does not create, schedule, or modify Atlas measurements.

## 5. Build and native interface map

| File | Role |
|---|---|
| `dgsm_version.py` | Single canonical application/artifact version |
| `pyproject.toml` | Python package metadata, CLI entry point, optional dependency groups |
| `build_windows.ps1` | Windows x64 toolchain discovery, isolated build, EXE/DLL compilation, metadata, smoke tests, signing orchestration, package and logs |
| `BUILD_AND_SIGN_WINDOWS.bat` | User-facing Windows build launcher |
| `sign_windows.ps1` | SignTool discovery, certificate selection, Authenticode signing, signature verification, signing report |
| `assets/DGSM.rc` | Windows DLL icon and version-resource template |
| `embed_windows_resources.py` | Parses RC `.res` records, validates required resource identities, and applies them with Win32 UpdateResourceW |
| `discord_monitor_dll_build.py` | CFFI embedding build for the native C ABI library |
| `discord_global_status.h` | Public C ABI declarations |
| `dll_smoke_test.py` | Loads the native library and validates expected exports/version without starting network activity |
| `build_linux.sh` | Linux zipapp and native C shared-library build |
| `sign_linux.sh` | Local detached-signature flow for Linux artifacts |
| `create_python_zipapp.py` | Python zipapp packaging helper |
| `RUN_DISCORD_STATUS.bat`, `RUN_DISCORD_STATUS.sh` | Prefer compiled artifacts when present, otherwise launch source with Python |

### C ABI exports

The C header and CFFI builder expose four functions:

- `dgsm_get_version(buffer, buffer_size)`
- `dgsm_get_last_error(buffer, buffer_size)`
- `dgsm_run_once()`
- `dgsm_run_monitor()`

The first two copy UTF-8 text into a caller-provided buffer and return the number of bytes written (excluding the trailing NUL), or a negative value for an invalid buffer. The run functions return an integer monitor status. The native library is a Python-backed integration library, not a fully self-contained runtime: it needs a compatible CPython **minor** runtime and the companion monitor source/backend files. The standalone Windows EXE bundles its Python runtime.

### Windows resource ordering

The DLL resource pipeline is deliberately ordered:

1. Generate RC metadata from the canonical version.
2. Compile `assets/DGSM.rc` with the Windows SDK `rc.exe`.
3. Require RT_VERSION (type 16, numeric name ID 1), RT_GROUP_ICON (type 14, numeric name ID 1), and at least one RT_ICON (type 3).
4. Apply the resource records to the linked DLL using Win32 UpdateResourceW.
5. Verify product/version metadata and run native smoke tests.
6. Sign the final PE binary, then verify its Authenticode signature and re-run smoke tests.

The RC template uses `1 VERSIONINFO`, not a string-named `VS_VERSION_INFO`, to preserve the standard numeric resource identity. Resource mutation must happen **before** Authenticode signing because modifying a signed PE invalidates its signature.

## 6. Tests and continuous integration

`test_discord_server_global_status.py` uses Python's `unittest` framework and offline fakes/mocks. Its main groups are:

- `TLSAndNetworkTests` — verified TLS invariants, CA bundle handling, bounded Statuspage responses, retry behavior, and rate-limit handling.
- `TelemetryTests` — CLI/RIPE Atlas wiring, JSON one-shot behavior, latency summaries, DNS normalization, and status change handling.
- `GlobalpingRequestSchemaTests` — Globalping HTTP target/options validation and request payload shape.
- `RipeAtlasBackendTests` — measurement ID validation, latest-result normalization, and read-only collection.
- `WindowsBuildBootstrapTests` — Windows Python/toolchain/signing/resource/packaging contracts and cross-platform build-script checks.

The GitHub Actions workflow runs the offline suite on Windows and Ubuntu across CPython 3.11–3.15, builds a wheel in the test matrix, and has native Linux and Windows artifact jobs. The Windows CI job explicitly uses `-SkipSigning`: a successful CI build is not evidence that a CA-trusted Authenticode release was produced.

Offline tests do not replace live provider validation, a real Windows resource-update execution, native target-host testing, or public-certificate signing.

## 7. Extension checklist

Before adding or changing a provider:

1. Keep the global-only scope; do not add guild, account, bot-login, or private user-data collection.
2. Label the provider and vantage point in every result and dashboard/JSON view.
3. Use verified HTTPS, explicit timeouts, response-size caps, and provider rate-limit handling.
4. Avoid automatic retries for non-idempotent operations.
5. Keep provider credentials in environment variables or a secret manager; never log them.
6. Normalize provider errors as provider errors, not Discord outages.
7. Add offline tests for success, malformed payloads, timeout, rate limit, and partial-result behavior.
8. Update `BACKENDS.txt`, the tests, and this map when an architectural contract changes.

## 8. Source of truth

For current behavior, inspect the source and build workflow rather than assuming this guide outranks them. Version comes from `dgsm_version.py`; backend behavior from `discord_server_global_status.py` and `backends/`; build behavior from the target build scripts; and CI behavior from `.github/workflows/ci.yml`.
