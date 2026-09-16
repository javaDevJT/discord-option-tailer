import asyncio
import json
import unittest

from relay.discord_auth import DiscordLoginDiagnostics


class _Emitter:
    def __init__(self):
        self.listeners = {}

    def on(self, event, callback):
        self.listeners.setdefault(event, []).append(callback)

    def off(self, event, callback):
        if callback in self.listeners.get(event, []):
            self.listeners[event].remove(callback)

    async def emit(self, event, value):
        for callback in tuple(self.listeners.get(event, ())):
            callback(value)


class _Response:
    def __init__(self, url, status, payload=None, *, delay=0):
        self.url = url
        self.status = status
        self.payload = payload
        self.delay = delay

    async def body(self):
        if self.delay:
            await asyncio.sleep(self.delay)
        return json.dumps(self.payload).encode()


class _Request:
    def __init__(self, url, failure):
        self.url = url
        self.failure = failure


class DiscordLoginDiagnosticsTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.context = _Emitter()
        self.diagnostics = DiscordLoginDiagnostics(self.context)
        self.addAsyncCleanup(self.diagnostics.close)

    async def test_exact_endpoints_and_failure_classifiers(self):
        for url in ("https://discord.com/api/v9/auth/login", "https://discord.com/api/v10/auth/mfa/totp"):
            await self.context.emit("response", _Response(url, 401))
        self.assertIn("auth_rejected", self.diagnostics.detail("fallback"))
        self.diagnostics.clear()
        for url in (
            "http://discord.com/api/v9/auth/login",
            "https://discord.com.evil/api/v9/auth/login",
            "https://discord.com/api/auth/login",
            "https://discord.com/api/v9/users/@me",
            "https://discord.com/api/v9/auth/login/extra",
        ):
            await self.context.emit("response", _Response(url, 401))
        self.assertEqual(self.diagnostics.detail("fallback"), "fallback")

        await self.context.emit(
            "response",
            _Response("https://discord.com/api/v9/auth/login", 400, {"captcha_key": ["captcha-required"]}),
        )
        await asyncio.sleep(0)
        self.assertIn("captcha_requested", self.diagnostics.detail("fallback"))
        self.assertNotIn("captcha_rejected", self.diagnostics.detail("fallback"))

        self.diagnostics.clear()
        await self.context.emit(
            "response",
            _Response("https://discord.com/api/v9/auth/login", 400, {"code": "INVALID_LOGIN"}),
        )
        await asyncio.sleep(0)
        self.assertIn("auth_rejected", self.diagnostics.detail("fallback"))
        self.assertIn("INVALID_LOGIN", self.diagnostics.detail("fallback"))
        self.diagnostics.clear()
        await self.context.emit("response", _Response("https://discord.com/api/v9/auth/login", 429))
        self.assertIn("rate_limited", self.diagnostics.detail("fallback"))
        self.diagnostics.clear()
        await self.context.emit("requestfailed", _Request("https://discord.com/api/v9/auth/mfa/totp", "ERR_NAME_NOT_RESOLVED"))
        self.assertIn("network_failed", self.diagnostics.detail("fallback"))

    async def test_no_secret_leak_and_success_does_not_clear(self):
        await self.context.emit(
            "response",
            _Response(
                "https://discord.com/api/v9/auth/login?password=SECRET",
                400,
                {"code": 40001, "message": "PRIVATE", "token": "SECRET_TOKEN", "captcha_rqdata": "SECRET_RQ"},
            ),
        )
        await asyncio.sleep(0)
        detail = self.diagnostics.detail("fallback")
        self.assertIn("40001", detail)
        for secret in ("SECRET", "PRIVATE", "SECRET_TOKEN", "SECRET_RQ"):
            self.assertNotIn(secret, detail)
        await self.context.emit("response", _Response("https://discord.com/api/v9/auth/login", 200, {"token": "NEW"}))
        self.assertEqual(self.diagnostics.detail("fallback"), detail)

    async def test_newest_failure_wins_clear_invalidates_old_tasks_and_close_cleans_up(self):
        old = _Response("https://discord.com/api/v9/auth/login", 400, {"code": "INVALID_LOGIN"}, delay=0.03)
        await self.context.emit("response", old)
        await self.context.emit("response", _Response("https://discord.com/api/v9/auth/login", 429))
        await asyncio.sleep(0.05)
        self.assertIn("rate_limited", self.diagnostics.detail("fallback"))

        pending = _Response("https://discord.com/api/v9/auth/login", 400, {"code": "INVALID_LOGIN"}, delay=60)
        await self.context.emit("response", pending)
        self.diagnostics.clear()
        await asyncio.sleep(0)
        self.assertEqual(self.diagnostics.detail("fallback"), "fallback")
        self.assertTrue(self.diagnostics._tasks)
        await self.diagnostics.close()
        self.assertFalse(self.diagnostics._tasks)
        self.assertFalse(self.context.listeners.get("response"))
        self.assertFalse(self.context.listeners.get("requestfailed"))


if __name__ == "__main__":
    unittest.main()
