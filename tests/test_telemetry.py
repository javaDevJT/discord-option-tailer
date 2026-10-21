"""Controlled fixtures only; never contact Discord, Robinhood, Codex or OpenWoof."""
import asyncio
from contextlib import redirect_stderr
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from relay import telemetry
from relay.telemetry_logs import Framer, RingWriter, record, redact


class OriginalOutputTests(unittest.TestCase):
    def test_json_utf8_and_multiline_are_preserved_across_every_byte_boundary(self):
        body = json.dumps({"state": "error", "at": "2026-10-21T18:00:00+00:00",
            "reason": "quote ‘retained’ and newline\nnext", "code": "schema_incompatible"}, indent=2, ensure_ascii=False)
        values = []
        framer = Framer(values.append)
        for byte in (body + "\n").encode():
            framer.feed("service", "42", "stdout", bytes([byte]))
        self.assertEqual(len(values), 1)
        value = values[0]
        self.assertEqual(value["body"], body)
        self.assertEqual(value["severity"], "ERROR")
        self.assertEqual(value["timestamp"], "2026-10-21T18:00:00+00:00")
        self.assertEqual(value["attributes"]["app.code"], "schema_incompatible")
        self.assertEqual(value["attributes"]["log.framing"], "json_document")

    def test_untraced_lines_and_exception_details_survive_in_source_order(self):
        values = []; framer = Framer(values.append)
        text = "WARNING:relay.account:account refresh failed\nTraceback (most recent call last):\n  File \"account.py\", line 10\nRuntimeError: retained detail\n"
        framer.feed("service", "9", "stdout", text.encode()); framer.flush()
        self.assertEqual([v["body"] for v in values], text.splitlines())
        self.assertEqual([v["attributes"]["log.sequence"] for v in values], [1, 2, 3, 4])
        self.assertEqual(values[0]["severity"], "WARNING")
        self.assertEqual(values[0]["attributes"]["log.logger.name"], "relay.account")
        self.assertNotIn("trace_id", values[0])

    def test_long_json_redacts_before_output_segmentation(self):
        values = []; framer = Framer(values.append)
        document = json.dumps({"reason": "meaningful " * 10000,
            "password": "fixture-private-value" * 100, "symbol": "SPY"})
        for start in range(0, len(document), 5000):
            framer.feed("service", "42", "stdout", document[start:start + 5000].encode())
        joined = "".join(value["body"] for value in values)
        self.assertEqual(joined, redact(document))
        self.assertNotIn("fixture-private-value", joined)
        self.assertGreater(len(values), 1)

    def test_only_private_fields_are_redacted_useful_context_and_benign_query_remain(self):
        body = '{"state":"held","symbol":"SPY","reason":"ask exceeded chase","password":"fixture-private","account_number":"123456789"} https://api.example/options?limit=100&access_token=fixture-token'
        result = redact(body)
        self.assertNotIn("fixture-private", result); self.assertNotIn("fixture-token", result)
        self.assertNotIn("123456789", result)
        for retained in ["SPY", "ask exceeded chase", '"state":"held"', "limit=100"]:
            self.assertIn(retained, result)
        self.assertEqual(redact("Bearer fixture-value Basic fixture-basic"), "Bearer [REDACTED] Basic [REDACTED]")
        self.assertNotIn("private-webhook", redact("https://discord.com/api/webhooks/123/private-webhook"))

    def test_health_noise_is_scoped_and_health_errors_survive(self):
        def line(status): return f'127.0.0.1 - - [21/Oct/2026:18:00:00 +0000] "GET /healthz HTTP/1.1" {status} 10 "-" "curl"'
        self.assertIsNone(record("nginx", "1", "stdout", line(200), 1, "newline"))
        bad = record("nginx", "1", "stdout", line(503), 2, "newline")
        self.assertEqual(bad["severity"], "ERROR"); self.assertIn("503", bad["body"])

    def test_telemetry_diagnostics_cannot_reenter_the_log_pipeline(self):
        for program in ["telemetry-collector", "telemetry-output"]:
            self.assertIsNone(record(program, "1", "stderr", "export failed", 1, "newline"))

    def test_disk_failure_is_isolated_and_never_erases_original_streams(self):
        with tempfile.TemporaryDirectory() as directory:
            writer = RingWriter(directory)
            error = io.StringIO()
            with patch("relay.telemetry_logs.os.open", side_effect=OSError("fixture full disk")), redirect_stderr(error):
                writer({"body": "retained"})
            self.assertIn("original console output is unaffected", error.getvalue())
            self.assertFalse((Path(directory) / "process-output.jsonl").exists())


