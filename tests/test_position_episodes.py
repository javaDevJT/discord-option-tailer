"""Offline checks for persistent source episodes and management attribution."""
import unittest
from unittest.mock import AsyncMock

import test_core as fixtures
from relay.core import Engine, Store, contract_key
from relay.episodes import resolve_episode
from relay.evaluation import EvaluationRouter


class PositionEpisodeTests(unittest.IsolatedAsyncioTestCase):
    setUp = fixtures.CoreChecks.setUp
    message = fixtures.CoreChecks.message

    def episode(self, message):
        return resolve_episode(self.store.db, message | {"source_group": "source-a"})

    async def open(self, **changes):
        message = self.message(**changes)
        result = await self.engine.handle(message)
        self.assertEqual(result["state"], "paper_order", result)
        return message

    async def test_owned_episode_survives_restart_and_more_than_context_window(self):
        entry = await self.open()
        for _ in range(75):
            self.store.observe(self.message(content="70%") | {"source_group": "source-a"})
        comment = self.message(content="CLOSED")
        self.assertNotIn(entry["id"], [m["id"] for m in self.store.context(comment)])
        self.store.close()
        self.store = Store(self.config["database"])
        episode = self.episode(comment)
        self.assertEqual(episode["state"], "owned", episode)
        self.assertEqual(episode["entry_message_id"], entry["id"])
        self.assertEqual(episode["quantity"], 1)
        self.assertEqual(episode["contract"], fixtures.CONTRACT)

    async def test_closed_latest_episode_does_not_fall_back_to_another_holding(self):
        await self.open()
        with self.store.db:
            self.store.db.execute("UPDATE positions SET quantity=0")
            other = fixtures.CONTRACT | {"symbol": "SPY"}
            self.store.db.execute("INSERT INTO positions VALUES (?,?,?,?)",
                                  ("source-a", contract_key(other), 2, "1.00"))
        comment = self.message(content="CLOSED")
        episode = self.episode(comment)
        self.assertEqual(episode["state"], "flat", episode)
        proposed = self.interpreter.decision | {"action": "CLOSE", "contract": other}
        guarded = self.engine.guard_position_episode(comment, proposed, episode)
        self.assertEqual(guarded["action"], "WAIT")

    async def test_new_unfilled_entry_is_a_barrier_to_an_older_owned_position(self):
        await self.open()
        newer = self.message(content="OPEN SPY 500 call @ 1.00") | {"source_group": "source-a"}
        self.store.observe(newer)
        comment = self.message(content="TOOK MOST HERE 35%")
        episode = self.episode(comment)
        self.assertEqual(episode["state"], "unresolved", episode)
        self.assertEqual(episode["entry_message_id"], newer["id"])
        self.assertEqual(self.engine.guard_position_episode(comment,
                         self.interpreter.decision | {"action": "REDUCE"}, episode)["action"], "WAIT")

    async def test_unknown_fill_is_not_ready_for_direct_management(self):
        self.broker.result = {"id": "fixture", "status": "open", "filled_quantity": 0}
        await self.open()
        self.assertEqual(self.episode(self.message(content="CLOSED"))["state"], "unresolved")

    async def test_an_edited_entry_and_a_missing_reply_do_not_authorize_management(self):
        entry = await self.open()
        changed = entry | {"source_group": "source-a", "revision": "different",
                           "edited_timestamp": self.now.isoformat()}
        self.store.observe(changed)
        self.assertEqual(self.episode(self.message(content="CLOSED"))["state"], "ambiguous")
        missing = self.message(content="CLOSED", reply_to="1545000000000000099")
        episode = self.episode(missing)
        self.assertEqual(episode["state"], "ambiguous")
        self.assertEqual(self.engine.guard_position_episode(missing,
                         self.interpreter.decision | {"action": "CLOSE"}, episode)["action"], "WAIT")

    async def test_explicit_reply_resolves_older_episode_without_latest_entry_guess(self):
        entry = await self.open()
        self.store.observe(self.message(content="OPEN SPY 500 call @ 1.00") | {"source_group": "source-a"})
        episode = self.episode(self.message(content="CLOSED", reply_to=entry["id"]))
        self.assertEqual(episode["state"], "owned", episode)
        self.assertEqual(episode["entry_message_id"], entry["id"])

    async def test_other_author_cannot_inherit_episode_without_an_explicit_reply(self):
        await self.open()
        comment = self.message(content="CLOSED", author_id="1545000000000000099")
        self.assertEqual(self.episode(comment)["state"], "missing")

    async def test_reopen_prevents_a_reply_from_selling_the_new_episode(self):
        entry = await self.open()
        with self.store.db:
            self.store.db.execute("UPDATE positions SET quantity=0")
        # A changed premium avoids the existing duplicate-entry fingerprint policy.
        newer = await self.open(content="OPEN BAC 63 call 9/18 @ .81")
        comment = self.message(content="CLOSED", reply_to=entry["id"])
        self.assertEqual(self.episode(comment)["state"], "flat")
        current = self.episode(self.message(content="CLOSED"))
        self.assertEqual(current["state"], "owned", current)
        self.assertEqual(current["entry_message_id"], newer["id"])

    async def test_upgrade_backfills_existing_messages_and_orders(self):
        entry = await self.open()
        with self.store.db:
            self.store.db.execute("DROP TABLE signal_episodes")
            self.store.db.execute("DELETE FROM metadata WHERE key='signal_episodes_v1'")
        self.store.close()
        self.store = Store(self.config["database"])
        episode = self.episode(self.message(content="CLOSED"))
        self.assertEqual(episode["state"], "owned", episode)
        self.assertEqual(episode["entry_message_id"], entry["id"])


