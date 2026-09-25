"""Watch preparation is advisory; only a fresh matched ENTRY can spend."""
import asyncio
import copy
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
import json
import unittest
from unittest.mock import AsyncMock

from tests import test_core as fixtures
from relay.core import Engine, Hold, Store
from relay.watch import watch_candidate


class WatchEntryChecks(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        fixtures.CoreChecks.setUp(self)
        self.store.close()
        self.config = copy.deepcopy(self.config)
        self.config["mode"] = "live"
        self.config["robinhood"].update(account_number="00012345", enable_live_orders=True)
        self.config["risk"]["max_chase_fraction"] = ".20"
        self.broker.account["account_id"] = "00012345"
        live_store = Store(Path(self.temp.name) / "live-watch.sqlite3")
        self.addCleanup(live_store.close)
        self.engine = Engine(self.config, live_store, self.interpreter, self.broker, lambda: self.now)
        self.store = live_store
        self.addAsyncCleanup(self.engine.watches.close)
        self.broker.prewarm_entry = AsyncMock()
        self.broker.nearest_expiry = AsyncMock(return_value=fixtures.CONTRACT)
        self.broker.prepared_entry = lambda contract, price, **kwargs: dict(
            contract=contract, tradable=True, multiplier=100, currency="USD",
            asset_type="equity_option", tick_size=".05", prepared_at=self.now.isoformat())

    message = fixtures.CoreChecks.message

    async def warm(self, content="On watch: BAC $63C"):
        message = self.message(content=content)
        message["source_group"] = self.config["channels"][0]["source_group"]
        self.store.observe(message)
        self.engine.watches.note(message)
        await asyncio.gather(*list(self.engine.watches.tasks))
        return message

    def test_watch_requires_one_contract_and_preserves_expiry(self):
        message = self.message(content="On watch: BAC $63C")
        self.assertEqual(watch_candidate(message), fixtures.CONTRACT | {"expiry": "nearest"})
        explicit = self.message(content="Eyes on BAC $63 calls 9/18")
        self.assertEqual(watch_candidate(explicit), fixtures.CONTRACT)
        with_purchase_time = self.message(content="Eyes on BAC $63C 9/18, money into these calls at the ask today")
        self.assertEqual(watch_candidate(with_purchase_time), fixtures.CONTRACT)
        for content in ("BAC on watch", "Test: BAC $0C on watch", "BAC $63C and QQQ $700P on watch",
                        "ENTRY BAC $63C @ .80, was on watch", "Eyes on BAC $63C 9/18 0DTE"):
            self.assertIsNone(watch_candidate(self.message(content=content)))

    async def test_watch_prepares_without_order_and_resolves_entry_without_rpc(self):
        await self.warm()
        self.assertEqual(self.broker.submissions, [])
        entry = self.message(content="ENTRY BAC $63C @ .80")
        decision = self.interpreter.decision | {"contract": fixtures.CONTRACT | {"expiry": "nearest"}}
        entry["source_group"] = self.config["channels"][0]["source_group"]
        self.store.observe(entry)
        decision["origin_message_id"] = entry["id"]
        decision["evidence"] = [{"message_id": entry["id"], "quote": entry["content"]}]
        resolved = await self.engine.resolve_expiry(entry, decision)
        self.assertEqual(resolved["contract"], fixtures.CONTRACT)
        self.assertEqual(self.broker.nearest_expiry.await_count, 1)

    async def test_observed_compact_and_spaced_watch_embeds_prepare_same_put(self):
        contract = dict(symbol="QQQ", strike="740", option_type="put", expiry=fixtures.CONTRACT["expiry"])
        self.broker.nearest_expiry.return_value = contract
        for description in ("QQQ740P 👀", "QQQ $740P 👀"):
            with self.subTest(description=description):
                watch = self.message(content="", embeds=[{
                    "title": "On watch", "description": description, "footer": {"text": "@zendotrades"},
                }])
                self.assertEqual(watch_candidate(watch), contract | {"expiry": "nearest"})
                watch["source_group"] = self.config["channels"][0]["source_group"]
                self.store.observe(watch)
                self.engine.watches.note(watch)
                await asyncio.gather(*list(self.engine.watches.tasks))
                entry = self.message(content="ENTRY QQQ $740P @ 1.40")
                decision = self.interpreter.decision | {"contract": contract | {"expiry": "nearest"}}
                diagnostic = {}
                self.assertEqual(self.engine.watches.match(entry, decision, diagnostic=diagnostic),
                                 self.engine.watches.entries[watch["id"]]["contract"])
                self.assertEqual(diagnostic["watch_message_id"], watch["id"])
        self.assertEqual(self.broker.submissions, [])

    async def test_preparation_failure_is_saved_on_held_entry_without_secret_text(self):
        self.broker.prewarm_entry.side_effect = RuntimeError("Authorization: Bearer secret-provider-response")
        with self.assertLogs("relay.status", level="ERROR") as logs:
            watch = await self.warm()
        self.broker.quotes.update(bid="9.90", ask="10.00")
        entry = self.message()
        result = await self.engine.handle(entry)
        self.assertEqual(result["state"], "held", result)
        row = self.store.db.execute("SELECT decision FROM events WHERE message_id=?", (entry["id"],)).fetchone()
        diagnostic = json.loads(row[0])["entry_preparation"]
        self.assertEqual(diagnostic["route"], "fresh")
        self.assertEqual(diagnostic["reason"], "watch_preparation_failed")
        self.assertEqual(diagnostic["watch_message_id"], watch["id"])
        self.assertEqual(diagnostic["failure"]["stage"], "watch_preparation")
        self.assertNotIn("secret-provider-response", json.dumps(diagnostic) + str(logs.output))
        self.assertEqual(self.broker.submissions, [])

    async def test_expired_cache_reason_survives_second_lookup(self):
        await self.warm()
        calls = 0

        def unavailable(contract, price, *, diagnostic):
            nonlocal calls
            calls += 1
            diagnostic.update(route="fresh", reason="prepared_cache_expired" if calls == 1 else "prepared_cache_missing")
            return None

        self.broker.prepared_entry = unavailable
        entry = self.message()
        entry["source_group"] = self.config["channels"][0]["source_group"]
        self.store.observe(entry)
        decision = copy.deepcopy(self.interpreter.decision)
        decision.update(
            origin_message_id=entry["id"],
            contract=fixtures.CONTRACT | {"expiry": "nearest"},
            evidence=[{"message_id": entry["id"], "quote": entry["content"]}],
        )
        resolved = await self.engine.resolve_expiry(entry, decision)
        order = await self.engine.plan(entry, resolved)
        self.assertNotIn("prepared_entry", order)
        self.assertEqual(resolved["entry_preparation"]["reason"], "prepared_cache_expired")

    async def test_prepared_plan_sizes_at_downward_rounded_cap_without_quote(self):
        await self.warm()
        entry = self.message()
        entry["source_group"] = self.config["channels"][0]["source_group"]
        self.store.observe(entry)
        self.broker.quote = AsyncMock(side_effect=AssertionError("standalone quote on prepared path"))
        decision = copy.deepcopy(self.interpreter.decision)
        decision["origin_message_id"] = entry["id"]
        order = await self.engine.plan(entry, decision)
        self.assertEqual(Decimal(order["limit_price"]), Decimal(".95"))
        self.assertTrue(order["prepared_entry"])
        self.assertEqual(decision["entry_preparation"]["route"], "prepared")
        self.assertEqual(order["entry_cancel_after_seconds"], 3)
        self.assertIsNone(order["quote_timestamp"])
        self.assertLessEqual(Decimal(order["limit_price"]), Decimal(".8") * Decimal("1.2"))
        self.broker.quote.assert_not_awaited()

    async def test_watch_edits_other_group_and_explicit_expiry_do_not_match(self):
        watch = await self.warm()
        decision = self.interpreter.decision | {"contract": fixtures.CONTRACT | {"expiry": "nearest"}}
        self.assertIsNone(self.engine.watches.match(self.message(channel=1), decision))
        self.engine.observations[watch["id"]] = "changed"
        self.assertIsNone(self.engine.watches.match(self.message(), decision))
        self.engine.watches.entries.clear()
        await self.warm("On watch BAC $63C 9/18")
        self.assertIsNone(self.engine.watches.match(self.message(), decision))

    async def test_preparation_failure_and_stale_metadata_fall_back(self):
        self.broker.prewarm_entry.side_effect = RuntimeError("offline")
        await self.warm()
        self.assertIsNone(self.engine.watches.match(self.message(), self.interpreter.decision))
        self.broker.prewarm_entry.side_effect = None
        await self.warm()
        self.broker.prepared_entry = lambda contract, price, **kwargs: None
        self.broker.quote = AsyncMock(wraps=self.broker.quote)
        entry = self.message()
        entry["source_group"] = self.config["channels"][0]["source_group"]
        self.store.observe(entry)
        order = await self.engine.plan(entry, self.interpreter.decision)
        self.assertNotIn("prepared_entry", order)
        self.broker.quote.assert_awaited_once()

    async def test_review_can_observe_ask_above_limit_without_raising_price(self):
        await self.warm()
        entry = self.message()
        entry["source_group"] = self.config["channels"][0]["source_group"]
        self.store.observe(entry)
        decision = copy.deepcopy(self.interpreter.decision)
        decision.update(origin_message_id=entry["id"], evidence=[{"message_id": entry["id"], "quote": entry["content"]}])
        order = await self.engine.plan(entry, decision)
        quote = await self.broker.quote(fixtures.CONTRACT)
        quote.update(bid="1.00", ask="1.05")
        await self.engine.verify_dispatch(entry, decision, order, self.broker.account, quote)
        self.assertEqual(Decimal(order["limit_price"]), Decimal(".95"))
        self.assertEqual(decision["entry_evaluation"]["ask"], "1.05")

    async def test_prepared_entry_reaches_final_guard_and_records_fill(self):
        await self.warm()
        self.config["require_source_verification"] = True
        self.engine.verify_current = AsyncMock(return_value=True)
        self.broker.result = dict(id="fixture-broker-order", status="filled", filled_quantity=1, fill_price=".80")
        self.config["risk"]["buying_power_reserve_fraction"] = "0"
        # Fixture sizing uses its configured entry cap; pin the buying power to one contract.
        self.broker.account["buying_power"] = "100"
        result = await self.engine.handle(self.message())
        self.assertEqual(result["state"], "broker_order", result)
        self.assertEqual(len(self.broker.submissions), 1)
        self.assertTrue(self.broker.submissions[0]["prepared_entry"])
        self.engine.verify_current.assert_awaited_once()
        self.assertEqual(self.store.positions()[0]["quantity"], 1)

    async def test_expired_entry_window_cannot_submit_after_slow_review(self):
        await self.warm()
        message = self.message()
        message["source_group"] = self.config["channels"][0]["source_group"]
        self.store.observe(message)
        decision = self.interpreter.decision | {"origin_message_id": message["id"],
                                                  "evidence": [{"message_id": message["id"], "quote": message["content"]}]}
        order = await self.engine.plan(message, decision)
        order["entry_cancel_at"] = (self.now - timedelta(seconds=1)).isoformat()
        with self.assertRaisesRegex(Hold, "validity window"):
            await self.engine.verify_dispatch(message, decision, order, self.broker.account,
                                              await self.broker.quote(fixtures.CONTRACT))

    async def test_changed_watch_cannot_dispatch_an_already_planned_entry(self):
        watch = await self.warm()
        message = self.message()
        message["source_group"] = self.config["channels"][0]["source_group"]
        self.store.observe(message)
        decision = self.interpreter.decision | {"origin_message_id": message["id"],
                                                  "evidence": [{"message_id": message["id"], "quote": message["content"]}]}
        order = await self.engine.plan(message, decision)
        self.store.reserve(message, decision, order, self.now)
        self.engine.observations[watch["id"]] = "changed"
        with self.assertRaisesRegex(Hold, "prepared watch"):
            await self.engine.verify_dispatch(message, decision, order, self.broker.account,
                                              await self.broker.quote(fixtures.CONTRACT))


if __name__ == "__main__":
    unittest.main()
