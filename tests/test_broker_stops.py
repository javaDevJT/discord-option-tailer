import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

from relay.broker import BrokerError, PaperBroker, RobinhoodBroker


CONTRACT = {"symbol": "SPY", "expiry": "2026-09-11", "strike": "500", "option_type": "call"}
OPTION_ID = "11111111-1111-4111-8111-111111111111"
BROKER_ID = "22222222-2222-4222-8222-222222222222"
NOW = datetime(2026, 9, 22, 15, tzinfo=timezone.utc)
QUOTE = {
    "contract": CONTRACT,
    "option_id": OPTION_ID,
    "bid": "0.95",
    "ask": "1.00",
    "timestamp": NOW.isoformat(),
    "tradable": True,
    "min_ticks": {"above_tick": "0.05", "below_tick": "0.01", "cutoff_price": "3"},
    "can_open_position": True,
}


def stop_order(client_order_id="client-stop", quantity=2):
    return {
        "client_order_id": client_order_id,
        "contract": CONTRACT,
        "side": "sell",
        "position_effect": "close",
        "quantity": quantity,
        "order_type": "stop_market",
        "stop_price": "0.90",
        "time_in_force": "gtc",
    }


def broker_order(state="queued", processed_quantity="0", processed_premium="0", *, stop=True):
    return {
        "id": BROKER_ID,
        "chain_symbol": "SPY",
        "trade_value_multiplier": "100",
        "type": "market",
        "trigger": "stop" if stop else "immediate",
        "direction": "credit",
        "quantity": "2",
        "processed_quantity": processed_quantity,
        "processed_premium": processed_premium,
        "stop_price": "0.90" if stop else None,
        "time_in_force": "gtc" if stop else "gfd",
        "state": state,
        "created_at": NOW.isoformat(),
        "legs": [{
            "option_id": OPTION_ID,
            "side": "sell",
            "position_effect": "close",
            "ratio_quantity": 1,
            "expiration_date": "2026-09-11",
            "strike_price": "500",
            "option_type": "call",
        }],
    }


