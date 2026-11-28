#!/usr/bin/env python3
"""Offline unit tests for Discord Global Status Monitor 3.4.5."""

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
    def test_rest_gateway_probe_rejects_unexpected_websocket_hosts(self):
        with mock.patch.object(
            monitor, "request_json", return_value=({"url": "wss://evil.example"}, 1.0)
        ):
            result = monitor.probe_rest_api(2)
        self.assertEqual(result.state, "WARN")
        self.assertIn("invalid", result.detail)

    def test_rest_gateway_probe_accepts_official_gateway_url(self):
        with mock.patch.object(
            monitor, "request_json", return_value=({"url": "wss://gateway.discord.gg"}, 1.0)
        ):
            result = monitor.probe_rest_api(2)
        self.assertEqual(result.state, "UP")
        self.assertIn("expected Discord Gateway URL", result.detail)

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
            monitor, "safe_urlopen", return_value=FakeResponse(payload)
        ) as urlopen:
            data, elapsed = monitor.request_json("https://discordstatus.com/api/v2/summary.json", 2)
        self.assertEqual(data, payload)
        self.assertGreaterEqual(elapsed, 0)
        self.assertIs(urlopen.call_args.kwargs["context"], monitor.tls_context())

    def test_statuspage_response_size_is_bounded(self):
        monitor.initialize_tls_context()
        oversized = {"padding": "x" * (2 * 1024 * 1024)}
        with mock.patch.object(
            monitor, "safe_urlopen", return_value=FakeResponse(oversized)
        ):
            with self.assertRaisesRegex(monitor.MonitorError, "exceeded the 2 MiB response safety limit"):
                monitor.request_json("https://discordstatus.com/api/v2/summary.json", 2)

    def test_globalping_get_retries_transient_network_failure(self):
        monitor.initialize_tls_context()
        payload = {"ok": True}
        responses = [urllib.error.URLError("temporary"), FakeResponse(payload)]
        with mock.patch.object(monitor, "safe_urlopen", side_effect=responses) as urlopen, \
             mock.patch.object(monitor.time, "sleep"):
            result, _headers = monitor.globalping_request("GET", "https://api.globalping.io/v1/limits", 2)
        self.assertEqual(result, payload)
        self.assertEqual(urlopen.call_count, 2)

    def test_globalping_post_is_not_retried(self):
        monitor.initialize_tls_context()
        with mock.patch.object(
            monitor, "safe_urlopen", side_effect=urllib.error.URLError("temporary")
        ) as urlopen:
            with self.assertRaises(monitor.MonitorError):
                monitor.globalping_request("POST", "https://api.globalping.io/v1/measurements", 2, payload={"type": "ping"})
        self.assertEqual(urlopen.call_count, 1)

    def test_globalping_429_surfaces_retry_after_without_retry_storm(self):
        monitor.initialize_tls_context()
        error = urllib.error.HTTPError(
            "https://api.globalping.io/v1/limits", 429, "Too Many Requests",
            {"Retry-After": "60"}, io.BytesIO(b'{"error":{"message":"limit reached"}}')
        )
        with mock.patch.object(monitor, "safe_urlopen", side_effect=error) as urlopen:
            with self.assertRaisesRegex(monitor.MonitorError, "Retry-After=60"):
                monitor.globalping_request("GET", "https://api.globalping.io/v1/limits", 2)
        self.assertEqual(urlopen.call_count, 1)

    def test_version_comparison_recognizes_stale_ca_bundle(self):
        minimum = monitor._version_tuple("2026.11.20")
        self.assertLess(monitor._version_tuple("2026.5.20"), minimum)
        self.assertGreaterEqual(monitor._version_tuple("2026.11.20"), minimum)


