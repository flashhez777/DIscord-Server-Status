#!/usr/bin/env python3
"""Offline unit tests for Discord Global Status Monitor 3.2.1."""

import io
import json
import ssl
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

import discord_server_global_status as monitor


class FakeResponse:
    def __init__(self, payload, headers=None, status=200):
        self._data = json.dumps(payload).encode("utf-8")
        self.headers = headers or {}
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def read(self, size=-1):
        return self._data if size < 0 else self._data[:size]


class TLSAndNetworkTests(unittest.TestCase):
    def setUp(self):
        monitor._TLS_CONTEXT = None
        monitor._TLS_CA_SOURCES = []

    def test_tls_verification_is_never_disabled(self):
        context = monitor.initialize_tls_context()
        self.assertEqual(context.verify_mode, ssl.CERT_REQUIRED)
        self.assertTrue(context.check_hostname)
        self.assertGreater(len(context.get_ca_certs()), 0)

    def test_tls_error_has_actionable_diagnosis(self):
        error = ssl.SSLCertVerificationError(
            1, "unable to get local issuer certificate"
        )
        message = monitor.tls_failure_message(error)
        self.assertIn("certificate verification failed", message.lower())
        self.assertIn("update-ca-certificates", message)
        self.assertIn("--ca-bundle", message)

    def test_wrapped_tls_error_is_detected(self):
        error = ssl.SSLCertVerificationError(
            1, "unable to get local issuer certificate"
        )
        wrapped = urllib.error.URLError(error)
        message = monitor.tls_failure_message(wrapped)
        self.assertIsNotNone(message)
        self.assertIn("unable to get local issuer certificate", message)

    def test_additional_ca_bundle_is_loaded_without_disabling_verify(self):
        monitor._TLS_CONTEXT = None
        with tempfile.TemporaryDirectory() as temp_dir:
            ca_file = Path(temp_dir) / "approved-root.pem"
            ca_file.write_text("test root placeholder", encoding="ascii")
            with mock.patch.object(ssl.SSLContext, "load_verify_locations", autospec=True) as load:
                context = monitor.initialize_tls_context(ca_file)
            self.assertEqual(context.verify_mode, ssl.CERT_REQUIRED)
            self.assertTrue(context.check_hostname)
            self.assertTrue(any(
                call.kwargs.get("cafile") == str(ca_file)
                for call in load.call_args_list
            ))

    def test_request_json_passes_verified_context(self):
        monitor.initialize_tls_context()
        payload = {"status": {"indicator": "none"}, "components": []}
        with mock.patch.object(
            monitor.urllib.request, "urlopen", return_value=FakeResponse(payload)
        ) as urlopen:
            data, elapsed = monitor.request_json("https://unit.test/status", 2)
        self.assertEqual(data, payload)
        self.assertGreaterEqual(elapsed, 0)
        self.assertIs(urlopen.call_args.kwargs["context"], monitor.tls_context())

    def test_globalping_get_retries_transient_network_failure(self):
        monitor.initialize_tls_context()
        payload = {"ok": True}
        responses = [urllib.error.URLError("temporary"), FakeResponse(payload)]
        with mock.patch.object(monitor.urllib.request, "urlopen", side_effect=responses) as urlopen, \
             mock.patch.object(monitor.time, "sleep"):
            result, _headers = monitor.globalping_request("GET", "https://unit.test", 2)
        self.assertEqual(result, payload)
        self.assertEqual(urlopen.call_count, 2)

    def test_globalping_post_is_not_retried(self):
        monitor.initialize_tls_context()
        with mock.patch.object(
            monitor.urllib.request, "urlopen", side_effect=urllib.error.URLError("temporary")
        ) as urlopen:
            with self.assertRaises(monitor.MonitorError):
                monitor.globalping_request("POST", "https://unit.test", 2, payload={"type": "ping"})
        self.assertEqual(urlopen.call_count, 1)

    def test_globalping_429_surfaces_retry_after_without_retry_storm(self):
        monitor.initialize_tls_context()
        error = urllib.error.HTTPError(
            "https://unit.test", 429, "Too Many Requests",
            {"Retry-After": "60"}, io.BytesIO(b'{"error":{"message":"limit reached"}}')
        )
        with mock.patch.object(monitor.urllib.request, "urlopen", side_effect=error) as urlopen:
            with self.assertRaisesRegex(monitor.MonitorError, "Retry-After=60"):
                monitor.globalping_request("GET", "https://unit.test", 2)
        self.assertEqual(urlopen.call_count, 1)

    def test_version_comparison_recognizes_stale_ca_bundle(self):
        minimum = monitor._version_tuple("2026.11.20")
        self.assertLess(monitor._version_tuple("2026.5.20"), minimum)
        self.assertGreaterEqual(monitor._version_tuple("2026.11.20"), minimum)


