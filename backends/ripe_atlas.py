#!/usr/bin/env python3
"""Read-only RIPE Atlas backend for existing public measurements.

This backend never creates measurements or spends RIPE Atlas credits. It reads
measurement IDs supplied by the user from the public results API. A RIPE Atlas
API key is optional and is only sent when RIPE_ATLAS_API_KEY is configured.
"""
from __future__ import annotations

import concurrent.futures
import ipaddress
import json
import math
import re
import statistics
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from typing import Any, Optional

from backends.http_transport import safe_urlopen
from dgsm_version import __version__ as DGSM_VERSION

API_BASE = "https://atlas.ripe.net/api/v2/measurements"
USER_AGENT = f"DiscordGlobalStatusMonitor-RIPEAtlas/{DGSM_VERSION}"
MAX_RESPONSE_BYTES = 8 * 1024 * 1024


class BackendError(RuntimeError):
    """Provider-specific error with an actionable diagnostic."""


def parse_measurement_ids(raw: Optional[str], maximum: int = 8) -> list[int]:
    if not raw:
        return []
    if not isinstance(maximum, int) or isinstance(maximum, bool) or maximum < 1:
        raise BackendError("RIPE Atlas measurement ID limit must be a positive integer.")
    if not isinstance(raw, str) or len(raw) > 512:
        raise BackendError("RIPE Atlas measurement ID list exceeds the 512-character limit.")
    values: list[int] = []
    seen: set[int] = set()
    for item in raw.split(","):
        token = item.strip()
        if not token:
            continue
        if not token.isascii() or not token.isdigit() or len(token) > 19:
            raise BackendError(
                f"Invalid RIPE Atlas measurement ID {token!r}; use positive ASCII numeric IDs up to 19 digits."
            )
        value = int(token)
        if value <= 0 or value > 9_223_372_036_854_775_807:
            raise BackendError("RIPE Atlas measurement IDs must be positive 64-bit integers.")
        if value not in seen:
            values.append(value)
            seen.add(value)
        if len(values) > maximum:
            raise BackendError(f"At most {maximum} RIPE Atlas measurement IDs can be queried per cycle.")
    return values


def _numeric(value: Any) -> Optional[float]:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) and number >= 0 else None


def _get_json(
    measurement_id: int,
    timeout: float,
    context: Any,
    api_key: Optional[str] = None,
) -> list[dict[str, Any]]:
    # /latest returns one recent result per probe instead of the full history.
    # This avoids downloading years of samples for every monitoring cycle.
    query = urllib.parse.urlencode({
        "format": "json",
        "lookback_days": 7,
        "versions": 1,
    })
    url = f"{API_BASE}/{measurement_id}/latest/?{query}"
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "application/json",
        "Cache-Control": "no-cache",
    }
    if api_key:
        headers["Authorization"] = f"Key {api_key}"
    request = urllib.request.Request(url, headers=headers, method="GET")
    started = time.perf_counter()
    try:
        with safe_urlopen(
            request,
            timeout=timeout,
            context=context,
            allowed_hosts={"atlas.ripe.net"},
        ) as response:
            raw = response.read(MAX_RESPONSE_BYTES + 1)
            if len(raw) > MAX_RESPONSE_BYTES:
                raise BackendError(
                    f"RIPE Atlas measurement {measurement_id} exceeded the {MAX_RESPONSE_BYTES // (1024 * 1024)} MiB response limit."
                )
            payload = json.loads(raw.decode("utf-8")) if raw else []
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            raise BackendError(f"RIPE Atlas measurement {measurement_id} was not found or is not public (HTTP 404).") from exc
        if exc.code in (401, 403):
            raise BackendError(
                f"RIPE Atlas denied measurement {measurement_id} (HTTP {exc.code}); check whether it is public or configure RIPE_ATLAS_API_KEY."
            ) from exc
        retry_after = exc.headers.get("Retry-After") if exc.headers else None
        if exc.code == 429:
            suffix = f"Retry-After={retry_after}" if retry_after else "respect the provider's rate limit"
            raise BackendError(f"RIPE Atlas rate limit reached for measurement {measurement_id}; {suffix}.") from exc
        suffix = f"; Retry-After={retry_after}" if retry_after else ""
        raise BackendError(f"RIPE Atlas HTTP {exc.code} {exc.reason}{suffix}") from exc
    except urllib.error.URLError as exc:
        reason = getattr(exc, "reason", exc)
        raise BackendError(f"RIPE Atlas network/TLS error: {reason}") from exc
    except (TimeoutError, OSError) as exc:
        raise BackendError(f"RIPE Atlas request failed: {exc}") from exc
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise BackendError(f"RIPE Atlas returned invalid JSON: {exc}") from exc

    elapsed_ms = (time.perf_counter() - started) * 1000.0
    if not isinstance(payload, (dict, list)):
        raise BackendError("RIPE Atlas latest endpoint returned an unexpected JSON shape.")
    if isinstance(payload, dict) and payload.get("detail"):
        raise BackendError(f"RIPE Atlas: {str(payload['detail'])[:400]}")
    rows: list[dict[str, Any]] = []
    if isinstance(payload, list):
        # Accept the results endpoint response shape for compatibility with
        # mocks and older deployments.
        rows = [item for item in payload if isinstance(item, dict)]
    elif isinstance(payload, dict):
        # /latest returns {"probe_id": [result, ...]}. Attach the key as
        # prb_id when the individual result omits it.
        for probe_id, values in payload.items():
            if isinstance(values, dict):
                values = [values]
            if not isinstance(values, list):
                continue
            for item in values:
                if isinstance(item, dict):
                    row = dict(item)
                    row.setdefault("prb_id", probe_id)
                    rows.append(row)
    else:
        raise BackendError("RIPE Atlas latest endpoint returned an unexpected JSON shape.")
    if not rows:
        raise BackendError(
            f"RIPE Atlas measurement {measurement_id} returned no recent result rows in the last 7 days (API read took {elapsed_ms:.1f} ms)."
        )
    return rows


