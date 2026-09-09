import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
import tempfile
import unittest

from relay.broker import BrokerError, PaperBroker


class PaperEquityChecks(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "quotes.json"
        self.contract = {"symbol": "SPY", "expiry": "2099-01-16",
                         "strike": "500", "option_type": "call"}
        self.quotes = [{"contract": self.contract, "bid": "0.90", "ask": "1.00",
                        "tick_size": "0.01", "timestamp": datetime.now(timezone.utc).isoformat(),
                        "tradable": True, "multiplier": 100, "currency": "USD",
                        "asset_type": "equity_option"}]
        self.path.write_text(json.dumps(self.quotes))

    def broker(self, fees="0"):
        return PaperBroker({"paper": {"quotes_file": str(self.path), "buying_power": "1000",
                                      "market_open": True},
                            "risk": {"fee_reserve_per_contract": fees}})

    def order(self, identity, quantity, side="buy", limit="1.00"):
        return {"client_order_id": identity, "contract": self.contract, "side": side,
                "position_effect": "open" if side == "buy" else "close",
                "quantity": quantity, "limit_price": limit}

    async def test_cash_only_snapshot_requires_no_quotes(self):
        broker = PaperBroker({"paper": {"buying_power": "1000", "market_open": True}})
        before = datetime.now(timezone.utc)
        snapshot = await broker.snapshot()
        self.assertEqual(snapshot["account_id"], "paper")
        self.assertIs(snapshot["simulated"], True)
        self.assertEqual(snapshot["currency"], "USD")
        self.assertEqual(snapshot["equity"], snapshot["buying_power"])
        self.assertEqual(snapshot["option_exposure_by_symbol"], {})
        timestamp = datetime.fromisoformat(snapshot["timestamp"])
        self.assertEqual(timestamp.utcoffset(), timedelta(0))
        self.assertLessEqual(before, timestamp)
        self.assertLessEqual(timestamp, datetime.now(timezone.utc))

    async def test_restored_positions_aggregate_and_use_bid_equity(self):
        second = dict(self.contract, strike="501")
        self.quotes.append(dict(self.quotes[0], contract=second, bid="1.80", ask="2.00"))
        self.path.write_text(json.dumps(self.quotes))
        broker = self.broker()
        positions = [{"source_group": "first", "contract": self.contract, "quantity": 1,
                      "average_price": "1.00"},
                     {"source_group": "second", "contract": dict(self.contract, symbol="spy", strike="500.00"),
                      "quantity": 1, "average_price": "3.00"},
                     {"contract": second, "quantity": 1, "average_price": "1.00"}]
        broker.restore_positions(positions)
        snapshot = await broker.snapshot()
        self.assertEqual(len(snapshot["positions"]), 2)
        self.assertEqual(snapshot["positions"][0]["quantity"], 2)
        self.assertEqual(Decimal(snapshot["positions"][0]["average_price"]), Decimal("2"))
        self.assertEqual(Decimal(snapshot["equity"]), Decimal("1360"))
        self.assertEqual(Decimal(snapshot["option_exposure_by_symbol"]["SPY"]), Decimal("600"))
        positions[0]["quantity"] = 99
        snapshot["positions"][0]["contract"]["symbol"] = "BAD"
        self.assertEqual(broker.positions[0]["quantity"], 2)
        self.assertEqual(broker.positions[0]["contract"]["symbol"], "SPY")

    async def test_explicit_simulation_timestamp_is_preserved_in_utc(self):
        broker = self.broker()
        broker.config["timestamp"] = "2026-09-08T09:45:00-04:00"
        self.assertEqual((await broker.snapshot())["timestamp"], "2026-09-08T13:45:00+00:00")
        broker.config["timestamp"] = "2026-09-08T13:45:00"
        with self.assertRaisesRegex(BrokerError, "timezone"):
            await broker.snapshot()

    async def test_fills_update_holdings_once_and_reject_oversells(self):
        broker = self.broker(fees="1")
        first_order = self.order("buy-first", 2)
        first = await broker.submit(first_order)
        self.assertEqual(first, await broker.submit(first_order))
        snapshot = await broker.snapshot()
        self.assertEqual(Decimal(snapshot["equity"]), Decimal("978"))
        self.assertEqual(broker.positions[0]["quantity"], 2)
        self.quotes[0].update(bid="1.80", ask="2.00")
        self.path.write_text(json.dumps(self.quotes))
        await broker.submit(self.order("buy-second", 2, limit="2"))
        self.assertEqual(Decimal(broker.positions[0]["average_price"]), Decimal("1.5"))
        sell = self.order("sell-one", 1, side="sell", limit="1.80")
        await broker.submit(sell)
        await broker.submit(sell)
        snapshot = await broker.snapshot()
        self.assertEqual(Decimal(snapshot["buying_power"]), Decimal("575"))
        self.assertEqual(Decimal(snapshot["equity"]), Decimal("1115"))
        self.assertEqual(Decimal(snapshot["option_exposure_by_symbol"]["SPY"]), Decimal("600"))
        self.assertEqual(broker.positions[0]["quantity"], 3)
        with self.assertRaisesRegex(BrokerError, "holdings"):
            await broker.submit(self.order("oversell", 4, side="sell", limit="1.80"))
        pending = await broker.submit(self.order("noncrossing", 1, limit="1"))
        self.assertEqual(pending["status"], "open")
        self.assertEqual(broker.positions[0]["quantity"], 3)
        self.assertEqual(broker.buying_power, Decimal("575"))
        await broker.submit(self.order("close-rest", 3, side="sell", limit="1.80"))
        snapshot = await broker.snapshot()
        self.assertEqual(snapshot["positions"], [])
        self.assertEqual(snapshot["option_exposure_by_symbol"], {})
        self.assertEqual(Decimal(snapshot["equity"]), Decimal("1112"))

    async def test_restored_holdings_require_exact_explicit_quotes(self):
        broker = self.broker()
        broker.restore_positions([{"contract": dict(self.contract, strike="502"), "quantity": 1,
                                   "average_price": "8.00"}])
        with self.assertRaisesRegex(BrokerError, "missing"):
            await broker.snapshot()
        self.assertEqual(broker.buying_power, Decimal("1000"))
        with self.assertRaises(BrokerError):
            broker.restore_positions([{"contract": self.contract, "quantity": -1, "average_price": "1"}])
        self.assertEqual(broker.positions[0]["contract"]["strike"], "502")


if __name__ == "__main__":
    unittest.main()