class NativeStopBrokerChecks(unittest.IsolatedAsyncioTestCase):
    def make_broker(self):
        broker = RobinhoodBroker({
            "mode": "live",
            "robinhood": {"account_number": "12345", "enable_live_orders": True},
            "risk": {"max_quote_age_seconds": 30},
        }, clock=lambda: NOW)
        broker.quote = AsyncMock(return_value=QUOTE)
        broker._fresh_quote = MagicMock()
        return broker

    async def test_order_args_support_stop_market_and_sell_close_market(self):
        broker = self.make_broker()

        stop_args, _ = await broker._order_args(stop_order())
        self.assertEqual(stop_args["type"], "stop_market")
        self.assertEqual(stop_args["stop_price"], "0.90")
        self.assertEqual(stop_args["time_in_force"], "gtc")
        self.assertNotIn("price", stop_args)

        market_order = {**stop_order(), "order_type": "market", "time_in_force": "gfd"}
        market_order.pop("stop_price")
        market_args, _ = await broker._order_args(market_order)
        self.assertEqual(market_args["type"], "market")
        self.assertEqual(market_args["time_in_force"], "gfd")
        self.assertNotIn("price", market_args)
        self.assertNotIn("stop_price", market_args)

        with self.assertRaisesRegex(BrokerError, "sell-to-close"):
            await broker._order_args({**stop_order(), "side": "buy", "position_effect": "open"})

    async def test_review_validates_stop_type_trigger_and_price(self):
        broker = self.make_broker()
        args, quote = await broker._order_args(stop_order())
        review = {
            "account_number": "12345",
            "type": "market", "trigger": "stop",
            "direction": "credit",
            "quantity": "2",
            "legs": args["legs"],
            "order_checks": {},
            "stop_price": "0.90",
            "time_in_force": "gtc",
            "market_hours": "regular_hours",
            "collateral": {"account_number": "12345", "cash": {"infinite": False, "amount": "0"}},
            "fees": {"total_fee": "0"},
        }
        broker._data = AsyncMock(return_value=review)
        checked, _ = await broker._review_args(args, quote)
        self.assertEqual((checked["type"], checked["trigger"]), ("market", "stop"))
        broker._data = AsyncMock(return_value={**review, "trigger": "immediate"})
        with self.assertRaisesRegex(BrokerError, "trigger"):
            await broker._review_args(args, quote)

        broker._data = AsyncMock(return_value={**review, "stop_price": "0.85"})
        with self.assertRaisesRegex(BrokerError, "stop price"):
            await broker._review_args(args, quote)

    def test_order_result_keeps_partial_stop_fill_and_rejects_limit_shape(self):
        broker = self.make_broker()
        result = broker._order_result(broker_order("partially_filled", "1", "95.00"), "client", stop_order())
        self.assertEqual(result["status"], "partially_filled")
        self.assertEqual(result["filled_quantity"], 1)
        self.assertEqual(result["fill_price"], "0.95")

        with self.assertRaisesRegex(BrokerError, "does not match the persisted order"):
            broker._order_result(broker_order(stop=False), "client", {"order_type": "limit", "quantity": 2, "limit_price": "0.95"})

    async def test_cancel_reconciles_ack_race_after_partial_fill(self):
        broker = self.make_broker()
        expected = stop_order()
        broker.order_results["client-stop"] = {"broker_order_id": BROKER_ID}
        broker.order_inputs["client-stop"] = expected
        broker._pages = AsyncMock(side_effect=[
            [broker_order("partially_filled", "1", "95.00")],
            [broker_order("filled", "2", "190.00")],
        ])
        broker._data = AsyncMock(side_effect=[
            {"accounts": [{"account_number": "12345", "brokerage_account_type": "individual"}]},
            {"accepted": True},
        ])

        result = await broker.cancel_order("client-stop", broker_order_id=BROKER_ID, expected_order=expected)

        self.assertEqual(result["status"], "filled")
        self.assertEqual(result["filled_quantity"], 2)
        broker._data.assert_any_await("cancel_option_order", {"account_number": "12345", "order_id": BROKER_ID})
        self.assertEqual(broker._data.await_count, 2)
        self.assertEqual(broker._pages.await_count, 2)

    async def test_cancellation_checks_caller_rights_without_full_snapshot(self):
        for caller, blocked in [(None, True), ("unknown", True), ("option_level_2", True), ("option_level_3", False)]:
            with self.subTest(caller=caller):
                broker = self.make_broker()
                account = {"account_number": "12345", "brokerage_account_type": "trust_revocable",
                           "option_level": "option_level_3", "state": "restricted"}
                if caller is not None:
                    account["user_option_level"] = caller
                broker.order_status = AsyncMock(side_effect=[{"status": "submitted"}, {"status": "canceled"}])
                broker.snapshot = AsyncMock(side_effect=AssertionError("Cancellation must not use full snapshot"))
                broker._data = AsyncMock(side_effect=[{"accounts": [account]}, {"accepted": True}])
                if blocked:
                    with self.assertRaisesRegex(BrokerError, "Cancellation blocked"):
                        await broker.cancel_order("client-stop", broker_order_id=BROKER_ID)
                    broker._data.assert_awaited_once_with("get_accounts", {})
                else:
                    self.assertEqual((await broker.cancel_order("client-stop", broker_order_id=BROKER_ID))["status"], "canceled")
                    broker._data.assert_any_await("cancel_option_order", {"account_number": "12345", "order_id": BROKER_ID})
                broker.snapshot.assert_not_awaited()

        for accounts in ([], [{"account_number": "other"}], [{"account_number": "12345"}] * 2):
            broker = self.make_broker()
            broker.order_status = AsyncMock(return_value={"status": "submitted"})
            broker._data = AsyncMock(return_value={"accounts": accounts})
            with self.assertRaisesRegex(BrokerError, "missing or ambiguous"):
                await broker.cancel_order("client-stop", broker_order_id=BROKER_ID)
            broker._data.assert_awaited_once_with("get_accounts", {})
        broker.order_status = AsyncMock(return_value={"status": "filled"})
        broker._data.reset_mock()
        self.assertEqual((await broker.cancel_order("client-stop", broker_order_id=BROKER_ID))["status"], "filled")
        broker._data.assert_not_awaited()

    async def test_order_status_restart_checks_contract_and_stop_price(self):
        broker = self.make_broker()
        expected = stop_order()
        expected["option_id"] = OPTION_ID
        broker._pages = AsyncMock(return_value=[broker_order()])

        result = await broker.order_status("client-stop", broker_order_id=BROKER_ID, expected_order=expected)
        self.assertEqual(result["status"], "open")

        with self.assertRaisesRegex(BrokerError, "stop price"):
            await broker.order_status("client-stop", broker_order_id=BROKER_ID, expected_order={**expected, "stop_price": "0.85"})


class PaperStopChecks(unittest.IsolatedAsyncioTestCase):
    async def test_paper_stop_rests_and_cancel_does_not_fabricate_fill(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "quotes.json"
            path.write_text(json.dumps({"quotes": [{
                "contract": CONTRACT,
                "bid": "0.95",
                "ask": "1.00",
                "tick_size": "0.01",
                "timestamp": NOW.isoformat(),
                "tradable": True,
                "multiplier": 100,
                "currency": "USD",
                "asset_type": "equity_option",
            }]}))
            broker = PaperBroker({"paper": {"quotes_file": str(path), "market_open": True}})
            broker.restore_positions([{"contract": CONTRACT, "quantity": 2, "average_price": "1.00"}])
            order = stop_order(quantity=2)

            result = await broker.submit(order)
            self.assertEqual(result["status"], "open")
            self.assertEqual(result["filled_quantity"], 0)
            self.assertEqual((await broker.cancel_order(order["client_order_id"], expected_order=order))["status"], "canceled")
            self.assertEqual(broker.positions[0]["quantity"], 2)


if __name__ == "__main__":
    unittest.main()
