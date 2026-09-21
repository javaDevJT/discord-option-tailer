"""Offline display-cache cadence, failure, and account-isolation checks."""
import asyncio
from datetime import datetime, timedelta, timezone
import json
import sqlite3
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from relay.account import AccountCache, AUTH_ERROR, CACHE_KEY, REFRESH_ERROR
from relay.core import Store
from relay.dashboard import DashboardApp


class AccountTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.store = Store(self.root / "relay.sqlite3")
        self.store.bind_execution("shadow", "123456789")
        self.now = datetime.now(timezone.utc)
        self.overview = {
            "currency": "USD", "scope": "option_positions", "equity": "1200.50",
            "cash": "-20", "buying_power": "100", "unleveraged_buying_power": "-20",
            "asset_values": {"equity_value": "1000", "options_value": "220.50"},
            "positions": [{"contract": {"symbol": "SPY", "expiry": "2026-09-18", "strike": "600", "option_type": "call"},
                           "quantity": "1", "average_price": "2", "market_value": "220.50",
                           "position_type": "long", "multiplier": "100", "quote_timestamp": self.now.isoformat()}],
        }
        self.broker = SimpleNamespace(account_number="123456789", account_changed=asyncio.Event(),
                                      account_overview=AsyncMock(return_value=self.overview))
        self.cache = AccountCache(self.store, self.broker, clock=lambda: self.now)
        self.config_path = self.root / "config.json"
        self.config = {"mode": "shadow", "database": "relay.sqlite3", "channels": [],
                       "robinhood": {"account_number": "123456789"}}
        self.config_path.write_text(json.dumps(self.config))
        self.app = DashboardApp(self.config_path)

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    async def test_hourly_cache_survives_restart_and_dashboard_reads(self):
        self.assertFalse(self.app.account()["available"])
        self.assertTrue(await self.cache.refresh())
        self.now += timedelta(seconds=3599)
        self.cache = AccountCache(self.store, self.broker, clock=lambda: self.now)
        self.assertFalse(await self.cache.refresh())
        for _ in range(3):
            account = self.app.account()
            self.assertEqual(account["equity"], "1200.50")
            self.assertEqual(account["cash"], "-20")
            self.assertEqual(len(account["positions"]), 1)
        self.broker.account_overview.assert_awaited_once()
        self.now += timedelta(seconds=1)
        self.assertEqual(self.cache.seconds_until_due(), 0)
        self.assertTrue(await self.cache.refresh())
        self.assertEqual(self.broker.account_overview.await_count, 2)

    async def test_failure_preserves_values_throttles_retries_and_hides_provider_details(self):
        await self.cache.refresh()
        old = self.app.account()["updated_at"]
        self.broker.account_overview.side_effect = RuntimeError("private token and account payload")
        await self.cache.refresh(force=True)
        account = self.app.account()
        self.assertTrue(account["available"])
        self.assertTrue(account["stale"])
        self.assertEqual(account["error"], REFRESH_ERROR)
        self.assertEqual(account["updated_at"], old)
        self.assertEqual(account["equity"], "1200.50")
        self.assertNotIn("private", json.dumps(account))
        self.assertFalse(await self.cache.refresh())
        self.broker.account_overview.side_effect = RuntimeError("authentication required")
        await self.cache.refresh(force=True)
        self.assertEqual(self.app.account()["error"], AUTH_ERROR)
        self.broker.account_overview.side_effect = None
        await self.cache.refresh(force=True)
        self.assertIsNone(self.app.account()["error"])

    async def test_event_refresh_and_clean_shutdown(self):
        refreshed = asyncio.Event()
        async def overview():
            refreshed.set()
            return self.overview
        self.broker.account_overview.side_effect = overview
        task = asyncio.create_task(self.cache.run())
        try:
            await asyncio.wait_for(refreshed.wait(), 2)
            refreshed.clear()
            self.broker.account_changed.set()
            await asyncio.wait_for(refreshed.wait(), 2)
            self.assertEqual(self.broker.account_overview.await_count, 2)
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def test_account_binding_projection_and_missing_data(self):
        self.broker.account_overview.side_effect = RuntimeError("secret")
        await self.cache.refresh()
        account = self.app.account()
        self.assertFalse(account["available"])
        self.assertIsNone(account["equity"])
        self.broker.account_overview.side_effect = None
        await self.cache.refresh(force=True)
        self.cache.value["provider_token"] = "SECRET"
        self.cache.value["positions"][0]["account_number"] = "SECRET"
        self.cache.save()
        payload = json.dumps(self.app.account())
        self.assertNotIn("SECRET", payload)
        self.assertNotIn("123456789", payload)
        self.config["robinhood"]["account_number"] = "987654321"
        self.config_path.write_text(json.dumps(self.config))
        self.assertFalse(self.app.account()["available"])
        self.config["mode"] = "paper"
        self.config_path.write_text(json.dumps(self.config))
        self.assertFalse(self.app.account()["available"])

    async def test_old_cache_is_stale_and_future_attempt_does_not_block_refresh(self):
        await self.cache.refresh()
        self.cache.value["updated_at"] = (self.now - timedelta(hours=2)).isoformat()
        self.cache.value["last_attempt_at"] = (self.now + timedelta(days=1)).isoformat()
        self.cache.save()
        self.assertTrue(self.app.account()["stale"])
        self.assertTrue(await self.cache.refresh())

    async def test_incomplete_robinhood_configuration_is_unavailable(self):
        await self.cache.refresh()
        with self.store.db:
            self.store.db.execute("DELETE FROM metadata WHERE key='execution_binding'")
        for section in (None, "unfinished", 42, {}):
            self.config["robinhood"] = section
            self.config_path.write_text(json.dumps(self.config))
            self.assertFalse(self.app.account()["available"])

    async def test_worker_refreshes_while_paused_without_messages_and_closes_cleanly(self):
        from relay.cli import run
        config = json.loads((Path(__file__).resolve().parents[1] / "config.example.json").read_text())
        config["discord"]["transport"] = "browser"
        config.update(mode="shadow", database=str(self.root / "worker.sqlite3"), kill_switch=str(self.root / "STOP"))
        Path(config["kill_switch"]).touch()
        config["robinhood"]["account_number"] = self.broker.account_number
        for channel in config["channels"]:
            channel["guild_id"] = "1000000000000000001"
        broker_context = MagicMock()
        broker_context.__aenter__ = AsyncMock(return_value=self.broker)
        broker_context.__aexit__ = AsyncMock(return_value=False)
        refreshed = asyncio.Event()

        async def overview():
            refreshed.set()
            return self.overview

        async def monitor(*args, **kwargs):
            await asyncio.wait_for(refreshed.wait(), 2)
            await asyncio.sleep(.02)

        self.broker.account_overview.side_effect = overview
        with patch("relay.broker.RobinhoodBroker", return_value=broker_context), patch("relay.browser.monitor", monitor):
            await asyncio.wait_for(run(config, observe_only=True), 3)
        self.broker.account_overview.assert_awaited_once()
        broker_context.__aexit__.assert_awaited_once()
        with sqlite3.connect(config["database"]) as connection:
            value = json.loads(connection.execute("SELECT value FROM metadata WHERE key=?", (CACHE_KEY,)).fetchone()[0])
            self.assertEqual(value["equity"], "1200.50")
            self.assertEqual(connection.execute("SELECT count(*) FROM orders").fetchone()[0], 0)


if __name__ == "__main__":
    unittest.main()
