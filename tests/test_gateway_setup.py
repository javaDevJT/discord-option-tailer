"""Offline checks for Discord gateway setup and credential handling."""

from __future__ import annotations

import json
import os
import stat
import tempfile
import unittest
from http.client import HTTPConnection
from pathlib import Path
from unittest.mock import patch

from relay.dashboard import DashboardApp, DashboardHTTPServer
from relay.discord_session import credential_configured, read_token, write_token
from relay.setup import SetupManager


ROOT = Path(__file__).resolve().parents[1]


class GatewaySetupTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        template = json.loads((ROOT / "config.example.json").read_text(encoding="utf-8"))
        template["mode"] = "shadow"
        template["discord"] = {
            "transport": "browser",
            "token_store": "state/discord-user.json",
            "token_env": "DISCORD_USER_TOKEN",
        }
        template["runtime_status_file"] = "state/runtime-status.json"
        self.config = self.root / "config.json"
        self.config.write_text(json.dumps(template), encoding="utf-8")
        self.manager = SetupManager(self.config)
        self.validate_patch = patch.object(self.manager, "_validate_candidate", return_value=None)
        self.validate_patch.start()

    def tearDown(self) -> None:
        self.validate_patch.stop()
        self.manager.close()
        self.temp.cleanup()

    def test_token_store_is_private_and_atomic(self) -> None:
        config = {"discord": {"token_store": str(self.root / "state" / "discord-user.json")}}
        write_token(config, "  synthetic-token  ")
        path = self.root / "state" / "discord-user.json"
        self.assertEqual(read_token(config), "synthetic-token")
        self.assertTrue(credential_configured(config))
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.assertEqual(json.loads(path.read_text(encoding="utf-8"))["token"], "synthetic-token")
        self.assertNotIn("synthetic-token", json.dumps({"discord": {"credential_configured": True}}))

    def test_symlink_token_store_is_rejected(self) -> None:
        state = self.root / "state"
        state.mkdir()
        target = state / "real-token"
        target.write_text(json.dumps({"token": "old-token"}), encoding="utf-8")
        path = state / "discord-user.json"
        path.symlink_to(target)
        with self.assertRaises(ValueError):
            write_token({"discord": {"token_store": str(path)}}, "new-token")
        self.assertEqual(json.loads(target.read_text(encoding="utf-8"))["token"], "old-token")

    def test_gateway_save_never_returns_token_and_requests_reconnect(self) -> None:
        result = self.manager.save_discord({"transport": "gateway", "token": "  synthetic-token  "})
        saved = json.loads(self.config.read_text(encoding="utf-8"))
        self.assertEqual(saved["discord"]["transport"], "gateway")
        self.assertNotIn("token", saved["discord"])
        token_path = self.root / "state" / "discord-user.json"
        self.assertEqual(json.loads(token_path.read_text(encoding="utf-8"))["token"], "synthetic-token")
        self.assertTrue((self.root / "state" / "RECONNECT").is_file())
        self.assertNotIn("synthetic-token", json.dumps(result))
        self.assertEqual(result["discord"]["transport"], "gateway")
        self.assertTrue(result["discord"]["credential_configured"])
        self.assertNotIn("browser_url", result["discord"])

    def test_empty_token_preserves_saved_credential(self) -> None:
        self.manager.save_discord({"transport": "gateway", "token": "synthetic-token"})
        token_path = self.root / "state" / "discord-user.json"
        before = token_path.read_bytes()
        self.manager.save_discord({"transport": "gateway", "token": "   "})
        self.assertEqual(token_path.read_bytes(), before)

    def test_gateway_post_is_csrf_and_same_origin_protected(self) -> None:
        app = DashboardApp(self.config, enable_setup=True)
        try:
            server = DashboardHTTPServer(("127.0.0.1", 0), app)
        except PermissionError:
            app.setup.close()
            self.skipTest("sandbox does not permit loopback HTTP binds")
        thread = __import__("threading").Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            connection = HTTPConnection("127.0.0.1", server.server_port, timeout=3)
            body = json.dumps({"transport": "gateway", "token": "synthetic-token"})
            headers = {
                "Content-Type": "application/json",
                "Content-Length": str(len(body.encode("utf-8"))),
                "Host": "127.0.0.1",
                "Origin": "http://127.0.0.1",
                "X-Relay-CSRF": app.csrf_token,
            }
            connection.request("POST", "/api/setup/discord", body=body, headers=headers)
            response = connection.getresponse()
            payload = json.loads(response.read())
            connection.close()
            self.assertEqual(response.status, 200, payload)
            self.assertNotIn("synthetic-token", json.dumps(payload))

            connection = HTTPConnection("127.0.0.1", server.server_port, timeout=3)
            headers["Origin"] = "https://foreign.example"
            connection.request("POST", "/api/setup/discord", body=body, headers=headers)
            response = connection.getresponse()
            response.read()
            connection.close()
            self.assertEqual(response.status, 403)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=3)
            app.setup.close()


if __name__ == "__main__":
    unittest.main()
