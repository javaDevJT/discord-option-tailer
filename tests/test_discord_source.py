"""Offline transport dispatch, configuration and directory IPC checks."""

import asyncio
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from relay import discord_source
from relay.core import Hold, load_config
from relay.discovery import discovery_status, request_discovery, serve_discovery
from relay.discord_session import read_token


class DiscordSourceTests(unittest.IsolatedAsyncioTestCase):
    async def test_both_transports_keep_callback_and_verifier_contract(self):
        callback, verifier, status = object(), object(), object()
        for transport in ("browser", "gateway"):
            config = {"discord": {"transport": transport}}
            with patch(f"relay.{transport}.monitor", new=AsyncMock()) as monitor:
                await discord_source.monitor(config, callback, register_verifier=verifier, on_status=status)
                monitor.assert_awaited_once_with(config, callback, register_verifier=verifier, on_status=status)
        self.assertEqual(discord_source.transport({}), "browser")
        with self.assertRaises(Hold):
            discord_source.transport({"discord": {"transport": "invalid"}})

    async def test_gateway_directory_uses_same_validated_private_ipc_without_browser(self):
        with tempfile.TemporaryDirectory() as directory:
            runtime = Path(directory) / "state/runtime.json"
            request = request_discovery(runtime, {})
            resolver = AsyncMock(return_value={
                "state": "ready", "request_id": request["request_id"],
                "guilds": [{"id": "111111111111111111", "name": "Fixture"}],
                "channels": [], "authors": [], "detail": "Gateway cache ready.",
                "private_field": "must not persist",
            })
            task = asyncio.create_task(serve_discovery(None, runtime, resolver=resolver))
            try:
                async def completed():
                    while discovery_status(runtime)["state"] == "waiting":
                        await asyncio.sleep(.005)
                await asyncio.wait_for(completed(), timeout=2)
                result = discovery_status(runtime)
                self.assertEqual(result["state"], "ready")
                self.assertEqual(result["guilds"][0]["name"], "Fixture")
                self.assertNotIn("private_field", result)
                resolver.assert_awaited_once()
            finally:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)

    def test_config_defaults_validation_and_symlink_is_not_resolved_away(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            template = json.loads((Path(__file__).resolve().parents[1] / "config.example.json").read_text())
            template.pop("discord", None)
            path = root / "config.json"
            path.write_text(json.dumps(template))
            config = load_config(path)
            self.assertEqual(discord_source.transport(config), "browser")
            store = root / "state/discord-user.json"
            store.parent.mkdir()
            target = root / "elsewhere"
            target.write_text("synthetic-private-token")
            store.symlink_to(target)
            with patch.dict("os.environ", {}, clear=True):
                self.assertIsNone(read_token(load_config(path)))
            for discord in ({"transport": "wrong"}, {"token": "synthetic-private-token"}, {"token_env": "BAD KEY"}):
                path.write_text(json.dumps(template | {"discord": discord}))
                with self.assertRaises(Hold):
                    load_config(path)


if __name__ == "__main__":
    unittest.main()
