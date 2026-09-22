"""Offline stop lifecycle checks; no provider requests or real orders."""
import copy
import unittest
from unittest.mock import AsyncMock, patch

import test_core as fixtures
from test_core import Broker, CONTRACT, NOW
from relay.core import Engine, Hold, contract_key
from relay.broker import BrokerPreflightHold


class StopBroker(Broker):
    def __init__(self):
        super().__init__()
        self.results, self.cancel_calls = {}, []
        self.cancel_state = "canceled"
        self.pending_trim = False

    async def quote(self, contract):
        return dict(await super().quote(contract), bid="0.95", ask="1.00")

    async def submit(self, order, before_submit=None):
        if before_submit:
            try:
                if self.review_hook:
                    self.review_hook()
                await before_submit(await self.snapshot(), await self.quote(order["contract"]))
            except Exception as exc:
                raise BrokerPreflightHold("fixture preflight rejection") from exc
        self.submissions.append(copy.deepcopy(order))
        resting = order.get("order_type") == "stop_market" or self.pending_trim and order.get("order_type", "limit") == "limit"
        result = {"id": "broker-" + order["client_order_id"], "status": "open" if resting else "filled",
                  "filled_quantity": 0 if resting else order["quantity"], "fill_price": None if resting else "0.95"}
        self.results[order["client_order_id"]] = result
        return result

    async def order_status(self, order_id, **kwargs):
        return self.results[order_id]

    async def cancel_order(self, order_id, **kwargs):
        self.cancel_calls.append(order_id)
        result = dict(self.results[order_id], status=self.cancel_state)
        if self.cancel_state == "filled":
            order = next(order for order in self.submissions if order["client_order_id"] == order_id)
            result.update(filled_quantity=order["quantity"], fill_price="0.75")
        self.results[order_id] = result
        return result


