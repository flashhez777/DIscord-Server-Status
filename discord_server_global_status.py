#!/usr/bin/env python3
"""Discord Global Status Monitor

Cross-platform (Windows / Linux / macOS) live monitor for Discord's worldwide
public service status, incidents, maintenance, voice regions and client status.
It also offers clearly-labelled local connectivity probes from this machine.

No Discord account, bot token, or API key is needed. The core monitor uses
Python's standard library. Optional notification/color packages are installed
automatically into a private app-local folder when possible.
Python 3.9+ recommended.
"""

from __future__ import annotations

import argparse
import base64
import concurrent.futures
import ctypes
import hashlib
import ipaddress
import importlib
import importlib.util
import json
import math
import os
import platform
import random
import re
import shutil
import socket
import ssl
import statistics
import subprocess
import sys
import tempfile
import textwrap
import unicodedata
import time
import urllib.error
import urllib.request
from urllib.parse import urlsplit
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from backends.http_transport import safe_urlopen
from dgsm_version import __version__ as VERSION

APP_NAME = "Discord Global Status Monitor"
STATUS_PAGE = "https://discordstatus.com"
SUMMARY_URL = "https://discordstatus.com/api/v2/summary.json"
REST_GATEWAY_URL = "https://discord.com/api/v10/gateway"
CDN_TEST_URL = "https://cdn.discordapp.com/embed/avatars/0.png"
GATEWAY_HOST = "gateway.discord.gg"
GATEWAY_PORT = 443
USER_AGENT = f"DiscordGlobalStatusMonitor/{VERSION} (+{STATUS_PAGE})"
GLOBALPING_API = "https://api.globalping.io/v1"
GLOBALPING_MEASUREMENTS = GLOBALPING_API + "/measurements"
GLOBALPING_RATE_LIMITS = GLOBALPING_API + "/limits"
GLOBALPING_DEFAULT_TARGET = "gateway.discord.gg"
MAX_GLOBALPING_RESULT_ROWS = 100
MAX_DNS_ANSWER_RECORDS = 64
DNS_RCODE_NAMES = {
    0: "NOERROR", 1: "FORMERR", 2: "SERVFAIL", 3: "NXDOMAIN",
    4: "NOTIMP", 5: "REFUSED", 6: "YXDOMAIN", 7: "YXRRSET",
    8: "NXRRSET", 9: "NOTAUTH", 10: "NOTZONE", 11: "DSOTYPENI",
    16: "BADVERS",
}

# ANSI palette. ANSI mode is enabled on Windows terminals where supported.
RESET = "\033[0m"
BOLD = "\033[1m"
DIM = "\033[2m"
RED = "\033[91m"
GREEN = "\033[92m"
YELLOW = "\033[93m"
ORANGE = "\033[38;5;208m"
CYAN = "\033[96m"
MAGENTA = "\033[95m"
WHITE = "\033[97m"

SPARK = "▁▂▃▄▅▆▇█"

# Optional quality-of-life packages are installed into an app-local vendor
# directory, never into Kali's/system Python environment. The core monitor
# itself remains standard-library-only and continues working if pip/network
# access is unavailable.
RUNTIME_CAPABILITIES: list[str] = []
_TLS_CONTEXT: Optional[ssl.SSLContext] = None
_TLS_CA_SOURCES: list[str] = []
_TLS_EXTRA_CA_BUNDLE: Optional[Path] = None


@dataclass
class ProbeResult:
    name: str
    state: str
    latency_ms: Optional[float] = None
    detail: str = ""


class MonitorError(Exception):
    """Expected monitor/API error with a user-readable message."""


def now_local() -> datetime:
    return datetime.now().astimezone()


def iso_now() -> str:
    return now_local().isoformat(timespec="seconds")


def state_directory() -> Path:
    """Return a per-user state directory on Windows, Linux, and macOS."""
    if os.name == "nt":
        base = os.environ.get("LOCALAPPDATA")
        root = Path(base) if base else Path.home() / "AppData" / "Local"
    elif sys.platform == "darwin":
        root = Path.home() / "Library" / "Application Support"
    else:
        base = os.environ.get("XDG_STATE_HOME")
        root = Path(base) if base else Path.home() / ".local" / "state"
    return root / "DiscordGlobalStatusMonitor"


def enable_windows_ansi() -> bool:
    """Enable ANSI escape processing in modern Windows consoles."""
    if os.name != "nt":
        return True
    try:
        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        handle = kernel32.GetStdHandle(-11)  # STD_OUTPUT_HANDLE
        mode = ctypes.c_uint()
        if not kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
            return False
        enable_vt = 0x0004
        return bool(kernel32.SetConsoleMode(handle, mode.value | enable_vt))
    except Exception:
        return False


def color_enabled(disabled: bool) -> bool:
    if disabled or os.environ.get("NO_COLOR") is not None:
        return False
    if not sys.stdout.isatty() or os.environ.get("TERM", "") == "dumb":
        return False
    return enable_windows_ansi()


def paint(value: str, color: str, enabled: bool, bold: bool = False) -> str:
    if not enabled:
        return value
    return f"{BOLD if bold else ''}{color}{value}{RESET}"


def status_color(status: str) -> str:
    value = (status or "unknown").strip().lower().replace(" ", "_")
    if value in {"operational", "none", "resolved", "completed", "up", "healthy"}:
        return GREEN
    if value in {"minor", "degraded_performance", "monitoring", "verifying", "maintenance", "scheduled"}:
        return YELLOW
    if value in {"partial_outage", "major", "identified", "investigating", "in_progress"}:
        return ORANGE
    if value in {"major_outage", "critical", "down", "failed", "unreachable"}:
        return RED
    return CYAN


def status_label(status: str) -> str:
    return (status or "unknown").replace("_", " ").upper()


_ANSI_ESCAPE_RE = re.compile(
    r"\x1B(?:\[[0-?]*[ -/]*[@-~]|\][^\x07\x1B]*(?:\x07|\x1B\\))"
    r"|\x9B[0-?]*[ -/]*[@-~]|\x9D[^\x07\x9C]*(?:\x07|\x9C)"
)
_BIDI_CONTROL_CODEPOINTS = (
    {0x061C, 0x200E, 0x200F}
    | set(range(0x202A, 0x202F))
    | set(range(0x2066, 0x206A))
)