class InertInstrumentationTests(unittest.IsolatedAsyncioTestCase):
    async def test_disabled_instrumentation_preserves_async_results_and_cancellation(self):
        sentinel = object()
        @telemetry.traced("relay.message")
        async def success(): return sentinel
        @telemetry.traced("relay.message")
        async def cancelled(): raise asyncio.CancelledError
        self.assertIs(await success(), sentinel)
        with self.assertRaises(asyncio.CancelledError): await cancelled()

    async def test_disabled_instrumentation_preserves_exact_exception_object(self):
        expected = RuntimeError("original application detail")
        @telemetry.traced("relay.evaluate")
        async def failing(): raise expected
        with self.assertRaises(RuntimeError) as caught: await failing()
        self.assertIs(caught.exception, expected)


class DeploymentWiringTests(unittest.TestCase):
    def test_health_reads_suppress_dependency_operations_and_restore_context(self):
        try:
            from opentelemetry.instrumentation.utils import is_instrumentation_enabled
        except ImportError:
            self.skipTest("optional telemetry dependencies are unavailable")
        from types import SimpleNamespace
        for path in ("/healthz", "/api/status", "/api/telemetry"):
            with self.subTest(path=path), patch.object(telemetry, "_runtime", {"closed": False}):
                with telemetry.http_request(SimpleNamespace(path=path)):
                    self.assertFalse(is_instrumentation_enabled())
                    with telemetry.operation("sqlite.SELECT") as span:
                        self.assertIsInstance(span, telemetry.NullSpan)
                self.assertTrue(is_instrumentation_enabled())

    def test_collector_diagnostics_suppress_http_spans_and_restore_context(self):
        try:
            from opentelemetry.instrumentation.utils import is_http_instrumentation_enabled
        except ImportError:
            self.skipTest("optional telemetry dependencies are unavailable")
        observed = []

        class Response(io.BytesIO):
            status = 200

        def request(url, **_kwargs):
            observed.append(is_http_instrumentation_enabled())
            return Response(b"otelcol_exporter_sent_spans_total 7\n")

        self.assertTrue(is_http_instrumentation_enabled())
        with patch.dict("os.environ", {"RELAY_OTEL_ENABLED": "true"}), patch("urllib.request.urlopen", side_effect=request):
            result = telemetry.status()
        self.assertEqual(observed, [False, False])
        self.assertTrue(is_http_instrumentation_enabled())
        self.assertTrue(result["collector_healthy"])
        self.assertEqual(result["collector_metrics"]["otelcol_exporter_sent_spans_total"], 7)

    def test_all_original_program_console_destinations_are_retained(self):
        import configparser
        p = configparser.RawConfigParser(); p.read(Path(__file__).parents[1] / "docker/supervisord.conf")
        for name in ["xvfb", "openbox", "x11vnc", "websockify", "dashboard", "service", "notifications", "nginx"]:
            self.assertEqual(p[f"program:{name}"]["stdout_logfile"], "/dev/stdout")
            self.assertEqual(p[f"program:{name}"]["redirect_stderr"], "true")
            self.assertEqual(p[f"program:{name}"]["stdout_events_enabled"], "true")
        for name in ["dashboard", "service", "notifications"]:
            self.assertIn("relay.telemetry_exec " + name, p[f"program:{name}"]["command"])
        self.assertNotIn("stdout_events_enabled", p["program:telemetry-collector"])


class SQLiteShortcutTests(unittest.TestCase):
    def test_row_factory_commit_rollback_and_parameter_iterator_are_unchanged(self):
        import sqlite3
        from relay.telemetry_sqlite import Connection
        db = Connection(sqlite3.connect(":memory:"))
        db.row_factory = sqlite3.Row
        db.execute("CREATE TABLE values_seen(value INTEGER UNIQUE)")
        seen = []
        def rows():
            for value in [1, 2]:
                seen.append(value)
                yield (value,)
        with db:
            db.executemany("INSERT INTO values_seen VALUES (?)", rows())
        self.assertEqual(seen, [1, 2])
        with self.assertRaises(sqlite3.IntegrityError):
            with db:
                db.execute("INSERT INTO values_seen VALUES (?)", (3,))
                db.execute("INSERT INTO values_seen VALUES (?)", (1,))
        self.assertEqual([r["value"] for r in db.execute("SELECT value FROM values_seen ORDER BY value")], [1, 2])
        db.close()


if __name__ == "__main__": unittest.main()