class StopChecks(unittest.IsolatedAsyncioTestCase):
    setUp = fixtures.CoreChecks.setUp
    message = fixtures.CoreChecks.message

    def seed(self, quantity=2, group="source-a"):
        self.broker = StopBroker()
        self.engine.broker = self.broker
        message = self.message() | {"source_group": group}
        self.store.observe(message)
        order = {"client_order_id": "seed-" + group, "contract": CONTRACT, "quantity": quantity,
                 "side": "buy", "position_effect": "open", "limit_price": "0.80"}
        self.store.reserve(message, {"action": "OPEN"}, order, NOW)
        self.store.apply_result(order["client_order_id"], {"id": "seed-fill-" + group, "status": "filled", "filled_quantity": quantity, "fill_price": "0.80"})

    async def signal(self, action="UPDATE_STOP", stop="breakeven", **values):
        self.interpreter.decision.update(dict(action=action, stop_price=stop, fraction=None, quantity=None) | values)
        return await self.engine.handle(self.message(content="Took half here - S/L BE"))

    def request(self):
        return self.store.db.execute("SELECT * FROM stop_requests").fetchone()

    def live_fixture(self):
        self.config["mode"] = self.engine.mode = "live"
        self.config["robinhood"]["account_number"] = self.engine.account = "12345678"
        self.config["robinhood"]["enable_live_orders"] = True
        self.broker.account["account_id"] = self.engine.account
        self.config["require_source_verification"] = True
        self.engine.verify_current = AsyncMock(return_value=True)

    async def test_trim_half_then_native_breakeven_on_exact_remaining_quantity(self):
        self.seed(2)
        result = await self.signal("REDUCE", fraction=.5)
        self.assertEqual(result["state"], "paper_order", result)
        self.assertEqual([o["quantity"] for o in self.broker.submissions], [1, 1])
        stop = self.broker.submissions[-1]
        self.assertEqual((stop["order_type"], stop["stop_price"], stop["time_in_force"]), ("stop_market", "0.80", "gtc"))
        self.assertEqual(self.request()["status"], "active")
        self.assertEqual(self.store.unresolved(), 0)

    async def test_one_contract_trim_leaves_nothing_to_protect(self):
        self.seed(1)
        result = await self.signal("REDUCE", fraction=.5)
        self.assertEqual(result["state"], "paper_order", result)
        self.assertEqual(len(self.broker.submissions), 1)
        self.assertIn("No remaining", result["reason"])
        self.assertFalse(self.store.positions())

    async def test_replacement_cancels_old_stop_before_new_one(self):
        self.seed()
        await self.signal()
        old_id = self.broker.submissions[-1]["client_order_id"]
        result = await self.signal(stop="0.85")
        self.assertEqual(result["state"], "paper_order", result)
        self.assertEqual(self.broker.cancel_calls, [old_id])
        self.assertEqual(self.broker.submissions[-1]["stop_price"], "0.85")
        self.assertEqual(len(self.engine.stops.active_orders("source-a", contract_key(CONTRACT))), 1)

    async def test_full_exit_cancels_stop_and_does_not_rearm(self):
        self.seed()
        await self.signal()
        result = await self.signal("CLOSE", stop=None)
        self.assertEqual(result["state"], "paper_order", result)
        self.assertEqual(len(self.broker.cancel_calls), 1)
        self.assertEqual(len(self.broker.submissions), 2)
        self.assertFalse(self.store.positions())
        self.assertEqual(self.request()["status"], "complete")

    async def test_cancel_ack_does_not_allow_competing_sale(self):
        self.seed()
        await self.signal()
        self.broker.cancel_state = "open"
        with patch("relay.stops.asyncio.sleep", new=AsyncMock()):
            result = await self.signal("CLOSE", stop=None)
        self.assertEqual(result["state"], "held", result)
        self.assertEqual(len(self.broker.submissions), 1)
        self.assertEqual(self.store.positions()[0]["quantity"], 2)

    async def test_racing_stop_fill_never_sells_twice(self):
        self.seed()
        await self.signal()
        self.broker.cancel_state = "filled"
        result = await self.signal("CLOSE", stop=None)
        self.assertEqual(result["state"], "held", result)
        self.assertFalse(self.store.positions())
        self.assertEqual(len(self.broker.submissions), 1)

    async def test_market_stop_fill_can_slip_below_trigger(self):
        self.seed()
        await self.signal()
        order = self.broker.submissions[-1]
        result = dict(self.broker.results[order["client_order_id"]], status="filled", filled_quantity=2, fill_price="0.70")
        self.store.apply_result(order["client_order_id"], result)
        self.store.apply_result(order["client_order_id"], result)
        await self.engine.stops.maintain()
        self.assertFalse(self.store.positions())
        self.assertEqual(self.request()["status"], "complete")
        self.assertEqual(len(self.broker.submissions), 1)

    async def test_pending_trim_waits_for_fill_before_protecting_remainder(self):
        self.seed()
        self.broker.pending_trim = True
        result = await self.signal("REDUCE", fraction=.5)
        self.assertEqual(result["state"], "paper_order", result)
        self.assertEqual(len(self.broker.submissions), 1)
        self.assertEqual(self.request()["status"], "pending")
        order = self.broker.submissions[0]
        self.store.apply_result(order["client_order_id"], dict(self.broker.results[order["client_order_id"]], status="filled", filled_quantity=1, fill_price="0.95"))
        await self.engine.stops.maintain()
        self.assertEqual(self.broker.submissions[-1]["quantity"], 1)
        self.assertEqual(self.request()["status"], "active")

    async def test_restart_reuses_active_stop_without_duplicate_submission(self):
        self.seed()
        await self.signal()
        engine = Engine(self.config, self.store, self.interpreter, self.broker, clock=lambda: self.now)
        await engine.stops.maintain()
        self.assertEqual(len(self.broker.submissions), 1)
        self.assertEqual(self.request()["status"], "active")

    async def test_breached_stop_closes_remainder_instead_of_invalid_above_market_stop(self):
        self.seed()
        result = await self.signal(stop="1.10")
        self.assertEqual(result["state"], "paper_order", result)
        self.assertEqual(self.broker.submissions[0]["order_type"], "market")
        self.assertNotIn("stop_price", self.broker.submissions[0])
        self.assertFalse(self.store.positions())

    async def test_shadow_proposes_stop_without_persisting_execution_intent(self):
        self.seed()
        self.config["mode"] = self.engine.mode = "shadow"
        self.config["robinhood"]["account_number"] = self.engine.account = "12345678"
        result = await self.signal()
        self.assertEqual(result["state"], "shadow_order", result)
        self.assertFalse(self.broker.submissions)
        self.assertIsNone(self.request())

    async def test_unowned_stop_does_not_submit(self):
        self.broker = StopBroker()
        self.engine.broker = self.broker
        result = await self.signal()
        self.assertEqual(result["state"], "held", result)
        self.assertFalse(self.broker.submissions)

    async def test_unknown_native_submission_blocks_new_orders(self):
        self.seed()
        self.broker.submit = AsyncMock(side_effect=TimeoutError("fixture"))
        result = await self.signal()
        self.assertEqual(result["state"], "held", result)
        self.assertEqual(self.store.unresolved(), 1)
        await self.engine.stops.maintain()
        self.assertEqual(self.broker.submit.await_count, 1)

    async def test_durable_compound_intent_survives_crash_before_followup(self):
        self.seed()
        message = self.message() | {"source_group": "source-a"}
        self.store.observe(message)
        decision = self.interpreter.decision | {"action": "REDUCE", "fraction": .5, "stop_price": "breakeven"}
        order = {"client_order_id": "crashed-trim", "contract": CONTRACT, "quantity": 1,
                 "side": "sell", "position_effect": "close", "limit_price": "0.95"}
        self.store.reserve(message, decision, order, NOW)
        self.store.apply_result(order["client_order_id"], {"id": "trim-fill", "status": "filled", "filled_quantity": 1, "fill_price": "0.95"})
        self.assertEqual(self.request()["status"], "pending")
        self.assertEqual(self.store.positions()[0]["quantity"], 1)
        engine = Engine(self.config, self.store, self.interpreter, self.broker, clock=lambda: self.now)
        await engine.stops.maintain()
        self.assertEqual(len(self.broker.submissions), 1)
        self.assertEqual(self.broker.submissions[-1]["quantity"], 1)
        self.assertEqual(self.request()["status"], "active")

    async def test_partial_native_fill_keeps_only_remaining_contracts_protected(self):
        self.seed(3)
        await self.signal()
        order = self.broker.submissions[-1]
        self.store.apply_result(order["client_order_id"], dict(self.broker.results[order["client_order_id"]], status="partially_filled", filled_quantity=1, fill_price="0.75"))
        await self.engine.stops.maintain()
        self.assertEqual(len(self.broker.submissions), 1)
        self.assertEqual(self.store.positions()[0]["quantity"], 2)
        self.assertEqual(self.request()["status"], "active")

    async def test_kill_switch_keeps_existing_stop_and_prevents_changes(self):
        from pathlib import Path
        self.seed()
        await self.signal()
        Path(self.config["kill_switch"]).touch()
        result = await self.signal(stop="0.85")
        self.assertEqual(result["state"], "held", result)
        self.assertEqual(len(self.broker.submissions), 1)
        self.assertFalse(self.broker.cancel_calls)

    async def test_tick_is_selected_for_stop_level_not_current_ask(self):
        self.seed()
        quote = await self.broker.quote(CONTRACT)
        self.broker.quote = AsyncMock(return_value=quote | {"bid": "3.10", "ask": "3.15", "tick_size": "0.05",
            "min_ticks": {"cutoff_price": "3", "above_tick": "0.05", "below_tick": "0.01"}})
        await self.signal(stop="1.44")
        self.assertEqual(self.broker.submissions[-1]["stop_price"], "1.44")

    async def test_pending_triggered_market_sale_stays_unresolved(self):
        self.seed()
        original = self.broker.submit
        async def pending(order, **kwargs):
            result = await original(order, **kwargs)
            return dict(result, status="open", filled_quantity=0, fill_price=None)
        self.broker.submit = pending
        await self.signal(stop="1.10")
        self.assertEqual(self.store.unresolved(), 1)
        self.assertEqual(self.request()["status"], "pending")

    async def test_expiry_exit_cancels_native_stop_before_liquidation(self):
        self.seed()
        await self.signal()
        message = self.message() | {"source_group": "source-a"}
        self.store.observe(message)
        decision = copy.deepcopy(self.interpreter.decision) | {"action": "CLOSE", "stop_price": None,
            "origin_message_id": message["id"], "expiry_exit": {"underlying_price": "64", "close_at": NOW.isoformat()}}
        result = await self.engine.execute_decision(message, decision, expiry_guard=AsyncMock())
        self.assertEqual(result["state"], "paper_order", result)
        self.assertEqual(len(self.broker.cancel_calls), 1)
        self.assertFalse(self.store.positions())

    async def test_entry_with_explicit_stop_is_protected_after_fill(self):
        self.broker = StopBroker()
        self.engine.broker = self.broker
        self.interpreter.decision["alert_price"] = "1.00"
        result = await self.signal("OPEN", stop="0.80")
        self.assertEqual(result["state"], "paper_order", result)
        self.assertEqual(len(self.broker.submissions), 2)
        self.assertEqual(self.broker.submissions[-1]["order_type"], "stop_market")
        self.assertEqual(self.broker.submissions[-1]["stop_price"], "0.80")
        self.assertEqual(self.broker.submissions[-1]["quantity"], self.store.positions()[0]["quantity"])

    async def test_signal_older_than_reopened_position_cannot_trim_or_protect_it(self):
        from datetime import timedelta
        self.seed()
        with self.store.db:
            self.store.db.execute("UPDATE orders SET created_at=? WHERE action='OPEN'", ((NOW + timedelta(seconds=1)).isoformat(),))
        self.now = NOW + timedelta(seconds=5)
        for action, values in (("UPDATE_STOP", {}), ("REDUCE", {"fraction": .5})):
            result = await self.signal(action, **values)
            self.assertEqual(result["state"], "held", result)
            self.assertIn("predates", result["reason"])
        self.assertFalse(self.broker.submissions)

    async def test_failed_broker_reconciliation_does_not_claim_active_stop(self):
        from relay.recovery import RecoveryEvaluator
        self.seed()
        await self.signal()
        self.broker.order_status = AsyncMock(side_effect=TimeoutError("fixture"))
        await RecoveryEvaluator(self.engine).reconcile_orders()
        self.assertEqual(self.request()["status"], "pending")
        self.assertEqual(self.store.unresolved(), 1)
        self.assertEqual(len(self.broker.submissions), 1)

    async def test_failed_exit_dispatch_immediately_restores_previous_protection(self):
        self.seed()
        await self.signal()
        self.engine.verify_dispatch = AsyncMock(side_effect=Hold("fixture dispatch rejection"))
        result = await self.signal("CLOSE", stop=None)
        self.assertEqual(result["state"], "held", result)
        self.assertEqual(self.store.positions()[0]["quantity"], 2)
        self.assertEqual(self.request()["status"], "active")
        self.assertEqual(len(self.broker.submissions), 2)
        self.assertTrue(all(order["order_type"] == "stop_market" for order in self.broker.submissions))

    async def test_live_stop_revalidates_source_after_own_order_reservation(self):
        self.seed()
        self.live_fixture()
        result = await self.signal()
        self.assertEqual(result["state"], "broker_order", result)
        self.assertEqual(self.request()["status"], "active")
        self.assertEqual(len(self.broker.submissions), 1)
        self.assertGreaterEqual(self.engine.verify_current.await_count, 2)

    async def test_new_source_message_during_review_prevents_stop_dispatch(self):
        self.seed()
        self.live_fixture()
        self.broker.review_hook = lambda: self.engine.note_observation(self.message(content="cancel that stop instruction"))
        result = await self.signal()
        self.assertEqual(result["state"], "held", result)
        self.assertEqual(self.request()["status"], "blocked")
        self.assertFalse(self.broker.submissions)
        self.assertEqual(self.store.unresolved(), 0)