def clip(value: Any, width: int) -> str:
    """Bound untrusted text and remove terminal/bidirectional control injection."""
    if width <= 0:
        return ""
    raw = _ANSI_ESCAPE_RE.sub("", str(value))
    safe_chars: list[str] = []
    for character in raw:
        codepoint = ord(character)
        category = unicodedata.category(character)
        if character in "\r\n\t":
            safe_chars.append(" ")
        elif category in {"Cc", "Cs"} or codepoint in _BIDI_CONTROL_CODEPOINTS:
            continue
        else:
            safe_chars.append(character)
    text = " ".join("".join(safe_chars).split())
    if len(text) <= width:
        return text
    if width == 1:
        return "…"
    return text[: width - 1] + "…"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="discord_server_global_status.py",
        description="Automatic worldwide Discord status, incident, region and maintenance monitor.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("-i", "--interval", type=float, default=30,
                        help="Refresh period in seconds (minimum 10).")
    parser.add_argument("-t", "--timeout", type=float, default=8,
                        help="Timeout for each network request in seconds.")
    parser.add_argument("--once", action="store_true", help="Fetch once and exit.")
    parser.add_argument("--json", action="store_true", help="Print one machine-readable JSON snapshot and exit.")
    parser.add_argument("--history", action="store_true", help="Show locally saved status-change history and exit.")
    parser.add_argument("--no-probes", action="store_true",
                        help="Disable local REST/Gateway/CDN connectivity probes.")
    parser.add_argument("--no-save", action="store_true",
                        help="Do not persist state or event history to disk.")
    parser.add_argument("--no-color", action="store_true", help="Disable terminal colors.")
    parser.add_argument("--no-clear", action="store_true", help="Do not clear terminal between refreshes.")
    parser.add_argument("--beep", action="store_true", help="Beep when a meaningful status change is detected.")
    parser.add_argument("--no-notifications", action="store_true",
                        help="Disable native desktop notifications.")
    parser.add_argument("--no-auto-install", action="store_true",
                        help="Do not automatically install optional packages.")
    parser.add_argument("--no-globalping", action="store_true",
                        help="Disable third-party worldwide measurements (Globalping).")
    parser.add_argument("--global-probes", type=int, default=10,
                        help="Number of worldwide probes for the periodic ping test (1-50).")
    parser.add_argument("--global-interval", type=float, default=600,
                        help="Worldwide ping interval in seconds (default: 10 minutes).")
    parser.add_argument("--http-interval", type=float, default=900,
                        help="Worldwide Discord HTTP test interval in seconds (default: 15 minutes).")
    parser.add_argument("--dns-interval", type=float, default=1800,
                        help="Worldwide DNS test interval in seconds (default: 30 minutes).")
    parser.add_argument("--cdn-interval", type=float, default=1800,
                        help="Worldwide Discord CDN HTTP test interval in seconds (default: 30 minutes).")
    parser.add_argument("--trace-interval", type=float, default=3600,
                        help="Worldwide MTR route-trace interval in seconds (default: 60 minutes).")
    parser.add_argument("--trace-probes", type=int, default=3,
                        help="Worldwide MTR probe count (1-10).")
    parser.add_argument("--trace-now", action="store_true",
                        help="Run a worldwide MTR route trace immediately at startup.")
    parser.add_argument("--globalping-token-env", default="GLOBALPING_API_TOKEN",
                        help="Environment variable containing an optional Globalping bearer token.")
    parser.add_argument("--ripe-atlas-ids", default=os.environ.get("RIPE_ATLAS_MEASUREMENT_IDS", ""),
                        help="Comma-separated existing RIPE Atlas measurement IDs (read-only; defaults to RIPE_ATLAS_MEASUREMENT_IDS).")
    parser.add_argument("--ripe-atlas-interval", type=float, default=900,
                        help="RIPE Atlas latest-results refresh interval in seconds (minimum 300).")
    parser.add_argument("--ripe-atlas-api-key-env", default="RIPE_ATLAS_API_KEY",
                        help="Environment variable containing an optional RIPE Atlas API key; the key is never printed.")
    parser.add_argument("--no-ripe-atlas", action="store_true",
                        help="Disable optional RIPE Atlas measurements even when measurement IDs are configured.")
    parser.add_argument("--telemetry-history", action="store_true",
                        help="Display saved worldwide telemetry samples and exit.")
    parser.add_argument("--doctor", action="store_true",
                        help="Run local dependency, TLS trust and endpoint diagnostics, then exit.")
    parser.add_argument("--ca-bundle", type=Path, default=None,
                        help="Additional PEM CA bundle for managed/corporate TLS inspection; verification remains enabled.")
    parser.add_argument("--data-dir", type=Path, default=None,
                        help="Override the directory used for local state, vendor packages and event history.")
    parser.add_argument("--version", action="version", version=f"%(prog)s {VERSION}")
    args = parser.parse_args()
    if not math.isfinite(args.interval) or args.interval < 10:
        parser.error("--interval must be finite and at least 10 seconds")
    if not math.isfinite(args.timeout) or not 0 < args.timeout <= 300:
        parser.error("--timeout must be finite and between 0 and 300 seconds")
    if not math.isfinite(args.ripe_atlas_interval) or args.ripe_atlas_interval < 300:
        parser.error("--ripe-atlas-interval must be finite and at least 300 seconds")
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", args.globalping_token_env):
        parser.error("--globalping-token-env must be a valid environment-variable name")
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", args.ripe_atlas_api_key_env):
        parser.error("--ripe-atlas-api-key-env must be a valid environment-variable name")
    if args.no_ripe_atlas:
        args.ripe_atlas_ids = ""
    else:
        from backends.ripe_atlas import BackendError as RipeAtlasError, parse_measurement_ids
        try:
            args.ripe_atlas_ids = ",".join(
                str(value) for value in parse_measurement_ids(args.ripe_atlas_ids)
            )
        except RipeAtlasError as exc:
            parser.error(str(exc))
    if not 1 <= args.global_probes <= 50:
        parser.error("--global-probes must be between 1 and 50")
    if not 1 <= args.trace_probes <= 10:
        parser.error("--trace-probes must be between 1 and 10")
    for name in ("global_interval", "http_interval", "dns_interval", "cdn_interval", "trace_interval"):
        value = getattr(args, name)
        if not math.isfinite(value) or value < 60:
            parser.error(f"--{name.replace('_', '-')} must be finite and at least 60 seconds")
    # Globalping counts probe executions as tests. Keep defaults well below
    # its published anonymous hourly budget and account for custom settings.
    estimated = (
        3600 / args.global_interval * args.global_probes
        + 3600 / args.http_interval * min(10, max(1, args.global_probes - 2))
        + 3600 / args.dns_interval * min(10, max(1, args.global_probes - 2))
        + 3600 / args.cdn_interval * min(5, max(1, args.global_probes // 2))
        + 3600 / args.trace_interval * args.trace_probes
    )
    # Local safety guard only; the live /v1/limits response remains authoritative.
    # Keep margin below the currently documented public/authenticated limits.
    budget = 450 if os.environ.get(args.globalping_token_env) else 220
    if estimated > budget:
        parser.error(
            f"selected Globalping schedule estimates {estimated:.0f} tests/hour; "
            f"limit is kept below {budget}/hour. Increase telemetry intervals or "
            "reduce probe counts (an optional bearer token is supported)."
        )
    return args


def _version_tuple(value: Any) -> tuple[int, ...]:
    """Extract a comparable numeric version tuple without extra dependencies."""
    numbers = re.findall(r"\d+", str(value or ""))
    return tuple(int(part) for part in numbers[:4]) if numbers else ()


def _module_satisfies(module_name: str, minimum_version: Optional[str] = None) -> bool:
    try:
        module = importlib.import_module(module_name)
    except Exception:
        return False
    if minimum_version is None:
        return True
    current = getattr(module, "__version__", None)
    if current is None:
        try:
            from importlib import metadata
            current = metadata.version(module_name)
        except Exception:
            return False
    return _version_tuple(current) >= _version_tuple(minimum_version)


def bootstrap_optional_dependencies(args: argparse.Namespace, data_dir: Path) -> None:
    """Install small optional UX packages into a private app-local directory.

    This avoids sudo, global pip changes, and Kali/Debian PEP 668 conflicts.
    The monitor has a standard-library fallback if installation fails.
    """
    packages: list[tuple[str, str, Optional[str]]] = [("certifi", "certifi>=2026.11.20", "2026.11.20")]
    if not args.no_notifications:
        packages.append(("plyer", "plyer>=2.1.0", "2.1.0"))
    else:
        RUNTIME_CAPABILITIES.append("Desktop notifications disabled")
    # psutil provides per-interface host counters; dnspython adds resolver
    # timing and answer visibility. Both have standard-library fallbacks.
    packages.append(("psutil", "psutil>=6.0.0", "6.0.0"))
    packages.append(("dns", "dnspython>=2.7.0", "2.7.0"))
    if os.name == "nt":
        packages.append(("colorama", "colorama>=0.4.6", "0.4.6"))

    vendor_dir = data_dir / "vendor"
    ensure_private_directory(vendor_dir)
    if str(vendor_dir) not in sys.path:
        sys.path.insert(0, str(vendor_dir))

    # Some Windows/Kali Python builds do not ship pip. Try Python's bundled
    # ensurepip bootstrap first, scoped to the current user (never sudo).
    if not args.no_auto_install and not getattr(sys, "frozen", False):
        try:
            pip_check = subprocess.run(
                [sys.executable, "-m", "pip", "--version"],
                capture_output=True, text=True, timeout=20, check=False
            )
            if pip_check.returncode != 0:
                pip_bootstrap = subprocess.run(
                    [sys.executable, "-m", "ensurepip", "--upgrade", "--user"],
                    capture_output=True, text=True, timeout=120, check=False
                )
                if pip_bootstrap.returncode == 0:
                    RUNTIME_CAPABILITIES.append("pip: bootstrapped with ensurepip")
                else:
                    detail = (pip_bootstrap.stderr or pip_bootstrap.stdout or "ensurepip unavailable").strip()
                    RUNTIME_CAPABILITIES.append(f"pip: automatic bootstrap unavailable ({clip(detail, 150)})")
        except (OSError, subprocess.TimeoutExpired) as exc:
            RUNTIME_CAPABILITIES.append(f"pip: bootstrap check failed ({clip(exc, 100)})")

    for module_name, requirement, minimum_version in packages:
        # Do not let an old, globally installed certifi silently win over the
        # bundled CA update. This was a common cause of certificate-chain
        # failures on systems with stale Python trust bundles.
        if _module_satisfies(module_name, minimum_version):
            RUNTIME_CAPABILITIES.append(f"{module_name}: ready (minimum {minimum_version})")
            continue

        if getattr(sys, "frozen", False):
            RUNTIME_CAPABILITIES.append(f"{module_name}: not bundled; built-in fallback active")
            continue

        if args.no_auto_install:
            RUNTIME_CAPABILITIES.append(f"{module_name}: missing (auto-install disabled)")
            continue

        try:
            vendor_dir.mkdir(parents=True, exist_ok=True)
            command = [
                sys.executable, "-m", "pip", "install",
                "--disable-pip-version-check", "--quiet", "--upgrade",
                "--target", str(vendor_dir), requirement,
            ]
            result = subprocess.run(
                command, capture_output=True, text=True, timeout=180, check=False
            )
            importlib.invalidate_caches()
            if str(vendor_dir) not in sys.path:
                sys.path.insert(0, str(vendor_dir))
            if result.returncode == 0:
                # Force a fresh import so a stale module already present in
                # sys.modules cannot mask the new app-local package.
                sys.modules.pop(module_name, None)
            if result.returncode == 0 and _module_satisfies(module_name, minimum_version):
                RUNTIME_CAPABILITIES.append(f"{module_name}: installed/updated automatically")
            else:
                detail = (result.stderr or result.stdout or "pip did not provide details").strip()
                RUNTIME_CAPABILITIES.append(
                    f"{module_name}: unavailable; standard-library/native fallback active"
                )
                if detail:
                    print(f"[dependency] {module_name} install skipped: {clip(detail, 220)}", file=sys.stderr)
        except (OSError, subprocess.TimeoutExpired) as exc:
            RUNTIME_CAPABILITIES.append(
                f"{module_name}: install failed; fallback active ({clip(exc, 100)})"
            )

    # Colorama fixes ANSI handling in older Windows consoles when available.
    if os.name == "nt":
        try:
            colorama = importlib.import_module("colorama")
            colorama.just_fix_windows_console()
        except Exception:
            pass


def send_desktop_notification(title: str, message: str) -> bool:
    """Send a best-effort native desktop notification on supported desktops."""
    message = clip(message, 450)
    try:
        plyer = importlib.import_module("plyer")
        notification = getattr(plyer, "notification", None)
        if notification is None:
            # fromlist triggers Plyer's facade export on versions that do not
            # eagerly expose it as a package attribute.
            notification = __import__("plyer", fromlist=["notification"]).notification
        notify = getattr(notification, "notify", None)
        if notify is not None:
            notify(title=title, message=message, app_name=APP_NAME, timeout=8)
            return True
    except Exception:
        pass

    # Native fallbacks require no Python packages.
    try:
        if sys.platform.startswith("linux") and shutil.which("notify-send"):
            result = subprocess.run(
                ["notify-send", "--app-name", APP_NAME, title, message],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=5, check=False
            )
            return result.returncode == 0
        if sys.platform == "darwin" and shutil.which("osascript"):
            script = f"display notification {json.dumps(message)} with title {json.dumps(title)}"
            result = subprocess.run(
                ["osascript", "-e", script],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=5, check=False
            )
            return result.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        pass
    return False


def initialize_tls_context(extra_ca_bundle: Optional[Path] = None) -> ssl.SSLContext:
    """Build one strict TLS context using OS roots plus certifi and optional extra roots.

    This deliberately never turns off certificate or hostname verification. A
    user-supplied bundle only adds trust anchors; it does not weaken validation.
    """
    global _TLS_CONTEXT, _TLS_CA_SOURCES, _TLS_EXTRA_CA_BUNDLE
    try:
        context = ssl.create_default_context(purpose=ssl.Purpose.SERVER_AUTH)
    except (OSError, ssl.SSLError) as exc:
        # A broken SSL_CERT_FILE/OS CA path should not prevent trying certifi.
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        context.check_hostname = True
        context.verify_mode = ssl.CERT_REQUIRED
        RUNTIME_CAPABILITIES.append(
            f"OS trust-store load failed; using certifi fallback ({clip(exc, 120)})"
        )
    context.check_hostname = True
    context.verify_mode = ssl.CERT_REQUIRED
    try:
        if hasattr(ssl, "TLSVersion"):
            context.minimum_version = ssl.TLSVersion.TLSv1_2
    except (AttributeError, ValueError):
        pass

    sources = ["OS/default trust store"]
    certifi_loaded = False
    for certifi_module_name in ("certifi", "pip._vendor.certifi"):
        try:
            certifi = importlib.import_module(certifi_module_name)
            certifi_bundle = Path(certifi.where()).expanduser()
            if certifi_bundle.is_file():
                context.load_verify_locations(cafile=str(certifi_bundle))
                sources.append(f"{certifi_module_name} ({certifi_bundle})")
                certifi_loaded = True
                break
        except (ImportError, OSError, AttributeError, ssl.SSLError) as exc:
            RUNTIME_CAPABILITIES.append(
                f"{certifi_module_name} CA bundle unavailable: {clip(exc, 120)}"
            )
    if not certifi_loaded:
        RUNTIME_CAPABILITIES.append("No certifi CA bundle could be loaded; OS roots remain enabled")

    # Respect common explicit CA settings in addition to Python's built-in
    # SSL_CERT_FILE/SSL_CERT_DIR handling. Loading is additive on this context.
    extra_candidates: list[Path] = []
    for env_name in ("REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE"):
        env_value = os.environ.get(env_name)
        if env_value:
            extra_candidates.append(Path(env_value).expanduser())
    if extra_ca_bundle is not None:
        extra_candidates.append(extra_ca_bundle.expanduser())

    seen: set[str] = set()
    for bundle in extra_candidates:
        key = str(bundle.resolve()) if bundle.exists() else str(bundle)
        if key in seen:
            continue
        seen.add(key)
        if not bundle.is_file():
            raise MonitorError(f"Additional CA bundle does not exist or is not a file: {bundle}")
        try:
            context.load_verify_locations(cafile=str(bundle))
            sources.append(f"additional CA ({bundle})")
        except (OSError, ssl.SSLError) as exc:
            raise MonitorError(f"Could not load CA bundle {bundle}: {exc}") from exc

    _TLS_CONTEXT = context
    _TLS_CA_SOURCES = sources
    _TLS_EXTRA_CA_BUNDLE = extra_ca_bundle
    RUNTIME_CAPABILITIES.append("TLS: certificate and hostname verification enabled")
    RUNTIME_CAPABILITIES.append(f"TLS trust: {len(sources)} source(s)")
    return context


def tls_context() -> ssl.SSLContext:
    """Return the initialized verified TLS context, creating it if needed."""
    global _TLS_CONTEXT
    if _TLS_CONTEXT is None:
        return initialize_tls_context(_TLS_EXTRA_CA_BUNDLE)
    return _TLS_CONTEXT


def tls_failure_message(exc: BaseException) -> Optional[str]:
    """Return an actionable diagnosis for certificate-chain verification errors."""
    current: Any = exc
    visited: set[int] = set()
    while current is not None and id(current) not in visited:
        visited.add(id(current))
        if isinstance(current, ssl.SSLCertVerificationError):
            reason = getattr(current, "verify_message", None) or str(current)
            return (
                f"TLS certificate verification failed ({reason}). The monitor keeps strict "
                "verification enabled and uses the OS trust store plus certifi. On Kali, "
                "run `sudo apt install --reinstall ca-certificates && sudo update-ca-certificates`. "
                "For an inspected corporate proxy, export its approved root CA as PEM and "
                "pass `--ca-bundle /path/to/corporate-root.pem` (Windows: `--ca-bundle C:\\path\\root.pem`)."
            )
        current = getattr(current, "reason", None) or getattr(current, "__cause__", None)
    return None


def run_doctor(args: argparse.Namespace) -> int:
    """Print trust-store details and test the three core HTTPS origins."""
    print(f"{APP_NAME} {VERSION} - diagnostics")
    print(f"Platform       : {platform.platform()}")
    print(f"Python         : {sys.version.split()[0]}")
    print(f"OpenSSL        : {ssl.OPENSSL_VERSION}")
    defaults = ssl.get_default_verify_paths()
    print(f"Default cafile : {defaults.cafile or 'not configured'}")
    print(f"Default capath : {defaults.capath or 'not configured'}")
    print(f"SSL_CERT_FILE  : {os.environ.get('SSL_CERT_FILE', '(not set)')}")
    print(f"SSL_CERT_DIR   : {os.environ.get('SSL_CERT_DIR', '(not set)')}")
    try:
        context = tls_context()
        print(f"TLS verify     : enabled (CERT_REQUIRED; hostname checks enabled)")
        print(f"Trusted roots  : {len(context.get_ca_certs())} certificates loaded")
        try:
            certifi = importlib.import_module("certifi")
            print(f"certifi        : {getattr(certifi, '__version__', 'unknown')} ({certifi.where()})")
        except Exception:
            print("certifi        : unavailable; OS trust store remains active")
        for source in _TLS_CA_SOURCES:
            print(f"  - {source}")
    except Exception as exc:
        print(f"TLS setup      : FAILED - {exc}")
        return 2

    endpoints = [
        ("Discord status", SUMMARY_URL),
        ("Discord REST", REST_GATEWAY_URL),
        ("Globalping API", GLOBALPING_RATE_LIMITS),
    ]
    failures = 0
    for label, url in endpoints:
        try:
            _payload, elapsed = request_json(url, args.timeout)
            print(f"{label:<16}: OK ({elapsed:.1f} ms)")
        except Exception as exc:
            failures += 1
            detail = tls_failure_message(exc) or str(exc)
            print(f"{label:<16}: FAILED - {clip(detail, 260)}")
    if failures:
        print("\nDiagnostics found connection failures. The program did not disable TLS verification.")
        return 1
    print("\nAll HTTPS endpoint checks succeeded.")
    return 0


def percentile(values: list[float], percentile_value: float) -> Optional[float]:
    """Calculate an interpolated percentile without requiring NumPy."""
    if not values:
        return None
    ordered = sorted(float(value) for value in values)
    if len(ordered) == 1:
        return ordered[0]
    position = (len(ordered) - 1) * min(100.0, max(0.0, percentile_value)) / 100.0
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    weight = position - lower
    return ordered[lower] * (1.0 - weight) + ordered[upper] * weight


def request_json(url: str, timeout: float) -> tuple[dict[str, Any], float]:
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "application/json",
            "Cache-Control": "no-cache",
        },
        method="GET",
    )
    start = time.perf_counter()
    try:
        with safe_urlopen(
            request, timeout=timeout, context=tls_context(),
            allowed_hosts={"discordstatus.com", "discord.com"},
        ) as response:
            code = getattr(response, "status", 200)
            if code != 200:
                raise MonitorError(f"HTTP {code}")
            raw = response.read(2 * 1024 * 1024 + 1)
            if len(raw) > 2 * 1024 * 1024:
                raise MonitorError("Discord Statuspage summary exceeded the 2 MiB response safety limit")
    except urllib.error.HTTPError as exc:
        retry_after = exc.headers.get("Retry-After") if exc.headers else None
        suffix = f"; Retry-After: {retry_after}s" if retry_after else ""
        raise MonitorError(f"HTTP {exc.code} {exc.reason}{suffix}") from exc
    except urllib.error.URLError as exc:
        tls_detail = tls_failure_message(exc)
        if tls_detail:
            raise MonitorError(tls_detail) from exc
        raise MonitorError(f"Network error: {exc.reason}") from exc
    except TimeoutError as exc:
        raise MonitorError("Request timed out") from exc
    except OSError as exc:
        tls_detail = tls_failure_message(exc)
        if tls_detail:
            raise MonitorError(tls_detail) from exc
        raise MonitorError(f"Connection error: {exc}") from exc

    elapsed = (time.perf_counter() - start) * 1000.0
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise MonitorError(f"Invalid JSON response: {exc}") from exc
    if not isinstance(data, dict):
        raise MonitorError("Unexpected API response (expected JSON object)")
    return data, elapsed


