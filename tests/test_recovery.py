"""Synthetic recovery assessments: no provider authentication or orders."""
import asyncio
import copy
import json
import unittest
from datetime import timedelta
from unittest.mock import AsyncMock, patch

from relay.core import Engine
from relay.recovery import RecoveryEvaluator
from tests import test_core as fixtures

NOW = fixtures.NOW


class RecoveryChecks(unittest.IsolatedAsyncioTestCase):
    setUp = fixtures.CoreChecks.setUp
    message = fixtures.CoreChecks.message

    def evaluator(self):
        self.assessments = []

        async def assess(message, context, positions, decision, facts):
            self.assessments.append((message, context, positions, decision, copy.deepcopy(facts)))
            return dict(status="viable", confidence=.97, reason="Fixture assessment",
                        evidence=[{"message_id": message["id"], "quote": message["content"]}])

        self.interpreter.assess_recovery = assess
        return RecoveryEvaluator(self.engine)

    async def pending(self, recovery, **changes):
        message = self.message(timestamp=(NOW - timedelta(hours=1)).isoformat(), **changes)
        message["ingestion"] = "baseline"
        message["source_group"] = self.config["channels"][0]["source_group"]
        observed = self.store.observe(message)
        await self.engine.handle(message, _observed=observed)
        self.assertEqual(recovery.enqueue(message, observed)["state"], "recovery_pending")
        return message

    def recorded(self, message):
        row = self.store.db.execute("SELECT decision FROM events WHERE message_id=? AND revision=?", (message["id"], message["revision"])).fetchone()
        return json.loads(row[0])["recovery"]

    async def test_later_messages_current_facts_live_mode_and_completed_dedup(self):
        self.config.update(mode="live")
        self.config["robinhood"].update(account_number="12345678", enable_live_orders=True)
        self.store.db.execute("DELETE FROM metadata WHERE key='execution_binding'")
        self.engine = Engine(self.config, self.store, self.interpreter, self.broker, lambda: self.now)
        self.broker.account["account_id"] = "12345678"
        recovery = self.evaluator()
        message = await self.pending(recovery)
        later = self.message(content="Stopped out", timestamp=(NOW - timedelta(minutes=5)).isoformat())
        await self.engine.handle(later | {"ingestion": "baseline"})
        original_assessor = self.interpreter.assess_recovery

        async def invalidate(*args):
            result = await original_assessor(*args)
            self.assertIn(later["id"], [m["id"] for m in args[1]])
            result.update(status="invalidated", reason="Later source stop-out", evidence=[
                {"message_id": message["id"], "quote": message["content"]},
                {"message_id": later["id"], "quote": later["content"]}])
            return result

        self.interpreter.assess_recovery = invalidate
        result = await recovery.assess(message)
        self.assertEqual(result["state"], "recovery_review")
        assessment = self.recorded(message)
        self.assertEqual(assessment["status"], "invalidated")
        self.assertEqual(assessment["original_timestamp"], message["timestamp"])
        self.assertEqual(assessment["facts"]["quote"]["ask"], ".80")
        self.assertNotIn(later["id"], [m["id"] for m in self.interpreter.calls[0][1]])
        self.assertIsNone(recovery.enqueue(message, "same"))
        self.assertIsNone(RecoveryEvaluator(self.engine).enqueue(message, "same"))
        self.assertEqual(self.broker.submissions, [])
        self.assertEqual(self.store.db.execute("SELECT count(*) FROM orders").fetchone()[0], 0)

    async def test_missing_closed_stale_and_unaffordable_facts_cannot_be_viable(self):
        recovery = self.evaluator()
        for kind in ("closed", "stale", "unaffordable", "unavailable"):
            with self.subTest(kind=kind):
                self.broker.account.update(market_open=True, buying_power="10000", timestamp=NOW.isoformat())
                self.broker.quotes = {}
                if kind == "closed":
                    self.broker.account["market_open"] = False
                elif kind == "stale":
                    self.broker.quotes["timestamp"] = (NOW - timedelta(hours=1)).isoformat()
                elif kind == "unaffordable":
                    self.broker.account["buying_power"] = "0"
                else:
                    self.broker.snapshot = AsyncMock(side_effect=RuntimeError("SECRET provider body"))
                message = await self.pending(recovery)
                await recovery.assess(message)
                assessment = self.recorded(message)
                self.assertEqual(assessment["status"], "uncertain")
                self.assertTrue(assessment["facts"]["blockers"])
                self.assertNotIn("SECRET", json.dumps(assessment))
        self.assertEqual(self.broker.submissions, [])

    async def test_sources_history_edits_imports_and_future(self):
        recovery = self.evaluator()
        for changes in ({"source": "import"}, {"edited_timestamp": NOW.isoformat()},
                        {"timestamp": (NOW + timedelta(minutes=1)).isoformat()},
                        {"ingestion_reason": "history"}, {"ingestion_reason": "backscroll"}):
            message = self.message() | changes
            self.assertFalse(recovery.eligible(message), changes)
        self.assertTrue(recovery.eligible(self.message(timestamp=(NOW - timedelta(days=30)).isoformat())))
        message = self.message()
        self.config["channels"][0]["authors"] = ["1999999999999999999"]
        self.assertFalse(recovery.eligible(message))

    async def test_stale_live_is_queued_but_fresh_and_previously_ordered_are_not(self):
        recovery = self.evaluator()
        stale = self.message(timestamp=(NOW - timedelta(minutes=5)).isoformat())
        await self.engine.handle(stale)
        self.assertEqual(recovery.enqueue(stale, "new")["state"], "recovery_pending")
        fresh = self.message()
        await self.engine.handle(fresh)
        self.assertIsNone(recovery.enqueue(fresh, "new"))
        self.assertIsNone(recovery.enqueue(fresh | {"ingestion": "baseline"}, "same"))

    async def test_context_is_source_scoped_bounded_and_detects_changes(self):
        recovery = self.evaluator()
        message = await self.pending(recovery)
        for i in range(62):
            later = self.message(timestamp=(NOW - timedelta(seconds=100-i)).isoformat())
            later["source_group"] = message["source_group"]
            self.store.observe(later)
        foreign = self.message(channel=1)
        foreign["source_group"] = "different-source"
        self.store.observe(foreign)
        context, truncated = recovery.context(message, NOW)
        self.assertEqual(len(context), 60)
        self.assertTrue(truncated)
        self.assertNotIn(foreign["id"], [m["id"] for m in context])
        original = self.interpreter.assess_recovery

        async def change(*args):
            result = await original(*args)
            new = self.message(content="All out") | {"source_group": message["source_group"]}
            self.store.observe(new)
            return result

        self.interpreter.assess_recovery = change
        await recovery.assess(message)
        self.assertEqual(self.recorded(message)["status"], "uncertain")
        self.assertTrue(self.recorded(message)["facts"]["context_changed"])

    async def test_omitted_older_background_does_not_imply_missing_later_updates(self):
        recovery = self.evaluator()
        for i in range(245):
            older = self.message(timestamp=(NOW - timedelta(hours=3, seconds=i)).isoformat())
            older["source_group"] = self.config["channels"][0]["source_group"]
            self.store.observe(older)
        message = await self.pending(recovery)
        context, truncated = recovery.context(message, NOW)
        self.assertEqual(len(context), 60)
        self.assertFalse(truncated)

    async def test_interruption_resumes_and_recovery_does_not_hold_fresh_lock(self):
        recovery = self.evaluator()
        missed = await self.pending(recovery)
        entered, release = asyncio.Event(), asyncio.Event()
        original = self.interpreter.assess_recovery

        async def slow(*args):
            entered.set()
            await release.wait()
            return await original(*args)

        self.interpreter.assess_recovery = slow
        task = asyncio.create_task(recovery.assess(missed))
        await asyncio.wait_for(entered.wait(), 2)
        fresh = self.message()
        result = await asyncio.wait_for(self.engine.handle(fresh), 1)
        self.assertEqual(result["state"], "paper_order")
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        row = self.store.db.execute("SELECT state FROM events WHERE message_id=?", (missed["id"],)).fetchone()
        self.assertEqual(row[0], "recovery_pending")
        self.store.record(missed, "recovery_evaluating", "interrupted")
        RecoveryEvaluator(self.engine)
        row = self.store.db.execute("SELECT state FROM events WHERE message_id=?", (missed["id"],)).fetchone()
        self.assertEqual(row[0], "recovery_pending")

    async def test_expired_and_non_actionable_never_need_a_quote(self):
        recovery = self.evaluator()
        self.interpreter.decision["contract"] = self.interpreter.decision["contract"] | {"expiry": "2026-09-04"}
        self.broker.quote = AsyncMock(side_effect=AssertionError("expired quote requested"))
        message = await self.pending(recovery)
        await recovery.assess(message)
        self.assertEqual(self.recorded(message)["status"], "invalidated")
        self.broker.quote.assert_not_called()
        self.interpreter.decision.update(action="IGNORE", contract=None)
        message = await self.pending(recovery)
        await recovery.assess(message)
        self.assertEqual(self.recorded(message)["status"], "not_actionable")
        self.broker.quote.assert_not_called()

    async def test_correction_keeps_origin_time_and_parser_errors_stay_visible(self):
        recovery = self.evaluator()
        origin = self.message(timestamp=(NOW - timedelta(days=3)).isoformat())
        await self.engine.handle(origin | {"ingestion": "baseline"})
        message = await self.pending(recovery, content="call*")
        self.interpreter.decision.update(origin_message_id=origin["id"], evidence=[
            {"message_id": origin["id"], "quote": origin["content"]},
            {"message_id": message["id"], "quote": message["content"]}])
        await recovery.assess(message)
        self.assertEqual(self.recorded(message)["original_timestamp"], origin["timestamp"])
        self.assertEqual(self.recorded(message)["signal_age_seconds"], 3 * 86400)
        message = await self.pending(recovery)
        self.interpreter.interpret = AsyncMock(side_effect=ValueError("SECRET malformed decision"))
        result = await recovery.assess(message)
        self.assertEqual(result["state"], "recovery_error")
        self.assertNotIn("SECRET", result["reason"])
        self.assertIsNone(recovery.enqueue(message, "same"))

    async def test_runtime_routes_baseline_to_recovery_and_shuts_down_cleanly(self):
        from relay.cli import run
        recovery = self.evaluator()
        message = self.message() | {"ingestion": "baseline", "ingestion_reason": "baseline"}
        self.config["channels"][0]["guild_id"] = "1545000000000000001"
        self.config["channels"][1]["guild_id"] = "1545000000000000001"
        self.config["database"] += ".runtime"
        assessed, results = asyncio.Event(), []

        async def monitor(config, on_message, **kwargs):
            await on_message(message)
            await asyncio.Future()

        async def assess(evaluator, missed):
            self.assertEqual(missed["id"], message["id"])
            assessed.set()
            return evaluator.store.record(missed, "recovery_review", "Fixture assessment only")

        def make_engine(*args):
            return Engine(*args, clock=lambda: NOW)

        with patch("relay.browser.monitor", monitor), patch("relay.cli.paper_broker", return_value=self.broker), \
                patch("relay.interpreter.CodexInterpreter", return_value=self.interpreter), \
                patch("relay.cli.Engine", make_engine), patch("relay.cli.emit", results.append), \
                patch.object(RecoveryEvaluator, "assess", assess):
            task = asyncio.create_task(run(self.config))
            try:
                await asyncio.wait_for(assessed.wait(), 3)
                self.assertTrue(any(r["state"] == "recovery_pending" for r in results))
            finally:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
        self.assertEqual(self.broker.submissions, [])

    def test_tracker_distinguishes_recovery_from_manual_history(self):
        from relay.browser import SnapshotTracker
        message = self.message()
        tracker = SnapshotTracker(message["channel_id"], clock=lambda: NOW)
        self.assertEqual(tracker.observe([message])[0]["ingestion_reason"], "baseline")
        new = self.message()
        self.assertEqual(tracker.observe([message, new])[0]["ingestion_reason"], "live")
        historical = self.message()
        self.assertEqual(tracker.observe([historical], force_baseline=True)[0]["ingestion_reason"], "history")

    async def test_startup_scan_recovers_unhandled_rows_without_dom_and_paginates(self):
        recovery = self.evaluator()
        for _ in range(102):
            message = self.message() | {"source_group": self.config["channels"][0]["source_group"]}
            self.store.observe(message)
        recovery.recover_saved()
        self.assertFalse(recovery.scan_complete)
        self.assertEqual(self.store.db.execute("SELECT count(*) FROM events WHERE state='recovery_pending'").fetchone()[0], 100)
        recovery.recover_saved()
        self.assertTrue(recovery.scan_complete)
        self.assertEqual(self.store.db.execute("SELECT count(*) FROM events WHERE state='recovery_pending'").fetchone()[0], 102)
        recovery.recover_saved()
        self.assertEqual(self.broker.submissions, [])

    async def test_stop_changes_and_position_changes_are_uncertain(self):
        recovery = self.evaluator()
        self.interpreter.decision.update(action="UPDATE_STOP", stop_price=".50")
        message = await self.pending(recovery)
        self.broker.quote = AsyncMock(wraps=self.broker.quote)
        await recovery.assess(message)
        self.assertEqual(self.recorded(message)["status"], "uncertain")
        self.broker.quote.assert_not_called()
        self.interpreter.decision.update(action="OPEN", stop_price=None)
        message = await self.pending(recovery)
        original = self.interpreter.assess_recovery

        async def positions_change(*args):
            result = await original(*args)
            fixtures.CoreChecks.owned(self, quantity=1, group=message["source_group"])
            return result

        self.interpreter.assess_recovery = positions_change
        await recovery.assess(message)
        self.assertEqual(self.recorded(message)["status"], "uncertain")
        self.assertIn("Relay positions changed during assessment", self.recorded(message)["facts"]["blockers"])
