"""Offline scheduling, stale-result and durable-claim checks."""

import asyncio
import copy
import json
import unittest

from relay.core import Engine, Store
import test_core as fixtures


class EvaluationConcurrency(unittest.IsolatedAsyncioTestCase):
    setUp = fixtures.CoreChecks.setUp
    message = fixtures.CoreChecks.message
    owned = fixtures.CoreChecks.owned

    def block_first(self, *, consume_cancel=False):
        entered, release = asyncio.Event(), asyncio.Event()
        delegate = fixtures.Interpreter()
        first = True

        async def interpret(message, context, positions):
            nonlocal first
            if first:
                first = False
                entered.set()
                try:
                    await release.wait()
                except asyncio.CancelledError:
                    if not consume_cancel:
                        raise
                    await release.wait()
            return await delegate.interpret(message, context, positions)

        self.engine.interpreter.interpret = interpret
        return entered, release

    async def test_other_source_finishes_while_first_model_waits(self):
        self.config["channels"][1]["role"] = "signals"
        entered, release = self.block_first()
        slow = asyncio.create_task(self.engine.handle(self.message()))
        await entered.wait()
        self.assertFalse(self.engine.lock.locked())
        try:
            fast = await asyncio.wait_for(self.engine.handle(self.message(channel=1)), 1)
            self.assertEqual(fast["state"], "paper_order", fast)
        finally:
            release.set()
        result = await slow
        self.assertEqual(result["state"], "paper_order", result)
        self.assertEqual(len(self.broker.submissions), 2)
        self.assertEqual(self.engine.evaluations_active, 0)

    async def test_newer_exit_prevents_older_unsubmitted_entry(self):
        entered, release = self.block_first()
        entry = asyncio.create_task(self.engine.handle(self.message()))
        await entered.wait()
        newer = self.message(content="All out, close the rest")
        newer["source_group"] = self.config["channels"][0]["source_group"]
        self.engine.note_observation(newer)
        self.store.observe(newer)
        release.set()
        result = await entry
        self.assertEqual(result["state"], "held")
        self.assertIn("source", result["reason"])
        self.assertFalse(self.broker.submissions)

    async def test_duplicate_receipt_cannot_start_second_interpretation(self):
        entered, release = self.block_first()
        message = self.message()
        message["source_group"] = self.config["channels"][0]["source_group"]
        observed = self.store.observe(message)
        task = asyncio.create_task(self.engine.handle(message, _observed=observed))
        await entered.wait()
        duplicate = await self.engine.handle(message, _observed=observed)
        self.assertEqual(duplicate["state"], "duplicate")
        release.set()
        self.assertEqual((await task)["state"], "paper_order")
        self.assertEqual(len(self.broker.submissions), 1)

    async def test_edit_invalidates_running_attempt(self):
        entered, release = self.block_first()
        message = self.message()
        task = asyncio.create_task(self.engine.handle(message))
        await entered.wait()
        changed = copy.deepcopy(message)
        changed.update(revision="updated-revision", content="Cancel that entry",
                       source_group=self.config["channels"][0]["source_group"])
        self.engine.note_observation(changed)
        self.store.observe(changed)
        release.set()
        self.assertEqual((await task)["state"], "held")
        self.assertFalse(self.broker.submissions)

    async def test_inventory_change_invalidates_running_attempt(self):
        entered, release = self.block_first()
        task = asyncio.create_task(self.engine.handle(self.message()))
        await entered.wait()
        async with self.engine.lock:
            self.owned(quantity=2)
        release.set()
        result = await task
        self.assertEqual(result["state"], "held")
        self.assertIn("inventory", result["reason"])
        self.assertFalse(self.broker.submissions)

    async def test_cancelled_model_cannot_dispatch_even_if_it_suppresses_cancel(self):
        entered, release = self.block_first(consume_cancel=True)
        task = asyncio.create_task(self.engine.handle(self.message()))
        await entered.wait()
        task.cancel()
        await asyncio.sleep(0)
        release.set()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertFalse(self.broker.submissions)
        self.assertEqual(self.engine.evaluations_active, 0)

    async def test_restart_keeps_finished_claim_and_does_not_replay(self):
        message = self.message()
        self.assertEqual((await self.engine.handle(message))["state"], "paper_order")
        self.store.close()
        self.store = Store(self.config["database"])
        self.engine = Engine(self.config, self.store, self.interpreter, self.broker, lambda: self.now)
        result = await self.engine.handle(message, _observed="new")
        self.assertEqual(result["state"], "duplicate")
        self.assertEqual(len(self.broker.submissions), 1)

    async def test_interpretation_ready_is_visible_before_execution_finishes(self):
        entered, release = asyncio.Event(), asyncio.Event()
        original = self.broker.snapshot
        self.interpreter.decision["evaluation_timing"] = {"decision_at": self.now.isoformat(), "posted_to_decision_seconds": 1}

        async def blocked_snapshot():
            entered.set()
            await release.wait()
            return await original()

        self.broker.snapshot = blocked_snapshot
        task = asyncio.create_task(self.engine.handle(self.message()))
        await entered.wait()
        row = self.store.db.execute("SELECT state,decision FROM events").fetchone()
        self.assertEqual(row["state"], "evaluated")
        timing = json.loads(row["decision"])["evaluation_timing"]
        self.assertIn("interpretation_ready_at", timing)
        self.assertNotIn("decision_at", timing)
        release.set()
        self.assertEqual((await task)["state"], "paper_order")
        timing = json.loads(self.store.db.execute("SELECT decision FROM events").fetchone()[0])["evaluation_timing"]
        self.assertIn("decision_at", timing)
        self.assertIn("execution_seconds", timing)
        for stage in ("snapshot_seconds", "quote_seconds", "submission_seconds", "broker_result_at"):
            self.assertIn(stage, timing)

    async def test_restart_retains_source_chronology(self):
        older = self.message()
        newer = dict(older, id=str(int(older["id"]) + 1), ingestion="baseline")
        await self.engine.handle(newer)
        self.store.close()
        self.store = Store(self.config["database"])
        self.engine = Engine(self.config, self.store, self.interpreter, self.broker, lambda: self.now)
        result = await self.engine.handle(older)
        self.assertEqual(result["state"], "held")
        self.assertIn("newer", result["reason"])
        self.assertFalse(self.interpreter.calls)
        self.assertFalse(self.broker.submissions)

    async def test_snapshot_and_quote_are_read_concurrently(self):
        snapshot_started, quote_started = asyncio.Event(), asyncio.Event()
        snapshot, quote = self.broker.snapshot, self.broker.quote

        async def snapshot_read():
            snapshot_started.set()
            await quote_started.wait()
            return await snapshot()

        async def quote_read(contract):
            quote_started.set()
            await snapshot_started.wait()
            return await quote(contract)

        self.broker.snapshot, self.broker.quote = snapshot_read, quote_read
        result = await asyncio.wait_for(self.engine.handle(self.message()), 1)
        self.assertEqual(result["state"], "paper_order")


if __name__ == "__main__":
    unittest.main()
