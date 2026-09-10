"""Exercise setup writes through real HTTP, without provider connections."""
import http.client
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

from relay.dashboard import DashboardApp, DashboardHTTPServer
from relay.setup import SetupManager


class SetupHTTPTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.config = Path(self.temp.name) / "config.json"
        self.config.write_text(json.dumps({"mode": "paper", "database": "ledger.sqlite3"}))
        self.app = DashboardApp(self.config)
        self.manager = Mock()
        self.manager.status.return_value = {"configured": False}
        for name in ("save_channels", "save_notifications", "start_auth", "cancel_auth", "complete_robinhood_callback", "set_paused", "set_mode", "set_expiry_policy", "reconnect", "discover_discord"):
            getattr(self.manager, name).return_value = {"accepted": True}
        self.app.setup = self.manager
        self.server = DashboardHTTPServer(("127.0.0.1", 0), self.app)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.host = "127.0.0.1:" + str(self.server.server_port)

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        self.manager.close.assert_called_once()
        self.temp.cleanup()

    def request(self, path, payload=None, *, method="POST", headers=None, body=None):
        values = {"Origin": "http://" + self.host, "Content-Type": "application/json",
                  "X-Relay-CSRF": self.app.csrf_token}
        values.update(headers or {})
        values = {k: v for k, v in values.items() if v is not None}
        client = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=3)
        try:
            client.request(method, path, body=json.dumps(payload or {}) if body is None else body, headers=values)
            response = client.getresponse()
            return response.status, json.loads(response.read())
        finally:
            client.close()

    def test_schema_diagnostics_are_a_fixed_read_only_route(self):
        self.manager.robinhood_schemas.return_value = {"tools": []}
        self.assertEqual(self.request("/api/setup/robinhood/schemas", method="GET"), (200, {"tools": []}))
        self.manager.robinhood_schemas.assert_called_once_with()
        self.assertEqual(self.request("/api/setup/robinhood/schemas?path=private", method="GET")[0], 400)
        self.assertEqual(self.request("/api/setup/robinhood/schemas")[0], 405)

    def test_fixed_actions_and_public_csrf(self):
        status, result = self.request("/api/setup", method="GET")
        self.assertEqual(status, 200)
        self.assertEqual(result["csrf_token"], self.app.csrf_token)
        actions = [
            ("channels", {"channels": []}, "save_channels", ({"channels": []},)),
            ("notifications", {"enabled": False}, "save_notifications", ({"enabled": False},)),
            ("discord/discover", {"guild_id": "111111111111111111"}, "discover_discord", ({"guild_id": "111111111111111111"},)),
            ("pause", {"paused": True}, "set_paused", (True,)),
            ("expiry-policy", {"allow_same_day_expiry": True}, "set_expiry_policy", ({"allow_same_day_expiry": True},)),
            ("mode", {"mode": "live", "confirm_live": True}, "set_mode", ({"mode": "live", "confirm_live": True},)),
            ("reconnect", {}, "reconnect", ()),
            ("auth/codex/start", {}, "start_auth", ("codex", {})),
            ("auth/robinhood/cancel", {}, "cancel_auth", ("robinhood",)),
            ("auth/robinhood/callback", {"callback_url": "http://127.0.0.1:8766/callback?code=synthetic&state=test"}, "complete_robinhood_callback", ({"callback_url": "http://127.0.0.1:8766/callback?code=synthetic&state=test"},)),
        ]
        for path, payload, name, args in actions:
            with self.subTest(path=path):
                status, result = self.request("/api/setup/" + path, payload)
                self.assertEqual(status, 200, result)
                self.assertEqual(result["csrf_token"], self.app.csrf_token)
                getattr(self.manager, name).assert_called_with(*args)

    def test_origin_csrf_and_request_shape_guards(self):
        cases = [
            ({"Origin": None}, 403), ({"Origin": "https://foreign.example"}, 403),
            ({"X-Relay-CSRF": None}, 403), ({"X-Relay-CSRF": "wrong"}, 403),
            ({"X-Relay-CSRF": "\u00e9"}, 403), ({"Sec-Fetch-Site": "cross-site"}, 403),
            ({"Content-Type": "text/plain"}, 415),
            ({"Content-Length": "32769"}, 413),
            ({"Transfer-Encoding": "chunked"}, 400),
        ]
        for headers, expected in cases:
            with self.subTest(headers=headers):
                self.assertEqual(self.request("/api/setup/reconnect", headers=headers)[0], expected)
        for body in ("[]", "null", "{", '"text"'):
            self.assertEqual(self.request("/api/setup/reconnect", body=body)[0], 400)
        for path, payload in [("pause", {"paused": 1}), ("pause", {"paused": True, "mode": "live"}),
                              ("reconnect", {"command": "anything"}), ("auth/codex/cancel", {"path": "/"})]:
            self.assertEqual(self.request("/api/setup/" + path, payload)[0], 400)
        self.assertEqual(self.request("/api/setup/reconnect?command=x")[0], 400)
        self.manager.reconnect.assert_not_called()
        self.manager.set_paused.assert_not_called()
        self.manager.cancel_auth.assert_not_called()
        self.assertEqual(self.request("/api/setup/discord/discover", headers={"X-Relay-CSRF": None})[0], 403)
        self.manager.discover_discord.assert_not_called()
        self.assertEqual(self.request("/api/setup/mode", {"mode": "live", "confirm_live": True},
                                      headers={"X-Relay-CSRF": None})[0], 403)
        self.manager.set_mode.assert_not_called()
        self.assertEqual(self.request("/api/setup/expiry-policy", {"allow_same_day_expiry": True},
                                      headers={"X-Relay-CSRF": None})[0], 403)
        self.manager.set_expiry_policy.assert_not_called()

    def test_unsupported_routes_and_errors(self):
        for method, path in [("PUT", "/api/setup/pause"), ("DELETE", "/api/setup/channels"),
                             ("POST", "/api/orders"), ("POST", "/api/setup/command")]:
            self.assertEqual(self.request(path, method=method)[0], 405)
        for error, status in [(ValueError("Invalid channels"), 400), (RuntimeError("Already running"), 409),
                              (OSError("private credential detail"), 500)]:
            self.manager.reconnect.side_effect = error
            result_status, result = self.request("/api/setup/reconnect")
            self.assertEqual(result_status, status)
            self.assertNotIn("private credential", json.dumps(result))
        self.app.setup = None
        self.assertEqual(self.request("/api/setup/reconnect")[0], 405)
        self.assertEqual(self.request("/api/setup", method="GET")[0], 404)
        self.app.setup = self.manager

    def test_expiry_checkbox_survives_refresh_and_manager_restart(self):
        try:
            from playwright.sync_api import expect, sync_playwright
        except ImportError:
            self.skipTest("Playwright is required for the real-backend refresh check")
        template = Path(__file__).resolve().parents[1] / "config.example.json"
        config = json.loads(template.read_text())
        self.config.write_text(json.dumps(config))
        with patch("relay.setup.SetupManager._codex_ready", return_value=False):
            manager = SetupManager(self.config)
            self.app.setup = manager
            manager.set_paused(True)
            try:
                with sync_playwright() as playwright:
                    browser = playwright.chromium.launch(headless=True)
                    try:
                        page = browser.new_page()
                        page.route("**/api/setup/discord/discover", lambda route: route.fulfill(
                            status=409, content_type="application/json",
                            body=json.dumps({"detail": "Discovery is disabled in this synthetic test"})))
                        page.goto("http://" + self.host + "/#setup")
                        expiry = page.get_by_role("checkbox", name="Allow same-day (0DTE) entries")
                        expect(expiry).to_be_enabled()
                        with page.expect_response(lambda response: response.url.endswith("/api/setup/expiry-policy")) as saved:
                            expiry.check()
                        self.assertEqual(saved.value.status, 200)
                        self.assertTrue(saved.value.json()["risk"]["allow_same_day_expiry"])
                        expected = json.loads(self.config.read_text())
                        self.assertTrue(expected["risk"]["allow_same_day_expiry"])
                        for restart in (False, True):
                            if restart:
                                manager.close()
                                manager = SetupManager(self.config)
                                self.app.setup = manager
                            page.reload()
                            expect(expiry).to_be_checked()
                            expect(expiry).to_be_disabled()
                            expect(page.get_by_role("button", name="Resume relay")).to_be_disabled()
                            self.assertEqual(json.loads(self.config.read_text()), expected)
                    finally:
                        browser.close()
            finally:
                manager.close()
                self.app.setup = self.manager

    def test_real_channel_save_accepts_all_authors_and_reports_bad_filter(self):
        template = Path(__file__).resolve().parents[1] / "config.example.json"
        config = json.loads(template.read_text())
        self.config.write_text(json.dumps(config))
        with patch("relay.setup.SetupManager._codex_ready", return_value=False):
            manager = SetupManager(self.config)
            self.app.setup = manager
            try:
                payload = {"poll_seconds": 2, "channels": [
                    {"url": "https://discord.com/channels/111111111111111111/222222222222222222",
                     "name": "Signals one", "role": "signals", "authors": []},
                    {"url": "https://discord.com/channels/111111111111111111/333333333333333333",
                     "name": "Signals two", "role": "signals"},
                ]}
                code, result = self.request("/api/setup/channels", payload)
                self.assertEqual(code, 200, result)
                self.assertEqual(result["accepted"], "channels_saved")
                self.assertEqual([row["authors"] for row in result["channels"]], [[], []])
                saved = json.loads(self.config.read_text())
                self.assertEqual(saved["risk"], config["risk"])
                self.assertEqual(saved["mode"], config["mode"])
                payload["channels"][0]["authors"] = None
                code, result = self.request("/api/setup/channels", payload)
                self.assertEqual(code, 400)
                self.assertIn("authors", result["error"])
                self.assertEqual(json.loads(self.config.read_text()), saved)
            finally:
                manager.close()
                self.app.setup = self.manager
