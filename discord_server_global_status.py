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
import importlib
import importlib.util
import json
import os
import platform
import random
import shutil
import socket
import ssl
import statistics
import subprocess
import sys
import textwrap
import time
import urllib.error
import urllib.request
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

APP_NAME = "Discord Global Status Monitor"
VERSION = "2.1.0"
STATUS_PAGE = "https://discordstatus.com"
SUMMARY_URL = "https://discordstatus.com/api/v2/summary.json"
REST_GATEWAY_URL = "https://discord.com/api/v10/gateway"
CDN_TEST_URL = "https://cdn.discordapp.com/embed/avatars/0.png"
GATEWAY_HOST = "gateway.discord.gg"
GATEWAY_PORT = 443
USER_AGENT = f"DiscordGlobalStatusMonitor/{VERSION} (+{STATUS_PAGE})"

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


def clip(value: Any, width: int) -> str:
    text = " ".join(str(value).replace("\r", " ").replace("\n", " ").split())
    if width <= 0:
        return ""
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
    parser.add_argument("--data-dir", type=Path, default=None,
                        help="Override the directory used for local state, vendor packages and event history.")
    parser.add_argument("--version", action="version", version=f"%(prog)s {VERSION}")
    args = parser.parse_args()
    if args.interval < 10:
        parser.error("--interval must be at least 10 seconds to avoid excessive polling")
    if args.timeout <= 0:
        parser.error("--timeout must be greater than zero")
    return args


def _try_import(module_name: str) -> bool:
    try:
        importlib.import_module(module_name)
        return True
    except Exception:
        return False


def bootstrap_optional_dependencies(args: argparse.Namespace, data_dir: Path) -> None:
    """Install small optional UX packages into a private app-local directory.

    This avoids sudo, global pip changes, and Kali/Debian PEP 668 conflicts.
    The monitor has a standard-library fallback if installation fails.
    """
    packages: list[tuple[str, str]] = []
    if not args.no_notifications:
        packages.append(("plyer", "plyer>=2.1.0"))
    if os.name == "nt":
        packages.append(("colorama", "colorama>=0.4.6"))

    if not packages:
        RUNTIME_CAPABILITIES.append("Desktop notifications disabled")
        return

    vendor_dir = data_dir / "vendor"
    if str(vendor_dir) not in sys.path:
        sys.path.insert(0, str(vendor_dir))

    for module_name, requirement in packages:
        if _try_import(module_name):
            RUNTIME_CAPABILITIES.append(f"{module_name}: ready")
            continue

        if args.no_auto_install:
            RUNTIME_CAPABILITIES.append(f"{module_name}: missing (auto-install disabled)")
            continue

        try:
            vendor_dir.mkdir(parents=True, exist_ok=True)
            command = [
                sys.executable, "-m", "pip", "install",
                "--disable-pip-version-check", "--quiet",
                "--target", str(vendor_dir), requirement,
            ]
            result = subprocess.run(
                command, capture_output=True, text=True, timeout=180, check=False
            )
            importlib.invalidate_caches()
            if str(vendor_dir) not in sys.path:
                sys.path.insert(0, str(vendor_dir))
            if result.returncode == 0 and _try_import(module_name):
                RUNTIME_CAPABILITIES.append(f"{module_name}: installed automatically")
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
        with urllib.request.urlopen(request, timeout=timeout) as response:
            code = getattr(response, "status", 200)
            if code != 200:
                raise MonitorError(f"HTTP {code}")
            raw = response.read(2 * 1024 * 1024)
    except urllib.error.HTTPError as exc:
        retry_after = exc.headers.get("Retry-After") if exc.headers else None
        suffix = f"; Retry-After: {retry_after}s" if retry_after else ""
        raise MonitorError(f"HTTP {exc.code} {exc.reason}{suffix}") from exc
    except urllib.error.URLError as exc:
        raise MonitorError(f"Network error: {exc.reason}") from exc
    except TimeoutError as exc:
        raise MonitorError("Request timed out") from exc
    except OSError as exc:
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
        context = ssl.create_default_context()
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
            raise MonitorError(str(last_error or "Could not connect to Gateway"))

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
        return ProbeResult("Gateway WS", "DOWN", (time.perf_counter() - started) * 1000.0, str(exc))
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
        with urllib.request.urlopen(request, timeout=timeout) as response:
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
        return ProbeResult("Media CDN", "DOWN", (time.perf_counter() - start) * 1000.0, str(exc))


