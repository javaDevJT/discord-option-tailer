import asyncio
from datetime import datetime, timedelta, timezone
import io
import http.client
import json
import os
import socket
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch
from urllib.parse import urlencode

from relay.setup import SetupManager, _AuthJob, _AuthCancelled
from relay.core import Store
from types import SimpleNamespace


ROOT = Path(__file__).resolve().parents[1]


class SetupManagerTests(unittest.TestCase):
    def test_runtime_reauth_overrides_saved_credential_presence(self):
        self.config["robinhood"]["account_number"] = "12345678"
        self.path.write_text(json.dumps(self.config))
        runtime_path = self.base / "state/runtime-status.json"
        runtime_path.parent.mkdir(parents=True, exist_ok=True)
        runtime_path.write_text(json.dumps({"codex": {"state": "auth_required"}, "broker": {"state": "login_required"}}))
        with patch.object(self.manager, "_codex_ready", return_value=True), patch.object(self.manager, "_token_store_ready", return_value=True):
            status = self.manager.status()
            self.assertEqual(status["codex"]["state"], "auth_required")
            self.assertEqual(status["robinhood"]["state"], "auth_required")
            self.assertTrue(status["robinhood"]["token_present"])
            runtime_path.write_text(json.dumps({"codex": {"state": "unauthenticated"}, "broker": {"state": "reauth_required"}}))
            status = self.manager.status()
            self.assertEqual(status["codex"]["state"], "auth_required")
            self.assertEqual(status["robinhood"]["state"], "auth_required")
            runtime_path.write_text(json.dumps({"codex": {"state": "ready"}, "broker": {"state": "connected"}}))
            status = self.manager.status()
            self.assertEqual(status["codex"]["state"], "connected")
            self.assertEqual(status["robinhood"]["state"], "connected")

    def test_evaluation_preferences_require_pause_and_preserve_other_settings(self):
        payload = {"model": "gpt-6-astra", "reasoning_effort": "medium", "service_tier": "fast", "max_chase_fraction": "0.10"}
        for invalid in ({}, payload | {"mode": "live"}, payload | {"model": "bad\nmodel"},
                        payload | {"reasoning_effort": "invalid"}, payload | {"service_tier": "invalid"},
                        payload | {"max_chase_fraction": "1.01"}, payload | {"max_chase_fraction": "NaN"}):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                self.manager.save_evaluation(invalid)
        with self.assertRaisesRegex(RuntimeError, "Pause"):
            self.manager.save_evaluation(payload)
        # Start with old preferences so this also proves an actual update.
        old = json.loads(self.path.read_text())
        old["llm"].update(model=None, reasoning_effort="low", service_tier="standard")
        old["risk"]["max_chase_fraction"] = "0.05"
        self.path.write_text(json.dumps(old))
        self.manager.set_paused(True)
        result = self.manager.save_evaluation(payload)
        self.assertEqual({key: result["evaluation"][key] for key in payload}, payload)
        self.assertEqual(result["evaluation"]["jev"]["mode"], "codex")
        self.assertTrue(result["paused"])
        self.assertTrue(result["trading"]["pending"])
        saved = json.loads(self.path.read_text())
        self.assertTrue(saved.pop("mode_change_id"))
        saved["llm"] = old["llm"]
        saved["risk"]["max_chase_fraction"] = old["risk"]["max_chase_fraction"]
        self.assertEqual(saved, old)
        # A paused worker with failed authentication must still be configurable.
        changed = payload | {"reasoning_effort": "low"}
        saved_preferences = self.manager.save_evaluation(changed)["evaluation"]
        self.assertEqual({key: saved_preferences[key] for key in changed}, changed)
        saved_preferences = self.manager.save_evaluation(payload)["evaluation"]
        self.assertEqual({key: saved_preferences[key] for key in payload}, payload)
        with self.assertRaisesRegex(RuntimeError, "worker"):
            self.manager.set_paused(False)
        self.manager.close()
        self.manager = SetupManager(self.path)
        saved_preferences = self.manager.status()["evaluation"]
        self.assertEqual({key: saved_preferences[key] for key in payload}, payload)
        self.assertTrue(self.manager.status()["paused"])

    def test_same_day_permission_requires_pause_and_worker_reload_preserving_other_settings(self):
        original = self.path.read_bytes()
        for payload in ({}, {"allow_same_day_expiry": 1}, {"allow_same_day_expiry": True, "mode": "live"}):
            with self.assertRaises(ValueError):
                self.manager.set_expiry_policy(payload)
        with self.assertRaisesRegex(RuntimeError, "Pause"):
            self.manager.set_expiry_policy({"allow_same_day_expiry": True})
        self.assertEqual(self.path.read_bytes(), original)
        self.manager.set_paused(True)
        result = self.manager.set_expiry_policy({"allow_same_day_expiry": True})
        self.assertTrue(result["risk"]["allow_same_day_expiry"])
        self.assertTrue(result["paused"])
        self.assertTrue(result["trading"]["pending"])
        saved = json.loads(self.path.read_text())
        self.assertTrue(saved.pop("mode_change_id"))
        saved["risk"]["allow_same_day_expiry"] = False
        self.assertEqual(saved, json.loads(original))
        with self.assertRaisesRegex(RuntimeError, "previous settings"):
            self.manager.set_expiry_policy({"allow_same_day_expiry": False})
        with self.assertRaisesRegex(RuntimeError, "worker"):
            self.manager.set_paused(False)

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.base = Path(self.temporary.name)
        self.config = json.loads((ROOT / "config.example.json").read_text())
        self.config.update(
            mode="shadow",
            database="state/original.sqlite3",
            kill_switch="state/STOP",
            runtime_status_file="state/runtime-status.json",
        )
        # These setup tests exercise the browser-session status and OAuth
        # paths. Keep that transport explicit because the public example
        # configuration defaults to Gateway.
        self.config["discord"]["transport"] = "browser"
        self.config["browser"]["profile_dir"] = "state/discord-browser"
        self.config["robinhood"]["token_store"] = "state/robinhood-oauth.json"
        self.path = self.base / "config.json"
        self.path.write_text(json.dumps(self.config))
        self.env = patch.dict(os.environ, {"CODEX_HOME": str(self.base / "codex")}, clear=False)
        self.env.start()
        self.manager = SetupManager(self.path)

    def tearDown(self):
        self.manager.close()
        self.env.stop()
        self.temporary.cleanup()

    def channels(self):
        return {
            "channels": [
                {
                    "url": "https://discord.com/channels/111111111111111111/222222222222222222",
                    "name": "Signals",
                    "role": "signals",
                    "authors": ["333333333333333333"],
                },
                {
                    "url": "https://discord.com/channels/111111111111111111/444444444444444444",
                    "name": "Context",
                    "role": "context",
                    "authors": ["555555555555555555"],
                },
            ],
            "poll_seconds": 7,
        }

    def wait_for(self, predicate, timeout=2):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return
            time.sleep(0.01)
        self.fail("timed out waiting for setup job")

    def test_optional_webhook_is_private_and_preserves_other_settings(self):
        url = "https://discord.com/api/webhooks/123456789012345678/" + "a" * 68
        self.assertFalse(self.manager.status()["notifications"]["configured"])
        result = self.manager.save_notifications({"enabled": True, "webhook_url": url})
        self.assertTrue(result["notifications"]["enabled"])
        self.assertTrue(result["notifications"]["configured"])
        self.assertNotIn(url, json.dumps(result))
        saved = json.loads(self.path.read_text())
        self.assertEqual({k: v for k, v in saved.items() if k != "notifications"}, self.config)
        self.manager.save_notifications({"enabled": False})
        self.assertEqual(json.loads(self.path.read_text())["notifications"]["webhook_url"], url)
        self.manager.save_notifications({"enabled": True})
        self.assertTrue(self.manager.status()["notifications"]["enabled"])
        self.manager.save_notifications({"enabled": False, "webhook_url": ""})
        self.assertFalse(self.manager.status()["notifications"]["configured"])
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)

    def test_webhook_rejects_missing_secret_and_unsafe_destinations_atomically(self):
        original = self.path.read_bytes()
        for payload in ({"enabled": True}, {"enabled": "true"}, {"enabled": False, "unknown": 1},
                        {"enabled": True, "webhook_url": "http://127.0.0.1:9999/secret"},
                        {"enabled": True, "webhook_url": "https://discord.com.evil.test/api/webhooks/123/token"},
                        {"enabled": True, "webhook_url": "https://discord.com/api/webhooks/123/token?thread_id=5"}):
            with self.subTest(payload=payload), self.assertRaises(ValueError):
                self.manager.save_notifications(payload)
            self.assertEqual(self.path.read_bytes(), original)

    def test_channels_are_bounded_and_atomic_fields_are_preserved(self):
        original_risk = self.config["risk"].copy()
        result = self.manager.save_channels(self.channels())
        self.assertEqual(result["accepted"], "channels_saved")
        saved = json.loads(self.path.read_text())
        self.assertEqual(saved["browser"]["poll_seconds"], 7)
        self.assertEqual(saved["risk"], original_risk)
        self.assertEqual(saved["database"], "state/original.sqlite3")
        self.assertEqual(saved["mode"], "shadow")
        self.assertEqual(saved["channels"][0]["id"], "222222222222222222")

        with self.assertRaises(ValueError):
            self.manager.save_channels({"channels": self.channels()["channels"][:1]})
        with self.assertRaises(ValueError):
            bad = self.channels()
            bad["channels"][0]["url"] = "https://evil.example/channels/111111111111111111/222222222222222222"
            self.manager.save_channels(bad)

    def test_pause_and_reconnect_use_fixed_state_markers(self):
        self.assertFalse(self.manager.status()["paused"])
        self.assertTrue(self.manager.set_paused(True)["paused"])
        self.assertTrue((self.base / "state/STOP").is_file())
        self.assertFalse(self.manager.set_paused(False)["paused"])
        self.assertFalse((self.base / "state/STOP").exists())
        self.assertEqual(self.manager.reconnect()["accepted"], "reconnect_requested")
        self.assertTrue((self.base / "state/RECONNECT").is_file())

    def mode_fixture(self):
        self.manager.save_channels(self.channels())
        raw = json.loads(self.path.read_text())
        raw["robinhood"]["account_number"] = "12345678"
        self.path.write_text(json.dumps(raw))
        for name in ("_public_codex_locked", "_public_robinhood_locked", "_public_discord"):
            mock = patch.object(self.manager, name, return_value={"state": "connected"})
            mock.start()
            self.addCleanup(mock.stop)
        self.manager.set_paused(True)
        ledger = Store(self.base / raw["database"])
        ledger.bind_execution("shadow", "12345678")
        ledger.close()

    def acknowledge_mode(self, **overrides):
        raw = json.loads(self.path.read_text())
        runtime = {"state": "running", "heartbeat_at": datetime.now(timezone.utc).isoformat(),
                   "mode": raw["mode"], "mode_change_id": raw.get("mode_change_id"),
                   "live_enabled": raw["robinhood"]["enable_live_orders"]}
        runtime.update(overrides)
        (self.base / "state/runtime-status.json").write_text(json.dumps(runtime))

    def test_mode_switch_preserves_settings_and_restores_separate_ledgers(self):
        self.mode_fixture()
        before = json.loads(self.path.read_text())
        original_ledger = (self.base / before["database"]).read_bytes()
        result = self.manager.set_mode({"mode": "live", "confirm_live": True})
        self.assertTrue(result["paused"])
        self.assertTrue(result["trading"]["pending"])
        saved = json.loads(self.path.read_text())
        self.assertEqual(saved["database"], "state/relay-live.sqlite3")
        self.assertTrue(saved["robinhood"]["enable_live_orders"])
        self.assertEqual(saved["mode_databases"]["shadow"], before["database"])
        self.assertEqual(saved["risk"], before["risk"])
        self.assertEqual(saved["channels"], before["channels"])
        self.assertEqual((self.base / before["database"]).read_bytes(), original_ledger)
        reader = Store(self.base / saved["database"], read_only=True)
        try:
            binding = json.loads(reader.db.execute("SELECT value FROM metadata WHERE key='execution_binding'").fetchone()[0])
            self.assertEqual(binding, {"mode": "live", "account": "12345678"})
            self.assertEqual(reader.db.execute("SELECT COUNT(*) FROM orders").fetchone()[0], 0)
        finally:
            reader.close()
        self.acknowledge_mode()
        live = Store(self.base / saved["database"])
        live.bind_execution("live", "12345678")
        live.close()
        self.manager.set_mode({"mode": "shadow"})
        restored = json.loads(self.path.read_text())
        self.assertEqual(restored["database"], before["database"])
        self.assertFalse(restored["robinhood"]["enable_live_orders"])
        self.assertTrue(self.manager.status()["paused"])
        self.acknowledge_mode()
        self.manager.set_mode({"mode": "live", "confirm_live": True})
        self.assertEqual(json.loads(self.path.read_text())["database"], saved["database"])

    def test_mode_payload_confirmation_pause_and_connections_are_required(self):
        self.mode_fixture()
        before = self.path.read_bytes()
        for payload in ({}, {"mode": "paper"}, {"mode": []}, {"mode": "live"},
                        {"mode": "live", "confirm_live": 1}, {"mode": "shadow", "database": "/tmp/other"}):
            with self.subTest(payload=payload), self.assertRaises(ValueError):
                self.manager.set_mode(payload)
        self.manager.set_paused(False)
        with self.assertRaisesRegex(RuntimeError, "Pause"):
            self.manager.set_mode({"mode": "live", "confirm_live": True})
        self.manager.set_paused(True)
        for name in ("_public_codex_locked", "_public_robinhood_locked", "_public_discord"):
            with patch.object(self.manager, name, return_value={"state": "not_connected"}):
                with self.assertRaisesRegex(RuntimeError, "Connect"):
                    self.manager.set_mode({"mode": "live", "confirm_live": True})
        job = _AuthJob("codex", thread=SimpleNamespace(is_alive=lambda: True))
        self.manager._jobs["codex"] = job
        try:
            with self.assertRaisesRegex(RuntimeError, "sign-in"):
                self.manager.set_mode({"mode": "live", "confirm_live": True})
        finally:
            self.manager._jobs.clear()
        self.assertEqual(self.path.read_bytes(), before)

    def test_resume_waits_for_current_worker_acknowledgement(self):
        self.mode_fixture()
        self.manager.set_mode({"mode": "live", "confirm_live": True})
        saved = self.path.read_bytes()
        for override in ({"mode": "shadow"}, {"mode_change_id": "previous"}, {"live_enabled": False},
                         {"state": "error"}, {"heartbeat_at": "2000-01-01T00:00:00Z"}):
            self.acknowledge_mode(**override)
            with self.assertRaisesRegex(RuntimeError, "Wait for the worker"):
                self.manager.set_paused(False)
            self.assertTrue(self.manager.status()["paused"])
        self.manager.set_mode({"mode": "live", "confirm_live": True})
        self.assertEqual(self.path.read_bytes(), saved)  # Idempotent retry preserves the acknowledgement ID.
        self.acknowledge_mode()
        self.assertFalse(self.manager.status()["trading"]["pending"])
        self.assertFalse(self.manager.set_paused(False)["paused"])

    def test_ledger_aliases_and_wrong_bindings_reject_without_writes(self):
        self.mode_fixture()
        original = self.path.read_bytes()
        live = self.base / "state/relay-live.sqlite3"
        source = self.base / "state/original.sqlite3"
        for kind in ("symlink", "hardlink", "same_path", "wrong_mode", "wrong_account", "corrupt"):
            with self.subTest(kind=kind):
                if kind == "symlink":
                    live.symlink_to(source)
                elif kind == "hardlink":
                    os.link(source, live)
                elif kind == "same_path":
                    raw = json.loads(original)
                    raw["mode_databases"] = {"live": "state/original.sqlite3"}
                    self.path.write_text(json.dumps(raw))
                elif kind == "corrupt":
                    live.write_text("not sqlite")
                else:
                    ledger = Store(live)
                    ledger.bind_execution("shadow" if kind == "wrong_mode" else "live",
                                          "87654321" if kind == "wrong_account" else "12345678")
                    ledger.close()
                before = self.path.read_bytes()
                with self.assertRaises(RuntimeError):
                    self.manager.set_mode({"mode": "live", "confirm_live": True})
                self.assertEqual(self.path.read_bytes(), before)
                live.unlink(missing_ok=True)
                self.path.write_bytes(original)

    def test_leaving_live_preserves_open_positions_and_unresolved_orders(self):
        self.mode_fixture()
        self.manager.set_mode({"mode": "live", "confirm_live": True})
        self.acknowledge_mode()
        path = self.base / "state/relay-live.sqlite3"
        before = self.path.read_bytes()
        path.unlink()  # Simulate a lost ledger, not an ordinary failed startup.
        with self.assertRaisesRegex(RuntimeError, "unavailable"):
            self.manager.set_mode({"mode": "shadow"})
        ledger = Store(path)
        try:
            ledger.bind_execution("live", "12345678")
            with ledger.db:
                ledger.db.execute("INSERT INTO positions VALUES ('fixture','{}',1,'1.00')")
            with self.assertRaisesRegex(RuntimeError, "open relay positions"):
                self.manager.set_mode({"mode": "shadow"})
            with ledger.db:
                ledger.db.execute("DELETE FROM positions")
                ledger.db.execute("INSERT INTO orders(id,message_id,source_group,contract,action,body,status,created_at) "
                                  "VALUES ('fixture','1','fixture','{}','OPEN','{}','submitting','2026-09-08T12:00:00Z')")
            with self.assertRaisesRegex(RuntimeError, "unresolved orders"):
                self.manager.set_mode({"mode": "shadow"})
            # Checking the ledger must not run Store's writable crash-recovery mutation.
            self.assertEqual(ledger.db.execute("SELECT status FROM orders").fetchone()[0], "submitting")
            self.assertEqual(self.path.read_bytes(), before)
        finally:
            ledger.close()

    def test_failed_pending_live_transition_can_return_to_shadow(self):
        self.mode_fixture()
        for state in (None, "error", "stopped", "stale"):
            with self.subTest(state=state):
                self.manager.set_mode({"mode": "live", "confirm_live": True})
                live = json.loads(self.path.read_text())
                if state is None:
                    (self.base / "state/runtime-status.json").unlink(missing_ok=True)
                else:
                    self.acknowledge_mode(state=state,
                        heartbeat_at="2000-01-01T00:00:00Z" if state == "stale" else datetime.now(timezone.utc).isoformat())
                self.assertTrue(self.manager.status()["trading"]["pending"])
                result = self.manager.set_mode({"mode": "shadow"})
                saved = json.loads(self.path.read_text())
                self.assertEqual(saved["database"], "state/original.sqlite3")
                self.assertFalse(saved["robinhood"]["enable_live_orders"])
                self.assertNotEqual(saved["mode_change_id"], live["mode_change_id"])
                self.assertTrue(result["paused"])
                self.assertTrue(result["trading"]["pending"])
                self.acknowledge_mode()

    def test_empty_author_filter_is_optional_but_malformed_filter_is_rejected(self):
        payload = self.channels()
        payload["channels"][0]["authors"] = []
        del payload["channels"][1]["authors"]
        result = self.manager.save_channels(payload)
        self.assertEqual([row["authors"] for row in result["channels"]], [[], []])
        saved = json.loads(self.path.read_text())
        self.assertTrue(self.manager._public_channels(saved)[1])
        self.assertEqual(saved["mode"], "shadow")
        self.assertEqual(saved["risk"], self.config["risk"])
        for bad in (None, "123456789012345678", ["not-an-id"], ["111111111111111111"] * 2):
            with self.subTest(authors=bad), self.assertRaises(ValueError):
                payload["channels"][0]["authors"] = bad
                self.manager.save_channels(payload)

    def test_discovery_enqueues_browser_request_without_changing_configuration(self):
        before = self.path.read_bytes()
        result = self.manager.discover_discord({})
        self.assertEqual(result["accepted"], "discovery_requested")
        self.assertEqual(result["discord"]["discovery"]["state"], "waiting")
        self.assertEqual(self.path.read_bytes(), before)
        for payload in ({"url": "https://example.invalid"}, {"guild_id": "bad"}, {"channel_id": "111111111111111111"}):
            with self.subTest(payload=payload), self.assertRaises(ValueError):
                self.manager.discover_discord(payload)

    def test_discord_login_feedback_distinguishes_sign_in_monitoring_and_stale_status(self):
        runtime_path = self.base / "state/runtime-status.json"
        runtime_path.parent.mkdir(parents=True, exist_ok=True)
        now = datetime.now(timezone.utc)

        def report(state, timestamp=now):
            runtime_path.write_text(json.dumps({
                "updated_at": timestamp.isoformat(),
                "discord": {"state": state, "channels": []},
            }))
            return self.manager.status()["discord"]

        pending = report("login_required")
        self.assertEqual(pending["state"], "not_connected")
        self.assertIn("verification", pending["detail"])
        connected = report("connected")
        self.assertEqual(connected["state"], "connected")
        self.assertIn("signed in", connected["detail"])
        self.assertIn("Save both channel URLs", connected["detail"])
        self.manager.save_channels(self.channels())
        self.assertIn("Waiting for channel monitoring", report("connected")["detail"])
        stale = report("connected", now - timedelta(minutes=2))
        self.assertEqual(stale["state"], "unknown")
        self.assertIn("status is unavailable", stale["detail"])
        self.assertNotIn("signed in", stale["detail"])
        self.assertEqual(report("needs_attention")["state"], "failed")

    def test_provider_diagnostics_survive_setup_projection_and_loading(self):
        from relay.interpreter import InterpretationError, safe_interpretation_reason
        from relay.status import failure_detail

        runtime_path = self.base / "state/runtime-status.json"
        runtime_path.parent.mkdir(parents=True, exist_ok=True)
        loading = "Discord is still loading. The browser will stay open; manual sign-in has no time limit."
        codex = safe_interpretation_reason(InterpretationError(code="timeout", attempts=2))
        broker = failure_detail(ConnectionError("private-token"), provider="Robinhood", phase="connection")
        runtime = {
            "updated_at": datetime.now(timezone.utc).isoformat(), "state": "running",
            "discord": {"state": "reconnecting", "detail": loading},
            "codex": {"state": "unavailable", "detail": codex},
            "broker": {"state": "unavailable", "detail": broker},
        }
        runtime_path.write_text(json.dumps(runtime))
        with patch.object(self.manager, "_codex_ready", return_value=True):
            status = self.manager.status()
        self.assertEqual(status["discord"]["state"], "starting")
        self.assertEqual(status["discord"]["detail"], loading)
        self.assertEqual(status["codex"]["detail"], codex)
        self.assertEqual(status["codex"]["state"], "failed")
        self.assertEqual(status["robinhood"]["detail"], broker)
        self.assertNotIn("private-token", json.dumps(status))
        self.manager._auth_state["robinhood"] = {"state": "failed", **self.manager._robinhood_failure(TimeoutError(), "callback")}
        self.assertEqual(self.manager.status()["robinhood"]["failure"]["code"], "auth_expired")
        runtime.update(state="error", detail="Worker startup failed [browser_profile_busy]. Keep the saved profile and reconnect.")
        runtime_path.write_text(json.dumps(runtime))
        discord = self.manager.status()["discord"]
        self.assertEqual(discord["state"], "failed")
        self.assertIn("browser_profile_busy", discord["detail"])
        runtime["updated_at"] = (datetime.now(timezone.utc) - timedelta(minutes=2)).isoformat()
        runtime_path.write_text(json.dumps(runtime))
        self.assertEqual(self.manager.status()["discord"]["state"], "unknown")
        for detail in ("Codex failure token=private-token", "Codex failure https://secret.example/token"):
            self.assertEqual(self.manager._runtime_auth_detail({"detail": detail}, "Codex", "fallback"), "fallback")

    def test_cancellation_closes_real_local_oauth_listener(self):
        from relay.broker import RobinhoodMCP
        started = threading.Event()
        captured = {}
        start_server = asyncio.start_server

        async def temporary_listener(callback, host, port, **kwargs):
            return await start_server(callback, host, 0, **kwargs)

        async def login(config, *, authorization_handler=None):
            broker = RobinhoodMCP(config, interactive=True, authorization_handler=authorization_handler)
            await broker._redirect("https://robinhood.com/oauth/authorize?state=synthetic-state")
            captured["broker"] = broker
            captured["address"] = broker.server.sockets[0].getsockname()
            started.set()
            await broker._callback()

        with patch("relay.setup.broker_login", login), patch("relay.broker.webbrowser.open", side_effect=AssertionError("Container browser must not open")), patch("relay.broker.asyncio.start_server", temporary_listener):
            self.manager.start_auth("robinhood")
            self.assertTrue(started.wait(2), "OAuth fixture did not start")
            waiting = self.manager.status()["robinhood"]
            self.assertEqual(waiting["authorization_url"], "https://robinhood.com/oauth/authorize?state=synthetic-state")
            result = self.manager.cancel_auth("robinhood")
            self.assertNotIn("authorization_url", result["robinhood"])
            self.assertEqual(result["robinhood"]["state"], "cancelled")
            self.assertFalse(self.manager._jobs["robinhood"].thread.is_alive())
            self.assertIsNone(captured["broker"].server)
            with socket.socket() as client:
                self.assertNotEqual(client.connect_ex(captured["address"]), 0)

    def test_pasted_callback_rejects_invalid_payloads_denial_and_offline_listener(self):
        for payload in (None, {}, {"callback_url": 1}, {"callback_url": ""},
                        {"callback_url": "x" * 8193}, {"callback_url": "x", "extra": True}):
            with self.assertRaises(ValueError):
                self.manager.complete_robinhood_callback(payload)
        base = "http://127.0.0.1:8766/callback"
        self.manager._jobs["robinhood"] = _AuthJob("robinhood")
        self.manager._auth_state["robinhood"] = {
            "state": "waiting", "authorization_url": "https://robinhood.com/oauth?" + urlencode({"redirect_uri": base}),
        }
        original = self.path.read_bytes()
        with patch("relay.setup.http.client.HTTPConnection") as connection:
            connection.return_value.getresponse.return_value.status = 200
            with self.assertRaisesRegex(ValueError, "authorization was denied"):
                self.manager.complete_robinhood_callback({"callback_url": base + "?error=access_denied&state=synthetic"})
            connection.return_value.close.assert_called_once()
            connection.return_value.request.side_effect = OSError("private listener detail")
            with self.assertRaisesRegex(RuntimeError, "callback listener is unavailable"):
                self.manager.complete_robinhood_callback({"callback_url": base + "?code=synthetic&state=synthetic"})
        self.assertEqual(self.path.read_bytes(), original)

    def test_pasted_callback_reuses_listener_state_and_preserves_saved_data(self):
        from relay.broker import RobinhoodMCP
        started = threading.Event()
        received = threading.Event()
        captured = {}
        start_server, http_connection = asyncio.start_server, http.client.HTTPConnection
        callback_base = "http://127.0.0.1:8766/callback"
        authorization = "https://robinhood.com/oauth?" + urlencode({
            "state": "synthetic-callback-state", "redirect_uri": callback_base})
        original = self.path.read_bytes()

        async def temporary_listener(callback, host, port, **kwargs):
            return await start_server(callback, host, 0, **kwargs)

        async def login(config, *, authorization_handler=None):
            broker = RobinhoodMCP(config, interactive=True, authorization_handler=authorization_handler)
            try:
                await broker._redirect(authorization)
                captured["address"] = broker.server.sockets[0].getsockname()
                started.set()
                captured["result"] = await broker._callback()
                received.set()
                await asyncio.sleep(3600)  # Hold before account inspection for this callback-only check.
            finally:
                await broker.__aexit__(None, None, None)

        def connect(host, port, **kwargs):
            self.assertEqual((host, port), ("127.0.0.1", 8766))
            return http_connection(*captured["address"], **kwargs)

        with patch("relay.setup.broker_login", login), \
                patch("relay.broker.asyncio.start_server", temporary_listener), \
                patch("relay.setup.http.client.HTTPConnection", side_effect=connect) as network:
            self.manager.start_auth("robinhood")
            self.assertTrue(started.wait(3))
            for url in ("https://foreign.example/callback?code=secret&state=x",
                        callback_base + "#code=secret", callback_base + "?code=x\r\nX: y",
                        "http://user@127.0.0.1:8766/callback?code=secret&state=x"):
                with self.assertRaises(ValueError):
                    self.manager.complete_robinhood_callback({"callback_url": url})
            network.assert_not_called()
            with self.assertRaisesRegex(ValueError, "rejected"):
                self.manager.complete_robinhood_callback({"callback_url": callback_base + "?code=secret&state=wrong"})
            self.assertFalse(received.is_set())
            result = self.manager.complete_robinhood_callback({"callback_url": callback_base + "?code=synthetic-code&state=synthetic-callback-state"})
            self.assertTrue(received.wait(3))
            self.assertEqual(captured["result"], ("synthetic-code", "synthetic-callback-state"))
            self.assertNotIn("synthetic-code", json.dumps(result))
            self.assertEqual(self.path.read_bytes(), original)
            self.manager.cancel_auth("robinhood")
            with self.assertRaisesRegex(RuntimeError, "No Robinhood sign-in"):
                self.manager.complete_robinhood_callback({"callback_url": callback_base + "?code=synthetic-code&state=synthetic-callback-state"})

    def test_cancelled_flow_never_starts_next_provider_operation(self):
        event = threading.Event()
        event.set()
        operation = AsyncMock()
        with self.assertRaises(_AuthCancelled):
            asyncio.run(self.manager._await_with_cancel(operation, event))
        operation.assert_not_called()

    def test_codex_device_output_is_whitelisted(self):
        class FakeProcess:
            pid = 22222

            def __init__(self):
                self.stdout = io.StringIO(
                    "secret=do-not-display\n"
                    "Open https://auth.openai.com/codex/device\n"
                    "Enter code ABCD-1234\n"
                )

            def poll(self):
                return 0

            def wait(self, timeout=None):
                (Path(os.environ["CODEX_HOME"]) / "auth.json").parent.mkdir(parents=True, exist_ok=True)
                (Path(os.environ["CODEX_HOME"]) / "auth.json").write_text("{}")
                return 0

            def terminate(self):
                return None

        with patch.object(self.manager, "_codex_executable", return_value="/usr/bin/codex"), patch(
            "relay.setup.subprocess.Popen", return_value=FakeProcess()
        ), patch("relay.setup.CodexInterpreter") as interpreter:
            interpreter.return_value.subscription_status = AsyncMock(return_value={"authenticated": True, "isolated_execution_available": True})
            self.manager.start_auth("codex")
            self.wait_for(lambda: self.manager.status()["codex"]["state"] == "connected")
        status = self.manager.status()["codex"]
        self.assertNotIn("secret", json.dumps(status))
        self.assertNotIn("do-not-display", json.dumps(status))
        self.assertNotIn("verification_url", status)

    def test_real_cli_prompt_and_reauthentication_status(self):
        job = _AuthJob("codex")
        self.manager._auth_state["codex"] = {"state": "starting", "detail": "Starting"}
        lines = [
            "Follow these steps to sign in with ChatGPT using device code authorization:",
            "   \x1b[94mhttps://auth.openai.com/codex/device\x1b[0m",
            "2. Enter this one-time code \x1b[90m(expires in 15 minutes)\x1b[0m",
            "   \x1b[94mABCD-1234\x1b[0m",
        ]
        for line in lines:
            self.manager._consume_codex_line(job, line)
        home = Path(os.environ["CODEX_HOME"])
        home.mkdir()
        (home / "auth.json").write_text("{}")
        status = self.manager.status()["codex"]
        self.assertEqual(status["state"], "waiting")
        self.assertEqual(status["user_code"], "ABCD-1234")
        self.assertEqual(status["verification_url"], "https://auth.openai.com/codex/device")
        self.manager._auth_state["codex"] = {"state": "failed", "detail": "Failure"}
        self.assertEqual(self.manager.status()["codex"]["state"], "failed")
        self.manager._auth_state["codex"] = {"state": "not_connected", "detail": "Check"}
        with patch("relay.setup.CodexInterpreter") as interpreter:
            interpreter.return_value.subscription_status = AsyncMock(return_value={"authenticated": False})
            self.assertEqual(self.manager.status()["codex"]["state"], "not_connected")
        self.assertIsNone(self.manager._whitelisted_device_url("https://auth.openai.com:444/codex/device"))

    def test_robinhood_inspection_failure_identifies_stage_without_private_data(self):
        from relay.broker import RobinhoodBroker
        original = self.path.read_bytes()

        async def fake_inspect(args, config):
            config["robinhood"]["account_number"] = "123456789"
            try:
                RobinhoodBroker(config)._qualified("get_accounts")
            except Exception as error:
                raise ExceptionGroup("private provider response", [error])

        catalog = [{"name": "get_accounts", "inputSchema": {"type": "object"}, "outputSchema": {"type": "object"},
                    "private_provider_field": "private provider response"},
                   {"name": "unsupported_tool", "inputSchema": {"type": "object"}}]
        with patch("relay.setup.broker_login", AsyncMock(return_value=catalog)), patch("relay.setup.inspect_broker", fake_inspect):
            self.manager.start_auth("robinhood")
            self.wait_for(lambda: self.manager.status()["robinhood"]["state"] == "failed")
        result = self.manager.status()["robinhood"]
        self.assertIn("account inspection", result["detail"])
        self.assertEqual(result["failure"]["phase"], "account_inspection")
        self.assertEqual(result["failure"]["type"], "BrokerError")
        self.assertEqual(result["failure"]["source"], "broker.py")
        self.assertIsInstance(result["failure"]["line"], int)
        self.assertFalse(result["token_present"])
        self.assertNotIn("123456789", json.dumps(result))
        self.assertNotIn("private provider response", json.dumps(result))
        self.assertEqual(self.path.read_bytes(), original)
        schemas = self.manager.robinhood_schemas()
        self.assertEqual([tool["name"] for tool in schemas["tools"]], ["get_accounts"])
        self.assertNotIn("private", json.dumps(schemas))
        schemas["tools"].clear()
        self.assertEqual(len(self.manager.robinhood_schemas()["tools"]), 1)

    def test_robinhood_failure_diagnostics_omit_exception_secrets(self):
        class UnprintableError(Exception):
            def __str__(self):
                raise ValueError("private error")

        self.assertIn("[operation_failed]", self.manager._robinhood_failure(UnprintableError(), "authorization")["detail"])
        error = RuntimeError("token=private-token code=private-code /private/path")
        error.response = SimpleNamespace(status_code=401)
        result = self.manager._robinhood_failure(ExceptionGroup("private response", [error]), "account_inspection")
        self.assertEqual(result["failure"]["http_status"], 401)
        self.assertNotIn("private", json.dumps(result))
        timeout = self.manager._robinhood_failure(TimeoutError("private timeout"), "callback")
        self.assertIn("expired before its callback", timeout["detail"])
        self.assertNotIn("private", json.dumps(timeout))

    def test_auth_failure_diagnostics_have_fixed_codes_and_actions(self):
        cases = (
            (TimeoutError("provider timeout secret=private"), "callback", "auth_expired"),
            (RuntimeError("capability report does not match account=123456789"), "account_binding", "report_mismatch"),
            (RuntimeError("robinhood.auth must be oauth or token"), "initialization", "setup_unsupported"),
            (RuntimeError("name or service not known /private/provider"), "authorization", "dns_failed"),
        )
        for error, phase, code in cases:
            with self.subTest(code=code):
                result = self.manager._robinhood_failure(error, phase)
                self.assertIn(f"[{code}]", result["detail"])
                self.assertIn("Action:", result["detail"])
                self.assertEqual(result["failure"]["code"], code)
                self.assertNotIn("private", json.dumps(result))

    def test_codex_cli_diagnostics_are_fixed_and_runtime_detail_is_bounded(self):
        self.assertEqual(self.manager._codex_line_code("TLS certificate verify failed"), "tls_failed")
        detail = self.manager._codex_failure_detail("runtime_unavailable", "initialization")
        self.assertIn("[runtime_unavailable]", detail)
        self.assertIn("Action:", detail)
        self.assertNotIn("https://", detail)
        self.assertEqual(
            self.manager._runtime_auth_detail(
                {"detail": "Codex login failed [auth_required]. Action: retry."},
                "Codex",
                "fallback",
            ),
            "Codex login failed [auth_required]. Action: retry.",
        )
        self.assertEqual(
            self.manager._runtime_auth_detail(
                {"detail": "provider=https://secret.example/token"},
                "Codex",
                "fallback",
            ),
            "fallback",
        )

    def test_fresh_robinhood_binding_uses_shadow_ledger(self):
        async def fake_login(config, *, authorization_handler=None):
            return {"tools": []}

        async def fake_inspect(args, config):
            config["robinhood"]["account_number"] = "123456789"
            report = {
                "account": {
                    "nickname": "Fixture",
                    "last_four": "6789",
                    "type": "cash",
                    "state": "active",
                    "option_level": "option_level_2",
                    "agentic_allowed": True,
                }
            }
            output = Path(args.output)
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_text(json.dumps(report))

        with patch("relay.setup.broker_login", fake_login), patch("relay.setup.inspect_broker", fake_inspect):
            self.manager.start_auth("robinhood", {"account_number": "123456789"})
            self.wait_for(lambda: self.manager.status()["robinhood"]["state"] == "connected")
        saved = json.loads(self.path.read_text())
        self.assertEqual(saved["robinhood"]["account_number"], "123456789")
        self.assertFalse(saved["robinhood"]["enable_live_orders"])
        self.assertEqual(saved["mode"], "shadow")
        self.assertEqual(saved["database"], "state/relay-shadow.sqlite3")
        self.assertEqual(self.manager.status()["robinhood"]["last_four"], "6789")
        job = self.manager._jobs["robinhood"]
        job.thread.join(2)
        self.assertFalse(job.thread.is_alive())
        self.assertEqual(self.manager.cancel_auth("robinhood")["robinhood"]["state"], "connected")
        self.assertEqual(json.loads(self.path.read_text()), saved)
        self.assertFalse(job.cancelled.is_set())

    def test_late_cancel_preserves_failed_login_status(self):
        self.manager._auth_state["codex"] = {"state": "failed", "detail": "Synthetic failure"}
        state = self.manager.cancel_auth("codex")["codex"]
        self.assertEqual(state["state"], "failed")
        self.assertEqual(state["detail"], "Synthetic failure")

    def test_existing_live_binding_is_not_downgraded(self):
        self.config["mode"] = "live"
        self.config["database"] = "state/live.sqlite3"
        self.config["robinhood"].update(account_number="123456789", enable_live_orders=True)
        self.path.write_text(json.dumps(self.config))

        async def fake_login(config, *, authorization_handler=None):
            return {"tools": []}

        async def fake_inspect(args, config):
            config["robinhood"]["account_number"] = "123456789"
            output = Path(args.output)
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_text(json.dumps({"account": {"last_four": "6789", "state": "active"}}))

        with patch("relay.setup.broker_login", fake_login), patch("relay.setup.inspect_broker", fake_inspect):
            self.manager.start_auth("robinhood")
            self.wait_for(lambda: self.manager.status()["robinhood"]["state"] == "connected")
        saved = json.loads(self.path.read_text())
        self.assertEqual(saved["mode"], "live")
        self.assertEqual(saved["database"], "state/live.sqlite3")
        self.assertTrue(saved["robinhood"]["enable_live_orders"])

    def test_robinhood_payload_rejects_non_digit_account(self):
        with self.assertRaises(ValueError):
            self.manager.start_auth("robinhood", {"account_number": "abc"})
        with self.assertRaises(ValueError):
            self.manager.start_auth("robinhood", {"account_number": "123", "extra": True})

    def test_robinhood_cancel_stops_provider_before_claiming_cancelled(self):
        started = threading.Event()
        closed = threading.Event()

        async def fake_login(config, *, authorization_handler=None):
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                closed.set()

        with patch("relay.setup.broker_login", fake_login):
            self.manager.start_auth("robinhood")
            self.assertTrue(started.wait(2))
            result = self.manager.cancel_auth("robinhood")
        self.assertTrue(closed.is_set())
        self.assertEqual(result["robinhood"]["state"], "cancelled")
        self.assertIsNone(json.loads(self.path.read_text())["robinhood"]["account_number"])
        self.assertFalse((self.base / "state/robinhood-capabilities.json").exists())

    def test_channel_edit_during_robinhood_auth_survives_commit(self):
        started = threading.Event()
        release = threading.Event()

        async def fake_login(config, *, authorization_handler=None):
            started.set()
            while not release.is_set():
                await asyncio.sleep(0.01)

        async def fake_inspect(args, config):
            config["robinhood"]["account_number"] = "123456789"
            output = Path(args.output)
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_text(json.dumps({"account": {"last_four": "6789", "state": "active"}}))

        with patch("relay.setup.broker_login", fake_login), patch("relay.setup.inspect_broker", fake_inspect):
            self.manager.start_auth("robinhood")
            self.assertTrue(started.wait(2))
            self.manager.save_channels(self.channels())
            release.set()
            self.wait_for(lambda: self.manager.status()["robinhood"]["state"] == "connected")
        saved = json.loads(self.path.read_text())
        self.assertEqual(saved["channels"][0]["id"], "222222222222222222")
        self.assertEqual(saved["robinhood"]["account_number"], "123456789")

    def test_conflicting_account_does_not_promote_report_or_change_config(self):
        self.config["robinhood"].update(account_number="111111111", enable_live_orders=False)
        self.path.write_text(json.dumps(self.config))
        original = self.path.read_text()

        async def fake_login(config, *, authorization_handler=None):
            return {"tools": []}

        async def fake_inspect(args, config):
            config["robinhood"]["account_number"] = "222222222"
            output = Path(args.output)
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_text(json.dumps({"account": {"last_four": "2222", "state": "active"}}))

        with patch("relay.setup.broker_login", fake_login), patch("relay.setup.inspect_broker", fake_inspect):
            self.manager.start_auth("robinhood")
            self.wait_for(lambda: not self.manager._jobs["robinhood"].thread.is_alive())
        self.assertEqual(self.manager.status()["robinhood"]["state"], "failed")
        self.assertEqual(self.path.read_text(), original)
        self.assertFalse((self.base / "state/robinhood-capabilities.json").exists())

    def test_status_validates_token_file_and_allowlists_risk_fields(self):
        self.config["robinhood"].update(account_number="123456789")
        self.config["risk"]["private_secret"] = "never-display"
        self.path.write_text(json.dumps(self.config))
        token_path = self.base / "state/robinhood-oauth.json"
        token_path.parent.mkdir()
        token_path.write_text("{\"tokens\": {\"access_token\": \"bad\"}}")
        with patch("relay.setup._OAuthStorage.get_tokens", new=AsyncMock(return_value=None)):
            status = self.manager.status()
        self.assertEqual(status["robinhood"]["state"], "not_connected")
        self.assertNotIn("private_secret", status["risk"])


if __name__ == "__main__":
    unittest.main()
