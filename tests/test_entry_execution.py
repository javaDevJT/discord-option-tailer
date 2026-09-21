"""Offline checks for direct entries and the final submission boundary."""

import asyncio
import copy
import json
import unittest
from pathlib import Path
from unittest.mock import AsyncMock

import test_core as fixtures
from relay.core import Engine, Store
from relay.evaluation import EvaluationRouter
from relay.entry_rules import deterministic_entry


class EntryExecutionTests(unittest.IsolatedAsyncioTestCase):
    setUp = fixtures.CoreChecks.setUp
    message = fixtures.CoreChecks.message

    def entry(self):
        return self.message(content="", embeds=[{
            "title": "ENTRY",
            "description": "🖼️ Contract: BAC $63C\n💰 Price: .80\n‼️ Comments: none",
        }])

    def router(self):
        codex = AsyncMock()
        codex.interpret.side_effect = AssertionError("direct entry reached Codex")
        router = EvaluationRouter({"evaluation": {"direct_entries": True}}, codex=codex, broker=self.broker)
        router.jev.interpret = AsyncMock(side_effect=AssertionError("direct entry reached JEV"))
        self.addAsyncCleanup(router.aclose)
        self.engine.interpreter = router
        return router

    async def test_direct_entry_needs_no_model_and_overlaps_expiry_with_fresh_snapshot(self):
        router = self.router()
        snapshot_started = asyncio.Event()
        snapshot_calls = 0

        async def snapshot():
            nonlocal snapshot_calls
            snapshot_calls += 1
            snapshot_started.set()
            return self.broker.account

        async def nearest(contract):
            await asyncio.wait_for(snapshot_started.wait(), .2)
            return dict(fixtures.CONTRACT)

        self.broker.snapshot = snapshot
        self.broker.nearest_expiry = nearest
        result = await self.engine.handle(self.entry())
        self.assertEqual(result["state"], "paper_order", result)
        self.assertEqual(snapshot_calls, 1)
        self.assertEqual(len(self.broker.submissions), 1)
        self.assertEqual(self.broker.submissions[0]["contract"], fixtures.CONTRACT)
        router.codex.interpret.assert_not_awaited()
        router.jev.interpret.assert_not_awaited()

    async def test_direct_entry_is_not_queued_behind_codex(self):
        router = self.router()
        await router._codex_lock.acquire()
        try:
            result = await asyncio.wait_for(router.interpret(self.entry(), [], []), .2)
        finally:
            router._codex_lock.release()
        self.assertEqual(result["action"], "OPEN")
        self.assertEqual(result["evaluation_timing"]["evaluator"], "rules")
        self.assertEqual(result["evaluation_timing"]["model_duration_seconds"], 0)

    async def test_shadow_and_opt_out_preserve_codex_authority(self):
        for mode, direct_entries in (("jev_shadow", True), ("codex", False)):
            with self.subTest(mode=mode):
                router = self.router()
                router.settings.update(mode=mode, direct_entries=direct_entries)
                message = self.entry()
                decision = deterministic_entry(message)
                decision.update(action="IGNORE", contract=None, origin_message_id=None,
                                alert_price=None, reason="Mock model abstains")
                router.codex.interpret.side_effect = None
                router.codex.interpret.return_value = decision
                result = await router.interpret(message, [], [])
                self.assertEqual(result["action"], "IGNORE")
                self.assertEqual(result["evaluation_timing"]["evaluator"], "codex")
                router.codex.interpret.assert_awaited_once()

    async def test_entry_jev_timeout_falls_back_before_default_budget(self):
        router = self.router()
        router.settings.update(mode="jev", direct_entries=False, timeout_ms=1200)
        blocked = asyncio.Event()

        async def waiting_provider(*args):
            await blocked.wait()
            raise AssertionError("provider should have been canceled")

        router.jev.interpret.side_effect = waiting_provider
        router.codex.interpret.side_effect = None
        message = self.entry()
        router.codex.interpret.return_value = deterministic_entry(message)
        result = await asyncio.wait_for(router.interpret(message, [], []), .95)
        self.assertEqual(result["evaluation_timing"]["fallback_reason"], "jev_timeout")
        router.codex.interpret.assert_awaited_once()

    async def test_live_source_verification_happens_once_after_review(self):
        config = copy.deepcopy(self.config)
        config["mode"] = "live"
        config["require_source_verification"] = True
        config["robinhood"].update(account_number="00012345", enable_live_orders=True)
        self.broker.account["account_id"] = "00012345"
        store = Store(Path(self.temp.name) / "live.sqlite3")
        self.addCleanup(store.close)
        engine = Engine(config, store, self.interpreter, self.broker, lambda: self.now)
        reviewed = False

        async def review():
            nonlocal reviewed
            reviewed = True

        async def verify(message):
            self.assertTrue(reviewed)
            return True

        self.broker.review_hook = review
        engine.verify_current = AsyncMock(side_effect=verify)
        result = await engine.handle(self.message())
        self.assertEqual(result["state"], "broker_order", result)
        engine.verify_current.assert_awaited_once()
        saved = store.db.execute("SELECT decision FROM events WHERE message_id=?", (result["message_id"],)).fetchone()
        timing = json.loads(saved["decision"])["evaluation_timing"]
        self.assertGreaterEqual(timing["received_to_submission_seconds"], 0)
        self.assertIn("submission_started_at", timing)
        self.assertEqual(len(self.broker.submissions), 1)

    async def test_missing_source_at_final_boundary_prevents_placement(self):
        config = copy.deepcopy(self.config)
        config["mode"] = "live"
        config["require_source_verification"] = True
        config["robinhood"].update(account_number="00012345", enable_live_orders=True)
        self.broker.account["account_id"] = "00012345"
        store = Store(Path(self.temp.name) / "live.sqlite3")
        self.addCleanup(store.close)
        engine = Engine(config, store, self.interpreter, self.broker, lambda: self.now)
        engine.verify_current = AsyncMock(return_value=False)
        result = await engine.handle(self.message())
        self.assertEqual(result["state"], "held", result)
        self.assertFalse(self.broker.submissions)
        self.assertEqual(store.unresolved(), 0)