def _extract_rtts(record: dict[str, Any]) -> tuple[list[float], int]:
    raw_result = record.get("result")
    samples: list[Any]
    if isinstance(raw_result, list):
        samples = raw_result
    elif isinstance(raw_result, dict):
        samples = [raw_result]
    else:
        samples = []

    rtts: list[float] = []
    for sample in samples:
        if isinstance(sample, dict):
            value = _numeric(sample.get("rtt"))
            if value is not None:
                rtts.append(value)
    # Some Atlas result variants expose aggregate values rather than an ICMP
    # packet list. Preserve those values without guessing at packet loss.
    if not rtts:
        for field in ("avg", "rtt_avg", "median"):
            value = _numeric(record.get(field))
            if value is not None:
                rtts.append(value)
                break
    return rtts, len(samples)


def normalize_results(measurement_id: int, records: list[dict[str, Any]]) -> dict[str, Any]:
    output_rows: list[dict[str, Any]] = []
    all_probe_averages: list[float] = []
    all_losses: list[float] = []
    targets: set[str] = set()
    for record in records:
        rtts, sent = _extract_rtts(record)
        probe_id = record.get("prb_id") or record.get("probe_id") or "?"
        raw_target = record.get("dst_name") or record.get("dst_addr") or "unknown"
        target = str(raw_target)[:255].replace("\r", " ").replace("\n", " ")
        targets.add(target)
        raw_source_ip = record.get("from") or ""
        try:
            source_ip = str(ipaddress.ip_address(raw_source_ip)) if raw_source_ip else ""
        except (ValueError, TypeError):
            source_ip = str(raw_source_ip)[:64].replace("\r", " ").replace("\n", " ")
        avg = statistics.mean(rtts) if rtts else None
        loss = (100.0 * (sent - len(rtts)) / sent) if sent > 0 else None
        row = {
            "probe_id": str(probe_id)[:32],
            "source_ip": source_ip,
            "target": target,
            "timestamp": record.get("timestamp"),
            "latency_ms": avg,
            "min_ms": min(rtts) if rtts else None,
            "max_ms": max(rtts) if rtts else None,
            "loss_pct": loss,
            "samples_received": len(rtts),
            "samples_sent": sent,
            "state": "UP" if rtts else "WARN",
        }
        output_rows.append(row)
        if avg is not None:
            all_probe_averages.append(avg)
        if loss is not None:
            all_losses.append(loss)

    sorted_values = sorted(all_probe_averages)
    p95 = None
    if sorted_values:
        position = 0.95 * (len(sorted_values) - 1)
        lower = int(position)
        upper = min(lower + 1, len(sorted_values) - 1)
        fraction = position - lower
        p95 = sorted_values[lower] * (1 - fraction) + sorted_values[upper] * fraction

    return {
        "type": "ripe_atlas",
        "provider": "RIPE Atlas public probe network",
        "vantage": "remote RIPE Atlas probes; not Discord-internal telemetry",
        "measurement_id": measurement_id,
        "target": ", ".join(sorted(targets)) or "unknown",
        "status": "ok" if all_probe_averages else "partial",
        "completed_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "probes_count": len(output_rows),
        "rows": output_rows,
        "summary": {
            "probe_results": len(output_rows),
            "successful_samples": len(all_probe_averages),
            "median_ms": statistics.median(all_probe_averages) if all_probe_averages else None,
            "p95_ms": p95,
            "mean_ms": statistics.mean(all_probe_averages) if all_probe_averages else None,
            "min_ms": min(all_probe_averages) if all_probe_averages else None,
            "max_ms": max(all_probe_averages) if all_probe_averages else None,
            "median_loss_pct": statistics.median(all_losses) if all_losses else None,
            "up": sum(1 for row in output_rows if row["state"] == "UP"),
            "warn": sum(1 for row in output_rows if row["state"] != "UP"),
            "down": 0,
        },
    }