class ManagementExecutionTests(unittest.IsolatedAsyncioTestCase):
    setUp = fixtures.CoreChecks.setUp
    message = fixtures.CoreChecks.message
    larger_orders = fixtures.CoreChecks.larger_orders

    def router(self):
        codex = AsyncMock()
        codex.interpret.side_effect = AssertionError("bounded rule reached Codex")
        router = EvaluationRouter({}, codex=codex, broker=self.broker)
        router.jev.interpret = AsyncMock(side_effect=AssertionError("bounded rule reached JEV"))
        self.addAsyncCleanup(router.aclose)
        self.engine.interpreter = router
        return router

    def comment(self, text):
        return self.message(content="", embeds=[{"title": "Comment", "description": text}])

    async def test_full_close_then_repeat_stays_flat_and_model_free_after_restart(self):
        self.router()
        self.assertEqual((await self.engine.handle(self.message()))["state"], "paper_order")
        self.assertEqual((await self.engine.handle(self.comment("CLOSED")))["state"], "paper_order")
        self.assertFalse(self.store.positions())
        count = len(self.broker.submissions)
        self.store.close()
        self.store = Store(self.config["database"])
        self.engine = Engine(self.config, self.store, self.engine.interpreter, self.broker, lambda: self.now)
        result = await self.engine.handle(self.comment("Stopped out 🛑"))
        self.assertEqual(result["state"], "wait", result)
        self.assertEqual(len(self.broker.submissions), count)

    async def test_half_rounding_and_durable_breakeven_request(self):
        self.larger_orders(2)
        self.router()
        self.assertEqual((await self.engine.handle(self.message()))["state"], "paper_order")
        self.assertEqual(self.store.positions()[0]["quantity"], 2)
        from test_stop_loss import StopBroker
        self.broker = StopBroker()
        self.engine.broker = self.broker
        result = await self.engine.handle(self.comment("Took half here — S/L BE"))
        self.assertEqual(result["state"], "paper_order", result)
        sales = [o for o in self.broker.submissions if o["side"] == "sell" and o.get("order_type") != "stop_market"]
        self.assertEqual(sales[0]["quantity"], 1)
        row = self.store.db.execute("SELECT stop_price FROM stop_requests").fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(row[0], "0.80")
        self.assertEqual(self.store.positions()[0]["quantity"], 1)
        stop = self.broker.submissions[-1]
        self.assertEqual((stop["order_type"], stop["quantity"], stop["stop_price"]), ("stop_market", 1, "0.80"))

    async def test_half_of_one_sells_one_and_leaves_no_stop(self):
        self.router()
        await self.engine.handle(self.message())
        result = await self.engine.handle(self.comment("Took half here — S/L BE"))
        self.assertEqual(result["state"], "paper_order", result)
        self.assertFalse(self.store.positions())
        self.assertEqual(len(self.broker.submissions), 2)
        self.assertEqual(self.broker.submissions[-1]["quantity"], 1)
        self.assertEqual(self.store.db.execute("SELECT status FROM stop_requests").fetchone()[0], "complete")

    async def test_half_of_three_sells_two_and_protects_one(self):
        self.larger_orders(3)
        self.router()
        await self.engine.handle(self.message())
        self.assertEqual(self.store.positions()[0]["quantity"], 3)
        from test_stop_loss import StopBroker
        self.broker = StopBroker()
        self.engine.broker = self.broker
        result = await self.engine.handle(self.comment("Took half here — S/L BE"))
        self.assertEqual(result["state"], "paper_order", result)
        self.assertEqual([o["quantity"] for o in self.broker.submissions], [2, 1])
        self.assertEqual(self.store.positions()[0]["quantity"], 1)

    async def test_gain_only_update_and_watch_do_not_call_a_model_or_sell(self):
        self.router()
        await self.engine.handle(self.message())
        count = len(self.broker.submissions)
        for text in ["70%", "100% here 🔥", "45%!!!!!!!!!!!!!!!!!!"]:
            result = await self.engine.handle(self.comment(text))
            self.assertEqual(result["state"], "wait", result)
        result = await self.engine.handle(self.message(content="", embeds=[
            {"title": "On watch", "description": "BAC $64C 👀"}]))
        self.assertEqual(result["state"], "wait", result)
        self.assertEqual(len(self.broker.submissions), count)

    async def test_entry_context_outside_window_is_supplied_to_fast_management(self):
        self.router()
        entry = self.message()
        await self.engine.handle(entry)
        for _ in range(75):
            self.store.observe(self.comment("70%") | {"source_group": "source-a"})
        result = await self.engine.handle(self.comment("CLOSED"))
        self.assertEqual(result["state"], "paper_order", result)
        self.assertFalse(self.store.positions())

    async def unknown_source_uses_fallback(self, previous_decision=None):
        router = self.router()
        entry = self.message()
        await self.engine.handle(entry)
        unknown = self.message(content="Picked up BAC 63 calls for 9/18 at .80") | {"source_group": "source-a"}
        self.engine.note_observation(unknown)
        self.store.observe(unknown)
        if previous_decision is not None:
            self.store.record(unknown, "wait", "Unresolved contextual fixture", previous_decision)
        current = self.comment("CLOSED")
        router.codex.interpret.side_effect = None
        router.codex.interpret.return_value = {
            "action": "WAIT", "origin_message_id": None, "contract": None,
            "quantity": None, "fraction": None, "alert_price": None, "stop_price": None,
            "confidence": 1, "ambiguous": True, "reason": "The intervening post needs contextual resolution.",
            "evidence": [{"message_id": current["id"], "quote": "CLOSED"}], "profit_only": False,
        }
        result = await self.engine.handle(current)
        self.assertEqual(result["state"], "wait", result)
        router.codex.interpret.assert_awaited_once()
        context = router.codex.interpret.call_args.args[1]
        ids = [item["id"] for item in context]
        self.assertIn(unknown["id"], ids)
        self.assertLess(ids.index(entry["id"]), ids.index(unknown["id"]))
        self.assertEqual(len(self.broker.submissions), 1)

    async def test_unknown_intervening_post_uses_model_with_chronological_context(self):
        await self.unknown_source_uses_fallback()

    async def test_ambiguous_wait_on_intervening_post_remains_a_model_barrier(self):
        await self.unknown_source_uses_fallback({"action": "WAIT", "ambiguous": True})

    async def test_pinned_entry_stays_chronological_in_model_fallback_context(self):
        router = self.router()
        entry = self.message()
        await self.engine.handle(entry)
        for _ in range(75):
            self.store.observe(self.comment("70%") | {"source_group": "source-a"})
        current = self.comment("TOOK MOST HERE 35%")
        router.codex.interpret.side_effect = None
        router.codex.interpret.return_value = {
            "action": "WAIT", "origin_message_id": None, "contract": None,
            "quantity": None, "fraction": None, "alert_price": None, "stop_price": None,
            "confidence": 1, "ambiguous": True, "reason": "Unspecified sale fraction.",
            "evidence": [{"message_id": current["id"], "quote": "TOOK MOST HERE 35%"}], "profit_only": False,
        }
        result = await self.engine.handle(current)
        self.assertEqual(result["state"], "wait", result)
        context = router.codex.interpret.call_args.args[1]
        self.assertEqual(len(context), 60)
        self.assertEqual(context[0]["id"], entry["id"])
        self.assertEqual([int(m["id"]) for m in context], sorted(int(m["id"]) for m in context))

    async def test_model_cannot_turn_percentage_status_into_a_sale(self):
        from relay.interpreter import validate_decision
        current = self.comment("70%") | {"source_group": "source-a"}
        proposed = {
            "action": "CLOSE", "origin_message_id": current["id"], "contract": fixtures.CONTRACT,
            "quantity": None, "fraction": None, "alert_price": None, "stop_price": None,
            "confidence": 1, "ambiguous": False, "reason": "Incorrect fixture sale proposal.",
            "evidence": [{"message_id": current["id"], "quote": "70%"}], "profit_only": False,
        }
        self.assertEqual(validate_decision(proposed, current, [])["action"], "WAIT")
        current = self.comment("up 50%, took 50% here") | {"source_group": "source-a"}
        proposed.update(action="REDUCE", origin_message_id=current["id"], fraction=.5,
                        evidence=[{"message_id": current["id"], "quote": "up 50%, took 50% here"}])
        self.assertEqual(validate_decision(proposed, current, [])["action"], "REDUCE")