def run_local_probes(timeout: float) -> list[ProbeResult]:
    tasks = [probe_rest_api, probe_gateway_websocket, probe_cdn]
    results: list[ProbeResult] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=3) as executor:
        futures = {executor.submit(task, timeout): task.__name__ for task in tasks}
        for future in concurrent.futures.as_completed(futures):
            try:
                results.append(future.result())
            except Exception as exc:
                results.append(ProbeResult(futures[future], "ERROR", None, str(exc)))
    order = {"REST API": 0, "Gateway WS": 1, "Media CDN": 2}
    return sorted(results, key=lambda item: order.get(item.name, 99))


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
    standalone.sort(key=lambda c: (int(c.get("position") or 999), str(c.get("name") or "").lower()))
    for values in grouped.values():
        values.sort(key=lambda c: (int(c.get("position") or 999), str(c.get("name") or "").lower()))
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
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return None


def save_json_atomic(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(value, indent=2, ensure_ascii=False), encoding="utf-8")
    temp.replace(path)


def append_event_log(path: Path, events: list[str], max_bytes: int = 2_000_000) -> None:
    if not events:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        if path.exists() and path.stat().st_size > max_bytes:
            backup = path.with_suffix(".previous.jsonl")
            try:
                if backup.exists():
                    backup.unlink()
                path.replace(backup)
            except OSError:
                pass
        with path.open("a", encoding="utf-8") as handle:
            for event in events:
                handle.write(json.dumps({"time": iso_now(), "event": event}, ensure_ascii=False) + "\n")
    except OSError as exc:
        print(f"Warning: could not write event log: {exc}", file=sys.stderr)


def read_event_log(path: Path, limit: int = 25) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows: deque[dict[str, Any]] = deque(maxlen=limit)
    try:
        with path.open("r", encoding="utf-8") as handle:
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
    return f"last {values[-1]:.0f} ms | median {statistics.median(values):.0f} ms | max {max(values):.0f} ms"


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
    for name in ("feed", "rest", "gateway", "cdn"):
        values = latencies.get(name)
        if values:
            out.append(f"      {name.upper():<8} {sparkline(values)}  {latency_summary(values)}")

    out.append("")
    out.append(paint("  RECENT STATUS CHANGES", CYAN, use_color, True))
    if events:
        for stamp, message in list(events)[-8:]:
            out.append(f"    [{stamp}] {clip(message, width - 15)}")
    else:
        out.append("    No changes detected during this session.")

    out.append("")
    out.append(paint(rule, DIM, use_color))
    out.append(paint("  Ctrl+C to stop  |  Refresh uses Discord's public status feed  |  No token required", DIM, use_color))
    print("\n".join(out), flush=True)


def output_json(payload: Optional[dict[str, Any]], official_latency: Optional[float],
                probes: list[ProbeResult], error: Optional[str]) -> None:
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
        print(f"[{row.get('time', '?')}] {row.get('event', '')}")
    return 0


def main() -> int:
    args = parse_args()
    try:
        # Avoid legacy Windows code pages failing on dashboard glyphs.
        if hasattr(sys.stdout, "reconfigure"):
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (OSError, ValueError):
        pass
    data_dir = args.data_dir or state_directory()
    state_path = data_dir / "last_state.json"
    event_path = data_dir / "events.jsonl"

    if args.history:
        return show_history(event_path)

    bootstrap_optional_dependencies(args, data_dir)
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
    }

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
                            key = {"REST API": "rest", "Gateway WS": "gateway", "Media CDN": "cdn"}.get(probe.name)
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

            if args.json:
                output_json(last_payload, last_latency, last_probes, last_error)
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


if __name__ == "__main__":
    raise SystemExit(main())
