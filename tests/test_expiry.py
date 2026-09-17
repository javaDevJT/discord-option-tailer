"""Offline expiry protection: python -m unittest discover -s tests -p test_expiry.py."""
import copy
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock

from relay.core import Engine, Store
from relay.expiry import ExpiryExits
from test_core import Broker, Interpreter


class ExpiryChecks(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.config = json.loads((Path(__file__).resolve().parents[1] / "config.example.json").read_text())
        self.config.update(mode="live", database=str(Path(self.temp.name) / "test.sqlite3"),
                           kill_switch=str(Path(self.temp.name) / "STOP"), require_source_verification=True)
        self.config["robinhood"].update(account_number="12345678", enable_live_orders=True)
        self.now = datetime(2026, 9, 18, 19, 5, tzinfo=timezone.utc)
        self.store = Store(self.config["database"])
        self.addCleanup(lambda: self.store.close())
        self.broker = Broker()
        self.broker.account.update(account_id="12345678", timestamp=self.now.isoformat())
        self.broker.quotes.update(timestamp=self.now.isoformat(), bid="0.50", ask="0.55")
        self.broker.underlying_quote = AsyncMock(return_value={"symbol": "SPY", "price": "501", "timestamp": self.now.isoformat()})
        self.engine = Engine(self.config, self.store, Interpreter(), self.broker, clock=lambda: self.now)
        self.reconcile = AsyncMock()
        self.monitor = ExpiryExits(self.engine, self.reconcile)
        self.contract = dict(symbol="SPY", expiry="2026-09-18", strike="500", option_type="call")
        self.group = self.config["channels"][0]["source_group"]

    def seed(self, quantity=2):
        message = dict(id="1545000000000000001", source_group=self.group)
        order = dict(client_order_id="entry", contract=copy.deepcopy(self.contract), side="buy", quantity=quantity, limit_price="2", position_effect="open")
        self.store.reserve(message, {"action": "OPEN"}, order, self.now - timedelta(hours=2))
        self.store.apply_result("entry", dict(id="entry-broker", status="filled", filled_quantity=quantity, fill_price="2"))

    async def test_itm_losing_option_closes_every_contract_without_discord(self):
        self.seed()
        result = await self.monitor.check()
        self.assertEqual(result[0]["state"], "broker_order")
        self.assertIn("Expiry exercise protection", result[0]["reason"])
        self.assertEqual(self.broker.submissions[0]["quantity"], 2)
        self.assertEqual(self.broker.submissions[0]["side"], "sell")
        self.assertEqual(self.store.positions(), [])
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM messages").fetchone()[0], 0)
        await ExpiryExits(self.engine, self.reconcile).check()
        self.assertEqual(len(self.broker.submissions), 1)

    async def test_put_itm_uses_underlying_below_strike(self):
        self.contract["option_type"] = "put"
        self.broker.underlying_quote.return_value["price"] = "499"
        self.seed(1)
        await self.monitor.check()
        self.assertEqual(self.broker.submissions[0]["quantity"], 1)

    async def test_otm_atm_and_pre_window_do_not_sell(self):
        self.seed()
        for price in ("499", "500"):
            self.broker.underlying_quote.return_value["price"] = price
            self.assertEqual((await self.monitor.check())[0]["state"], "context")
        self.now -= timedelta(hours=2)
        self.assertEqual(await self.monitor.check(), [])
        self.assertEqual(self.broker.submissions, [])

    async def test_other_expiry_and_flat_account_do_not_fetch(self):
        await self.monitor.check()
        self.contract["expiry"] = "2026-09-25"
        self.seed()
        await self.monitor.check()
        self.broker.underlying_quote.assert_not_awaited()

    async def test_observe_only_never_evaluates_or_submits_expiry(self):
        self.seed()
        self.engine.interpreter = None
        self.assertEqual(await self.monitor.check(), [])
        self.broker.underlying_quote.assert_not_awaited()
        self.assertEqual(self.broker.submissions, [])

    async def test_early_close_uses_exchange_calendar(self):
        self.now = datetime(2026, 11, 27, 17, 5, tzinfo=timezone.utc)
        self.contract["expiry"] = "2026-11-27"
        self.broker.account["timestamp"] = self.broker.quotes["timestamp"] = self.now.isoformat()
        self.broker.underlying_quote.return_value["timestamp"] = self.now.isoformat()
        self.seed()
        await self.monitor.check()
        self.assertEqual(self.broker.submissions[0]["expiry_exit"]["close_at"], "2026-11-27T18:00:00+00:00")

    async def test_partial_pending_and_unknown_never_duplicate_on_restart(self):
        self.seed()
        self.broker.result = dict(id="sell-broker", status="partially_filled", filled_quantity=1, fill_price="0.50")
        await self.monitor.check()
        self.store.close()
        self.store = Store(self.config["database"])
        self.engine = Engine(self.config, self.store, Interpreter(), self.broker, clock=lambda: self.now)
        result = await ExpiryExits(self.engine, self.reconcile).check()
        self.assertEqual(result[0]["state"], "held")
        self.assertEqual(self.store.positions()[0]["quantity"], 1)
        self.assertEqual(len(self.broker.submissions), 1)

    async def test_unknown_submission_and_external_unresolved_order_hold(self):
        self.seed()
        self.broker.error = RuntimeError("transport failed")
        self.assertEqual((await self.monitor.check())[0]["state"], "unknown")
        self.assertEqual((await self.monitor.check())[0]["state"], "held")
        self.assertEqual(len(self.broker.submissions), 1)

    async def test_stale_or_wrong_underlying_and_pause_hold(self):
        self.seed()
        quote = self.broker.underlying_quote.return_value
        quote["timestamp"] = (self.now - timedelta(minutes=2)).isoformat()
        self.assertEqual((await self.monitor.check())[0]["state"], "held")
        quote["timestamp"], quote["symbol"] = self.now.isoformat(), "QQQ"
        self.assertEqual((await self.monitor.check())[0]["state"], "held")
        quote["symbol"] = "SPY"
        Path(self.config["kill_switch"]).touch()
        self.assertEqual((await self.monitor.check())[0]["state"], "held")
        self.assertEqual(self.broker.submissions, [])

    async def test_final_review_stale_underlying_keeps_rejected_audit_and_can_retry(self):
        self.seed()
        async def delay():
            self.now += timedelta(seconds=30)
        self.broker.review_hook = delay
        self.assertEqual((await self.monitor.check())[0]["state"], "held")
        self.assertEqual(self.broker.submissions, [])
        self.broker.review_hook = None
        self.broker.account["timestamp"] = self.broker.quotes["timestamp"] = self.now.isoformat()
        self.broker.underlying_quote.return_value["timestamp"] = self.now.isoformat()
        self.assertEqual((await self.monitor.check())[0]["state"], "broker_order")
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM orders WHERE status='rejected'").fetchone()[0], 1)

    async def test_closed_session_alerts_no_submission(self):
        self.seed()
        self.now = self.now.replace(hour=20)
        result = await self.monitor.check()
        self.assertEqual(result[0]["state"], "held")
        self.assertIn("market closed", result[0]["reason"])
        self.assertEqual(self.broker.submissions, [])

    async def test_after_utc_midnight_audits_same_new_york_expiry_day(self):
        self.now = datetime(2026, 9, 22, 0, 30, tzinfo=timezone.utc)
        self.contract["expiry"] = "2026-09-21"
        self.seed()
        result = await self.monitor.check()
        self.assertEqual(result[0]["state"], "held")
        self.assertIn("market closed", result[0]["reason"])
        self.broker.underlying_quote.assert_not_awaited()

    async def test_final_underlying_refresh_prevents_exit_after_crossing_out_of_money(self):
        self.seed()
        initial = copy.deepcopy(self.broker.underlying_quote.return_value)
        self.broker.underlying_quote.side_effect = [initial, dict(initial, price="499")]
        result = await self.monitor.check()
        self.assertEqual(self.broker.underlying_quote.await_count, 2)
        self.assertEqual(result[0]["state"], "held")
        self.assertEqual(self.broker.submissions, [])
        self.assertEqual(self.store.positions()[0]["quantity"], 2)

    async def test_wide_spread_does_not_block_expiry_bid_exit(self):
        self.seed()
        self.broker.quotes.update(bid="0.10", ask="2.00")
        await self.monitor.check()
        self.assertEqual(self.broker.submissions[0]["limit_price"], "0.10")


if __name__ == "__main__":
    unittest.main()
