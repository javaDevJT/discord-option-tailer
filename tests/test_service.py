import asyncio
import json
import stat
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from relay.browser import AUTH_REQUIRED_JS, monitor, session_state
from relay.discovery import EXTRACT_GUILDS_JS
from relay.core import Engine, Hold, Store, load_config
from relay.ingest import normalize
from relay.service import RuntimeStatus, observe, run_until_change, serve, signature, watch_changes

ROOT = Path(__file__).resolve().parents[1]


class ServiceTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        self.config = json.loads((ROOT / "config.example.json").read_text())
        self.config["database"] = str(self.base / "relay.sqlite3")
        self.config["browser"]["profile_dir"] = str(self.base / "profile")
        for channel in self.config["channels"]:
            channel["guild_id"] = "3000000000000000010"
        self.status = RuntimeStatus(self.base / "state/runtime.json", self.config["channels"])

    def test_unbound_setup_does_not_enable_execution(self):
        self.config["mode"] = "shadow"
        path = self.base / "config.json"
        path.write_text(json.dumps(self.config))
        with self.assertRaises(Hold):
            load_config(path)
        loaded = load_config(path, allow_unbound=True)
        store = Store(loaded["database"])
        try:
            with self.assertRaises(Hold):
                Engine(loaded, store, None, None)
        finally:
            store.close()
        self.config["mode"] = "live"
        self.config["robinhood"]["enable_live_orders"] = True
        path.write_text(json.dumps(self.config))
        with self.assertRaises(Hold):
            load_config(path, allow_unbound=True)

    def test_session_classification_and_private_status(self):
        channel = self.config["channels"][0]
        url = f"https://discord.com/channels/{channel['guild_id']}/{channel['id']}"
        self.assertEqual(session_state(url, channel), "connected")
        self.assertEqual(session_state("https://discord.com/login?redirect_to=/channels/@me", channel), "login_required")
        self.assertEqual(session_state("https://discord.com/channels/@me", channel), "restore_channel")
        self.assertEqual(session_state("https://discord.com.evil.test/login", channel), "reconnecting")
        self.status.ready = True
        self.status.event({"component": "discord", "channel_id": channel["id"], "state": "login_required"})
        self.status.event({"component": "discord", "channel_id": self.config["channels"][1]["id"], "state": "connected"})
        self.assertEqual(self.status.value["discord"]["state"], "login_required")
        self.status.event({"component": "discord", "channel_id": channel["id"], "state": "connected"})
        snapshot = json.loads(self.status.path.read_text())
        self.assertEqual(snapshot["state"], "running")
        self.assertTrue(all(row["last_seen_at"] for row in snapshot["discord"]["channels"]))
        self.assertEqual(stat.S_IMODE(self.status.path.stat().st_mode), 0o600)
        self.assertEqual(list(self.status.path.parent.glob(".runtime-*")), [])

    async def test_setup_observation_records_no_orders_or_interpretations(self):
        channel = self.config["channels"][0]
        channel["authors"] = []
        message = normalize({"id": "1545000000000000001", "channel_id": channel["id"],
                             "author": {"id": "999999999999999999", "username": "fixture"},
                             "timestamp": "2026-09-06T12:00:00Z", "content": "BUY SPY 600 call"})
        async def fake_monitor(config, callback, **kwargs):
            await callback(message)
            await callback(message)
        with patch("relay.service.monitor", fake_monitor):
            await observe(self.config, self.status)
        reader = Store(self.config["database"], read_only=True)
        try:
            self.assertEqual(reader.db.execute("SELECT count(*) FROM messages").fetchone()[0], 1)
            self.assertEqual(reader.db.execute("SELECT count(*) FROM orders").fetchone()[0], 0)
            row = reader.db.execute("SELECT state,decision FROM events").fetchone()
            self.assertEqual(tuple(row), ("context", None))
        finally:
            reader.close()

    async def test_config_reload_cancels_worker_cleanly(self):
        cleaned = []
        async def operation():
            try:
                await asyncio.Future()
            finally:
                cleaned.append(True)
        async def changed(*args):
            return
        with patch("relay.service.watch_changes", changed):
            await run_until_change(operation(), [], self.status)
        self.assertEqual(cleaned, [True])

    async def test_config_change_during_startup_is_not_missed(self):
        path = self.base / "config.json"
        path.write_text("old")
        loaded = signature([path])
        path.write_text("changed during authentication startup")
        await asyncio.wait_for(watch_changes([path], self.status, initial_signature=loaded), timeout=.1)

    async def test_worker_publishes_loaded_mode_and_change_id(self):
        self.config.update(mode_change_id="fixture-change")
        path = self.base / "config.json"
        path.write_text(json.dumps(self.config))
        async def inspect_operation(operation, paths, status, **kwargs):
            operation.close()
            saved = json.loads(status.path.read_text())
            self.assertEqual(saved["mode"], "paper")
            self.assertEqual(saved["mode_change_id"], "fixture-change")
            self.assertIs(saved["live_enabled"], False)
            self.assertIn("initial_signature", kwargs)
            raise asyncio.CancelledError()
        with patch("relay.service.CodexInterpreter.subscription_status", new=AsyncMock(return_value={
                "authenticated": True, "isolated_execution_available": True})), \
                patch("relay.service.run_until_change", inspect_operation):
            with self.assertRaises(asyncio.CancelledError):
                await serve(path)

    async def test_worker_failure_detail_omits_private_exception_text(self):
        path = self.base / "config.json"
        with patch("relay.service.load_config", side_effect=ExceptionGroup("SECRET group", [RuntimeError("SECRET provider payload")])), \
             patch("relay.service.asyncio.sleep", new=AsyncMock(side_effect=asyncio.CancelledError)):
            with self.assertRaises(asyncio.CancelledError):
                await serve(path)
        snapshot = json.loads((self.base / "state/runtime-status.json").read_text())
        self.assertEqual(snapshot["state"], "error")
        self.assertIn("RuntimeError", snapshot["detail"])
        self.assertNotIn("SECRET", json.dumps(snapshot))

    async def test_reload_waits_for_worker_cleanup_before_returning(self):
        started, cleaning, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
        async def operation():
            started.set()
            try:
                await asyncio.Future()
            finally:
                cleaning.set()
                await release.wait()
        async def changed(*args):
            await started.wait()
        with patch("relay.service.watch_changes", changed):
            task = asyncio.create_task(run_until_change(operation(), [], self.status))
            try:
                await asyncio.wait_for(cleaning.wait(), timeout=1)
                self.assertFalse(task.done())
            finally:
                release.set()
                await asyncio.wait_for(task, timeout=1)

    async def test_process_heartbeat_does_not_refresh_worker_progress(self):
        self.status.write()
        progress = self.status.value["updated_at"]
        with patch("relay.service.timestamp", return_value="2099-01-01T00:00:00Z"):
            with self.assertRaises(asyncio.TimeoutError):
                await asyncio.wait_for(watch_changes([], self.status, interval=.001), timeout=.01)
        saved = json.loads(self.status.path.read_text())
        self.assertEqual(saved["updated_at"], progress)
        self.assertEqual(saved["heartbeat_at"], "2099-01-01T00:00:00Z")

    async def test_interrupted_consumer_preserves_queued_messages_without_replay(self):
        from relay.cli import run
        self.config["discord"]["transport"] = "browser"
        channel = self.config["channels"][0]
        consumed = asyncio.Event()
        messages = [normalize({"id": str(1545000000000000011 + i), "channel_id": channel["id"],
                               "author": {"id": channel["authors"][0], "username": "fixture"},
                               "timestamp": "2026-09-06T12:00:00Z", "content": f"Fixture {i}"})
                    for i in range(2)]

        async def fake_monitor(config, callback, **kwargs):
            for message in messages:
                await callback(message)
            await asyncio.Future()

        async def blocked_handle(engine, message, **kwargs):
            self.assertEqual(kwargs["_observed"], "new")
            consumed.set()
            await asyncio.Future()

        with patch("relay.browser.monitor", fake_monitor), patch("relay.cli.paper_broker", return_value=object()), patch.object(Engine, "handle", blocked_handle):
            task = asyncio.create_task(run(self.config, observe_only=True))
            try:
                await asyncio.wait_for(consumed.wait(), timeout=2)
                reader = Store(self.config["database"], read_only=True)
                try:
                    self.assertEqual(reader.db.execute("SELECT count(*) FROM messages").fetchone()[0], 2)
                finally:
                    reader.close()
            finally:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
        reader = Store(self.config["database"])
        try:
            self.assertEqual([row[0] for row in reader.db.execute("SELECT state FROM events")], ["held", "held"])
            self.assertEqual(reader.db.execute("SELECT count(*) FROM orders").fetchone()[0], 0)
            engine = Engine(self.config, reader, None, object())
            self.assertEqual((await engine.handle(messages[0]))["state"], "duplicate")
        finally:
            reader.close()

    async def test_committed_queue_receipt_is_processed_once_in_current_run(self):
        channel = self.config["channels"][0]
        message = normalize({"id": "1545000000000000031", "channel_id": channel["id"],
                             "author": {"id": channel["authors"][0], "username": "fixture"},
                             "timestamp": "2026-09-06T12:00:00Z", "content": "Fixture"})
        message["source_group"] = channel["source_group"]
        store = Store(self.config["database"])
        try:
            receipt = store.observe(message)
            engine = Engine(self.config, store, None, object())
            self.assertEqual((await engine.handle(message, _observed=receipt))["state"], "context")
            self.assertEqual((await engine.handle(message))["state"], "duplicate")
        finally:
            store.close()

    async def test_browser_login_recovery_never_replays_gap_as_live(self):
        step = [0]
        events, statuses = [], []
        channels = self.config["channels"]

        class Page:
            def __init__(self, index):
                self.index, self.url = index, "about:blank"
            def is_closed(self):
                return False
            async def goto(self, url, **kwargs):
                self.url = url
            async def evaluate(self, expression, channel_id=None):
                if expression == EXTRACT_GUILDS_JS:
                    return {"sidebar_present": True, "login_required": False, "guilds": [{"id": "3000000000000000010"}]}
                if expression == AUTH_REQUIRED_JS:
                    return False
                numbers = [1] if self.index else [1] + ([2] if step[0] >= 1 else []) + ([3] if step[0] >= 4 else [])
                rows = [{"id": str(1545000000000000000 + self.index * 100 + number),
                         "channel_id": channel_id,
                         "author": {"id": channels[self.index]["authors"][0], "username": "fixture"},
                         "timestamp": "2026-09-06T12:00:00Z", "content": str(number)} for number in numbers]
                return {"url": self.url, "ready": True, "messages": rows, "at_bottom": True, "connection_epoch": "fixture"}

        class Context:
            pages = [Page(0), Page(1)]
            closed = False
            async def close(self):
                self.closed = True

        context = Context()
        async def launch(*args, **kwargs):
            return context
        class Playwright:
            async def __aenter__(self):
                return SimpleNamespace(chromium=SimpleNamespace(launch_persistent_context=launch))
            async def __aexit__(self, *args):
                return False

        async def sleep(seconds):
            step[0] += 1
            if step[0] == 1:
                context.pages[0].url = "https://discord.com/login"
            elif step[0] == 2:
                context.pages[0].url = "https://discord.com/channels/@me"
            elif step[0] >= 5:
                raise RuntimeError("fixture complete")
        async def record(message):
            events.append(message)
        with patch("relay.browser._playwright", return_value=Playwright), patch("relay.browser.asyncio.sleep", sleep):
            with self.assertRaisesRegex(RuntimeError, "fixture complete"):
                await monitor(self.config, record, on_status=statuses.append)
        first_channel = [message for message in events if message["channel_id"] == channels[0]["id"]]
        self.assertEqual([message["ingestion"] for message in first_channel], ["baseline", "baseline", "live"])
        self.assertTrue(any(status["state"] == "login_required" for status in statuses))
        self.assertTrue(context.closed)


    def test_provider_auth_failure_survives_runtime_restart_until_real_health_event(self):
        self.status.event({"component": "broker", "state": "auth_required"})
        restarted = RuntimeStatus(self.status.path, self.config["channels"])
        self.assertEqual(restarted.value["broker"]["state"], "auth_required")
        restarted.event({"component": "broker", "state": "configured"})
        self.assertEqual(restarted.value["broker"]["state"], "auth_required")
        restarted.event({"component": "broker", "state": "unavailable"})
        self.assertEqual(restarted.value["broker"]["state"], "auth_required")
        restarted.event({"component": "broker", "state": "connected"})
        self.assertEqual(restarted.value["broker"]["state"], "connected")


if __name__ == "__main__":
    unittest.main()
