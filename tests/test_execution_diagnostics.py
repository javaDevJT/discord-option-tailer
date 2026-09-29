"""Offline regressions for safe execution failure diagnostics."""

import json
import logging
import stat
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from relay.broker import BrokerError, BrokerPreflightHold, RobinhoodMCP
from relay.core import Engine, Hold, Store
from relay.status import configure_execution_log, execution_check, execution_failure, log_execution, project_dispatch_checks
from relay.dashboard import _project_decision
from relay.interpreter import InterpretationError, _decision_for_recovery
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

    async def test_interpreter_failure_keeps_provenance_without_provider_text(self):
        fixture = self.fixture
        fixture.interpreter.interpret = AsyncMock(side_effect=InterpretationError("private-provider-body", code="internal_error", retryable=False))
        result = await fixture.engine.handle(fixture.message())
        self.assertEqual(result["state"], "error")
        saved = fixture.store.db.execute("SELECT decision FROM events ORDER BY id DESC LIMIT 1").fetchone()[0]
        self.assertEqual(json.loads(saved)["execution_diagnostic"]["stage"], "interpretation")
        self.assertNotIn("private-provider-body", saved + result["reason"])
        self.assertEqual(fixture.broker.submissions, [])

    def live_fixture(self):
        fixture = self.fixture
        fixture.config["mode"] = "live"
        fixture.config["robinhood"].update(account_number="00012345", enable_live_orders=True)
        fixture.broker.account["account_id"] = "00012345"
        fixture.store.close()
        fixture.store = Store(str(Path(fixture.temp.name) / "live.sqlite3"))
        self.addCleanup(fixture.store.close)
        fixture.engine = Engine(fixture.config, fixture.store, fixture.interpreter, fixture.broker, clock=lambda: fixture.now)
        return fixture

    async def test_dispatch_failure_retains_named_guard_and_cause_after_restart(self):
        fixture = self.live_fixture()

        async def change_during_review():
            fixture.broker.account["market_open"] = False

        fixture.broker.review_hook = change_during_review
        message = fixture.message()
        result = await fixture.engine.handle(message)
        self.assertEqual(result["state"], "held")
        self.assertEqual(fixture.broker.submissions, [])
        self.assertIn("check=account_session", result["reason"])
        fixture.store.close()
        reopened = Store(str(Path(fixture.temp.name) / "live.sqlite3"))
        self.addCleanup(reopened.close)
        decision = json.loads(reopened.db.execute("SELECT decision FROM events WHERE message_id=?", (message["id"],)).fetchone()[0])
        diag = decision["execution_diagnostic"]
        self.assertEqual(diag["exception"], "BrokerPreflightHold")
        cause = next(c for c in diag["causes"] if c.get("check") == "account_session")
        self.assertEqual(cause["exception"], "Hold")
        self.assertFalse(cause["context"]["market_open"])
        self.assertTrue(cause["context"]["account_matches"])
        self.assertEqual(diag["context"]["message_id"], message["id"])
        self.assertEqual(len(diag["id"]), 32)
        self.assertEqual(decision["dispatch_checks"][-1]["status"], "failed")
        projected = _project_decision(decision)
        self.assertEqual(projected["execution_diagnostic"], diag)
        self.assertEqual(reopened.db.execute("SELECT status FROM orders").fetchone()[0], "rejected")

    async def test_unexpected_callback_exception_is_retained_without_submission(self):
        fixture = self.live_fixture()
        original = fixture.engine.verify_dispatch

        async def broken(message, decision, order, snapshot=None, quote=None, **kwargs):
            if snapshot is not None:
                with execution_check(decision.setdefault("dispatch_checks", []), "entry_capacity"):
                    raise TypeError("private fixture provider text")
            return await original(message, decision, order, snapshot, quote, **kwargs)

        fixture.engine.verify_dispatch = broken
        result = await fixture.engine.handle(fixture.message())
        self.assertEqual(result["state"], "held")
        decision = json.loads(fixture.store.db.execute("SELECT decision FROM events ORDER BY rowid DESC LIMIT 1").fetchone()[0])
        diag = decision["execution_diagnostic"]
        self.assertEqual(diag["check"], "entry_capacity")
        self.assertIn("TypeError", [c.get("exception") for c in diag["causes"]])
        self.assertNotIn("private fixture provider text", json.dumps(diag))
        self.assertEqual(fixture.broker.submissions, [])

    async def test_source_generation_failure_records_expected_and_actual(self):
        fixture = self.live_fixture()

        async def newer_context():
            newer = fixture.message(content="new context")
            newer["source_group"] = fixture.config["channels"][0]["source_group"]
            fixture.store.observe(newer)

        fixture.broker.review_hook = newer_context
        result = await fixture.engine.handle(fixture.message())
        self.assertEqual(result["state"], "held")
        decision = json.loads(fixture.store.db.execute("SELECT decision FROM events WHERE decision IS NOT NULL ORDER BY rowid LIMIT 1").fetchone()[0])
        cause = next(c for c in decision["execution_diagnostic"]["causes"] if c.get("check") == "source_consistency")
        self.assertNotEqual(cause["context"]["expected_source_generation"], cause["context"]["actual_source_generation"])
        self.assertEqual(fixture.broker.submissions, [])

    def test_rotated_logs_remain_private_and_retain_only_safe_data(self):
        logger = logging.getLogger("relay.status")
        original_handlers, original_level, original_propagate = logger.handlers[:], logger.level, logger.propagate
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "execution-diagnostics.log"
            try:
                logger.handlers = []
                logger.propagate = False
                configure_execution_log(path)
                handler = logger.handlers[0]
                with patch.dict("os.environ", {"RELAY_SOURCE_REVISION": "a" * 40}):
                    log_execution("outcome", context={"message_id": "1545700000000000001", "token": "private-token", "state": "held"})
                handler.doRollover()
                log_execution("outcome", context={"message_id": "1545700000000000002", "state": "held"})
                handler.flush()
                self.assertIn('"build_revision": "' + "a" * 40 + '"', Path(str(path) + ".1").read_text())
                for file in (path, Path(str(path) + ".1")):
                    self.assertEqual(stat.S_IMODE(file.stat().st_mode), 0o600)
                    self.assertNotIn("private-token", file.read_text())
                    self.assertIn('"event": "outcome"', file.read_text())
            finally:
                for handler in logger.handlers:
                    handler.close()
                logger.handlers, logger.level, logger.propagate = original_handlers, original_level, original_propagate

    def test_dispatch_projection_bounds_and_rejects_hostile_values(self):
        checks = [{"check": "entry_capacity", "status": "failed", "context": {"buying_power": "12.50", "token": "private-token"}}] * 200
        projected = project_dispatch_checks(checks)
        self.assertEqual(len(projected), 96)
        self.assertNotIn("private-token", json.dumps(projected))
        self.assertEqual(project_dispatch_checks([{"check": [], "status": {}}]), [])
        self.assertNotIn("dispatch_checks", _decision_for_recovery({"action": "OPEN", "dispatch_checks": checks}))
        self.assertNotIn("reconciliation_diagnostic", _decision_for_recovery({"action": "OPEN", "reconciliation_diagnostic": {"stage": "reconciliation"}}))
        self.assertEqual(_project_decision({"reconciliation_diagnostic": {"stage": "reconciliation", "token": "private-token"}}),
                         {"reconciliation_diagnostic": {"stage": "reconciliation", "frames": []}})

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
