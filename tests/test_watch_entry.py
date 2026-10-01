"""Watch preparation is advisory; only a fresh matched ENTRY can spend."""
import asyncio
import copy
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
import json
import sqlite3
import time
import unittest
from unittest.mock import AsyncMock, Mock, patch

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

    async def test_hot_poll_window_uses_notice_time_and_does_not_extend_on_duplicate(self):
        watch = await self.warm()
        first = self.engine.watches.entries[watch["id"]]["hot_until"]
        self.now += timedelta(seconds=120)
        # A delayed repeat of the same event cannot restart its ten-minute budget.
        self.engine.fresh = lambda *_: None
        self.engine.watches.note(watch)
        await asyncio.gather(*list(self.engine.watches.tasks))
        entry = self.engine.watches.entries[watch["id"]]
        self.assertAlmostEqual(first - entry["hot_until"], 120, delta=1)
        entry["hot_until"] = time.monotonic() - 1
        self.broker.refresh_watch_market = AsyncMock()
        self.assertIsNone(self.engine.watches._next_market_entry())
        await self.engine.watches._refresh_market(entry)
        self.broker.refresh_watch_market.assert_not_awaited()

    async def test_market_poll_rotation_deduplicates_contracts_and_records_failure(self):
        first = await self.warm("On watch BAC $63C 9/18")
        second = await self.warm("On watch QQQ $740P 9/18")
        duplicate = await self.warm("On watch BAC $63C 9/18")
        entries = self.engine.watches.entries
        entries[first["id"]]["market_polled_at"] = 10
        entries[duplicate["id"]]["market_polled_at"] = 0
        entries[second["id"]]["market_polled_at"] = 5
        selected = self.engine.watches._next_market_entry()
        self.assertIs(selected, entries[second["id"]])
        self.broker.refresh_watch_market = AsyncMock(side_effect=RuntimeError("offline"))
        await self.engine.watches._refresh_market(selected)
        self.assertEqual(selected["market_diagnostic"]["state"], "failed")
        self.assertGreater(selected["market_polled_at"], 10)
        self.assertIs(self.engine.watches._next_market_entry(), entries[duplicate["id"]])
        self.assertEqual(self.broker.submissions, [])

    async def test_slow_refresh_yields_to_another_contract_before_watch_window_ends(self):
        first = await self.warm("On watch BAC $63C 9/18")
        second = await self.warm("On watch QQQ $740P 9/18")
        entry = self.engine.watches.entries[first["id"]]

        async def pending_read(_contract):
            await asyncio.Event().wait()

        self.broker.refresh_watch_market = pending_read
        with patch("relay.watch.WATCH_REFRESH_TIMEOUT_SECONDS", .02):
            await self.engine.watches._refresh_market(entry)
        self.assertEqual(entry["market_diagnostic"]["state"], "failed")
        self.assertGreater(entry["hot_until"], time.monotonic())
        self.assertIs(self.engine.watches._next_market_entry(), self.engine.watches.entries[second["id"]])

    async def test_market_poll_cancels_pending_read_at_window_end(self):
        watch = await self.warm()
        entry = self.engine.watches.entries[watch["id"]]
        entry["hot_until"] = time.monotonic() + .02
        cancelled = asyncio.Event()

        async def pending_read(_contract):
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

        self.broker.refresh_watch_market = pending_read
        await self.engine.watches._refresh_market(entry)
        self.assertTrue(cancelled.is_set())
        self.assertEqual(entry["market_diagnostic"]["state"], "failed")
        self.assertIsNone(self.engine.watches._next_market_entry())

    async def test_watch_snapshot_only_bypasses_rpc_for_current_rules_entry(self):
        watch = await self.warm()
        message = self.message()
        message["source_group"] = self.config["channels"][0]["source_group"]
        self.store.observe(message)
        decision = copy.deepcopy(self.interpreter.decision)
        decision["evaluation_timing"] = {"evaluator": "rules"}
        self.broker.snapshot = AsyncMock(return_value=self.broker.account)
        self.broker.watch_entry_snapshot = Mock(return_value=copy.deepcopy(self.broker.account))
        result = await self.engine.entry_snapshot(message, decision)
        self.assertEqual(result, self.broker.account)
        self.broker.snapshot.assert_not_awaited()
        self.broker.watch_entry_snapshot.assert_called_once()
        decision["evaluation_timing"]["evaluator"] = "codex"
        await self.engine.entry_snapshot(message, decision)
        self.broker.snapshot.assert_awaited_once()
        decision["evaluation_timing"]["evaluator"] = "rules"
        self.engine.observations[watch["id"]] = "changed"
        await self.engine.entry_snapshot(message, decision)
        self.assertEqual(self.broker.snapshot.await_count, 2)
        self.broker.watch_entry_snapshot.assert_called_once()

    async def test_missing_watch_snapshot_falls_back_and_explicit_expiry_plan_uses_cache(self):
        await self.warm("On watch BAC $63C 9/18")
        message = self.message()
        message["source_group"] = self.config["channels"][0]["source_group"]
        self.store.observe(message)
        decision = copy.deepcopy(self.interpreter.decision)
        decision["origin_message_id"] = message["id"]
        decision["evaluation_timing"] = {"evaluator": "rules"}
        self.broker.snapshot = AsyncMock(return_value=self.broker.account)
        self.broker.watch_entry_snapshot = Mock(return_value=None)
        await self.engine.entry_snapshot(message, decision)
        self.broker.snapshot.assert_awaited_once()
        self.broker.snapshot.reset_mock()
        self.broker.watch_entry_snapshot.return_value = copy.deepcopy(self.broker.account)
        order = await self.engine.plan(message, decision)
        self.assertTrue(order["prepared_entry"])
        self.broker.snapshot.assert_not_awaited()

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

    async def test_prepared_entry_deadline_starts_after_slow_review_and_is_persisted_before_dispatch(self):
        await self.warm()
        self.config["require_source_verification"] = True
        self.engine.verify_current = AsyncMock(return_value=True)
        self.config["risk"]["buying_power_reserve_fraction"] = "0"
        self.broker.account["buying_power"] = "100"
        self.broker.result = dict(id="fixture-slow-review", status="filled", filled_quantity=1, fill_price=".80")
        message = self.message()
        captured = {}

        async def slow_review():
            row = self.store.db.execute("SELECT body FROM orders WHERE message_id=?", (message["id"],)).fetchone()
            self.assertIsNotNone(row)
            self.assertNotIn("entry_cancel_at", json.loads(row["body"]))
            self.now += timedelta(seconds=4)

        self.broker.review_hook = slow_review
        original_submit = self.broker.submit

        async def capture_dispatch(order, before_submit=None):
            async def capture_after_guard(snapshot, quote):
                await before_submit(snapshot, quote)
                row = self.store.db.execute("SELECT body FROM orders WHERE id=?", (order["client_order_id"],)).fetchone()
                captured["body"] = json.loads(row["body"])

            return await original_submit(order, before_submit=capture_after_guard)

        self.broker.submit = capture_dispatch
        result = await self.engine.handle(message)
        self.assertEqual(result["state"], "broker_order", result)
        self.engine.verify_current.assert_awaited_once()
        expected_deadline = (self.now + timedelta(seconds=3)).isoformat()
        self.assertEqual(captured["body"]["entry_cancel_at"], expected_deadline)
        self.assertEqual(self.broker.submissions[0]["entry_cancel_at"], expected_deadline)

    async def test_unprepared_entry_dispatch_trace_is_durable_before_placement(self):
        self.config["require_source_verification"] = True
        self.engine.verify_current = AsyncMock(return_value=True)
        self.config["risk"]["buying_power_reserve_fraction"] = "0"
        self.broker.account["buying_power"] = "100"
        self.broker.error = RuntimeError("simulated response lost")
        original_submit = self.broker.submit
        captured = {}

        async def capture_dispatch(order, before_submit=None):
            self.assertFalse(order.get("prepared_entry"))
            order["broker_submission"].update(stage="dispatch", review_seconds=0.2)

            async def capture_after_guard(snapshot, quote):
                await before_submit(snapshot, quote)
                restarted = sqlite3.connect((Path(self.temp.name) / "live-watch.sqlite3").as_uri() + "?mode=ro", uri=True)
                restarted.row_factory = sqlite3.Row
                try:
                    row = restarted.execute("SELECT body,status FROM orders WHERE id=?", (order["client_order_id"],)).fetchone()
                    captured.update(body=json.loads(row["body"]), status=row["status"])
                finally:
                    restarted.close()

            return await original_submit(order, before_submit=capture_after_guard)

        self.broker.submit = capture_dispatch
        result = await self.engine.handle(self.message())
        self.assertEqual(result["state"], "unknown", result)
        self.assertEqual(captured["status"], "submitting")
        self.assertEqual(captured["body"]["broker_submission"], {"stage": "dispatch", "review_seconds": 0.2})
        self.assertNotIn("entry_cancel_at", captured["body"])

    async def test_snapshot_stage_timings_survive_later_submission_failure(self):
        await self.warm()
        self.config["require_source_verification"] = True
        self.engine.verify_current = AsyncMock(return_value=True)
        self.config["risk"]["buying_power_reserve_fraction"] = "0"
        self.broker.account["buying_power"] = "100"
        self.broker.account["read_timing"] = {
            "account": .1234567,
            "portfolio": .2,
            "positions": .3,
            "position_details": .4,
            "orders": .5,
            "ignored": .6,
            "account_invalid": float("inf"),
            "portfolio_invalid": True,
            "orders_unbounded": 3601,
        }
        self.broker.error = RuntimeError("fixture response lost")
        message = self.message()
        result = await self.engine.handle(message)
        self.assertEqual(result["state"], "unknown", result)
        row = self.store.db.execute(
            "SELECT decision FROM events WHERE message_id=? ORDER BY id DESC LIMIT 1", (message["id"],)
        ).fetchone()
        timing = json.loads(row["decision"])["evaluation_timing"]
        self.assertEqual(timing["snapshot_account_seconds"], .123457)
        self.assertEqual(timing["snapshot_portfolio_seconds"], .2)
        self.assertEqual(timing["snapshot_positions_seconds"], .3)
        self.assertEqual(timing["snapshot_position_details_seconds"], .4)
        self.assertEqual(timing["snapshot_orders_seconds"], .5)
        self.assertNotIn("ignored", timing)
        self.assertNotIn("snapshot_account_invalid_seconds", timing)
        self.assertNotIn("snapshot_portfolio_invalid_seconds", timing)
        self.assertNotIn("snapshot_orders_unbounded_seconds", timing)

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
