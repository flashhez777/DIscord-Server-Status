#!/usr/bin/env python3
"""Offline unit tests for Discord Global Status Monitor 3.3.3."""

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


class GlobalpingRequestSchemaTests(unittest.TestCase):
    def setUp(self):
        monitor._TLS_CONTEXT = monitor.initialize_tls_context()

    def _capture_create_payload(self, target):
        with mock.patch.object(
            monitor,
            "globalping_request",
            side_effect=[
                ({"id": "measurement-1"}, {}),
                ({"id": "measurement-1", "status": "finished"}, {}),
            ],
        ) as request, mock.patch.object(monitor.time, "sleep"):
            result = monitor.create_globalping_measurement("http", target, 4, 5, None)
        self.assertEqual(result["id"], "measurement-1")
        args = request.call_args_list[0].args
        self.assertEqual(args[0], "POST")
        return args[4]

    def test_gateway_http_target_is_split_into_hostname_and_http_options(self):
        payload = self._capture_create_payload("https://discord.com/api/v10/gateway")
        self.assertEqual(payload["target"], "discord.com")
        self.assertEqual(payload["type"], "http")
        self.assertEqual(payload["measurementOptions"], {
            "protocol": "HTTPS",
            "port": 443,
            "request": {"method": "GET", "path": "/api/v10/gateway"},
        })
        self.assertNotIn("https://", payload["target"])

    def test_cdn_http_target_preserves_path_and_query(self):
        payload = self._capture_create_payload(
            "https://cdn.discordapp.com/embed/avatars/0.png?size=128"
        )
        self.assertEqual(payload["target"], "cdn.discordapp.com")
        self.assertEqual(
            payload["measurementOptions"]["request"]["path"],
            "/embed/avatars/0.png",
        )
        self.assertEqual(
            payload["measurementOptions"]["request"]["query"],
            "size=128",
        )

    def test_custom_http_port_is_preserved(self):
        payload = self._capture_create_payload("http://status.example.test:8080/health")
        self.assertEqual(payload["target"], "status.example.test")
        self.assertEqual(payload["measurementOptions"]["protocol"], "HTTP")
        self.assertEqual(payload["measurementOptions"]["port"], 8080)
        self.assertEqual(payload["measurementOptions"]["request"]["path"], "/health")

    def test_invalid_http_url_is_rejected_before_network_request(self):
        with mock.patch.object(monitor, "globalping_request") as request:
            with self.assertRaisesRegex(monitor.MonitorError, r"expected an http\(s\) URL"):
                monitor.create_globalping_measurement("http", "not-a-url", 4, 5, None)
        request.assert_not_called()

    def test_validation_error_includes_parameter_details(self):
        body = json.dumps({
            "error": {
                "type": "validation_error",
                "message": "Parameter validation failed",
                "params": {"target": "must be a hostname or IP address"},
            }
        }).encode("utf-8")
        error = urllib.error.HTTPError(
            "https://api.globalping.io/v1/measurements",
            400,
            "Bad Request",
            {},
            io.BytesIO(body),
        )
        with mock.patch.object(monitor.urllib.request, "urlopen", side_effect=error):
            with self.assertRaises(monitor.MonitorError) as caught:
                monitor.globalping_request(
                    "POST", "https://api.globalping.io/v1/measurements", 5,
                    payload={"type": "http", "target": "https://discord.com"},
                )
        self.assertIn("Parameter validation failed", str(caught.exception))
        self.assertIn("target", str(caught.exception))
        self.assertIn("hostname or IP address", str(caught.exception))