class TelemetryTests(unittest.TestCase):
    def test_optional_runtime_installation_is_opt_in(self):
        with mock.patch("sys.argv", ["dgsm", "--no-globalping", "--no-probes"]):
            args = monitor.parse_args()
        self.assertFalse(args.auto_install)
        self.assertTrue(args.no_auto_install)

        with mock.patch("sys.argv", ["dgsm", "--auto-install", "--no-globalping", "--no-probes"]):
            opted_in = monitor.parse_args()
        self.assertTrue(opted_in.auto_install)
        self.assertFalse(opted_in.no_auto_install)

    def test_ripe_atlas_measurement_ids_are_read_from_environment(self):
        with mock.patch.dict("os.environ", {"RIPE_ATLAS_MEASUREMENT_IDS": "12345,67890"}, clear=True), \
             mock.patch("sys.argv", ["dgsm", "--no-globalping", "--no-probes"]):
            args = monitor.parse_args()
        self.assertEqual(args.ripe_atlas_ids, "12345,67890")
        self.assertEqual(args.ripe_atlas_interval, 900)

    def test_ripe_atlas_can_be_disabled_explicitly(self):
        with mock.patch.dict("os.environ", {"RIPE_ATLAS_MEASUREMENT_IDS": "12345"}, clear=True), \
             mock.patch("sys.argv", ["dgsm", "--no-ripe-atlas"]):
            args = monitor.parse_args()
        self.assertEqual(args.ripe_atlas_ids, "")

    def test_ripe_atlas_parser_rejects_invalid_ids_and_short_intervals(self):
        with mock.patch("sys.argv", ["dgsm", "--ripe-atlas-ids", "abc"]):
            with self.assertRaises(SystemExit):
                monitor.parse_args()
        with mock.patch("sys.argv", ["dgsm", "--ripe-atlas-interval", "60"]):
            with self.assertRaises(SystemExit):
                monitor.parse_args()

    def test_ripe_atlas_runner_only_passes_configured_key_to_read_only_backend(self):
        args = monitor.argparse.Namespace(
            ripe_atlas_ids="12345", ripe_atlas_api_key_env="DGSM_TEST_ATLAS_KEY", timeout=4
        )
        with mock.patch.dict("os.environ", {"DGSM_TEST_ATLAS_KEY": "secret-value"}, clear=True), \
             mock.patch("backends.ripe_atlas.collect", return_value={"status": "ok"}) as collect, \
             mock.patch.object(monitor, "tls_context", return_value="verified-context"):
            result = monitor.run_ripe_atlas(args)
        self.assertEqual(result["status"], "ok")
        collect.assert_called_once_with("12345", 4, "verified-context", "secret-value")

    def test_main_json_once_executes_configured_ripe_atlas_collection(self):
        payload = {
            "page": {"updated_at": "2026-11-28T00:00:00Z"},
            "status": {"indicator": "none", "description": "All Systems Operational"},
            "components": [],
            "incidents": [],
            "scheduled_maintenances": [],
        }
        atlas = {
            "type": "ripe_atlas",
            "status": "ok",
            "provider": "RIPE Atlas public probe network",
            "vantage": "remote RIPE Atlas probes; not Discord-internal telemetry",
            "rows": [],
            "summary": {"probe_results": 0, "successful_samples": 0, "up": 0, "warn": 0, "down": 0},
            "probes_count": 0,
            "target": "gateway.discord.gg",
            "completed_at": "2026-11-28T00:00:00+00:00",
        }
        captured = {}

        def capture_json(*args, **kwargs):
            captured["telemetry"] = args[4]

        with tempfile.TemporaryDirectory() as temp_dir, \
             mock.patch("sys.argv", [
                 "dgsm", "--json", "--no-globalping", "--no-probes",
                 "--no-notifications", "--no-auto-install", "--no-save",
                 "--ripe-atlas-ids", "12345", "--data-dir", temp_dir,
             ]), \
             mock.patch.object(monitor, "bootstrap_optional_dependencies"), \
             mock.patch.object(monitor, "initialize_tls_context"), \
             mock.patch.object(monitor, "fetch_official_summary", return_value=(payload, 1.0)), \
             mock.patch.object(monitor, "local_network_counters", return_value=None), \
             mock.patch.object(monitor, "run_ripe_atlas", return_value=atlas) as run_atlas, \
             mock.patch.object(monitor, "output_json", side_effect=capture_json):
            exit_code = monitor.main()

        self.assertEqual(exit_code, 0)
        run_atlas.assert_called_once()
        self.assertIn("ripe_atlas", captured["telemetry"])
        self.assertEqual(captured["telemetry"]["ripe_atlas"]["status"], "ok")

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

    def test_globalping_dns_numeric_zero_is_success_and_answers_are_structured(self):
        data = {
            "status": "finished",
            "probesCount": 1,
            "results": [{
                "probe": {"continent": "Europe", "country": "SK", "city": "Bratislava"},
                "result": {
                    "status": "finished",
                    "statusCode": 0,
                    "statusCodeName": "NOERROR",
                    "timings": {"total": 8.5},
                    "answers": [{"name": "gateway.discord.gg.", "type": "A", "ttl": 60, "value": "162.159.135.232"}],
                },
            }],
        }
        normalized = monitor.normalize_globalping_result("dns", "gateway.discord.gg", data)
        row = normalized["rows"][0]
        self.assertEqual(row["state"], "UP")
        self.assertEqual(row["dns_status"], "NOERROR")
        self.assertEqual(row["answers"], ["gateway.discord.gg. A 162.159.135.232 (TTL 60)"])
        self.assertEqual(normalized["summary"]["up"], 1)

    def test_globalping_dns_noerror_without_answers_is_not_mislabeled_as_transport_failure(self):
        data = {
            "results": [{"probe": {}, "result": {"statusCode": 0, "statusCodeName": "NOERROR", "answers": []}}]
        }
        normalized = monitor.normalize_globalping_result("dns", "example.invalid", data)
        self.assertEqual(normalized["rows"][0]["state"], "WARN")
        self.assertEqual(normalized["rows"][0]["dns_answer_state"], "NODATA")
        self.assertEqual(
            normalized["rows"][0]["detail"],
            "DNS NOERROR/NODATA; resolver responded; 0 record(s), 0 A/AAAA address answer(s)",
        )

    def test_globalping_dns_nxdomain_remains_warning(self):
        data = {
            "results": [{"probe": {}, "result": {"statusCode": 3, "statusCodeName": "NXDOMAIN", "answers": []}}]
        }
        normalized = monitor.normalize_globalping_result("dns", "missing.discord.example", data)
        self.assertEqual(normalized["rows"][0]["state"], "WARN")

    def test_globalping_dns_rrsets_are_order_independent_and_ttl_independent(self):
        addresses = [
            "162.159.135.234", "162.159.130.234",
            "162.159.133.234", "162.159.134.234",
        ]
        def row(city, country, order, ttl):
            return {
                "probe": {"city": city, "country": country},
                "result": {
                    "statusCode": 0,
                    "statusCodeName": "NOERROR",
                    "answers": [
                        {"name": "gateway.discord.gg.", "type": "A", "value": addresses[index], "ttl": ttl}
                        for index in order
                    ],
                },
            }
        data = {
            "status": "finished",
            "results": [
                row("Buffalo", "US", [0, 1, 2, 3], 139),
                row("Los Angeles", "US", [3, 0, 2, 1], 300),
                row("Sydney", "AU", [2, 1, 0, 3], 300),
                row("Lagos", "NG", [0, 3, 2, 1], 175),
                row("Sao Paulo", "BR", [1, 2, 3, 0], 86),
            ],
        }
        normalized = monitor.normalize_globalping_result("dns", "gateway.discord.gg", data)
        self.assertEqual(normalized["summary"]["unique_answer_sets"], 1)
        self.assertTrue(normalized["summary"]["answer_set_consistent"])
        self.assertEqual(normalized["summary"]["resolved"], 5)
        self.assertEqual(normalized["summary"]["warn"], 0)
        expected = sorted(f"gateway.discord.gg. A {ip}" for ip in addresses)
        for dns_row in normalized["rows"]:
            self.assertEqual(dns_row["state"], "UP")
            self.assertEqual(dns_row["answer_set"], expected)
            self.assertEqual(len(dns_row["answers"]), 4)
            self.assertIn("TTL", dns_row["answers"][0])
        self.assertEqual(
            [item["ttl_min"] for item in normalized["rows"]],
            [139, 300, 300, 175, 86],
        )

    def test_globalping_dns_accepts_addresses_reached_through_cname_chain(self):
        data = {"results": [{
            "probe": {},
            "result": {
                "statusCode": 0,
                "statusCodeName": "NOERROR",
                "answers": [
                    {"name": "gateway.discord.gg.", "type": "CNAME", "ttl": 120,
                     "value": "gateway.edge.example.net."},
                    {"name": "gateway.edge.example.net.", "type": "A", "ttl": 90,
                     "value": "162.159.135.234"},
                ],
            },
        }]}
        row = monitor.normalize_globalping_result("dns", "gateway.discord.gg", data)["rows"][0]
        self.assertEqual(row["state"], "UP")
        self.assertEqual(row["address_answers"], ["162.159.135.234"])

    def test_globalping_dns_rejects_unrelated_address_records(self):
        data = {"results": [{
            "probe": {},
            "result": {
                "statusCode": 0,
                "statusCodeName": "NOERROR",
                "answers": [
                    {"name": "unrelated.example.", "type": "A", "ttl": 60,
                     "value": "162.159.135.234"},
                ],
            },
        }]}
        row = monitor.normalize_globalping_result("dns", "gateway.discord.gg", data)["rows"][0]
        self.assertEqual(row["state"], "WARN")
        self.assertEqual(row["address_answer_count"], 0)
        self.assertEqual(row["unrelated_address_answers"], 1)

    def test_globalping_dns_missing_ttl_is_warned(self):
        data = {"results": [{
            "probe": {},
            "result": {
                "statusCode": 0,
                "statusCodeName": "NOERROR",
                "answers": [
                    {"name": "gateway.discord.gg.", "type": "A",
                     "value": "162.159.135.234"},
                ],
            },
        }]}
        row = monitor.normalize_globalping_result("dns", "gateway.discord.gg", data)["rows"][0]
        self.assertEqual(row["state"], "WARN")
        self.assertEqual(row["missing_ttls"], 1)

    def test_globalping_dns_invalid_a_record_is_ignored_and_warned(self):
        data = {"results": [{
            "probe": {},
            "result": {
                "statusCode": 0,
                "statusCodeName": "NOERROR",
                "answers": [
                    {"name": "gateway.discord.gg.", "type": "A", "ttl": 60, "value": "not-an-ip"},
                ],
            },
        }]}
        row = monitor.normalize_globalping_result("dns", "gateway.discord.gg", data)["rows"][0]
        self.assertEqual(row["state"], "WARN")
        self.assertEqual(row["answer_count"], 0)
        self.assertEqual(row["invalid_records"], 1)

    def test_globalping_dns_ttl_conflict_is_reported(self):
        data = {"results": [{
            "probe": {},
            "result": {
                "statusCode": 0,
                "statusCodeName": "NOERROR",
                "answers": [
                    {"name": "gateway.discord.gg.", "type": "A", "ttl": 60, "value": "162.159.135.234"},
                    {"name": "gateway.discord.gg.", "type": "A", "ttl": 90, "value": "162.159.130.234"},
                ],
            },
        }]}
        row = monitor.normalize_globalping_result("dns", "gateway.discord.gg", data)["rows"][0]
        self.assertEqual(row["state"], "WARN")
        self.assertEqual(row["ttl_conflicts"], 1)
        self.assertIn("inconsistent TTLs", row["detail"])

    def test_globalping_dns_servfail_is_down(self):
        data = {"results": [{
            "probe": {},
            "result": {"statusCode": 2, "statusCodeName": "SERVFAIL", "answers": []},
        }]}
        row = monitor.normalize_globalping_result("dns", "gateway.discord.gg", data)["rows"][0]
        self.assertEqual(row["state"], "DOWN")

    def test_globalping_dns_result_count_is_bounded(self):
        data = {"results": [
            {"probe": {}, "result": {"statusCode": 0, "answers": [
                {"name": "gateway.discord.gg.", "type": "A", "ttl": 60, "value": "162.159.135.234"}
            ]}}
            for _ in range(monitor.MAX_GLOBALPING_RESULT_ROWS + 1)
        ]}
        normalized = monitor.normalize_globalping_result("dns", "gateway.discord.gg", data)
        self.assertEqual(len(normalized["rows"]), monitor.MAX_GLOBALPING_RESULT_ROWS)
        self.assertTrue(normalized["results_truncated"])

    def test_numeric_rejects_non_finite_negative_and_boolean_values(self):
        for value in (float("nan"), float("inf"), float("-inf"), -1, True, False):
            self.assertIsNone(monitor.numeric(value))
        self.assertEqual(monitor.numeric(0), 0.0)

    def test_clip_removes_terminal_and_bidi_control_injection(self):
        value = "clean\x1b[31mRED\x1b[0m\x1b]0;title\x07\u202ehidden"
        output = monitor.clip(value, 200)
        self.assertEqual(output, "cleanREDhidden")
        self.assertNotIn("\x1b", output)
        self.assertNotIn("\u202e", output)

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


