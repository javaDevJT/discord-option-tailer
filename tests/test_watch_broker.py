"""Provider-shaped prepared entry and lost-response recovery; no network calls."""
import copy
from datetime import timedelta
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock

from relay.broker import BrokerError, BrokerPreflightHold
from relay.core import Engine, Store, canonical_contract
from relay.ingest import normalize
from relay.recovery import RecoveryEvaluator
from tests import test_broker_live as broker_fixtures
from tests.test_core import Broker, Interpreter


class WatchBrokerChecks(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        broker_fixtures.LiveBrokerChecks.setUp(self)
        original = self.broker._data.side_effect

        async def data(name, args):
            result = await original(name, args)
            if name == "review_option_order":
                result["option_quotes"] = [copy.deepcopy(self.quote)]
            return result

        self.broker._data.side_effect = data

    def raw_order(self, **changes):
        raw = dict(id="22222222-2222-4222-8222-222222222222", chain_symbol="SPY",
                   legs=[dict(option_id=self.instrument["id"], side="buy", position_effect="open",
                              ratio_quantity=1, expiration_date=self.contract["expiry"],
                              strike_price=self.contract["strike"], option_type="call")],
                   trade_value_multiplier="100", type="limit", trigger="immediate", direction="debit",
                   quantity="1", price="1.00", processed_quantity="1", processed_premium="100.00",
                   state="filled", placed_agent="agentic", time_in_force="gfd", market_hours="regular_hours",
                   created_at=self.now.isoformat(), updated_at=self.now.isoformat())
        return raw | changes

    async def test_prepared_submit_uses_review_quote_without_separate_quote(self):
        await self.broker.prewarm_entry(self.contract)
        self.calls.clear()
        self.broker.quote = AsyncMock(side_effect=AssertionError("separate quote"))
        self.broker.snapshot = AsyncMock(return_value=dict(
            account_id="TEST0001", market_open=True, restrictions=[], buying_power="1000", positions=[],
            timestamp=self.now.isoformat()))
        before = AsyncMock()
        result = await self.broker.submit(self.order | {"prepared_entry": True}, before_submit=before)
        self.assertEqual(result["status"], "filled")
        self.assertEqual([name for name, _ in self.calls], ["review_option_order", "place_option_order"])
        self.broker.quote.assert_not_awaited()
        before.assert_awaited_once()
        self.assertEqual(before.await_args.args[1]["ask"], "1.00")

    async def test_metadata_reports_tick_for_unrounded_ceiling_and_expires(self):
        self.instrument["min_ticks"]["below_tick"] = ".05"
        await self.broker.prewarm_entry(self.contract)
        metadata = self.broker.prepared_entry(self.contract, ".96")
        self.assertIsNotNone(metadata)
        self.assertEqual(metadata["tick_size"], "0.05")
        self.now += timedelta(seconds=61)
        self.assertIsNone(self.broker.prepared_entry(self.contract, ".95"))

    async def test_zero_cutoff_tick_schedule_can_be_prepared(self):
        # Captured Robinhood option metadata uses a zero cutoff for penny ticks.
        self.instrument["min_ticks"] = {
            "above_tick": "0.01", "below_tick": "0.01", "cutoff_price": "0.00",
        }
        quote = await self.broker.quote(self.contract)
        self.assertEqual(quote["tick_size"], "0.01")
        await self.broker.prewarm_entry(self.contract)
        prepared = self.broker.prepared_entry(self.contract, "1.40")
        self.assertEqual(prepared["tick_size"], "0.01")

    async def test_invalid_tick_schedules_remain_rejected(self):
        for field, value in (("above_tick", "0"), ("below_tick", "-0.01"), ("cutoff_price", "-1")):
            with self.subTest(field=field):
                self.instrument["min_ticks"] = {
                    "above_tick": "0.01", "below_tick": "0.01", "cutoff_price": "0.00",
                } | {field: value}
                with self.assertRaises(BrokerError):
                    await self.broker.prewarm_entry(self.contract)

    async def test_prepared_cache_explains_unavailable_and_ready_states(self):
        diagnostic = {}
        self.assertIsNone(self.broker.prepared_entry(self.contract, "1", diagnostic=diagnostic))
        self.assertEqual(diagnostic["reason"], "prepared_cache_missing")
        await self.broker.prewarm_entry(self.contract)
        self.assertIsNotNone(self.broker.prepared_entry(self.contract, "1", diagnostic=diagnostic))
        self.assertEqual(diagnostic["route"], "prepared")
        self.assertEqual(diagnostic["prepared_age_seconds"], 0)
        self.now += timedelta(seconds=61)
        self.assertIsNone(self.broker.prepared_entry(self.contract, "1", diagnostic=diagnostic))
        self.assertEqual(diagnostic["reason"], "prepared_cache_expired")
        self.assertEqual(diagnostic["route"], "fresh")

    async def test_review_quote_invalidity_prevents_placement(self):
        await self.broker.prewarm_entry(self.contract)
        self.broker.snapshot = AsyncMock(return_value=dict(
            account_id="TEST0001", market_open=True, restrictions=[], buying_power="1000", positions=[],
            timestamp=self.now.isoformat()))
        for change in ({"updated_at": (self.now - timedelta(seconds=60)).isoformat()},
                       {"bid_price": ".10"}, {"instrument_id": "wrong-contract"}):
            with self.subTest(change=change):
                self.calls.clear()
                saved = copy.deepcopy(self.quote)
                self.quote.update(change)
                order = self.order | {"prepared_entry": True, "client_order_id": str(change)}
                with self.assertRaises((BrokerError, BrokerPreflightHold)):
                    await self.broker.submit(order, before_submit=AsyncMock())
                self.assertNotIn("place_option_order", [name for name, _ in self.calls])
                self.quote = saved

    async def test_unknown_without_uuid_or_echoed_ref_matches_exact_agentic_order(self):
        client = hashlib.sha256(b"lost-response").hexdigest()
        expected = self.order | {"client_order_id": client, "created_at": self.now.isoformat()}
        self.orders[:] = [self.raw_order()]
        result = await self.broker.order_status(client, expected_order=expected)
        self.assertEqual(result["filled_quantity"], 1)
        self.assertEqual(result["fill_price"], "1.00")
        self.assertEqual(result["id"], self.orders[0]["id"])
        self.assertNotIn("place_option_order", [name for name, _ in self.calls])

    async def test_missing_ambiguous_manual_wrong_contract_and_old_orders_remain_unknown(self):
        client = hashlib.sha256(b"ambiguous-response").hexdigest()
        expected = self.order | {"client_order_id": client, "created_at": self.now.isoformat()}
        different = self.raw_order()
        different["legs"][0]["strike_price"] = "501"
        unidentified = self.raw_order(chain_symbol=None)
        for key in ("expiration_date", "strike_price", "option_type"):
            unidentified["legs"][0].pop(key)
        cases = [[], [self.raw_order(), self.raw_order(id="33333333-3333-4333-8333-333333333333")],
                 [self.raw_order(placed_agent="user")], [self.raw_order(placed_agent=None)],
                 [different], [unidentified], [self.raw_order(time_in_force="gtc")],
                 [self.raw_order(market_hours="all_day")],
                 [self.raw_order(created_at=(self.now - timedelta(days=1)).isoformat())]]
        for rows in cases:
            with self.subTest(rows=len(rows), first=rows[0] if rows else None):
                self.orders[:] = rows
                with self.assertRaises(BrokerError):
                    await self.broker.order_status(client, expected_order=expected)

    async def test_restart_window_is_anchored_to_attempt_and_excludes_later_same_order(self):
        anchor = self.now
        client = hashlib.sha256(b"late-lookalike").hexdigest()
        expected = self.order | {"client_order_id": client, "created_at": anchor.isoformat()}
        self.now += timedelta(days=1)
        later = self.raw_order(id="33333333-3333-4333-8333-333333333333",
                               created_at=(anchor + timedelta(minutes=5)).isoformat())
        self.orders[:] = [later]
        with self.assertRaises(BrokerError):
            await self.broker.order_status(client, expected_order=expected)
        correct = self.raw_order(created_at=anchor.isoformat())
        self.orders[:] = [later, correct]
        result = await self.broker.order_status(client, expected_order=expected)
        self.assertEqual(result["id"], correct["id"])

    async def test_lost_buy_response_restart_restores_ownership_then_later_sell(self):
        with tempfile.TemporaryDirectory() as directory:
            config = json.loads((Path(__file__).parents[1] / "config.example.json").read_text())
            config.update(mode="live", database=str(Path(directory) / "ledger.sqlite3"), kill_switch=str(Path(directory) / "STOP"))
            config["robinhood"].update(account_number="00012345", enable_live_orders=True)
            self.broker.account_number = "00012345"
            channel = config["channels"][0]
            author = (channel.get("authors") or ["1545000000000000000"])[0]
            message = normalize(dict(id="1548000000000000000", channel_id=channel["id"], author_id=author,
                                     timestamp=self.now.isoformat(), source="browser", content="OPEN SPY 500 call 9/11 @ 1.00"))
            message.update(ingestion="live", source_group=channel["source_group"])
            client = hashlib.sha256((message["id"] + ":" + message["revision"]).encode()).hexdigest()
            order = self.order | {"client_order_id": client, "origin_message_id": message["id"]}
            decision = dict(action="OPEN", contract=self.contract, origin_message_id=message["id"],
                            evidence=[{"message_id": message["id"], "quote": message["content"]}])
            store = Store(config["database"])
            store.bind_execution("live", "00012345")
            store.observe(message)
            store.reserve(message, decision, order, self.now)
            store.close()
            self.orders[:] = [self.raw_order()]
            store = Store(config["database"])
            try:
                engine = Engine(config, store, None, self.broker, lambda: self.now)
                recovery = RecoveryEvaluator(engine)
                await recovery.reconcile_orders(force=True)
                await recovery.reconcile_orders(force=True)
                self.assertEqual(store.unresolved(), 0)
                self.assertEqual(store.positions()[0]["quantity"], 1)
                self.assertEqual(store.positions()[0]["source_group"], channel["source_group"])
                self.assertEqual(store.positions()[0]["contract"], canonical_contract(self.contract))
                self.assertEqual(len([name for name, _ in self.calls if name == "place_option_order"]), 0)
                self.now += timedelta(seconds=1)
                interpreter = Interpreter()
                interpreter.decision.update(action="CLOSE", contract=self.contract, alert_price=None)
                engine.interpreter = interpreter
                fake = Broker()
                fake.account.update(account_id="00012345", timestamp=self.now.isoformat())
                fake.quotes["timestamp"] = self.now.isoformat()
                fake.result = dict(id="later-sell", status="filled", filled_quantity=1, fill_price=".77")
                engine.broker = fake
                sell = normalize(dict(id="1548000000000000001", channel_id=channel["id"], author_id=author,
                                      timestamp=self.now.isoformat(), source="browser", content="CLOSE SPY 500 call 9/11"))
                sell["ingestion"] = "live"
                result = await engine.handle(sell)
                self.assertEqual(result["state"], "broker_order", result)
                self.assertEqual(store.positions(), [])
            finally:
                store.close()


if __name__ == "__main__":
    unittest.main()
