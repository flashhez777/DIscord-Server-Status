
#!/usr/bin/env python3
"""Live Discord platform status monitor for Kali Linux."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import time
from datetime import datetime
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

API_URL = "https://discordstatus.com/api/v2/summary.json"
USER_AGENT = "discord-status-cli/1.0 (Python urllib)"

RESET = "\033[0m"
BOLD = "\033[1m"
DIM = "\033[2m"
GREEN = "\033[92m"
YELLOW = "\033[93m"
ORANGE = "\033[38;5;208m"
RED = "\033[91m"
CYAN = "\033[96m"


def parse_args():
    parser = argparse.ArgumentParser(
        description="Live Discord status and incident monitor"
    )

    parser.add_argument(
        "-i", "--interval",
        type=float,
        default=30,
        help="Refresh interval in seconds (minimum 5)"
    )

    parser.add_argument(
        "-t", "--timeout",
        type=float,
        default=10,
        help="HTTP timeout in seconds"
    )

    parser.add_argument(
        "--once",
        action="store_true",
        help="Fetch once and exit"
    )

    parser.add_argument(
        "--no-color",
        action="store_true",
        help="Disable terminal colors"
    )

    parser.add_argument(
        "--no-clear",
        action="store_true",
        help="Do not clear terminal between updates"
    )

    parser.add_argument(
        "--beep",
        action="store_true",
        help="Beep when a status changes"
    )

    args = parser.parse_args()

    if args.interval < 5:
        parser.error("--interval must be at least 5 seconds")

    if args.timeout <= 0:
        parser.error("--timeout must be greater than zero")

    return args


def supports_color(disabled):
    return (
        not disabled
        and sys.stdout.isatty()
        and os.environ.get("NO_COLOR") is None
        and os.environ.get("TERM", "") != "dumb"
    )


def paint(text, color, enabled, bold=False):
    if not enabled:
        return text

    prefix = (BOLD if bold else "") + color
    return f"{prefix}{text}{RESET}"


def status_color(value):
    value = (value or "").lower()

    if value in {
        "operational", "none", "resolved",
        "completed", "up"
    }:
        return GREEN

    if value in {
        "degraded_performance", "minor",
        "monitoring", "verifying"
    }:
        return YELLOW

    if value in {
        "partial_outage", "major",
        "identified", "investigating",
        "in_progress"
    }:
        return ORANGE

    if value in {
        "major_outage", "critical", "down"
    }:
        return RED

    return CYAN


def fetch_summary(timeout):
    request = Request(
        API_URL,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "application/json",
            "Cache-Control": "no-cache",
        }
    )

    started = time.monotonic()

    with urlopen(request, timeout=timeout) as response:
        if response.status != 200:
            raise RuntimeError(
                f"HTTP {response.status}"
            )

        raw = response.read()

    elapsed_ms = (time.monotonic() - started) * 1000

    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError(
            f"Invalid JSON response: {exc}"
        ) from exc

    if not isinstance(payload, dict):
        raise RuntimeError("Invalid API response")

    if not isinstance(payload.get("status"), dict):
        raise RuntimeError("Missing status information")

    return payload, elapsed_ms


def latest_update(item):
    updates = item.get("incident_updates") or []

    if not isinstance(updates, list):
        return {}

    valid = [
        entry for entry in updates
        if isinstance(entry, dict)
    ]

    if not valid:
        return {}

    return max(
        valid,
        key=lambda entry: str(
            entry.get("created_at")
            or entry.get("display_at")
            or ""
        )
    )


def make_state(payload):
    status = payload.get("status") or {}
    components = payload.get("components") or []
    incidents = payload.get("incidents") or []
    maintenances = payload.get(
        "scheduled_maintenances"
    ) or []

    component_state = {}

    for component in components:
        if not isinstance(component, dict):
            continue

        key = str(
            component.get("id")
            or component.get("name")
            or "unknown"
        )

        component_state[key] = (
            str(component.get("name") or key),
            str(component.get("status") or "unknown")
        )

    incident_state = {}

    for incident in incidents:
        if not isinstance(incident, dict):
            continue

        key = str(
            incident.get("id")
            or incident.get("name")
            or "unknown"
        )

        update = latest_update(incident)

        incident_state[key] = (
            str(incident.get("name") or "Unnamed incident"),
            str(incident.get("status") or "unknown"),
            str(update.get("body") or "")
        )

    maintenance_state = {}

    for maintenance in maintenances:
        if not isinstance(maintenance, dict):
            continue

        key = str(
            maintenance.get("id")
            or maintenance.get("name")
            or "unknown"
        )

        maintenance_state[key] = (
            str(maintenance.get("name") or "Unnamed maintenance"),
            str(maintenance.get("status") or "unknown")
        )

    return {
        "overall": (
            str(status.get("indicator") or "unknown"),
            str(status.get("description") or "No status description")
        ),
        "components": component_state,
        "incidents": incident_state,
        "maintenances": maintenance_state
    }


def detect_changes(previous, current):
    if previous is None:
        return []

    changes = []

    old_indicator, old_description = previous["overall"]
    new_indicator, new_description = current["overall"]

    if (old_indicator, old_description) != (
        new_indicator, new_description
    ):
        changes.append(
            f"Overall status: {old_description} -> {new_description}"
        )

    old_components = previous["components"]
    new_components = current["components"]

    for key, (name, state) in new_components.items():

        if key in old_components:
            old_state = old_components[key][1]

            if old_state != state:
                changes.append(
                    f"Component changed: {name}: "
                    f"{old_state} -> {state}"
                )
        else:
            changes.append(
                f"Component added: {name} ({state})"
            )

    old_incidents = previous["incidents"]
    new_incidents = current["incidents"]

    for key, (name, state, body) in new_incidents.items():

        if key not in old_incidents:
            changes.append(
                f"New incident: {name} ({state})"
            )

        elif old_incidents[key] != (name, state, body):
            changes.append(
                f"Incident updated: {name} ({state})"
            )

    for key, (name, _, _) in old_incidents.items():
        if key not in new_incidents:
            changes.append(
                f"Incident no longer listed: {name} "
                "(possibly resolved)"
            )

    old_maintenances = previous["maintenances"]
    new_maintenances = current["maintenances"]

    for key, (name, state) in new_maintenances.items():

        if key not in old_maintenances:
            changes.append(
                f"Maintenance notice: {name} ({state})"
            )

        elif old_maintenances[key][1] != state:
            changes.append(
                f"Maintenance updated: {name}: "
                f"{old_maintenances[key][1]} -> {state}"
            )

    return changes


def clip(text, width):
    text = " ".join(str(text).split())

    if width <= 1:
        return text[:width]

    if len(text) > width:
        return text[:width - 1] + "…"

    return text


def render(
    payload,
    latency_ms,
    last_success,
    last_error,
    events,
    use_color
):
    width = max(
        60,
        shutil.get_terminal_size((100, 28)).columns
    )

    line = "─" * min(width, 100)

    output = []

    output.append(
        paint("DISCORD STATUS MONITOR", CYAN, use_color, True)
    )

    output.append(paint(line, DIM, use_color))

    output.append(f"Source: {API_URL}")

    output.append(
        "Checked: "
        + datetime.now().astimezone().strftime(
            "%Y-%m-%d %H:%M:%S %Z"
        )
    )

    if latency_ms is not None:
        output.append(
            f"Status API response: {latency_ms:.0f} ms"
        )

    if last_success:
        output.append(
            f"Last successful check: {last_success}"
        )

    if last_error:
        output.append(
            paint(
                f"FETCH ERROR: {last_error}",
                RED,
                use_color,
                True
            )
        )

    output.append(paint(line, DIM, use_color))

    if payload is None:
        output.append(
            "No successful status response received yet."
        )

    else:
        status = payload.get("status") or {}

        indicator = str(
            status.get("indicator") or "unknown"
        )

        description = str(
            status.get("description") or "No status description"
        )

        output.append(
            "OVERALL: "
            + paint(
                description,
                status_color(indicator),
                use_color,
                True
            )
        )

        components = payload.get("components") or []

        output.append("")
        output.append(
            paint(
                f"COMPONENTS ({len(components)})",
                CYAN,
                use_color,
                True
            )
        )

        if not components:
            output.append("  No component data returned.")

        for component in components:
            if not isinstance(component, dict):
                continue

            name = str(
                component.get("name") or "Unknown component"
            )

            state = str(
                component.get("status") or "unknown"
            )

            label = state.replace("_", " ").upper()

            name_width = max(20, width - 24)

            row = (
                f"  {clip(name, name_width):<{name_width}}  "
                f"{label}"
            )

            output.append(
                paint(row, status_color(state), use_color)
            )

        incidents = payload.get("incidents") or []

        output.append("")
        output.append(
            paint(
                f"ACTIVE INCIDENTS ({len(incidents)})",
                CYAN,
                use_color,
                True
            )
        )

        if not incidents:
            output.append(
                paint(
                    "  No unresolved incidents reported.",
                    GREEN,
                    use_color
                )
            )

        for incident in incidents:
            if not isinstance(incident, dict):
                continue

            name = str(
                incident.get("name") or "Unnamed incident"
            )

            state = str(
                incident.get("status") or "unknown"
            )

            impact = str(
                incident.get("impact") or "unknown"
            )

            output.append(
                paint(
                    f"  [{impact.upper()} / {state.upper()}] {name}",
                    status_color(impact),
                    use_color,
                    True
                )
            )

            update = latest_update(incident)

            body = str(
                update.get("body") or ""
            ).strip()

            if body:
                output.append(
                    f"    {clip(body, width - 6)}"
                )

            shortlink = incident.get("shortlink")

            if shortlink:
                output.append(f"    {shortlink}")

        maintenances = payload.get(
            "scheduled_maintenances"
        ) or []

        if maintenances:
            output.append("")

            output.append(
                paint(
                    f"SCHEDULED / ACTIVE MAINTENANCE ({len(maintenances)})",
                    CYAN,
                    use_color,
                    True
                )
            )

            for item in maintenances:
                if not isinstance(item, dict):
                    continue

                name = str(
                    item.get("name") or "Unnamed maintenance"
                )

                state = str(
                    item.get("status") or "unknown"
                )

                when = (
                    item.get("scheduled_for")
                    or item.get("scheduled_until")
                    or ""
                )

                output.append(
                    f"  {paint(state.upper(), status_color(state), use_color)}  {name}"
                )

                if when:
                    output.append(f"    Scheduled: {when}")

    output.append("")

    output.append(
        paint("RECENT CHANGES", CYAN, use_color, True)
    )

    if events:
        for event_time, event in events[-8:]:
            output.append(
                f"  [{event_time}] {clip(event, width - 12)}"
            )
    else:
        output.append(
            "  No changes detected since monitoring started."
        )

    output.append("")

    output.append(
        paint(
            "Ctrl+C to stop | Polling the official public status feed",
            DIM,
            use_color
        )
    )

    print("\n".join(output), flush=True)


def main():
    args = parse_args()

    use_color = supports_color(args.no_color)

    previous_state = None
    latest_payload = None
    latest_latency = None
    last_success = None
    last_error = None

    events = []

    try:
        while True:

            try:
                payload, latency = fetch_summary(args.timeout)

                current_state = make_state(payload)

                changes = detect_changes(
                    previous_state,
                    current_state
                )

                if changes:
                    event_time = datetime.now().astimezone().strftime(
                        "%H:%M:%S"
                    )

                    events.extend(
                        (event_time, change)
                        for change in changes
                    )

                    events = events[-8:]

                    if args.beep and sys.stdout.isatty():
                        print("\a", end="", flush=True)

                previous_state = current_state
                latest_payload = payload
                latest_latency = latency

                last_success = datetime.now().astimezone().strftime(
                    "%H:%M:%S %Z"
                )

                last_error = None

            except (
                HTTPError,
                URLError,
                TimeoutError,
                OSError,
                RuntimeError
            ) as exc:

                if isinstance(exc, HTTPError):
                    last_error = (
                        f"HTTP {exc.code}: {exc.reason}"
                    )

                elif isinstance(exc, URLError):
                    last_error = (
                        f"Network error: {exc.reason}"
                    )

                else:
                    last_error = (
                        str(exc) or exc.__class__.__name__
                    )

            if not args.no_clear and sys.stdout.isatty():
                print("\033[2J\033[H", end="")

            render(
                latest_payload,
                latest_latency,
                last_success,
                last_error,
                events,
                use_color
            )

            if args.once:
                return 1 if last_error else 0

            time.sleep(args.interval)

    except KeyboardInterrupt:
        print("\nStopped.")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
