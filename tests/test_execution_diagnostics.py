"""Offline regressions for safe execution failure diagnostics."""

import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from relay.broker import BrokerError, RobinhoodMCP
from relay.dashboard import _project_decision
from relay.interpreter import _decision_for_recovery
import test_core


class ExecutionDiagnosticsTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.fixture = test_core.CoreChecks("test_missing_expiry_resolves_once_and_preserves_explicit_dates")
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.addCleanup(self.fixture.tearDown)
        self.addCleanup(self.fixture.store.close)

    async def test_quote_failure_persists_safe_provenance_without_submission(self):
        fixture = self.fixture
        secrets = ("token-sensitive-98af", "provider-body-private", "/Users/alice/private/credentials.json")
        error = RuntimeError("Bearer " + secrets[0] + "; " + secrets[1] + "; " + secrets[2])
        fixture.broker.quote = AsyncMock(side_effect=error)
        message = fixture.message()

        with self.assertLogs("relay.status", level="ERROR") as captured:
            result = await fixture.engine.handle(message)

        self.assertEqual(result["state"], "error")
        self.assertIn("stage=quote", result["reason"])
        self.assertEqual(fixture.broker.submissions, [])
        row = fixture.store.db.execute(
            "SELECT reason, decision FROM events WHERE message_id=? ORDER BY created_at DESC LIMIT 1",
            (message["id"],),
        ).fetchone()
        decision = json.loads(row["decision"])
        diagnostic = decision["execution_diagnostic"]
        self.assertEqual(diagnostic["stage"], "quote")
        for private_value in secrets:
            self.assertNotIn(private_value, result["reason"])
            self.assertNotIn(private_value, row["reason"] + row["decision"])
            self.assertNotIn(private_value, "\n".join(captured.output))

    async def test_prefetched_snapshot_origin_survives_quote_measurement(self):
        error = RuntimeError("private snapshot response")
        decision = {}

        async def fail():
            raise error

        with self.assertRaises(RuntimeError):
            await self.fixture.engine.measure(decision, "snapshot_seconds", fail())
        with self.assertRaises(RuntimeError):
            await self.fixture.engine.measure(decision, "quote_seconds", fail())

        self.fixture.engine.diagnose_failure(decision, error)
        self.assertEqual(decision["execution_diagnostic"]["stage"], "snapshot")

    async def test_unknown_submission_remains_blocking_after_diagnosis(self):
        fixture = self.fixture
        submit = AsyncMock(side_effect=RuntimeError("private provider response"))
        fixture.broker.submit = submit

        first = await fixture.engine.handle(fixture.message())
        second = await fixture.engine.handle(fixture.message())

        self.assertEqual(first["state"], "unknown")
        self.assertIn("reconciliation", first["reason"])
        self.assertNotEqual(second["state"], "paper_order")
        self.assertEqual(submit.await_count, 1)
        self.assertEqual(fixture.store.report()["unresolved_orders"], 1)

    async def test_schema_and_tool_failures_keep_tool_provenance(self):
        broker = RobinhoodMCP({})
        broker.session = SimpleNamespace(call_tool=AsyncMock())
        broker.schemas = {
            "search": {
                "type": "object",
                "properties": {"query": {"type": "string"}},
                "required": ["query"],
            }
        }

        with self.assertRaises(BrokerError) as schema_error:
            await broker.call("search", {})
        self.assertEqual(schema_error.exception._relay_failure["tool"], "search")
        broker.session.call_tool.assert_not_awaited()

        provider_error = RuntimeError("private tool response")
        broker.session = SimpleNamespace(call_tool=AsyncMock(side_effect=provider_error))
        with self.assertRaises(RuntimeError):
            await broker.call("search", {"query": "QQQ"})
        self.assertEqual(provider_error._relay_failure["tool"], "search")

    def test_dashboard_projects_only_allowlisted_diagnostic_fields(self):
        projected = _project_decision({
            "execution_diagnostic": {
                "stage": "quote",
                "exception": "RuntimeError",
                "tool": "search",
                "code": "http_429",
                "frames": ["relay/broker.py:755:call"],
                "message": "token-sensitive-98af",
                "provider_body": "provider-body-private",
                "path": "/Users/alice/private/credentials.json",
            }
        })

        self.assertEqual(projected["execution_diagnostic"], {
            "stage": "quote",
            "exception": "RuntimeError",
            "tool": "search",
            "code": "http_429",
            "frames": ["relay/broker.py:755:call"],
        })

    def test_recovery_decision_strips_execution_diagnostic(self):
        recovered = _decision_for_recovery({
            "action": "OPEN",
            "execution_diagnostic": {"stage": "quote", "message": "private provider body"},
        })

        self.assertNotIn("execution_diagnostic", recovered)
        self.assertNotIn("private provider body", json.dumps(recovered))

    async def test_logging_failure_does_not_replace_quote_outcome(self):
        fixture = self.fixture
        fixture.broker.quote = AsyncMock(side_effect=RuntimeError("private quote response"))
        with patch("relay.status.logging.getLogger") as get_logger:
            get_logger.return_value.error.side_effect = OSError("log sink unavailable")
            result = await fixture.engine.handle(fixture.message())

        self.assertEqual(result["state"], "error")
        self.assertIn("stage=quote", result["reason"])
        self.assertEqual(fixture.broker.submissions, [])


if __name__ == "__main__":
    unittest.main()