def fetch_official_summary(timeout: float) -> tuple[dict[str, Any], float]:
    data, elapsed = request_json(SUMMARY_URL, timeout)
    if not isinstance(data.get("status"), dict):
        raise MonitorError("Discord status feed did not contain a status object")
    if not isinstance(data.get("components"), list):
        raise MonitorError("Discord status feed did not contain a components list")
    return data, elapsed


def probe_rest_api(timeout: float) -> ProbeResult:
    start = time.perf_counter()
    try:
        data, _ = request_json(REST_GATEWAY_URL, timeout)
        gateway_url = data.get("url")
        if not isinstance(gateway_url, str) or not gateway_url.startswith("wss://"):
            return ProbeResult("REST API", "WARN", (time.perf_counter() - start) * 1000,
                               "HTTP responded, but Gateway URL was missing")
        return ProbeResult("REST API", "UP", (time.perf_counter() - start) * 1000,
                           "GET /api/v10/gateway returned a valid Gateway URL")
    except Exception as exc:
        return ProbeResult("REST API", "DOWN", (time.perf_counter() - start) * 1000, str(exc))


def recv_http_headers(sock: ssl.SSLSocket, timeout: float) -> bytes:
    sock.settimeout(timeout)
    data = bytearray()
    while b"\r\n\r\n" not in data:
        chunk = sock.recv(4096)
        if not chunk:
            break
        data.extend(chunk)
        if len(data) > 16384:
            raise MonitorError("Gateway returned oversized HTTP headers")
    if b"\r\n\r\n" not in data:
        raise MonitorError("Gateway closed before completing the WebSocket handshake")
    return bytes(data).split(b"\r\n\r\n", 1)[0]


def probe_gateway_websocket(timeout: float) -> ProbeResult:
    """Perform an unauthenticated RFC 6455 handshake against Discord Gateway.

    This checks Gateway WebSocket edge reachability from this machine only; it
    does not identify a Discord user or establish a persistent client session.
    """
    started = time.perf_counter()
    raw_sock: Optional[socket.socket] = None
    tls_sock: Optional[ssl.SSLSocket] = None
    key = base64.b64encode(os.urandom(16)).decode("ascii")
    try:
        addresses = socket.getaddrinfo(GATEWAY_HOST, GATEWAY_PORT, type=socket.SOCK_STREAM)
        last_error: Optional[BaseException] = None
        context = tls_context()
        for family, socktype, proto, _, sockaddr in addresses:
            try:
                raw_sock = socket.socket(family, socktype, proto)
                raw_sock.settimeout(timeout)
                raw_sock.connect(sockaddr)
                tls_sock = context.wrap_socket(raw_sock, server_hostname=GATEWAY_HOST)
                raw_sock = None  # Ownership transferred to TLS socket.
                break
            except Exception as exc:
                last_error = exc
                if tls_sock:
                    try:
                        tls_sock.close()
                    except OSError:
                        pass
                    tls_sock = None
                if raw_sock:
                    try:
                        raw_sock.close()
                    except OSError:
                        pass
                    raw_sock = None
        if tls_sock is None:
            tls_detail = tls_failure_message(last_error) if last_error else None
            raise MonitorError(tls_detail or str(last_error or "Could not connect to Gateway"))

        request = (
            "GET /?v=10&encoding=json HTTP/1.1\r\n"
            f"Host: {GATEWAY_HOST}\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            "Sec-WebSocket-Version: 13\r\n"
            "Origin: https://discord.com\r\n"
            f"User-Agent: {USER_AGENT}\r\n\r\n"
        )
        tls_sock.sendall(request.encode("ascii"))
        headers = recv_http_headers(tls_sock, timeout).decode("iso-8859-1", errors="replace")
        lines = headers.split("\r\n")
        status_line = lines[0] if lines else ""
        if " 101 " not in f" {status_line} ":
            raise MonitorError(f"Gateway rejected WebSocket handshake: {status_line or 'no status line'}")
        parsed_headers: dict[str, str] = {}
        for line in lines[1:]:
            if ":" in line:
                name, value = line.split(":", 1)
                parsed_headers[name.strip().lower()] = value.strip()
        expected = base64.b64encode(
            hashlib.sha1((key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode("ascii")).digest()
        ).decode("ascii")
        if parsed_headers.get("sec-websocket-accept") != expected:
            raise MonitorError("Gateway WebSocket handshake returned an invalid accept key")
        latency = (time.perf_counter() - started) * 1000.0
        return ProbeResult("Gateway WS", "UP", latency, "WebSocket upgrade accepted (HTTP 101)")
    except Exception as exc:
        detail = tls_failure_message(exc) or str(exc)
        return ProbeResult("Gateway WS", "DOWN", (time.perf_counter() - started) * 1000.0, detail)
    finally:
        if tls_sock:
            try:
                tls_sock.close()
            except OSError:
                pass
        if raw_sock:
            try:
                raw_sock.close()
            except OSError:
                pass


def probe_cdn(timeout: float) -> ProbeResult:
    start = time.perf_counter()
    request = urllib.request.Request(
        CDN_TEST_URL,
        headers={"User-Agent": USER_AGENT, "Accept": "image/png,*/*;q=0.8"},
        method="GET",
    )
    try:
        with safe_urlopen(
            request, timeout=timeout, context=tls_context(),
            allowed_hosts={"cdn.discordapp.com"},
        ) as response:
            code = getattr(response, "status", 200)
            # Read a small amount to verify that the endpoint actually serves data.
            sample = response.read(64)
            elapsed = (time.perf_counter() - start) * 1000.0
            if code == 200 and sample:
                return ProbeResult("Media CDN", "UP", elapsed, "Discord CDN image endpoint returned data")
            return ProbeResult("Media CDN", "WARN", elapsed, f"HTTP {code}; response body was empty")
    except urllib.error.HTTPError as exc:
        return ProbeResult("Media CDN", "DOWN", (time.perf_counter() - start) * 1000.0,
                           f"HTTP {exc.code} {exc.reason}")
    except Exception as exc:
        detail = tls_failure_message(exc) or str(exc)
        return ProbeResult("Media CDN", "DOWN", (time.perf_counter() - start) * 1000.0, detail)


def run_local_probes(timeout: float) -> list[ProbeResult]:
    tasks = [probe_rest_api, probe_gateway_websocket, probe_cdn, probe_local_dns]
    results: list[ProbeResult] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as executor:
        futures = {executor.submit(task, timeout): task.__name__ for task in tasks}
        for future in concurrent.futures.as_completed(futures):
            try:
                results.append(future.result())
            except Exception as exc:
                results.append(ProbeResult(futures[future], "ERROR", None, str(exc)))
    order = {"REST API": 0, "Gateway WS": 1, "Media CDN": 2, "Local DNS": 3}
    return sorted(results, key=lambda item: order.get(item.name, 99))


def globalping_request(method: str, url: str, timeout: float,
                       token: Optional[str] = None,
                       payload: Optional[dict[str, Any]] = None) -> tuple[dict[str, Any], dict[str, str]]:
    """Call only the official Globalping HTTPS API with bounded safe retries."""
    normalized_method = method.upper()
    if normalized_method not in {"GET", "POST"}:
        raise MonitorError("Globalping requests are limited to GET and POST.")
    try:
        parsed_url = urlsplit(url)
        port = parsed_url.port
    except (TypeError, ValueError) as exc:
        raise MonitorError(f"Invalid Globalping API URL: {exc}") from exc
    if (
        parsed_url.scheme.casefold() != "https"
        or (parsed_url.hostname or "").rstrip(".").casefold() != "api.globalping.io"
        or port not in (None, 443)
        or parsed_url.username is not None
        or parsed_url.password is not None
        or parsed_url.query
        or parsed_url.fragment
    ):
        raise MonitorError("Globalping requests must use the official api.globalping.io HTTPS origin.")
    if not math.isfinite(timeout) or not 0 < timeout <= 300:
        raise MonitorError("Globalping request timeout must be finite and between 0 and 300 seconds.")
    allowed_paths = {"/v1/limits", "/v1/measurements"}
    measurement_path = re.fullmatch(r"/v1/measurements/[A-Za-z0-9_-]{1,128}", parsed_url.path)
    if parsed_url.path not in allowed_paths and not measurement_path:
        raise MonitorError("Globalping API path is not an allowed DGSM endpoint.")
    if normalized_method == "POST" and parsed_url.path != "/v1/measurements":
        raise MonitorError("Globalping POST is restricted to measurement creation.")
    if normalized_method == "GET" and payload is not None:
        raise MonitorError("Globalping GET requests cannot include a request body.")
    if normalized_method == "POST" and not isinstance(payload, dict):
        raise MonitorError("Globalping measurement creation requires a JSON object payload.")
    if token is not None and (
        not isinstance(token, str) or not token or len(token) > 8192
        or "\r" in token or "\n" in token
    ):
        raise MonitorError("Globalping bearer token contains invalid characters or exceeds the size limit.")
    try:
        body = None if payload is None else json.dumps(payload, allow_nan=False).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise MonitorError(f"Globalping request payload is not valid JSON: {exc}") from exc
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "application/json",
        "Accept-Encoding": "identity",
        "Cache-Control": "no-cache",
    }
    if body is not None:
        headers["Content-Type"] = "application/json"
    if token:
        headers["Authorization"] = f"Bearer {token}"

    attempts = 3 if normalized_method == "GET" else 1
    last_error: Optional[BaseException] = None
    for attempt in range(attempts):
        request = urllib.request.Request(
            url, data=body, headers=headers, method=normalized_method
        )
        try:
            with safe_urlopen(
                request,
                timeout=timeout,
                context=tls_context(),
                allowed_hosts={"api.globalping.io"},
            ) as response:
                raw = response.read(4 * 1024 * 1024 + 1)
                if len(raw) > 4 * 1024 * 1024:
                    raise MonitorError("Globalping response exceeded the 4 MiB safety limit")
                response_headers = {
                    str(k).lower(): str(v) for k, v in response.headers.items()
                }
                data = json.loads(raw.decode("utf-8")) if raw else {}
                if not isinstance(data, dict):
                    raise MonitorError("Globalping returned a non-object JSON response")
                return data, response_headers

        except urllib.error.HTTPError as exc:
            detail = ""
            try:
                raw_error = exc.read(64 * 1024)
                error_data = json.loads(raw_error.decode("utf-8")) if raw_error else {}
                if isinstance(error_data, dict):
                    err = error_data.get("error")
                    params = {}
                    if isinstance(err, dict):
                        detail = str(err.get("message") or "")
                        params = err.get("params") if isinstance(err.get("params"), dict) else {}
                    elif err:
                        detail = str(err)
                    elif error_data.get("message"):
                        detail = str(error_data.get("message"))
                    if not params and isinstance(error_data.get("params"), dict):
                        params = error_data["params"]
                    if params:
                        parameter_text = "; ".join(
                            f"{key}: {value}" for key, value in sorted(params.items())
                        )
                        detail = f"{detail}; parameters: {parameter_text}" if detail else f"parameters: {parameter_text}"
                    errors = error_data.get("errors")
                    if errors and not detail:
                        detail = clip(errors, 1000)
            except Exception:
                pass
            retry = exc.headers.get("Retry-After") if exc.headers else None
            suffix = f"; Retry-After={retry}s" if retry else ""
            if exc.code == 429:
                raise MonitorError(
                    f"Globalping rate limit (HTTP 429){suffix}. Wait for the provider reset "
                    f"or reduce measurements; optional authentication uses {argsafe_env_name()}."
                ) from exc
            if normalized_method == "GET" and exc.code in {500, 502, 503, 504} and attempt + 1 < attempts:
                time.sleep(0.5 * (2 ** attempt) + random.uniform(0.0, 0.25))
                last_error = exc
                continue
            raise MonitorError(
                f"Globalping HTTP {exc.code}: {detail or exc.reason}{suffix}"
            ) from exc

        except urllib.error.URLError as exc:
            tls_detail = tls_failure_message(exc)
            if tls_detail:
                raise MonitorError(f"Globalping TLS error: {tls_detail}") from exc
            if normalized_method == "GET" and attempt + 1 < attempts:
                last_error = exc
                time.sleep(0.5 * (2 ** attempt) + random.uniform(0.0, 0.25))
                continue
            raise MonitorError(f"Globalping network error: {exc.reason}") from exc

        except (TimeoutError, OSError) as exc:
            tls_detail = tls_failure_message(exc)
            if tls_detail:
                raise MonitorError(f"Globalping TLS error: {tls_detail}") from exc
            if normalized_method == "GET" and attempt + 1 < attempts:
                last_error = exc
                time.sleep(0.5 * (2 ** attempt) + random.uniform(0.0, 0.25))
                continue
            raise MonitorError(f"Globalping connection error: {exc}") from exc

        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise MonitorError(f"Globalping returned invalid JSON: {exc}") from exc

    raise MonitorError(f"Globalping request failed after retries: {last_error}")


