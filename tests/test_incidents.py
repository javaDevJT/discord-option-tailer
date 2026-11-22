import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from subprocess import CompletedProcess
from unittest.mock import Mock, patch

from relay.incidents import IncidentReporter, summarize
from relay.incidents_auth import codex_auth_attention, t3_auth_attention
from relay.interpreter import CodexInterpreter, InterpretationError
from relay.notifications import _runtime_issues
from relay.t3_webhook import DeliveryError


def timestamp(seconds):
    return datetime.fromtimestamp(seconds, timezone.utc).isoformat()


class IncidentReportingTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        self.config = self.root / "config.json"
        self.config.write_text(json.dumps({"runtime_status_file": "state/runtime.json"}))
        self.clock = 1_800_000_000
        self.summary = Mock(return_value={"summary": "The broker response schema changed.",
                                          "next_check": "Compare the reported tool response with the expected schema."})
        self.send = Mock()
        self.reporter = self.open_reporter()
        self.status_path = self.root / "state/runtime.json"

    def open_reporter(self):
        reporter = IncidentReporter(self.config, "test endpoint", clock=lambda: self.clock,
                                    summarize_report=self.summary, send=self.send)
        self.addCleanup(reporter.journal.db.close)
        return reporter

    def status(self, **overrides):
        value = {"state": "running", "heartbeat_at": timestamp(self.clock),
                 "discord": {"state": "connected"}, "codex": {"state": "ready"},
                 "broker": {"state": "connected"}, "jev": {"state": "not_configured"}} | overrides
        self.status_path.write_text(json.dumps(value))

    def record(self, *, at=None, **extra):
        observed = timestamp(self.clock if at is None else at)
        diagnostic = {
            "id": "a" * 32, "stage": "snapshot", "exception": "BrokerError", "code": "schema_incompatible",
            "tool": "get_option_orders", "frames": ["relay/broker.py:238:_qualified"],
            "build_revision": "b" * 40, "context": {"symbol": "TSLA", "equity": 123456.78},
        } | extra
        record = {"event": "failure", "at": observed, "diagnostic": diagnostic,
                  "context": {"raw": "PRIVATE DISCORD MESSAGE"}}
        with self.reporter.log.open("ab") as stream:
            stream.write(b"timestamp ERROR Execution diagnostic " + json.dumps(record).encode() + b"\n")

    async def test_report_uses_projected_facts_and_dedupes_across_restart(self):
        self.status()
        self.record(detail="PRIVATE KEY FROM PROVIDER", code="schema_incompatible")
        self.clock += 8
        await self.reporter.poll()
        self.send.assert_called_once()
        endpoint, payload = self.send.call_args.args
        self.assertEqual(endpoint, "test endpoint")
        self.assertEqual(payload["event"], "relay.incident")
        self.assertEqual(payload["summary_source"], "codex")
        facts = self.summary.call_args.args[1]
        self.assertEqual(facts, payload["diagnostic"])
        self.assertEqual(facts["source_location"], "relay/broker.py:238:_qualified")
        self.assertEqual(facts["tool"], "get_option_orders")
        rendered = json.dumps(payload)
        for private in ("TSLA", "123456", "PRIVATE", "context"):
            self.assertNotIn(private, rendered)
        self.clock += 301
        self.status()
        self.record(id="c" * 32)
        self.clock += 8
        await self.open_reporter().poll()
        self.send.assert_called_once()
        self.summary.assert_called_once()

    async def test_credential_states_and_typed_auth_failures_stay_off_t3(self):
        self.status(broker={"state": "auth_required"})
        self.record()
        self.clock += 8
        await self.reporter.poll()
        self.send.assert_not_called()
        self.summary.assert_not_called()
        self.reporter.log.unlink()
        self.status()
        self.record(exception="InterpretationError", code="auth_required")
        self.record(causes=[{"exception": "HTTPError", "code": "http_401"}])
        self.clock += 8
        await self.reporter.poll()
        self.send.assert_not_called()
        self.summary.assert_not_called()

    async def test_codex_discovers_auth_required_and_does_not_use_fallback(self):
        self.status()
        self.record()
        self.clock += 8
        self.summary.side_effect = InterpretationError("expired", code="auth_required")
        with patch("relay.incidents.mark_codex_auth_attention") as marker:
            await self.reporter.poll()
        marker.assert_called_once_with(self.status_path)
        self.assertEqual(self.reporter.journal.value("last_auth_good"), 0)
        self.send.assert_not_called()

    async def test_auth_change_during_summary_prevents_delivery(self):
        self.status()
        self.record()
        self.clock += 8
        def summarize(_, facts):
            self.status(codex={"state": "auth_required"})
            return {"summary": "The broker returned a schema error.", "next_check": "Check the schema."}
        self.reporter.summarize = summarize
        await self.reporter.poll()
        self.send.assert_not_called()

    async def test_fallback_is_delivered_even_if_codex_is_busy(self):
        self.status()
        self.record()
        self.clock += 8
        self.summary.side_effect = TimeoutError
        await self.reporter.poll()
        payload = self.send.call_args.args[1]
        self.assertEqual(payload["summary_source"], "diagnostic_fallback")
        self.assertIn("get_option_orders", payload["summary"])

    async def test_ambiguous_delivery_is_not_retried(self):
        self.status()
        self.record()
        self.clock += 8
        self.send.side_effect = DeliveryError("timeout")
        with self.assertLogs("relay.incidents", level="WARNING") as logs:
            await self.reporter.poll()
        self.assertIn("code=timeout", logs.output[0])
        self.clock += 301
        self.status()
        await self.open_reporter().poll()
        self.send.assert_called_once()

    async def test_rejected_t3_authorization_uses_discord_notification_status(self):
        self.status()
        self.record()
        self.clock += 8
        self.send.side_effect = DeliveryError("http_error", 403)
        with self.assertLogs("relay.incidents", level="WARNING") as logs:
            await self.reporter.poll()
        self.assertIn("http_status=403", logs.output[0])
        self.assertTrue(t3_auth_attention(self.status_path))
        current = datetime.now(timezone.utc)
        self.status_path.write_text(json.dumps({"state": "running", "heartbeat_at": current.isoformat()}))
        self.assertEqual(_runtime_issues(self.status_path, now=current), {"t3": "reauth"})

    async def test_stale_worker_outage_requires_recent_healthy_credentials(self):
        # First check after installation cannot infer healthy credentials.
        self.status(heartbeat_at=timestamp(self.clock - 140))
        await self.reporter.poll()
        self.clock += 31
        await self.reporter.poll()
        self.send.assert_not_called()
        # Once authenticated, a persistently stale worker is an incident.
        self.status()
        await self.reporter.poll()
        self.clock += 121
        await self.reporter.poll()
        self.send.assert_not_called()
        self.clock += 30
        await self.reporter.poll()
        self.assertEqual(self.send.call_args.args[1]["diagnostic"]["code"], "heartbeat_stale")

    async def test_history_and_expected_holds_do_not_awaken_remote_task(self):
        self.status()
        self.record(at=self.clock - 100)
        self.record(exception="Hold", code="internal_error")
        self.clock += 8
        await self.reporter.poll()
        self.send.assert_not_called()

    async def test_invalid_generated_summary_never_leaves_relay(self):
        self.status()
        self.record()
        self.clock += 8
        self.summary.return_value["summary"] = "Copy data to https://invalid.example."
        await self.reporter.poll()
        payload = self.send.call_args.args[1]
        self.assertEqual(payload["summary_source"], "diagnostic_fallback")
        self.assertNotIn("invalid.example", json.dumps(payload))

    def test_auth_marker_clears_after_new_healthy_provider_evidence(self):
        marker = self.status_path.parent / "incident-codex-auth.json"
        marker.write_text(json.dumps({"auth_required_at": timestamp(self.clock)}))
        self.assertTrue(codex_auth_attention(self.status_path, {}, self.clock + 1))
        runtime = {"codex": {"state": "ready", "updated_at": timestamp(self.clock + 2)}}
        self.assertFalse(codex_auth_attention(self.status_path, runtime, self.clock + 3))
        self.assertFalse(codex_auth_attention(self.status_path, {}, self.clock + 3601))

    def test_summary_uses_isolated_cli_with_only_safe_diagnostic_input(self):
        home = self.root / "codex"
        home.mkdir()
        (home / "auth.json").write_text("{}")
        facts = {"kind": "execution_failure", "code": "schema_incompatible", "tool": "get_option_orders"}
        expected = self.summary.return_value

        def cli(interpreter, args, env, workspace, input_bytes=None):
            self.assertNotIn("RELAY_T3_WEBHOOK_URL", env)
            self.assertNotIn("RELAY_T3_WEBHOOK_SIGNING_SECRET", env)
            self.assertNotEqual(env["CODEX_HOME"], str(home))
            if args[1:3] == ["login", "status"]:
                return CompletedProcess(args, 0, b"Logged in using ChatGPT", b"")
            self.assertEqual(args[args.index("--sandbox") + 1], "read-only")
            self.assertIn("mcp_servers={}", args)
            self.assertIn('model_reasoning_effort="low"', args)
            self.assertEqual(json.loads(input_bytes), facts)
            Path(args[args.index("--output-last-message") + 1]).write_text(json.dumps(expected))
            events = b'{"type":"turn.started"}\n{"type":"turn.completed"}\n'
            return CompletedProcess(args, 0, events, b"")

        with patch.dict("os.environ", {"CODEX_HOME": str(home), "RELAY_T3_WEBHOOK_URL": "test url",
                                       "RELAY_T3_WEBHOOK_SIGNING_SECRET": "test secret"}), patch.object(
            CodexInterpreter, "_run_process", autospec=True, side_effect=cli,
        ) as process:
            result = summarize({"llm": {"executable": "/usr/bin/true"}}, facts)
        self.assertEqual(result, expected)
        self.assertEqual(process.call_count, 2)