class SecureTransportTests(unittest.TestCase):
    def test_safe_urlopen_rejects_http_and_unallowlisted_hosts(self):
        from backends.http_transport import safe_urlopen
        context = ssl.create_default_context()
        with mock.patch("backends.http_transport.urllib.request.build_opener") as build:
            with self.assertRaisesRegex(ValueError, "Only absolute HTTPS"):
                safe_urlopen("http://api.globalping.io/v1/limits", timeout=1, context=context,
                             allowed_hosts={"api.globalping.io"})
            with self.assertRaisesRegex(ValueError, "not in the allowed host list"):
                safe_urlopen("https://evil.example/", timeout=1, context=context,
                             allowed_hosts={"api.globalping.io"})
            build.assert_not_called()

    def test_redirect_handler_blocks_cross_origin_and_http_redirects(self):
        from backends.http_transport import SameOriginHTTPSRedirectHandler
        handler = SameOriginHTTPSRedirectHandler(("api.globalping.io", 443))
        request = monitor.urllib.request.Request(
            "https://api.globalping.io/v1/limits",
            headers={"Authorization": "Bearer do-not-leak"},
        )
        with self.assertRaises(urllib.error.HTTPError):
            handler.redirect_request(
                request, io.BytesIO(), 302, "Found", {}, "https://evil.example/collect"
            )
        with self.assertRaises(urllib.error.HTTPError):
            handler.redirect_request(
                request, io.BytesIO(), 302, "Found", {}, "http://api.globalping.io/v1/limits"
            )

    def test_redirect_handler_allows_same_origin_https_get(self):
        from backends.http_transport import SameOriginHTTPSRedirectHandler
        handler = SameOriginHTTPSRedirectHandler(("api.globalping.io", 443))
        request = monitor.urllib.request.Request("https://api.globalping.io/v1/limits")
        redirected = handler.redirect_request(
            request, io.BytesIO(), 302, "Found", {}, "https://api.globalping.io/v1/status"
        )
        self.assertEqual(redirected.full_url, "https://api.globalping.io/v1/status")
        self.assertEqual(redirected.get_method(), "GET")

    def test_globalping_url_and_path_are_allowlisted_before_network(self):
        monitor.initialize_tls_context()
        with mock.patch.object(monitor, "safe_urlopen") as opener:
            with self.assertRaisesRegex(monitor.MonitorError, "official api.globalping.io HTTPS origin"):
                monitor.globalping_request("GET", "https://evil.example/v1/limits", 2)
            with self.assertRaisesRegex(monitor.MonitorError, "allowed DGSM endpoint"):
                monitor.globalping_request("GET", "https://api.globalping.io/v1/measurements/../../secrets", 2)
            opener.assert_not_called()


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

    def test_zero_http_port_is_rejected_before_network_request(self):
        with mock.patch.object(monitor, "globalping_request") as request:
            with self.assertRaisesRegex(monitor.MonitorError, "port must be between 1 and 65535"):
                monitor.create_globalping_measurement("http", "https://discord.com:0/api", 4, 5, None)
        request.assert_not_called()

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
        with mock.patch.object(monitor, "safe_urlopen", side_effect=error):
            with self.assertRaises(monitor.MonitorError) as caught:
                monitor.globalping_request(
                    "POST", "https://api.globalping.io/v1/measurements", 5,
                    payload={"type": "http", "target": "https://discord.com"},
                )
        self.assertIn("Parameter validation failed", str(caught.exception))
        self.assertIn("target", str(caught.exception))
        self.assertIn("hostname or IP address", str(caught.exception))