def argsafe_env_name() -> str:
    return "GLOBALPING_API_TOKEN (optional bearer token)"


def numeric(value: Any) -> Optional[float]:
    """Parse finite, non-negative network metrics; reject booleans and NaN/Inf."""
    if value is None or value == "" or isinstance(value, bool):
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if not math.isfinite(parsed) or parsed < 0:
        return None
    return parsed


def fetch_globalping_limits(args: argparse.Namespace) -> dict[str, Any]:
    """Fetch the provider-reported current measurement quota/credit state."""
    token = os.environ.get(args.globalping_token_env) or None
    payload, _headers = globalping_request(
        "GET", GLOBALPING_RATE_LIMITS, args.timeout, token
    )
    rate_limit = payload.get("rateLimit") if isinstance(payload.get("rateLimit"), dict) else {}
    measurements = rate_limit.get("measurements") if isinstance(rate_limit.get("measurements"), dict) else {}
    create = measurements.get("create") if isinstance(measurements.get("create"), dict) else {}
    if not create:
        raise MonitorError("Globalping limits response did not include measurement creation limits")
    credits = payload.get("credits") if isinstance(payload.get("credits"), dict) else {}
    return {
        "type": str(create.get("type") or "unknown"),
        "limit": numeric(create.get("limit")),
        "remaining": numeric(create.get("remaining")),
        "reset_seconds": numeric(create.get("reset")),
        "credits_remaining": numeric(credits.get("remaining")),
        "checked_at": iso_now(),
    }


def build_globalping_measurement_payload(
    test_type: str, target: str, probe_limit: int
) -> dict[str, Any]:
    """Build a Globalping v1 payload using hostname and measurement options.

    The API's target field is a hostname/IP. HTTP scheme, port, path, and query
    are conveyed through measurementOptions, never embedded in target.
    """
    if not isinstance(target, str) or not target.strip():
        raise MonitorError("Globalping target must be a non-empty hostname, IP address or URL")
    clean_target = target.strip()
    payload: dict[str, Any] = {
        "type": test_type,
        "target": clean_target,
        "limit": max(1, min(int(probe_limit), 100)),
    }
    if test_type == "ping":
        payload["measurementOptions"] = {"packets": 3}
    elif test_type in {"http", "cdn"}:
        try:
            parsed = urlsplit(clean_target)
            hostname = parsed.hostname
            port = parsed.port
        except ValueError as exc:
            raise MonitorError(f"Invalid Globalping HTTP target: {exc}") from exc
        if parsed.scheme.lower() not in {"http", "https"} or not hostname:
            raise MonitorError("Invalid Globalping HTTP target; expected an http(s) URL with a hostname")
        if port == 0:
            raise MonitorError("Invalid Globalping HTTP target; port must be between 1 and 65535")
        if parsed.username is not None or parsed.password is not None:
            raise MonitorError("Globalping HTTP target must not contain embedded credentials")
        if any(ch.isspace() for ch in hostname):
            raise MonitorError("Invalid Globalping HTTP target; hostname contains whitespace")
        try:
            hostname = hostname.encode("idna").decode("ascii").lower()
        except UnicodeError as exc:
            raise MonitorError(f"Invalid internationalized hostname: {exc}") from exc
        request_options: dict[str, Any] = {"method": "GET", "path": parsed.path or "/"}
        if parsed.query:
            request_options["query"] = parsed.query
        payload["target"] = hostname
        payload["measurementOptions"] = {
            "protocol": parsed.scheme.upper(),
            "port": port or (443 if parsed.scheme.lower() == "https" else 80),
            "request": request_options,
        }
    return payload


def create_globalping_measurement(test_type: str, target: str, probe_limit: int,
                                  timeout: float, token: Optional[str]) -> dict[str, Any]:
    """Submit one asynchronous Globalping test and poll at a conservative interval."""
    payload = build_globalping_measurement_payload(test_type, target, probe_limit)
    started = time.perf_counter()
    created, _ = globalping_request("POST", GLOBALPING_MEASUREMENTS, timeout, token, payload)
    measurement_id = created.get("id")
    if not isinstance(measurement_id, str) or not measurement_id:
        raise MonitorError(f"Globalping did not return a measurement ID: {clip(created, 200)}")

    deadline = time.monotonic() + (75.0 if test_type in {"mtr", "traceroute"} else 45.0)
    final: dict[str, Any] = created
    while time.monotonic() < deadline:
        time.sleep(1.0)
        final, _ = globalping_request("GET", f"{GLOBALPING_MEASUREMENTS}/{measurement_id}", timeout, token)
        status = str(final.get("status") or "").lower()
        if status in {"failed", "error", "cancelled", "canceled"}:
            detail = final.get("error") or final.get("message") or "provider marked measurement failed"
            raise MonitorError(f"Globalping measurement {measurement_id} failed: {clip(detail, 240)}")
        if status not in {"in-progress", "in_progress", "pending", "running", ""}:
            break
    else:
        raise MonitorError(f"Globalping measurement {measurement_id} timed out while waiting for results")

    final["_elapsed_ms"] = (time.perf_counter() - started) * 1000.0
    final["_measurement_id"] = measurement_id
    return final