def collect(
    raw_ids: Optional[str],
    timeout: float,
    context: Any,
    api_key: Optional[str] = None,
) -> dict[str, Any]:
    """Read and aggregate user-selected existing public Atlas measurements."""
    ids = parse_measurement_ids(raw_ids)
    if not ids:
        return {
            "type": "ripe_atlas",
            "provider": "RIPE Atlas public probe network",
            "status": "disabled",
            "rows": [],
            "summary": {},
            "error": "No measurement IDs configured; set RIPE_ATLAS_MEASUREMENT_IDS.",
        }

    measurements = []
    errors = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=min(4, len(ids))) as executor:
        futures = {
            executor.submit(_get_json, measurement_id, timeout, context, api_key): measurement_id
            for measurement_id in ids
        }
        for future in concurrent.futures.as_completed(futures):
            measurement_id = futures[future]
            try:
                records = future.result()
                measurements.append(normalize_results(measurement_id, records))
            except Exception as exc:
                errors.append(f"Measurement {measurement_id}: {exc}")

    rows = [row for item in measurements for row in item["rows"]]
    latencies = [float(row["latency_ms"]) for row in rows if isinstance(row.get("latency_ms"), (int, float))]
    losses = [float(row["loss_pct"]) for row in rows if isinstance(row.get("loss_pct"), (int, float))]
    ordered = sorted(latencies)
    position = 0.95 * (len(ordered) - 1) if ordered else 0
    if ordered:
        low, high = int(position), min(int(position) + 1, len(ordered) - 1)
        p95 = ordered[low] * (1 - (position - low)) + ordered[high] * (position - low)
    else:
        p95 = None
    summary = {
        "probe_results": len(rows),
        "successful_samples": len(latencies),
        "median_ms": statistics.median(latencies) if latencies else None,
        "p95_ms": p95,
        "mean_ms": statistics.mean(latencies) if latencies else None,
        "min_ms": min(latencies) if latencies else None,
        "max_ms": max(latencies) if latencies else None,
        "median_loss_pct": statistics.median(losses) if losses else None,
        "up": sum(1 for row in rows if row.get("state") == "UP"),
        "warn": sum(1 for row in rows if row.get("state") == "WARN"),
        "down": 0,
    }
    return {
        "type": "ripe_atlas",
        "provider": "RIPE Atlas public probe network",
        "vantage": "remote RIPE Atlas probes; not Discord-internal telemetry",
        "measurement_ids": ids,
        "target": ", ".join(sorted({str(row.get("target")) for row in rows})) or "unknown",
        "status": "ok" if latencies else ("error" if errors else "partial"),
        "completed_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "probes_count": len(rows),
        "rows": rows,
        "summary": summary,
        "errors": errors,
        "error": "; ".join(errors) if errors and not latencies else None,
    }