class RipeAtlasBackendTests(unittest.TestCase):
    def test_parse_measurement_ids_deduplicates_and_validates(self):
        from backends.ripe_atlas import parse_measurement_ids
        self.assertEqual(parse_measurement_ids("123, 456,123"), [123, 456])

    def test_parse_measurement_ids_rejects_non_numeric(self):
        from backends.ripe_atlas import BackendError, parse_measurement_ids
        with self.assertRaises(BackendError):
            parse_measurement_ids("123,discord")

    def test_parse_measurement_ids_caps_number_of_measurements(self):
        from backends.ripe_atlas import BackendError, parse_measurement_ids
        with self.assertRaises(BackendError):
            parse_measurement_ids("1,2,3,4,5,6,7,8,9")

    def test_normalize_ping_results_computes_rtt_and_loss(self):
        from backends.ripe_atlas import normalize_results
        normalized = normalize_results(1234, [{
            "prb_id": 55,
            "dst_name": "gateway.discord.gg",
            "from": "192.0.2.10",
            "timestamp": 1795800000,
            "result": [{"rtt": 20.0}, {"rtt": 22.0}, {"x": "timeout"}],
        }])
        row = normalized["rows"][0]
        self.assertEqual(row["probe_id"], 55)
        self.assertAlmostEqual(row["latency_ms"], 21.0)
        self.assertAlmostEqual(row["loss_pct"], 100 / 3)
        self.assertAlmostEqual(normalized["summary"]["median_ms"], 21.0)

    def test_latest_results_dict_shape_is_normalized_by_probe_id(self):
        from backends.ripe_atlas import _get_json
        payload = {
            "1001": [{
                "dst_name": "gateway.discord.gg",
                "result": [{"rtt": 24.0}, {"rtt": 26.0}],
            }],
            "1002": [{
                "dst_name": "gateway.discord.gg",
                "result": [{"rtt": 40.0}, {"x": "timeout"}],
            }],
        }
        response = FakeResponse(payload)
        context = ssl.create_default_context()
        with mock.patch("backends.ripe_atlas.urllib.request.urlopen", return_value=response) as urlopen:
            rows = _get_json(9988, 5, context)
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["prb_id"], "1001")
        request = urlopen.call_args.args[0]
        self.assertIn("/9988/latest/?", request.full_url)
        self.assertIn("lookback_days=7", request.full_url)
        self.assertIn("versions=1", request.full_url)

    def test_collect_reads_existing_ids_without_creating_measurements(self):
        from backends import ripe_atlas
        record = {
            "prb_id": 88,
            "dst_name": "gateway.discord.gg",
            "result": [{"rtt": 32.5}, {"rtt": 34.0}],
        }
        with mock.patch.object(ripe_atlas, "_get_json", return_value=[record]) as fetch:
            result = ripe_atlas.collect("98765", 5, ssl.create_default_context())
        fetch.assert_called_once()
        self.assertEqual(result["provider"], "RIPE Atlas public probe network")
        self.assertEqual(result["probes_count"], 1)
        self.assertAlmostEqual(result["summary"]["median_ms"], 33.25)

    def test_collect_no_ids_is_disabled_without_network(self):
        from backends.ripe_atlas import collect
        result = collect("", 5, ssl.create_default_context())
        self.assertEqual(result["status"], "disabled")
        self.assertEqual(result["rows"], [])


class WindowsBuildBootstrapTests(unittest.TestCase):
    def test_builder_uses_safe_process_capture_for_runtime_discovery(self):
        script = Path(__file__).with_name("build_windows.ps1").read_text(encoding="utf-8")
        self.assertIn("function Invoke-CapturedProcess", script)
        self.assertIn("function Ensure-Python312", script)
        self.assertIn("-Source 'msstore'", script)
        self.assertIn("py install", script)
        self.assertIn("'list', '-1', '--format=exe', '3.12'", script)
        self.assertIn("$pythonManagerBin = Join-Path $env:LOCALAPPDATA 'Python\\bin'", script)
        self.assertIn("System.Collections.Generic.HashSet[string]", script)
        self.assertNotIn("& $launcher.Source -3.12", script)

    def test_builder_stages_additional_backend_modules(self):
        script = Path(__file__).with_name("build_windows.ps1").read_text(encoding="utf-8")
        self.assertIn("Get-ChildItem -LiteralPath $BackendsDir -Filter '*.py' -File | Copy-Item -Destination $DllBackends -Force", script)
        self.assertIn("Get-ChildItem -LiteralPath $BackendsDir -Filter '*.py' -File | Copy-Item -Destination $PackageBackends -Force", script)


    def test_signing_uses_process_capture_not_native_call_operator(self):
        signer = Path(__file__).with_name("sign_windows.ps1").read_text(encoding="utf-8")
        self.assertIn("function Invoke-CapturedProcess", signer)
        self.assertIn("function Invoke-SignTool", signer)
        self.assertNotIn("& $signTool.FullName sign", signer)

    def test_github_actions_ci_workflow_exists(self):
        workflow = Path(__file__).parent / ".github" / "workflows" / "ci.yml"
        self.assertTrue(workflow.is_file())
        text = workflow.read_text(encoding="utf-8")
        self.assertIn("windows-latest", text)
        self.assertIn("ubuntu-latest", text)
        self.assertIn("test_discord_server_global_status.py", text)


if __name__ == "__main__":
    unittest.main(verbosity=2)