def _parse_dns_ttl(value: Any) -> Optional[int]:
    """Validate a DNS TTL as an unsigned 31-bit integer."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        ttl = value
    elif isinstance(value, float) and math.isfinite(value) and value.is_integer():
        ttl = int(value)
    elif isinstance(value, str) and value.isascii() and value.isdigit() and len(value) <= 10:
        try:
            ttl = int(value)
        except ValueError:
            return None
    else:
        return None
    return ttl if 0 <= ttl <= 2_147_483_647 else None


def normalize_dns_answers(answers: Any, fallback_target: str) -> dict[str, Any]:
    """Canonicalize DNS RRsets; answer order and cache TTL do not define identity."""
    raw_answers = answers if isinstance(answers, list) else []
    truncated = len(raw_answers) > MAX_DNS_ANSWER_RECORDS
    grouped: dict[tuple[str, str, str], dict[str, Any]] = {}
    rrset_ttls: dict[tuple[str, str], set[int]] = {}
    invalid_records = 0
    missing_ttls = 0

    for answer in raw_answers[:MAX_DNS_ANSWER_RECORDS]:
        if not isinstance(answer, dict):
            invalid_records += 1
            continue
        raw_name = answer.get("name") or fallback_target
        if not isinstance(raw_name, str):
            invalid_records += 1
            continue
        name = raw_name.strip().lower().rstrip(".")
        if (
            not name or len(name) > 253
            or any(ch.isspace() or ord(ch) < 33 for ch in name)
        ):
            invalid_records += 1
            continue
        name += "."

        raw_type = answer.get("type")
        if not isinstance(raw_type, str):
            invalid_records += 1
            continue
        record_type = raw_type.strip().upper()
        if not re.fullmatch(r"[A-Z][A-Z0-9-]{0,15}", record_type):
            invalid_records += 1
            continue

        raw_value = answer.get("value")
        if not isinstance(raw_value, (str, int, float)) or isinstance(raw_value, bool):
            invalid_records += 1
            continue
        raw_value_text = str(raw_value).strip()
        if not raw_value_text or len(raw_value_text) > 1024:
            invalid_records += 1
            continue
        value = clip(raw_value_text, 1024)
        if record_type in {"A", "AAAA"}:
            try:
                address = ipaddress.ip_address(value)
                if (record_type == "A" and address.version != 4) or (
                    record_type == "AAAA" and address.version != 6
                ):
                    raise ValueError("address family does not match record type")
                value = str(address)
            except ValueError:
                invalid_records += 1
                continue

        ttl = _parse_dns_ttl(answer.get("ttl"))
        if ttl is None:
            missing_ttls += 1

        identity = (name.casefold(), record_type, value)
        record = grouped.setdefault(
            identity,
            {"name": name, "type": record_type, "value": value, "_ttls": set()},
        )
        if ttl is not None:
            record["_ttls"].add(ttl)
            rrset_ttls.setdefault((name.casefold(), record_type), set()).add(ttl)

    ordered_records = sorted(
        grouped.values(),
        key=lambda item: (
            item["name"].casefold(), item["type"],
            item["value"].casefold(), item["value"],
        ),
    )
    normalized_records: list[dict[str, Any]] = []
    rendered_answers: list[str] = []
    answer_set: list[str] = []
    address_answers: list[str] = []
    all_ttls: list[int] = []

    for record in ordered_records:
        ttl_values = sorted(record["_ttls"])
        ttl = ttl_values[0] if ttl_values else None
        normalized_records.append({
            "name": record["name"], "type": record["type"],
            "value": record["value"], "ttl": ttl,
        })
        identity_text = f"{record['name']} {record['type']} {record['value']}"
        answer_set.append(identity_text)
        rendered_answers.append(identity_text + (f" (TTL {ttl})" if ttl is not None else ""))
        if record["type"] in {"A", "AAAA"}:
            address_answers.append(record["value"])
        all_ttls.extend(ttl_values)

    ttl_conflicts = sum(1 for values in rrset_ttls.values() if len(values) > 1)
    ttl_values = sorted(all_ttls)
    return {
        "answer_records": normalized_records,
        "answers": rendered_answers,
        "answer_set": answer_set,
        "address_answers": sorted(set(address_answers)),
        "answer_count": len(normalized_records),
        "ttl_min": min(ttl_values) if ttl_values else None,
        "ttl_max": max(ttl_values) if ttl_values else None,
        "ttl_consistent": ttl_conflicts == 0,
        "ttl_conflicts": ttl_conflicts,
        "missing_ttls": missing_ttls,
        "invalid_records": invalid_records,
        "answers_truncated": truncated,
        "validation_ok": ttl_conflicts == 0 and invalid_records == 0 and not truncated,
    }


def normalize_globalping_result(test_type: str, target: str, data: dict[str, Any]) -> dict[str, Any]:
    """Normalize provider data with bounded rows and validated DNS RRsets."""
    if not isinstance(data, dict):
        raise MonitorError("Globalping measurement response must be a JSON object.")
    rows: list[dict[str, Any]] = []
    all_rtts: list[float] = []
    all_losses: list[float] = []
    raw_results = data.get("results") if isinstance(data.get("results"), list) else []
    results_truncated = len(raw_results) > MAX_GLOBALPING_RESULT_ROWS
    results = raw_results[:MAX_GLOBALPING_RESULT_ROWS]

    for entry in results:
        if not isinstance(entry, dict):
            continue
        probe = entry.get("probe") if isinstance(entry.get("probe"), dict) else {}
        result = entry.get("result") if isinstance(entry.get("result"), dict) else {}
        stats = result.get("stats") if isinstance(result.get("stats"), dict) else {}
        timings = result.get("timings") if isinstance(result.get("timings"), dict) else {}
        loc = ", ".join(str(x) for x in (probe.get("city"), probe.get("country")) if x)
        avg = numeric(stats.get("avg"))
        loss = numeric(stats.get("loss"))
        row: dict[str, Any] = {
            "continent": clip(probe.get("continent") or "?", 40),
            "region": clip(probe.get("region") or "", 48),
            "country": clip(probe.get("country") or "?", 40),
            "city": clip(probe.get("city") or "Unknown", 72),
            "location": clip(loc or probe.get("country") or "Unknown", 96),
            "network": clip(probe.get("network") or "unknown network", 96),
            "asn": clip(probe.get("asn") or "", 32),
            "status": clip(result.get("status") or "unknown", 40),
            "latency_ms": avg,
            "min_ms": numeric(stats.get("min")),
            "max_ms": numeric(stats.get("max")),
            "loss_pct": loss,
            "http_status": result.get("statusCode"),
            "dns_ms": numeric(timings.get("dns")),
            "tcp_ms": numeric(timings.get("tcp")),
            "tls_ms": numeric(timings.get("tls")),
            "ttfb_ms": numeric(timings.get("firstByte")),
            "total_ms": numeric(timings.get("total")),
            "detail": "",
            "hops": [],
        }
        if test_type == "ping":
            if avg is not None:
                all_rtts.append(avg)
            if loss is not None:
                all_losses.append(loss)
            if result.get("error"):
                row["detail"] = str(result.get("error"))
            elif avg is None and result.get("rawOutput"):
                row["detail"] = clip(result.get("rawOutput"), 160)
            row["state"] = "UP" if avg is not None and (loss is None or loss < 100) else "WARN"
        elif test_type in {"http", "cdn"}:
            code = numeric(result.get("statusCode"))
            total = numeric(timings.get("total"))
            row["latency_ms"] = total
            if total is not None:
                all_rtts.append(total)
            row["state"] = "UP" if code is not None and 200 <= code < 400 else "WARN" if code is not None and code < 500 else "DOWN"
            row["detail"] = f"HTTP {int(code)}" if code is not None else str(result.get("error") or result.get("rawOutput") or "No HTTP status")
        elif test_type == "dns":
            dns_time = numeric(timings.get("total"))
            row["latency_ms"] = dns_time
            if dns_time is not None:
                all_rtts.append(dns_time)
            raw_status_code = result.get("statusCode")
            status_name = result.get("statusCodeName")
            if numeric_rcode is not None and numeric_rcode.is_integer():
                status_text = DNS_RCODE_NAMES.get(
                    int(numeric_rcode), clip(raw_status_name or str(int(numeric_rcode)), 32).upper()
                )
            elif raw_status_name not in (None, ""):
                status_text = clip(raw_status_name, 32).upper()
            elif raw_status_code not in (None, ""):
                status_text = clip(raw_status_code, 32).upper()
            else:
                status_text = "UNKNOWN"

            dns_answers = normalize_dns_answers(result.get("answers"), target)
            row.update(dns_answers)
            successful_rcode = numeric_rcode == 0 or status_text in {"NOERROR", "SUCCESS"}
            resolver_responded = (
                numeric_rcode is not None
                or raw_status_name not in (None, "")
                or raw_status_code not in (None, "")
            )
            resolver_error = clip(result.get("error"), 240) if result.get("error") else ""
            answer_count = dns_answers["answer_count"]

            if resolver_error:
                answer_state = "ERROR"
            elif not resolver_responded:
                answer_state = "UNKNOWN"
            elif successful_rcode and answer_count:
                answer_state = "RESOLVED"
            elif successful_rcode:
                answer_state = "NODATA"
            else:
                answer_state = status_text

            row["dns_status"] = status_text
            row["resolver_responded"] = bool(resolver_responded)
            row["dns_answer_state"] = answer_state
            row["dns_error"] = resolver_error or None

            # NOERROR confirms the resolver answered; it does not confirm that
            # the requested gateway A RRset contains usable addresses.
            if resolver_error or status_text == "SERVFAIL":
                row["state"] = "DOWN"
            elif not successful_rcode or answer_count == 0 or not dns_answers["validation_ok"]:
                row["state"] = "WARN"
            else:
                row["state"] = "UP"

            if resolver_error:
                detail = f"DNS resolver error: {resolver_error}"
            elif successful_rcode and answer_count == 0:
                detail = f"DNS {status_text}/NODATA; resolver responded; 0 answer(s)"
            else:
                detail = f"DNS {status_text}; {answer_count} answer(s)"
            if dns_answers["ttl_conflicts"]:
                detail += "; inconsistent TTLs within an RRset"
            if dns_answers["missing_ttls"]:
                detail += f"; {dns_answers['missing_ttls']} record(s) missing a valid TTL"
            if dns_answers["invalid_records"]:
                detail += f"; {dns_answers['invalid_records']} malformed record(s) ignored"
            if dns_answers["answers_truncated"]:
                detail += "; answer list truncated at the safety limit"
            row["detail"] = clip(detail, 320)
        elif test_type in {"mtr", "traceroute"}:
            hops = result.get("hops") if isinstance(result.get("hops"), list) else []
            hop_summaries = []
            for hop in hops:
                if not isinstance(hop, dict):
                    continue
                hop_stats = hop.get("stats") if isinstance(hop.get("stats"), dict) else {}
                hop_avg = numeric(hop_stats.get("avg"))
                hop_summaries.append({
                    "address": clip(hop.get("resolvedAddress") or "*", 96),
                    "hostname": clip(hop.get("resolvedHostname") or "", 128),
                    "avg_ms": hop_avg,
                    "loss_pct": numeric(hop_stats.get("loss")),
                    "asn": hop.get("asn") or [],
                })
            row["hops"] = hop_summaries
            last_hop = next((h for h in reversed(hop_summaries) if h.get("avg_ms") is not None), None)
            row["latency_ms"] = last_hop.get("avg_ms") if last_hop else None
            row["loss_pct"] = last_hop.get("loss_pct") if last_hop else None
            if row["latency_ms"] is not None:
                all_rtts.append(float(row["latency_ms"]))
            row["state"] = "UP" if last_hop else "WARN"
            row["detail"] = f"{len(hop_summaries)} hops; destination {result.get('resolvedAddress') or 'not reached'}"

        rows.append(row)

    good_rtts = sorted(all_rtts)
    median_rtt = statistics.median(good_rtts) if good_rtts else None
    summary = {
        "probe_results": len(rows),
        "successful_samples": len(good_rtts),
        "median_ms": median_rtt,
        "p95_ms": percentile(good_rtts, 95),
        "mean_ms": statistics.mean(good_rtts) if good_rtts else None,
        "min_ms": min(good_rtts) if good_rtts else None,
        "max_ms": max(good_rtts) if good_rtts else None,
        "median_loss_pct": statistics.median(all_losses) if all_losses else None,
        "up": sum(1 for row in rows if row.get("state") == "UP"),
        "warn": sum(1 for row in rows if row.get("state") == "WARN"),
        "down": sum(1 for row in rows if row.get("state") == "DOWN"),
    }
    if test_type == "dns":
        signatures = {tuple(row.get("answer_set", [])) for row in rows}
        summary.update({
            "resolver_responses": sum(1 for row in rows if row.get("resolver_responded")),
            "resolved": sum(1 for row in rows if row.get("dns_answer_state") == "RESOLVED"),
            "nodata": sum(1 for row in rows if row.get("dns_answer_state") == "NODATA"),
            "unique_answer_sets": len(signatures),
            "answer_set_consistent": len(signatures) <= 1,
        })
    return {
        "type": clip(test_type, 24),
        "target": clip(target, 255),
        "status": clip(data.get("status") or "unknown", 40),
        "measurement_id": clip(data.get("_measurement_id") or data.get("id") or "", 128),
        "completed_at": iso_now(),
        "elapsed_ms": numeric(data.get("_elapsed_ms")),
        "probes_count": len(rows),
        "results_truncated": results_truncated,
        "rows": rows,
        "summary": summary,
    }


def run_ripe_atlas(args: argparse.Namespace) -> dict[str, Any]:
    """Read operator-selected existing Atlas measurements; never create tests."""
    from backends.ripe_atlas import collect

    api_key = os.environ.get(args.ripe_atlas_api_key_env) or None
    return collect(args.ripe_atlas_ids, args.timeout, tls_context(), api_key)


def run_global_telemetry(test_type: str, args: argparse.Namespace) -> dict[str, Any]:
    token = os.environ.get(args.globalping_token_env) or None
    targets = {
        "ping": GLOBALPING_DEFAULT_TARGET,
        "http": "https://discord.com/api/v10/gateway",
        "dns": "gateway.discord.gg",
        "cdn": CDN_TEST_URL,
        "mtr": GLOBALPING_DEFAULT_TARGET,
        "traceroute": GLOBALPING_DEFAULT_TARGET,
    }
    limits = {
        "ping": args.global_probes,
        "http": min(10, max(1, args.global_probes - 2)),
        "dns": min(10, max(1, args.global_probes - 2)),
        "cdn": min(5, max(1, args.global_probes // 2)),
        "mtr": args.trace_probes,
        "traceroute": args.trace_probes,
    }
    target = targets[test_type]
    try:
        provider_type = "http" if test_type == "cdn" else test_type
        raw = create_globalping_measurement(provider_type, target, limits[test_type], args.timeout, token)
        normalized = normalize_globalping_result(test_type, target, raw)
        normalized["provider"] = "Globalping community probes"
        normalized["vantage"] = "remote probes worldwide"
        return normalized
    except Exception as exc:
        return {
            "type": test_type,
            "target": target,
            "provider": "Globalping community probes",
            "vantage": "remote probes worldwide",
            "status": "error",
            "completed_at": iso_now(),
            "rows": [],
            "summary": {},
            "error": str(exc),
        }


def ensure_private_directory(path: Path) -> None:
    """Create a private app data directory and reject a symlink at its leaf."""
    path = path.expanduser()
    if path.is_symlink():
        raise MonitorError(f"Refusing to use a symlink as the DGSM data directory: {path}")
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    if not path.is_dir():
        raise MonitorError(f"DGSM data path is not a directory: {path}")
    if os.name != "nt":
        os.chmod(path, 0o700)


def _open_state_file(path: Path, mode: str):
    """Open DGSM state files with POSIX 0600 permissions and no symlink following."""
    path = Path(path)
    if path.is_symlink():
        raise OSError(f"Refusing to follow a symlink state file: {path}")
    if mode == "r":
        flags = os.O_RDONLY
    elif mode == "a":
        flags = os.O_WRONLY | os.O_APPEND | os.O_CREAT
    else:
        raise ValueError("Unsupported DGSM state-file mode.")
    flags |= getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags, 0o600)
    try:
        if os.name != "nt" and hasattr(os, "fchmod"):
            os.fchmod(descriptor, 0o600)
        return os.fdopen(descriptor, mode, encoding="utf-8")
    except Exception:
        os.close(descriptor)
        raise


def append_telemetry_log(path: Path, measurement: dict[str, Any], max_bytes: int = 5_000_000) -> None:
    ensure_private_directory(path.parent)
    try:
        if path.exists() and path.stat().st_size > max_bytes:
            backup = path.with_suffix(".previous.jsonl")
            try:
                if backup.exists():
                    backup.unlink()
                path.replace(backup)
            except OSError:
                pass
        with _open_state_file(path, "a") as handle:
            handle.write(json.dumps(measurement, ensure_ascii=False, allow_nan=False) + "\n")
    except OSError as exc:
        print(f"Warning: could not write telemetry history: {exc}", file=sys.stderr)


def show_telemetry_history(path: Path, limit: int = 30) -> int:
    print(f"{APP_NAME} - worldwide telemetry history")
    print(f"Log: {path}")
    if not path.exists():
        print("No worldwide telemetry samples saved yet.")
        return 0
    rows: deque[dict[str, Any]] = deque(maxlen=limit)
    try:
        with _open_state_file(path, "r") as handle:
            for line in handle:
                try:
                    row = json.loads(line)
                    if isinstance(row, dict):
                        rows.append(row)
                except json.JSONDecodeError:
                    continue
    except OSError as exc:
        print(f"Could not read telemetry history: {exc}")
        return 1
    for sample in rows:
        summary = sample.get("summary") if isinstance(sample.get("summary"), dict) else {}
        median = numeric(summary.get("median_ms"))
        latency = f"{median:.1f} ms median" if median is not None else "no latency summary"
        stamp = clip(sample.get("completed_at", "?"), 32)
        test_type = clip(sample.get("type", "?"), 10).upper()
        target = clip(sample.get("target", "?"), 36)
        probes = numeric(sample.get("probes_count")) or 0
        print(f"[{stamp}] {test_type:<10} {target:<36} {latency}; probes={probes:.0f}")
    return 0


def local_network_counters() -> Optional[dict[str, Any]]:
    """Read host network counters via psutil when installed."""
    try:
        psutil = importlib.import_module("psutil")
        counters = psutil.net_io_counters()
        if counters is None:
            return None
        return {
            "bytes_sent": int(counters.bytes_sent),
            "bytes_recv": int(counters.bytes_recv),
            "packets_sent": int(counters.packets_sent),
            "packets_recv": int(counters.packets_recv),
            "errin": int(counters.errin),
            "errout": int(counters.errout),
            "dropin": int(counters.dropin),
            "dropout": int(counters.dropout),
            "sampled_at": time.monotonic(),
        }
    except Exception:
        return None


def calculate_local_network_rates(previous: Optional[dict[str, Any]],
                                  current: Optional[dict[str, Any]]) -> Optional[dict[str, float]]:
    if not previous or not current:
        return None
    elapsed = max(0.001, float(current["sampled_at"]) - float(previous["sampled_at"]))
    rates = {
        "rx_mbps": max(0.0, current["bytes_recv"] - previous["bytes_recv"]) * 8 / elapsed / 1_000_000,
        "tx_mbps": max(0.0, current["bytes_sent"] - previous["bytes_sent"]) * 8 / elapsed / 1_000_000,
        "rx_pps": max(0.0, current["packets_recv"] - previous["packets_recv"]) / elapsed,
        "tx_pps": max(0.0, current["packets_sent"] - previous["packets_sent"]) / elapsed,
        "errors": float(max(0, current["errin"] - previous["errin"]) + max(0, current["errout"] - previous["errout"])),
        "drops": float(max(0, current["dropin"] - previous["dropin"]) + max(0, current["dropout"] - previous["dropout"])),
    }
    return rates


def probe_local_dns(timeout: float) -> ProbeResult:
    """Measure local DNS resolution time; dnspython is optional."""
    started = time.perf_counter()
    try:
        dns = importlib.import_module("dns.resolver")
        resolver = dns.Resolver(configure=True)
        resolver.timeout = max(1.0, timeout / 2)
        resolver.lifetime = timeout
        answer = resolver.resolve("gateway.discord.gg", "A")
        latency = (time.perf_counter() - started) * 1000.0
        return ProbeResult("Local DNS", "UP", latency, f"{len(answer)} A record(s) via system resolver")
    except ImportError:
        try:
            socket.getaddrinfo("gateway.discord.gg", 443, type=socket.SOCK_STREAM)
            latency = (time.perf_counter() - started) * 1000.0
            return ProbeResult("Local DNS", "UP", latency, "System getaddrinfo resolver; install dnspython for detailed DNS metrics")
        except Exception as exc:
            return ProbeResult("Local DNS", "DOWN", (time.perf_counter() - started) * 1000.0, str(exc))
    except Exception as exc:
        return ProbeResult("Local DNS", "DOWN", (time.perf_counter() - started) * 1000.0, str(exc))


def latest_incident_update(item: dict[str, Any]) -> dict[str, Any]:
    updates = item.get("incident_updates")
    if not isinstance(updates, list):
        return {}
    valid = [entry for entry in updates if isinstance(entry, dict)]
    if not valid:
        return {}
    return max(valid, key=lambda entry: str(entry.get("created_at") or entry.get("display_at") or ""))


def parse_timestamp(value: Any) -> str:
    if not value:
        return ""
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return parsed.astimezone().strftime("%Y-%m-%d %H:%M:%S %Z")
    except (ValueError, TypeError, OverflowError):
        return str(value)


def _safe_position(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):
        return 999


def component_groups(components: list[dict[str, Any]]) -> tuple[dict[str, list[dict[str, Any]]], list[dict[str, Any]]]:
    groups = {str(c.get("id")): str(c.get("name") or "Other")
              for c in components if isinstance(c, dict) and c.get("group")}
    grouped: dict[str, list[dict[str, Any]]] = {}
    standalone: list[dict[str, Any]] = []
    for item in components:
        if not isinstance(item, dict) or item.get("group"):
            continue
        parent = str(item.get("group_id") or "")
        if parent in groups:
            grouped.setdefault(groups[parent], []).append(item)
        else:
            standalone.append(item)
    standalone.sort(key=lambda c: (_safe_position(c.get("position")), str(c.get("name") or "").lower()))
    for values in grouped.values():
        values.sort(key=lambda c: (_safe_position(c.get("position")), str(c.get("name") or "").lower()))
    return grouped, standalone


def build_snapshot(payload: dict[str, Any]) -> dict[str, Any]:
    status = payload.get("status") if isinstance(payload.get("status"), dict) else {}
    components = payload.get("components") if isinstance(payload.get("components"), list) else []
    incidents = payload.get("incidents") if isinstance(payload.get("incidents"), list) else []
    maintenances = payload.get("scheduled_maintenances") if isinstance(payload.get("scheduled_maintenances"), list) else []
    component_state: dict[str, dict[str, str]] = {}
    for component in components:
        if not isinstance(component, dict):
            continue
        key = str(component.get("id") or component.get("name") or "unknown")
        component_state[key] = {
            "name": str(component.get("name") or key),
            "status": str(component.get("status") or "unknown"),
            "group_id": str(component.get("group_id") or ""),
        }
    incident_state: dict[str, dict[str, str]] = {}
    for incident in incidents:
        if not isinstance(incident, dict):
            continue
        key = str(incident.get("id") or incident.get("name") or "unknown")
        update = latest_incident_update(incident)
        incident_state[key] = {
            "name": str(incident.get("name") or "Unnamed incident"),
            "status": str(incident.get("status") or "unknown"),
            "impact": str(incident.get("impact") or "none"),
            "update": str(update.get("body") or ""),
        }
    maintenance_state: dict[str, dict[str, str]] = {}
    for maintenance in maintenances:
        if not isinstance(maintenance, dict):
            continue
        key = str(maintenance.get("id") or maintenance.get("name") or "unknown")
        maintenance_state[key] = {
            "name": str(maintenance.get("name") or "Unnamed maintenance"),
            "status": str(maintenance.get("status") or "unknown"),
        }
    return {
        "overall": {
            "indicator": str(status.get("indicator") or "unknown"),
            "description": str(status.get("description") or "Unknown status"),
        },
        "components": component_state,
        "incidents": incident_state,
        "maintenances": maintenance_state,
    }


def detect_changes(previous: Optional[dict[str, Any]], current: dict[str, Any]) -> list[str]:
    if previous is None:
        indicator = current["overall"]["indicator"]
        changes = []
        if indicator != "none":
            changes.append(f"Initial status is {current['overall']['description']}")
        for item in current["incidents"].values():
            changes.append(f"Active incident: {item['name']} [{item['impact']}/{item['status']}]")
        return changes

    changes: list[str] = []
    old_overall = previous.get("overall", {})
    new_overall = current.get("overall", {})
    if old_overall != new_overall:
        changes.append(f"Global status: {old_overall.get('description', '?')} -> {new_overall.get('description', '?')}")

    old_components = previous.get("components", {})
    new_components = current.get("components", {})
    for key, new in new_components.items():
        old = old_components.get(key)
        if old is None:
            changes.append(f"New component: {new['name']} [{new['status']}]")
        elif old.get("status") != new.get("status"):
            changes.append(f"Component: {new['name']} {old.get('status')} -> {new.get('status')}")
    for key, old in old_components.items():
        if key not in new_components:
            changes.append(f"Component removed from status page: {old.get('name', key)}")

    old_incidents = previous.get("incidents", {})
    new_incidents = current.get("incidents", {})
    for key, new in new_incidents.items():
        old = old_incidents.get(key)
        if old is None:
            changes.append(f"NEW INCIDENT: {new['name']} [{new['impact']}/{new['status']}]")
        elif old != new:
            changes.append(f"Incident updated: {new['name']} [{new['impact']}/{new['status']}]")
    for key, old in old_incidents.items():
        if key not in new_incidents:
            changes.append(f"Incident no longer unresolved (likely resolved): {old.get('name', key)}")

    old_maint = previous.get("maintenances", {})
    new_maint = current.get("maintenances", {})
    for key, new in new_maint.items():
        old = old_maint.get(key)
        if old is None:
            changes.append(f"Maintenance announced: {new['name']} [{new['status']}]")
        elif old != new:
            changes.append(f"Maintenance updated: {new['name']} [{old.get('status')} -> {new['status']}]")
    for key, old in old_maint.items():
        if key not in new_maint:
            changes.append(f"Maintenance no longer listed: {old.get('name', key)}")
    return changes


def load_json_file(path: Path) -> Optional[dict[str, Any]]:
    try:
        with _open_state_file(path, "r") as handle:
            data = json.load(handle)
        return data if isinstance(data, dict) else None
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return None


def save_json_atomic(path: Path, value: dict[str, Any]) -> None:
    """Atomically write state through a unique private temporary file."""
    ensure_private_directory(path.parent)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent)
    )
    descriptor_open = True
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            descriptor_open = False
            handle.write(json.dumps(value, indent=2, ensure_ascii=False, allow_nan=False))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_name, path)
    finally:
        if descriptor_open:
            os.close(descriptor)
        try:
            os.unlink(temporary_name)
        except FileNotFoundError:
            pass


def append_event_log(path: Path, events: list[str], max_bytes: int = 2_000_000) -> None:
    if not events:
        return
    ensure_private_directory(path.parent)
    try:
        if path.exists() and path.stat().st_size > max_bytes:
            backup = path.with_suffix(".previous.jsonl")
            try:
                if backup.exists():
                    backup.unlink()
                path.replace(backup)
            except OSError:
                pass
        with _open_state_file(path, "a") as handle:
            for event in events:
                handle.write(json.dumps({
                    "time": iso_now(), "event": clip(event, 2000)
                }, ensure_ascii=False, allow_nan=False) + "\n")
    except OSError as exc:
        print(f"Warning: could not write event log: {exc}", file=sys.stderr)


def read_event_log(path: Path, limit: int = 25) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows: deque[dict[str, Any]] = deque(maxlen=limit)
    try:
        with _open_state_file(path, "r") as handle:
            for line in handle:
                try:
                    row = json.loads(line)
                    if isinstance(row, dict):
                        rows.append(row)
                except json.JSONDecodeError:
                    continue
    except OSError:
        return []
    return list(rows)


def sparkline(values: deque[float]) -> str:
    if not values:
        return "n/a"
    low = min(values)
    high = max(values)
    if high == low:
        return SPARK[2] * len(values)
    chars = []
    for value in values:
        index = int((value - low) / (high - low) * (len(SPARK) - 1))
        chars.append(SPARK[max(0, min(len(SPARK) - 1, index))])
    return "".join(chars)


def latency_summary(values: deque[float]) -> str:
    if not values:
        return "no samples"
    samples = list(values)
    jitter = statistics.mean(abs(samples[i] - samples[i - 1]) for i in range(1, len(samples))) if len(samples) > 1 else 0.0
    p95 = percentile(samples, 95)
    p95_text = f" | p95 {p95:.0f} ms" if p95 is not None else ""
    return (f"last {samples[-1]:.0f} ms | median {statistics.median(samples):.0f} ms"
            f"{p95_text} | jitter {jitter:.1f} ms | max {max(samples):.0f} ms")


def render_dashboard(
    payload: Optional[dict[str, Any]],
    official_latency: Optional[float],
    probes: list[ProbeResult],
    last_success: Optional[str],
    last_error: Optional[str],
    last_page_update: Optional[str],
    events: deque[tuple[str, str]],
    latencies: dict[str, deque[float]],
    use_color: bool,
    stale: bool,
    global_telemetry: Optional[dict[str, dict[str, Any]]] = None,
    local_rates: Optional[dict[str, float]] = None,
    global_limits: Optional[dict[str, Any]] = None,
    global_limits_error: Optional[str] = None,
) -> None:
    columns = shutil.get_terminal_size((110, 34)).columns
    width = max(72, min(columns, 140))
    rule = "─" * min(width, 120)
    out: list[str] = []
    title = f"{APP_NAME}  v{VERSION}"
    out.append(paint(title.center(min(width, 110)), CYAN, use_color, True))
    out.append(paint(rule, DIM, use_color))
    out.append(f"  Scope       : GLOBAL / WORLDWIDE DISCORD STATUS")
    out.append(f"  Official feed: {STATUS_PAGE}")
    out.append(f"  Checked      : {now_local().strftime('%Y-%m-%d %H:%M:%S %Z')}")
    if RUNTIME_CAPABILITIES:
        out.append(f"  Runtime      : {clip(' | '.join(RUNTIME_CAPABILITIES), width - 18)}")
    if os.environ.get("GLOBALPING_API_TOKEN"):
        out.append("  Global probes: authenticated Globalping API token detected")
    else:
        out.append("  Global probes: anonymous public API budget; measurements are rate-limited")
    if last_success:
        out.append(f"  Last success : {last_success}")
    if last_page_update:
        out.append(f"  Page updated : {last_page_update}")
    if official_latency is not None:
        out.append(f"  Feed latency : {official_latency:.0f} ms")
    if stale:
        out.append(paint("  DATA WARNING : displaying last successful snapshot; latest fetch failed.", YELLOW, use_color, True))
    if last_error:
        out.append(paint(f"  FETCH ERROR  : {clip(last_error, width - 19)}", RED, use_color, True))
    out.append(paint(rule, DIM, use_color))

    if payload is None:
        out.append(paint("  WAITING FOR FIRST SUCCESSFUL OFFICIAL STATUS RESPONSE", YELLOW, use_color, True))
    else:
        status = payload.get("status") if isinstance(payload.get("status"), dict) else {}
        indicator = str(status.get("indicator") or "unknown")
        description = str(status.get("description") or "Unknown")
        components = [c for c in payload.get("components", []) if isinstance(c, dict)]
        count_states: dict[str, int] = {}
        for component in components:
            if component.get("group"):
                continue
            state = str(component.get("status") or "unknown")
            count_states[state] = count_states.get(state, 0) + 1
        incidents = [i for i in payload.get("incidents", []) if isinstance(i, dict)]
        maintenances = [m for m in payload.get("scheduled_maintenances", []) if isinstance(m, dict)]
        service_components = [c for c in components if not c.get("group")]
        healthy = sum(1 for c in service_components if c.get("status") == "operational")

        out.append("  WORLDWIDE STATUS")
        overall_text = f" {description.upper()} "
        out.append("  " + paint(overall_text, status_color(indicator), use_color, True))
        status_counts = "  |  ".join(
            f"{label}: {count}" for label, count in [
                ("Operational", count_states.get("operational", 0)),
                ("Degraded", count_states.get("degraded_performance", 0)),
                ("Partial outage", count_states.get("partial_outage", 0)),
                ("Major outage", count_states.get("major_outage", 0)),
            ] if count
        )
        out.append(f"  Services: {healthy}/{len(service_components)} operational" + (f"  |  {status_counts}" if status_counts else ""))
        out.append(f"  Active incidents: {len(incidents)}  |  Scheduled/active maintenance: {len(maintenances)}")

        grouped, standalone = component_groups(components)
        out.append("")
        out.append(paint("  GLOBAL SERVICE COMPONENTS", CYAN, use_color, True))
        if standalone:
            out.append(paint("  Core / standalone services", WHITE, use_color, True))
            for component in standalone:
                name = str(component.get("name") or "Unknown")
                state = str(component.get("status") or "unknown")
                description_text = component.get("description")
                suffix = f" — {clip(description_text, max(15, width - 42))}" if description_text else ""
                row = f"    {clip(name, 25):<25} {status_label(state):<22}{suffix}"
                out.append(paint(row, status_color(state), use_color))
        for group_name in sorted(grouped, key=lambda x: (x != "Voice", x.lower())):
            children = grouped[group_name]
            out.append(paint(f"  {group_name}", WHITE, use_color, True))
            # Voice is intentionally displayed by named world region.
            if group_name.lower() == "voice":
                region_items = [f"{c.get('name', '?')}: {status_label(str(c.get('status') or 'unknown'))}" for c in children]
                for start in range(0, len(region_items), 2):
                    row = "    " + "    |    ".join(region_items[start:start + 2])
                    out.append(row)
            else:
                for component in children:
                    name = str(component.get("name") or "Unknown")
                    state = str(component.get("status") or "unknown")
                    out.append(paint(f"    {clip(name, 25):<25} {status_label(state)}", status_color(state), use_color))

        out.append("")
        out.append(paint(f"  ACTIVE INCIDENTS ({len(incidents)})", CYAN, use_color, True))
        if not incidents:
            out.append(paint("    No unresolved incidents reported by Discord.", GREEN, use_color))
        for incident in incidents[:12]:
            name = str(incident.get("name") or "Unnamed incident")
            impact = str(incident.get("impact") or "unknown")
            state = str(incident.get("status") or "unknown")
            update = latest_incident_update(incident)
            updated_at = parse_timestamp(update.get("created_at") or update.get("display_at"))
            out.append(paint(f"    [{impact.upper()} / {state.upper()}] {name}", status_color(impact), use_color, True))
            if update.get("body"):
                for line in textwrap.wrap(" ".join(str(update["body"]).split()), width=max(40, width - 8))[:3]:
                    out.append(f"      {line}")
            if updated_at:
                out.append(f"      Updated: {updated_at}")
            if incident.get("shortlink"):
                out.append(f"      {incident['shortlink']}")

        out.append("")
        out.append(paint(f"  MAINTENANCE ({len(maintenances)})", CYAN, use_color, True))
        if not maintenances:
            out.append("    No upcoming or active maintenance currently listed.")
        for item in maintenances[:8]:
            name = str(item.get("name") or "Unnamed maintenance")
            state = str(item.get("status") or "unknown")
            when = parse_timestamp(item.get("scheduled_for"))
            until = parse_timestamp(item.get("scheduled_until"))
            out.append(paint(f"    [{state.upper()}] {name}", status_color(state), use_color, True))
            if when or until:
                out.append(f"      {when or 'Start time unknown'}" + (f"  ->  {until}" if until else ""))

    out.append("")
    out.append(paint("  LOCAL CONNECTIVITY CHECKS  (measured from this computer, not worldwide telemetry)", MAGENTA, use_color, True))
    if not probes:
        out.append("    Disabled (--no-probes).")
    else:
        for probe in probes:
            latency = f"{probe.latency_ms:.0f} ms" if probe.latency_ms is not None else "n/a"
            label = f"    {probe.name:<12} {probe.state:<7} {latency:<9} {clip(probe.detail, width - 45)}"
            out.append(paint(label, status_color(probe.state), use_color))
    for name in ("feed", "rest", "gateway", "cdn", "dns"):
        values = latencies.get(name)
        if values:
            out.append(f"      {name.upper():<8} {sparkline(values)}  {latency_summary(values)}")

    out.append("")
    out.append(paint("  HOST NETWORK TELEMETRY  (this computer only)", MAGENTA, use_color, True))
    if local_rates:
        out.append(
            f"    RX {local_rates['rx_mbps']:.3f} Mbps / {local_rates['rx_pps']:.1f} pkt/s"
            f"    TX {local_rates['tx_mbps']:.3f} Mbps / {local_rates['tx_pps']:.1f} pkt/s"
        )
        out.append(f"    Interface errors: {local_rates['errors']:.0f} delta  |  drops: {local_rates['drops']:.0f} delta")
    else:
        out.append("    Host counters unavailable (psutil not installed/accessible yet).")

    out.append("")
    out.append(paint("  WORLDWIDE REMOTE TELEMETRY  (Globalping + optional RIPE Atlas)", MAGENTA, use_color, True))
    if global_limits:
        remaining = global_limits.get("remaining")
        limit = global_limits.get("limit")
        reset = global_limits.get("reset_seconds")
        quota = (f"{remaining:.0f}/{limit:.0f} measurements remaining in current window" 
                 if isinstance(remaining, (int, float)) and isinstance(limit, (int, float))
                 else "provider quota available")
        reset_text = f"; window resets in {reset:.0f}s" if isinstance(reset, (int, float)) else ""
        out.append(f"    Globalping API quota: {quota} ({global_limits.get('type', 'unknown')}){reset_text}")
        credits = global_limits.get("credits_remaining")
        if isinstance(credits, (int, float)):
            out.append(f"    Account credits remaining: {credits:.0f}")
    elif global_limits_error:
        out.append(paint(f"    Globalping quota lookup unavailable: {clip(global_limits_error, width - 45)}", YELLOW, use_color))
    else:
        out.append("    Globalping provider quota: waiting for first lookup.")
    if global_telemetry:
        for kind in ("ping", "http", "dns", "cdn", "mtr", "ripe_atlas"):
            measurement = global_telemetry.get(kind)
            if not measurement:
                continue
            summary = measurement.get("summary") if isinstance(measurement.get("summary"), dict) else {}
            error = measurement.get("error")
            if error:
                out.append(paint(f"    {kind.upper():<8} ERROR: {clip(error, width - 18)}", RED, use_color))
                continue
            median = summary.get("median_ms")
            p95 = summary.get("p95_ms")
            median_text = f"p50 {median:.1f} ms" if isinstance(median, (int, float)) else "latency n/a"
            if isinstance(p95, (int, float)):
                median_text += f" / p95 {p95:.1f} ms"
            probes_n = measurement.get("probes_count", 0)
            target = measurement.get("target", "")
            state = "UP" if summary.get("up", 0) else "WARN"
            if summary.get("warn", 0) or summary.get("down", 0) or measurement.get("status") == "partial":
                state = "WARN"
            line_text = f"    {kind.upper():<10} {state:<5} {median_text:<19} probes={probes_n:<3} target={target}"
            out.append(paint(line_text, status_color(state), use_color))
            if kind == "ripe_atlas":
                out.append("      Existing RIPE Atlas measurements; remote volunteer probes, not Discord-internal telemetry.")
                for atlas_row in measurement.get("rows", [])[:5]:
                    if not isinstance(atlas_row, dict):
                        continue
                    latency = atlas_row.get("latency_ms")
                    latency_text = f"{latency:.1f} ms" if isinstance(latency, (int, float)) else "latency n/a"
                    loss = atlas_row.get("loss_pct")
                    loss_text = f"{loss:.0f}% loss" if isinstance(loss, (int, float)) else "loss n/a"
                    out.append(
                        f"      Probe {clip(atlas_row.get('probe_id', '?'), 10):<10} "
                        f"{latency_text:<13} {loss_text:<12} {clip(atlas_row.get('target', ''), 30)}"
                    )
            if kind in {"http", "cdn"} and measurement.get("rows"):
                http_rows = [r for r in measurement["rows"] if isinstance(r, dict)]
                for field, label in (("dns_ms", "DNS"), ("tcp_ms", "TCP"), ("tls_ms", "TLS"), ("ttfb_ms", "TTFB")):
                    samples = [r.get(field) for r in http_rows if isinstance(r.get(field), (int, float))]
                    if samples:
                        out.append(f"      {label} median: {statistics.median(samples):.1f} ms")
            if kind == "dns" and measurement.get("rows"):
                for dns_row in measurement["rows"][:5]:
                    if not isinstance(dns_row, dict):
                        continue
                    answers = dns_row.get("answers")
                    answers = answers if isinstance(answers, list) else []
                    answer_text = ", ".join(clip(answer, 64) for answer in answers[:8])
                    if len(answers) > 8:
                        answer_text += f", +{len(answers) - 8} more"
                    if not answer_text:
                        answer_text = clip(dns_row.get("detail", "no DNS answer"), 100)
                    state = clip(dns_row.get("state", "WARN"), 8)
                    out.append(
                        f"      {clip(dns_row.get('location', 'remote probe'), 25):<25} "
                        f"{clip(answer_text, 120)} [{state}]"
                    )
                if len(measurement.get("rows", [])) > 1:
                    if summary.get("answer_set_consistent") is True:
                        note = "same DNS answer set across probes (TTL cache ages may differ)"
                    elif summary.get("answer_set_consistent") is False:
                        note = "DNS answer sets vary by resolver/location; inspect regional results"
                    else:
                        note = "DNS answer-set comparison unavailable"
                    out.append(f"      RRset comparison: {note}")
            if kind == "ping" and measurement.get("rows"):
                by_continent: dict[str, list[float]] = {}
                for item in measurement["rows"]:
                    if isinstance(item, dict) and isinstance(item.get("latency_ms"), (int, float)):
                        by_continent.setdefault(str(item.get("continent") or "?"), []).append(float(item["latency_ms"]))
                if by_continent:
                    regional = "  |  ".join(
                        f"{continent}: p50 {statistics.median(samples):.1f} / p95 {percentile(samples, 95):.1f} ms (n={len(samples)})"
                        for continent, samples in sorted(by_continent.items())
                    )
                    out.append(f"      Regional p50: {clip(regional, width - 20)}")
                ranked = sorted(
                    [r for r in measurement["rows"] if isinstance(r, dict)],
                    key=lambda r: (r.get("latency_ms") is None, r.get("latency_ms") or 0),
                )
                for row in ranked[:min(8, len(ranked))]:
                    ms = row.get("latency_ms")
                    loss = row.get("loss_pct")
                    ms_text = f"{ms:.1f} ms" if isinstance(ms, (int, float)) else "timeout"
                    loss_text = f"{loss:.0f}% loss" if isinstance(loss, (int, float)) else "loss n/a"
                    location = f"{row.get('city', '?')}, {row.get('country', '?')}"
                    out.append(f"      {clip(location, 24):<24} {ms_text:<12} {loss_text:<12} {clip(row.get('network', ''), 26)}")
            if kind == "mtr" and measurement.get("rows"):
                traced = next((r for r in measurement["rows"] if r.get("hops")), None)
                if traced:
                    out.append(f"      Route from {traced.get('location', 'remote probe')}:")
                    for index, hop in enumerate(traced.get("hops", [])[:8], start=1):
                        hop_ms = hop.get("avg_ms")
                        hop_text = f"{hop_ms:.2f} ms" if isinstance(hop_ms, (int, float)) else "*"
                        out.append(f"        {index:>2}. {clip(hop.get('address', '*'), 24):<24} {hop_text}")
    else:
        out.append("    Waiting for the first scheduled worldwide probe measurement.")

    out.append("")
    out.append(paint("  RECENT STATUS CHANGES", CYAN, use_color, True))
    if events:
        for stamp, message in list(events)[-8:]:
            out.append(f"    [{stamp}] {clip(message, width - 15)}")
    else:
        out.append("    No changes detected during this session.")

    out.append("")
    out.append(paint(rule, DIM, use_color))
    out.append(paint("  Ctrl+C to stop  |  Discord status page + Globalping remote probes  |  Optional Globalping token raises limits", DIM, use_color))
    print("\n".join(out), flush=True)


def output_json(payload: Optional[dict[str, Any]], official_latency: Optional[float],
                probes: list[ProbeResult], error: Optional[str],
                global_telemetry: Optional[dict[str, dict[str, Any]]] = None,
                local_rates: Optional[dict[str, float]] = None,
                global_limits: Optional[dict[str, Any]] = None) -> None:
    result = {
        "checked_at": iso_now(),
        "source": STATUS_PAGE,
        "official_summary": payload,
        "official_feed_latency_ms": official_latency,
        "local_probes": [
            {"name": p.name, "state": p.state, "latency_ms": p.latency_ms, "detail": p.detail}
            for p in probes
        ],
        "error": error,
        "worldwide_telemetry": global_telemetry or {},
        "globalping_api_limits": global_limits,
        "local_host_network_rates": local_rates,
        "measurement_vantage_note": "Globalping and RIPE Atlas rows are measurements from independent remote volunteer probes, not Discord-internal telemetry; local_probes and host rates are from this computer.",
        "tls_verification": "enabled",
        "tls_trust_sources": list(_TLS_CA_SOURCES),
    }
    print(json.dumps(result, indent=2, ensure_ascii=False))


def show_history(path: Path) -> int:
    rows = read_event_log(path, limit=50)
    print(f"{APP_NAME} - saved change history")
    print(f"Log: {path}")
    if not rows:
        print("No saved events yet.")
        return 0
    for row in rows:
        print(f"[{clip(row.get('time', '?'), 32)}] {clip(row.get('event', ''), 240)}")
    return 0


def main() -> int:
    args = parse_args()
    try:
        # Avoid legacy Windows code pages failing on dashboard glyphs.
        if hasattr(sys.stdout, "reconfigure"):
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (OSError, ValueError):
        pass
    data_dir = (args.data_dir or state_directory()).expanduser()
    state_path = data_dir / "last_state.json"
    event_path = data_dir / "events.jsonl"
    telemetry_path = data_dir / "worldwide_telemetry.jsonl"

    if args.history:
        return show_history(event_path)
    if args.telemetry_history:
        return show_telemetry_history(telemetry_path)

    try:
        ensure_private_directory(data_dir)
    except Exception as exc:
        print(f"Unsafe DGSM data directory: {exc}", file=sys.stderr)
        return 2
    bootstrap_optional_dependencies(args, data_dir)
    try:
        initialize_tls_context(args.ca_bundle)
    except Exception as exc:
        print(f"TLS initialization failed: {exc}", file=sys.stderr)
        return 2
    if args.doctor:
        return run_doctor(args)
    use_color = color_enabled(args.no_color)
    notifications_enabled = not args.no_notifications

    previous_state = None if args.no_save else load_json_file(state_path)
    last_payload: Optional[dict[str, Any]] = None
    last_latency: Optional[float] = None
    last_success: Optional[str] = None
    last_page_update: Optional[str] = None
    last_error: Optional[str] = None
    last_probes: list[ProbeResult] = []
    previous_probe_states: dict[str, str] = {}
    previous_feed_error: Optional[str] = None
    events: deque[tuple[str, str]] = deque(maxlen=30)
    latency_history: dict[str, deque[float]] = {
        "feed": deque(maxlen=24),
        "rest": deque(maxlen=24),
        "gateway": deque(maxlen=24),
        "cdn": deque(maxlen=24),
        "dns": deque(maxlen=24),
    }
    global_telemetry: dict[str, dict[str, Any]] = {}
    host_previous = local_network_counters()
    host_rates: Optional[dict[str, float]] = None
    started_at = time.monotonic()
    next_global_due = started_at
    next_http_due = started_at + 60.0
    next_dns_due = started_at + 120.0
    next_cdn_due = started_at + 240.0
    next_trace_due = started_at + 180.0
    global_limits: Optional[dict[str, Any]] = None
    global_limits_error: Optional[str] = None
    global_limits_future: Optional[concurrent.futures.Future] = None
    next_limits_due = started_at
    if args.trace_now:
        next_trace_due = started_at
    global_executor = concurrent.futures.ThreadPoolExecutor(
        max_workers=2, thread_name_prefix="globalping"
    )
    pending_global: dict[str, concurrent.futures.Future] = {}
    ripe_atlas_executor = (
        concurrent.futures.ThreadPoolExecutor(max_workers=1, thread_name_prefix="ripe-atlas")
        if args.ripe_atlas_ids else None
    )
    pending_ripe_atlas: Optional[concurrent.futures.Future] = None
    next_ripe_atlas_due = started_at
    previous_ripe_atlas_error: Optional[str] = None

    try:
        while True:
            # Run public status-feed retrieval and local probes concurrently.
            with concurrent.futures.ThreadPoolExecutor(max_workers=4) as executor:
                feed_future = executor.submit(fetch_official_summary, args.timeout)
                probe_future = None if args.no_probes else executor.submit(run_local_probes, args.timeout)
                try:
                    payload, feed_latency = feed_future.result()
                    last_payload = payload
                    last_latency = feed_latency
                    latency_history["feed"].append(feed_latency)
                    page = payload.get("page") if isinstance(payload.get("page"), dict) else {}
                    last_page_update = parse_timestamp(page.get("updated_at")) or None
                    last_success = now_local().strftime("%Y-%m-%d %H:%M:%S %Z")
                    last_error = None

                    current_state = build_snapshot(payload)
                    changes = detect_changes(previous_state, current_state)
                    if previous_feed_error is not None:
                        changes.append("Official Discord status feed has recovered.")
                    previous_feed_error = None
                    if changes:
                        stamp = now_local().strftime("%H:%M:%S")
                        for change in changes:
                            events.append((stamp, change))
                        if not args.no_save:
                            append_event_log(event_path, changes)
                        if notifications_enabled:
                            send_desktop_notification(
                                "Discord global status update", "\n".join(changes[:5])
                            )
                        if args.beep and sys.stdout.isatty():
                            print("\a", end="", flush=True)
                    previous_state = current_state
                    if not args.no_save:
                        save_json_atomic(state_path, current_state)
                except Exception as exc:
                    last_error = str(exc)
                    payload = None
                    if previous_feed_error is None:
                        previous_feed_error = last_error
                        message = f"Official Discord status feed unreachable: {last_error}"
                        events.append((now_local().strftime("%H:%M:%S"), message))
                        if not args.no_save:
                            append_event_log(event_path, [message])
                        if notifications_enabled:
                            send_desktop_notification("Discord status feed unreachable", last_error)
                        if args.beep and sys.stdout.isatty():
                            print("\a", end="", flush=True)

                if probe_future is not None:
                    try:
                        last_probes = probe_future.result()
                        probe_changes = []
                        for probe in last_probes:
                            key = {"REST API": "rest", "Gateway WS": "gateway", "Media CDN": "cdn", "Local DNS": "dns"}.get(probe.name)
                            if key and probe.latency_ms is not None:
                                latency_history[key].append(probe.latency_ms)
                            old_probe_state = previous_probe_states.get(probe.name)
                            if old_probe_state is not None and old_probe_state != probe.state:
                                probe_changes.append(f"Local {probe.name} connectivity: {old_probe_state} -> {probe.state}")
                            previous_probe_states[probe.name] = probe.state
                        if probe_changes:
                            stamp = now_local().strftime("%H:%M:%S")
                            for message in probe_changes:
                                events.append((stamp, message))
                            if not args.no_save:
                                append_event_log(event_path, probe_changes)
                            if args.beep and sys.stdout.isatty():
                                print("\a", end="", flush=True)
                    except Exception as exc:
                        last_probes = [ProbeResult("Probes", "ERROR", None, str(exc))]
                else:
                    last_probes = []

            host_current = local_network_counters()
            host_rates = calculate_local_network_rates(host_previous, host_current)
            if host_current:
                host_previous = host_current

            if not args.no_globalping:
                now_mono = time.monotonic()
                if now_mono >= next_limits_due and global_limits_future is None:
                    global_limits_future = global_executor.submit(fetch_globalping_limits, args)
                    next_limits_due = now_mono + 900.0
                if global_limits_future is not None and global_limits_future.done():
                    try:
                        global_limits = global_limits_future.result()
                        global_limits_error = None
                    except Exception as exc:
                        global_limits_error = str(exc)
                    global_limits_future = None

                schedules = [
                    ("ping", args.global_interval, next_global_due),
                    ("http", args.http_interval, next_http_due),
                    ("dns", args.dns_interval, next_dns_due),
                    ("cdn", args.cdn_interval, next_cdn_due),
                    ("mtr", args.trace_interval, next_trace_due),
                ]
                for kind, period, due_at in schedules:
                    if now_mono < due_at or kind in pending_global:
                        continue
                    print(
                        f"[global telemetry] Queued worldwide {kind.upper()} measurement ...",
                        file=sys.stderr,
                    )
                    pending_global[kind] = global_executor.submit(
                        run_global_telemetry, kind, args
                    )
                    next_due = now_mono + period
                    if kind == "ping":
                        next_global_due = next_due
                    elif kind == "http":
                        next_http_due = next_due
                    elif kind == "dns":
                        next_dns_due = next_due
                    elif kind == "cdn":
                        next_cdn_due = next_due
                    elif kind == "mtr":
                        next_trace_due = next_due

                # --once and --json wait for the work they explicitly requested;
                # normal monitoring never blocks the dashboard on a slow remote probe.
                if args.once or args.json:
                    for future in list(pending_global.values()):
                        try:
                            future.result(timeout=100)
                        except Exception:
                            pass
                    if global_limits_future is not None:
                        try:
                            global_limits = global_limits_future.result(timeout=20)
                            global_limits_error = None
                        except Exception as exc:
                            global_limits_error = str(exc)
                        global_limits_future = None

                completed = [
                    (kind, future)
                    for kind, future in pending_global.items()
                    if future.done()
                ]
                for test_type, future in completed:
                    pending_global.pop(test_type, None)
                    try:
                        measurement = future.result()
                    except Exception as exc:
                        measurement = {
                            "type": test_type,
                            "target": "unknown",
                            "status": "error",
                            "rows": [],
                            "summary": {},
                            "error": str(exc),
                            "completed_at": iso_now(),
                        }
                    global_telemetry[test_type] = measurement
                    if not args.no_save and not measurement.get("error"):
                        append_telemetry_log(telemetry_path, measurement)
                    telemetry_events: list[str] = []
                    if measurement.get("error"):
                        msg = f"Globalping {test_type} measurement failed: {measurement['error']}"
                        telemetry_events.append(msg)
                        if notifications_enabled:
                            send_desktop_notification("Worldwide telemetry measurement failed", msg)
                    else:
                        summary = measurement.get("summary", {})
                        if test_type == "ping" and summary.get("probe_results", 0) >= 4:
                            loss = summary.get("median_loss_pct")
                            failed = summary.get("warn", 0) + summary.get("down", 0)
                            count = summary.get("probe_results", 0)
                            # ICMP can be filtered; this is a symptom, not a declared outage.
                            if failed / max(1, count) >= 0.5:
                                msg = (
                                    f"Globalping symptom: {failed}/{count} worldwide probes "
                                    f"reported missing/failed ICMP replies to {measurement.get('target')}"
                                )
                                telemetry_events.append(msg)
                                if notifications_enabled:
                                    send_desktop_notification(
                                        "Discord global network telemetry warning", msg
                                    )
                            elif isinstance(loss, (int, float)) and loss >= 25:
                                msg = f"Globalping median packet loss to gateway.discord.gg: {loss:.0f}%"
                                telemetry_events.append(msg)
                    if telemetry_events:
                        stamp = now_local().strftime("%H:%M:%S")
                        for event in telemetry_events:
                            events.append((stamp, event))
                        if not args.no_save:
                            append_event_log(event_path, telemetry_events)
                    if args.beep and sys.stdout.isatty() and telemetry_events:
                        print("\a", end="", flush=True)

            # RIPE Atlas is independent of Globalping and only runs when the
            # operator supplied existing measurement IDs. It never creates tests.
            if ripe_atlas_executor is not None:
                now_mono = time.monotonic()
                if pending_ripe_atlas is None and now_mono >= next_ripe_atlas_due:
                    print("[RIPE Atlas] Queued read-only latest-results collection ...", file=sys.stderr)
                    pending_ripe_atlas = ripe_atlas_executor.submit(run_ripe_atlas, args)
                    next_ripe_atlas_due = now_mono + args.ripe_atlas_interval

                if (args.once or args.json) and pending_ripe_atlas is not None:
                    try:
                        pending_ripe_atlas.result(timeout=max(30.0, args.timeout * 4.0))
                    except concurrent.futures.TimeoutError:
                        pending_ripe_atlas.cancel()
                        global_telemetry["ripe_atlas"] = {
                            "type": "ripe_atlas",
                            "provider": "RIPE Atlas public probe network",
                            "vantage": "remote RIPE Atlas probes; not Discord-internal telemetry",
                            "status": "error",
                            "rows": [],
                            "summary": {},
                            "error": f"Timed out waiting for RIPE Atlas results after {max(30.0, args.timeout * 4.0):.0f} seconds",
                            "completed_at": iso_now(),
                        }
                        pending_ripe_atlas = None

                if pending_ripe_atlas is not None and pending_ripe_atlas.done():
                    try:
                        atlas_measurement = pending_ripe_atlas.result()
                    except Exception as exc:
                        atlas_measurement = {
                            "type": "ripe_atlas",
                            "provider": "RIPE Atlas public probe network",
                            "vantage": "remote RIPE Atlas probes; not Discord-internal telemetry",
                            "status": "error",
                            "rows": [],
                            "summary": {},
                            "error": str(exc),
                            "completed_at": iso_now(),
                        }
                    pending_ripe_atlas = None
                    global_telemetry["ripe_atlas"] = atlas_measurement
                    if not args.no_save and not atlas_measurement.get("error"):
                        append_telemetry_log(telemetry_path, atlas_measurement)
                    atlas_error = str(atlas_measurement.get("error") or "")
                    atlas_events: list[str] = []
                    if atlas_error:
                        if atlas_error != previous_ripe_atlas_error:
                            atlas_events.append(f"RIPE Atlas collection failed: {atlas_error}")
                        previous_ripe_atlas_error = atlas_error
                    else:
                        if previous_ripe_atlas_error:
                            atlas_events.append("RIPE Atlas collection has recovered.")
                        previous_ripe_atlas_error = None
                        partial_errors = atlas_measurement.get("errors")
                        if isinstance(partial_errors, list) and partial_errors:
                            atlas_events.append(
                                f"RIPE Atlas returned partial results: {len(partial_errors)} measurement(s) failed."
                            )
                    if atlas_events:
                        stamp = now_local().strftime("%H:%M:%S")
                        for event in atlas_events:
                            events.append((stamp, event))
                        if not args.no_save:
                            append_event_log(event_path, atlas_events)
                        if notifications_enabled:
                            send_desktop_notification("RIPE Atlas telemetry update", "\n".join(atlas_events[:3]))
                        if args.beep and sys.stdout.isatty():
                            print("\a", end="", flush=True)

            if args.json:
                output_json(last_payload, last_latency, last_probes, last_error, global_telemetry, host_rates, global_limits)
                return 1 if last_error else 0

            if not args.no_clear and sys.stdout.isatty() and use_color:
                print("\033[2J\033[H", end="")
            render_dashboard(
                last_payload,
                last_latency,
                last_probes,
                last_success,
                last_error,
                last_page_update,
                events,
                latency_history,
                use_color,
                stale=bool(last_error and last_payload is not None),
                global_telemetry=global_telemetry,
                local_rates=host_rates,
                global_limits=global_limits,
                global_limits_error=global_limits_error,
            )

            if args.once:
                return 1 if last_error else 0
            time.sleep(args.interval)
    except KeyboardInterrupt:
        print("\nMonitor stopped.")
        return 0
    except OSError as exc:
        print(f"Fatal local I/O error: {exc}", file=sys.stderr)
        return 2
    finally:
        global_executor.shutdown(wait=False, cancel_futures=True)
        if ripe_atlas_executor is not None:
            ripe_atlas_executor.shutdown(wait=False, cancel_futures=True)


if __name__ == "__main__":
    raise SystemExit(main())