class StateFileSecurityTests(unittest.TestCase):
    def test_atomic_state_write_is_private_and_leaves_no_fixed_temp_file(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            directory = Path(temp_dir) / "dgsm"
            monitor.ensure_private_directory(directory)
            path = directory / "state.json"
            monitor.save_json_atomic(path, {"ready": True})
            self.assertEqual(monitor.load_json_file(path), {"ready": True})
            self.assertEqual(list(directory.glob("*.tmp")), [])
            if __import__("os").name != "nt":
                self.assertEqual(path.stat().st_mode & 0o777, 0o600)
                self.assertEqual(directory.stat().st_mode & 0o777, 0o700)

    def test_state_file_symlink_is_not_followed(self):
        if not hasattr(Path, "symlink_to"):
            self.skipTest("symlinks are unavailable")
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            secret = root / "outside.txt"
            secret.write_text("unchanged", encoding="utf-8")
            link = root / "state.json"
            try:
                link.symlink_to(secret)
            except (OSError, NotImplementedError):
                self.skipTest("symlink creation is not permitted")
            with self.assertRaises(OSError):
                monitor._open_state_file(link, "r")
            self.assertEqual(secret.read_text(encoding="utf-8"), "unchanged")


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
        with mock.patch("backends.ripe_atlas.safe_urlopen", return_value=response) as urlopen:
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
    def test_builder_reuses_installed_python_315_without_win_get(self):
        script = Path(__file__).with_name("build_windows.ps1").read_text(encoding="utf-8")
        self.assertIn("function Invoke-CapturedProcess", script)
        self.assertIn("function Ensure-PythonBuildRuntime", script)
        self.assertIn("function Get-PythonBuildRuntime", script)
        self.assertIn("function Test-PythonBuildPath", script)
        self.assertIn("'3.15', '3.14', '3.13', '3.12', '3.11'", script)
        self.assertIn("(3, 11) <= sys.version_info[:2] < (3, 16)", script)
        self.assertIn('Programs\\Python\\Python$compact', script)
        self.assertIn("python-manager-26.4.msix", script)
        self.assertIn("002CFE4F4B63E322E9C8170CEA4D7D97E3E2D18246887325F969BB75F482F4DC", script)
        self.assertIn("function Install-Cpython313Standalone", script)
        self.assertIn("Python Software Foundation", script)
        self.assertIn("python-3.13.17-amd64.exe", script)
        self.assertIn("5DA012101BA2E17113A9A1CD901E9CE1441086F2B7ECB5DE06EC94163899A190", script)
        self.assertNotIn("WinGet is unavailable", script)
        self.assertNotIn("python-3.12.10-amd64.exe", script)
        self.assertNotIn("function Invoke-WingetInstall", script)
        self.assertIn("requirements-build.txt", script)
        self.assertIn("'-r', $BuildRequirements", script)
        requirements = Path(__file__).with_name("requirements-build.txt").read_text(encoding="utf-8")
        self.assertIn("pyinstaller>=6.21", requirements)
        self.assertIn("cffi>=2.1.1", requirements)

    def test_builder_rejects_end_of_life_python_310_for_native_builds(self):
        script = Path(__file__).with_name("build_windows.ps1").read_text(encoding="utf-8")
        self.assertIn("sys.version_info[:2] < (3, 16)", script)
        self.assertIn("[int]$pyVersionParts[1] -lt 11", script)
        self.assertNotIn("'3.10'", script)

    def test_windows_docs_have_valid_log_paths_and_current_fallback(self):
        docs = Path(__file__).with_name("BUILD_TROUBLESHOOTING.txt").read_text(encoding="utf-8")
        self.assertIn(".build\\windows\\logs\\build-*.log", docs)
        self.assertNotIn("logsbuild-", docs)
        self.assertIn("CPython 3.13.17", docs)

    def test_existing_python315_user_install_directory_is_discovered(self):
        script = Path(__file__).with_name("build_windows.ps1").read_text(encoding="utf-8")
        self.assertIn('Join-Path $env:LOCALAPPDATA "Programs\\Python\\Python$compact"', script)
        self.assertIn("$compact = $tag.Replace('.', '')", script)
        self.assertIn("$expectedMinor = ($pyVersion.Split('.')[0..1] -join '.')", script)
        self.assertIn("$venvCheckCode", script)

    def test_legacy_py_is_never_used_for_manager_install_or_list_subcommands(self):
        script = Path(__file__).with_name("build_windows.ps1").read_text(encoding="utf-8")
        self.assertIn("Get-Command 'py.exe'", script)
        self.assertIn("Get-Command 'pymanager.exe'", script)
        self.assertIn("@('list', '-1', '--format=exe', $tag)", script)
        self.assertIn("-FilePath $manager -Arguments @('install', $tag)", script)
        self.assertNotIn("-FilePath $legacy.Source -Arguments @('install'", script)
        self.assertNotIn("-FilePath $legacy.Source -Arguments @('list'", script)
        self.assertNotIn("Ensure-Python312", script)

    def test_launcher_has_separate_success_and_log_lines(self):
        launcher = Path(__file__).with_name("BUILD_AND_SIGN_WINDOWS.bat").read_text(encoding="ascii")
        self.assertIn("echo EXE, DLL, Authenticode signatures and package are under dist\\windows\\\n", launcher)
        self.assertIn("echo Build transcript: .build\\windows\\logs\\\n", launcher)
        self.assertNotIn("windowsecho", launcher)

    def test_toolchain_installs_without_winget_and_elevates_only_installers(self):
        script = Path(__file__).with_name("build_windows.ps1").read_text(encoding="utf-8")
        self.assertIn("https://aka.ms/vs/17/release/vs_BuildTools.exe", script)
        self.assertIn("ExpectedPublisher 'Microsoft Corporation'", script)
        self.assertIn("function Invoke-ElevatedInstaller", script)
        self.assertIn("-Verb RunAs", script)
        self.assertNotIn("function Restart-Elevated", script)
        self.assertNotIn("$ElevatedRetry", script)
        self.assertIn("Build Tools directly (WinGet is not required)", script)
        self.assertIn("Microsoft.Windows.SDK.BuildTools", Path(__file__).with_name("sign_windows.ps1").read_text(encoding="utf-8"))

    def test_builder_creates_a_persistent_diagnostic_transcript(self):
        script = Path(__file__).with_name("build_windows.ps1").read_text(encoding="utf-8")
        self.assertIn("Start-Transcript -Path $BuildLog", script)
        self.assertIn("Full build log: $BuildLog", script)
        self.assertIn(".build\\windows\\logs", script)

    def test_builder_stages_additional_backend_modules(self):
        script = Path(__file__).with_name("build_windows.ps1").read_text(encoding="utf-8")
        self.assertIn("Get-ChildItem -LiteralPath $BackendsDir -Filter '*.py' -File | Copy-Item -Destination $DllBackends -Force", script)
        self.assertIn("Get-ChildItem -LiteralPath $BackendsDir -Filter '*.py' -File | Copy-Item -Destination $PackageBackends -Force", script)


    def test_signing_uses_process_capture_not_native_call_operator(self):
        signer = Path(__file__).with_name("sign_windows.ps1").read_text(encoding="utf-8")
        self.assertIn("function Invoke-CapturedProcess", signer)
        self.assertIn("function Invoke-SignTool", signer)
        self.assertNotIn("& $signTool.FullName sign", signer)

    def test_signing_matches_damage_tracker_certificate_and_sign_tool_flow(self):
        signer = Path(__file__).with_name("sign_windows.ps1").read_text(encoding="utf-8")
        for required in (
            "Microsoft.Windows.SDK.BuildTools",
            "Get-AuthenticodeSignature",
            "CurrentUser\\My",
            "Cert:\\CurrentUser\\Root",
            "Cert:\\CurrentUser\\TrustedPublisher",
            "New-SelfSignedCertificate",
            "-Type CodeSigningCert",
            "-KeyAlgorithm RSA",
            "-KeyLength 3072",
            "-HashAlgorithm SHA256",
            "timestamp.digicert.com",
            "timestamp.sectigo.com",
            "'verify', '/pa', '/all', '/v'",
            "finally",
            "ZeroFreeBSTR",
        ):
            self.assertIn(required, signer)

    def test_exe_and_dll_are_smoke_tested_then_signed(self):
        script = Path(__file__).with_name("build_windows.ps1").read_text(encoding="utf-8")
        self.assertIn("--version-file", script)
        self.assertIn("--resource-file", script)
        self.assertIn("embed_windows_resources.py", script)
        self.assertIn("dll_smoke_test.py", script)
        self.assertIn("$Artifacts = @($ExePath, $DllPath)", script)
        self.assertIn("Discord Global Status Monitor.lnk", script)
        self.assertIn("-KeepBuildCache", script)

    def test_native_dll_builder_accepts_and_links_resource_file(self):
        builder = Path(__file__).with_name("discord_monitor_dll_build.py").read_text(encoding="utf-8")
        self.assertIn('"--resource-file"', builder)
        self.assertIn("extra_objects=extra_objects", builder)
        self.assertIn("from dgsm_version import __version__", builder)

    def test_dll_rc_template_uses_self_contained_numeric_windows_constants(self):
        rc = (Path(__file__).parent / "assets" / "DGSM.rc").read_text(encoding="utf-8")
        self.assertRegex(rc, r"(?m)^\s*1\s+VERSIONINFO\b")
        self.assertNotRegex(rc, r"(?m)^\s*VS_VERSION_INFO\s+VERSIONINFO\b")
        self.assertRegex(rc, r"(?m)^\s*FILEOS\s+0x00040004L\s*$")
        self.assertRegex(rc, r"(?m)^\s*FILETYPE\s+0x00000002L\s*$")
        self.assertNotRegex(rc, r"\bVOS_NT_WINDOWS32\b|\bVFT_DLL\b")
        builder = (Path(__file__).parent / "build_windows.ps1").read_text(encoding="utf-8")
        self.assertIn("DLL RC template must use FILEOS 0x00040004L", builder)
        self.assertIn("DLL RC template must use FILETYPE 0x00000002L", builder)

    def test_windows_pyinstaller_only_collects_windows_plyer_backend(self):
        script = (Path(__file__).parent / "build_windows.ps1").read_text(encoding="utf-8")
        self.assertIn("plyer.platforms.win.notification", script)
        self.assertNotIn("'--collect-all', 'plyer'", script)

    def test_build_metadata_templates_use_single_canonical_version(self):
        root = Path(__file__).parent
        version = (root / "dgsm_version.py").read_text(encoding="utf-8")
        project = (root / "pyproject.toml").read_text(encoding="utf-8")
        self.assertIn('__version__ = "3.4.5"', version)
        self.assertIn('__version_info__ = (3, 4, 4)', version)
        self.assertIn('version = "3.4.5"', project)
        self.assertIn("@DGSM_VERSION_TUPLE@", (root / "assets" / "version_info.txt").read_text(encoding="utf-8"))
        self.assertIn("@DGSM_VERSION_COMMA4@", (root / "assets" / "DGSM.rc").read_text(encoding="utf-8"))

    def test_linux_build_includes_canonical_version_and_abi_manifest(self):
        script = Path(__file__).with_name("build_linux.sh").read_text(encoding="utf-8")
        self.assertIn('cp "$VERSION_MODULE" "$STAGE/"', script)
        self.assertIn('cp "$VERSION_MODULE" "$DIST/"', script)
        self.assertIn('"$DLL_SMOKE_TEST"', script)
        self.assertIn('BUILD_ABI.txt', script)

    def test_signing_reports_timestamp_failures_and_uses_current_user_scope(self):
        signer = Path(__file__).with_name("sign_windows.ps1").read_text(encoding="utf-8")
        self.assertIn("Timestamp attempt failed:", signer)
        self.assertIn("Cert:\\CurrentUser\\Root", signer)
        self.assertIn("Cert:\\CurrentUser\\TrustedPublisher", signer)
        self.assertIn("Temporary Microsoft SDK BuildTools NuGet package", signer)
        self.assertIn("'verify', '/pa', '/all', '/v'", signer)

    def test_windows_build_elevates_only_microsoft_installers(self):
        script = Path(__file__).with_name("build_windows.ps1").read_text(encoding="utf-8")
        self.assertIn("function Invoke-ElevatedInstaller", script)
        self.assertIn("Get-AuthenticodeSignature -FilePath $FilePath", script)
        self.assertIn("-Verb RunAs", script)
        self.assertNotIn("Start-Process -FilePath 'powershell.exe' -Verb RunAs", script)
        self.assertIn("the invoking user's profile", script)

    def test_dll_version_and_exe_version_derive_from_canonical_module(self):
        script = Path(__file__).with_name("build_windows.ps1").read_text(encoding="utf-8")
        self.assertIn("$versionMatch = [regex]::Match($versionSourceText", script)
        self.assertIn("$AppVersion = $versionMatch.Groups['version'].Value", script)
        self.assertIn("--expected-version', $AppVersion", script)
        self.assertIn('$VersionDotted4 = "$AppVersion.0"', script)


    def test_run_launchers_prefer_compiled_artifacts_and_fallback_to_source(self):
        root = Path(__file__).parent
        windows = (root / "RUN_DISCORD_STATUS.bat").read_text(encoding="ascii")
        linux = (root / "RUN_DISCORD_STATUS.sh").read_text(encoding="utf-8")
        self.assertIn("DiscordGlobalStatusMonitor.exe", windows)
        self.assertIn("dist\\windows\\exe", windows)
        self.assertIn("discord_server_global_status.py", windows)
        self.assertIn("dist/linux/DiscordGlobalStatusMonitor.pyz", linux)
        self.assertIn("discord_server_global_status.py", linux)

    def test_dll_smoke_test_checks_all_exports_and_abi_version(self):
        smoke = (Path(__file__).parent / "dll_smoke_test.py").read_text(encoding="utf-8")
        self.assertIn("dgsm_get_version", smoke)
        self.assertIn("dgsm_get_last_error", smoke)
        self.assertIn("dgsm_run_once", smoke)
        self.assertIn("dgsm_run_monitor", smoke)
        self.assertIn("export.restype = ctypes.c_int", smoke)
        self.assertIn("version length mismatch", smoke)

    def test_windows_builder_checks_embedded_resource_versions_and_zip_contents(self):
        script = (Path(__file__).parent / "build_windows.ps1").read_text(encoding="utf-8")
        self.assertIn("$exeVersionResource = (Get-Item -LiteralPath $ExePath).VersionInfo", script)
        self.assertIn("$dllVersionResource = (Get-Item -LiteralPath $DllPath).VersionInfo", script)
        self.assertIn("[System.IO.Compression.ZipFile]::OpenRead($PackagePath)", script)
        self.assertIn("Build dependency versions:", script)
        self.assertIn("requirements-build.txt", script)

    def test_windows_resource_script_uses_rc_safe_absolute_icon_path(self):
        script = (Path(__file__).parent / "build_windows.ps1").read_text(encoding="utf-8")
        self.assertIn("$absoluteIconPath = $IconPath.Replace([char]92, [char]47)", script)
        self.assertIn("$absoluteIconResource = '1 ICON \"' + $absoluteIconPath + '\"'", script)

    def test_linux_build_checks_matching_python_headers_and_supports_root(self):
        script = (Path(__file__).parent / "build_linux.sh").read_text(encoding="utf-8")
        self.assertIn("PY_MINOR=", script)
        self.assertIn("apt_run()", script)
        self.assertIn('[[ -f "$PY_INCLUDE/Python.h" ]]', script)
        self.assertIn("Python 3.10 or newer", script)

    def test_build_uses_utf8_without_bom_for_generated_windows_resources(self):
        script = (Path(__file__).parent / "build_windows.ps1").read_text(encoding="utf-8")
        self.assertIn("[System.Text.UTF8Encoding]::new($false)", script)
        self.assertIn("$absoluteIconPath = $IconPath.Replace([char]92, [char]47)", script)

    def test_windows_ci_parses_powershell_scripts(self):
        workflow = (Path(__file__).parent / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
        self.assertIn("PowerShell build and signing scripts", workflow)
        self.assertIn("System.Management.Automation.Language.Parser]::ParseFile", workflow)
        self.assertIn("'build_windows.ps1', 'sign_windows.ps1'", workflow)

    def test_linux_build_removes_cffi_intermediate_outputs(self):
        script = (Path(__file__).parent / "build_linux.sh").read_text(encoding="utf-8")
        self.assertIn('rm -f "$DIST/_dgsm_embed.c" "$DIST/_dgsm_embed.o"', script)
        self.assertIn('find "$DIST" -type d -name __pycache__', script)

    def test_github_actions_ci_workflow_exists(self):
        workflow = Path(__file__).parent / ".github" / "workflows" / "ci.yml"
        self.assertTrue(workflow.is_file())
        text = workflow.read_text(encoding="utf-8")
        self.assertIn("windows-latest", text)
        self.assertIn("3.15", text)
        self.assertIn("ubuntu-latest", text)
        self.assertIn("test_discord_server_global_status.py", text)


if __name__ == "__main__":
    unittest.main(verbosity=2)