class TelemetryTests(unittest.TestCase):
    def test_percentile_interpolation(self):
        self.assertAlmostEqual(monitor.percentile([0, 10, 20, 30], 50), 15)
        self.assertEqual(monitor.percentile([], 95), None)

    def test_global_ping_summary_has_p95(self):
        data = {
            "id": "test-id",
            "status": "finished",
            "probesCount": 3,
            "results": [
                {"probe": {"continent": "Europe", "country": "AT", "city": "Vienna"},
                 "result": {"stats": {"avg": 10, "loss": 0}, "status": "finished"}},
                {"probe": {"continent": "Europe", "country": "DE", "city": "Berlin"},
                 "result": {"stats": {"avg": 20, "loss": 0}, "status": "finished"}},
                {"probe": {"continent": "Asia", "country": "JP", "city": "Tokyo"},
                 "result": {"stats": {"avg": 30, "loss": 0}, "status": "finished"}},
            ],
        }
        normalized = monitor.normalize_globalping_result("ping", "gateway.discord.gg", data)
        self.assertEqual(normalized["summary"]["probe_results"], 3)
        self.assertAlmostEqual(normalized["summary"]["median_ms"], 20)
        self.assertAlmostEqual(normalized["summary"]["p95_ms"], 29)

    def test_cdn_remote_http_measurement_normalizes(self):
        data = {
            "id": "cdn-test",
            "status": "finished",
            "probesCount": 1,
            "results": [
                {"probe": {"continent": "Europe", "country": "SK", "city": "Bratislava"},
                 "result": {"status": "finished", "statusCode": 200,
                            "timings": {"dns": 5, "tcp": 12, "tls": 20,
                                        "firstByte": 45, "total": 50}}}
            ],
        }
        normalized = monitor.normalize_globalping_result("cdn", monitor.CDN_TEST_URL, data)
        row = normalized["rows"][0]
        self.assertEqual(row["state"], "UP")
        self.assertEqual(row["http_status"], 200)
        self.assertEqual(row["tls_ms"], 20)
        self.assertEqual(normalized["summary"]["median_ms"], 50)

    def test_bad_component_position_does_not_crash(self):
        groups, standalone = monitor.component_groups([
            {"id": "group", "name": "Services", "group": True},
            {"id": "child", "name": "API", "group_id": "group", "position": "n/a"},
            {"id": "loose", "name": "Other", "position": None},
        ])
        self.assertEqual(groups["Services"][0]["name"], "API")
        self.assertEqual(standalone[0]["name"], "Other")

    def test_state_change_detection(self):
        first = {
            "overall": {"indicator": "none", "description": "All Systems Operational"},
            "components": {"api": {"name": "API", "status": "operational"}},
            "incidents": {}, "maintenances": {},
        }
        second = {
            "overall": {"indicator": "minor", "description": "Partial Service Disruption"},
            "components": {"api": {"name": "API", "status": "degraded_performance"}},
            "incidents": {"inc-1": {"name": "API issues", "impact": "minor", "status": "investigating", "update": "Investigating"}},
            "maintenances": {},
        }
        changes = monitor.detect_changes(first, second)
        self.assertTrue(any("Global status" in change for change in changes))
        self.assertTrue(any("Component" in change for change in changes))
        self.assertTrue(any("NEW INCIDENT" in change for change in changes))


if __name__ == "__main__":
    unittest.main(verbosity=2)
