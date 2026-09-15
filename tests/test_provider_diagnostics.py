import asyncio
import contextlib
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from relay.browser import login, monitor
from relay.dashboard import _safe_runtime
from relay.service import RuntimeStatus
from relay.status import failure_detail


class ProviderDiagnosticTests(unittest.IsolatedAsyncioTestCase):
    async def test_navigation_timeout_keeps_browser_open_for_unlimited_manual_signin(self):
        events, navigation, steps = [], [], []

        class Page:
            url = "about:blank"
            closed = False

            def is_closed(self):
                return self.closed

            async def goto(self, url, **options):
                navigation.append(options)
                raise asyncio.TimeoutError("https://private.example/?token=PRIVATE")

        page = Page()

        class Context:
            pages = [page]
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

        async def advance(seconds):
            self.assertFalse(context.closed)
            steps.append(seconds)
            if len(steps) == 1:
                page.url = "https://discord.com/login"
            elif len(steps) == 122:
                page.url = "https://discord.com/channels/@me"
            elif len(steps) == 123:
                page.closed = True

        with tempfile.TemporaryDirectory() as directory, patch("relay.browser._playwright", return_value=Playwright), patch("relay.browser.asyncio.sleep", advance):
            await login(directory, keep_open=True, on_status=events.append)
        self.assertEqual(navigation, [{"wait_until": "commit", "timeout": 60000}])
        self.assertGreater(sum(steps), 360)
        self.assertTrue(any("exceeded 60 seconds" in event.get("detail", "") for event in events))
        self.assertTrue(any(event["state"] == "connected" for event in events))
        self.assertFalse(any(event["state"] == "error" for event in events))
        self.assertNotIn("PRIVATE", json.dumps(events))
        self.assertTrue(context.closed)

    def test_classified_errors_are_actionable_without_provider_payload(self):
        cases = [
            (TimeoutError("PRIVATE request"), "timeout", "internet"),
            (RuntimeError("net::ERR_NAME_NOT_RESOLVED https://PRIVATE"), "dns_failed", "DNS"),
            (RuntimeError("net::ERR_CERT_AUTHORITY_INVALID PRIVATE"), "tls_failed", "clock"),
            (PermissionError("PRIVATE/path"), "local_permission", "permissions"),
        ]
        for error, code, action in cases:
            with self.subTest(code=code):
                result = failure_detail(ExceptionGroup("PRIVATE", [error]), provider="Discord", phase="navigation")
                self.assertIn("[" + code + "]", result)
                self.assertIn(action, result)
                self.assertNotIn("PRIVATE", result)
        error = RuntimeError("PRIVATE OAuth response")
        error.response = SimpleNamespace(status_code=429)
        self.assertIn("HTTP 429", failure_detail(error, provider="Robinhood", phase="connection"))

    def test_failure_detail_never_masks_unprintable_exception_text(self):
        class UnprintableError(Exception):
            def __str__(self):
                raise RuntimeError("PRIVATE provider payload")

        result = failure_detail(UnprintableError(), provider="Discord", phase="navigation")
        self.assertIn("[operation_failed]", result)
        self.assertNotIn("PRIVATE", result)

    async def test_monitor_reports_slow_navigation_and_cleans_on_cancel(self):
        with tempfile.TemporaryDirectory() as directory:
            config = json.loads(Path("config.example.json").read_text())
            config["browser"]["profile_dir"] = str(Path(directory) / "profile")
            for channel in config["channels"]:
                channel["guild_id"] = "3000000000000000010"
            progress_seen = asyncio.Event()
            navigation_cancelled = asyncio.Event()
            events, navigation = [], []

            class Page:
                url = "about:blank"

                def is_closed(self):
                    return False

                async def goto(self, url, **options):
                    navigation.append((url, options))
                    try:
                        await asyncio.Future()
                    finally:
                        navigation_cancelled.set()

            class Context:
                pages = [Page(), Page()]
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

            def on_status(event):
                events.append(event)
                if event["state"] == "starting":
                    progress_seen.set()

            async def record(message):
                return None

            with patch("relay.browser._playwright", return_value=Playwright), patch(
                "relay.browser.NAVIGATION_PROGRESS_SECONDS", 0.01
            ):
                task = asyncio.create_task(monitor(config, record, on_status=on_status))
                try:
                    await asyncio.wait_for(progress_seen.wait(), timeout=1)
                finally:
                    task.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await task

            self.assertTrue(navigation_cancelled.is_set())
            self.assertTrue(context.closed)
            self.assertEqual(navigation[0][1], {"wait_until": "commit", "timeout": 60000})
            self.assertTrue(any("still loading" in event.get("detail", "") for event in events))
            self.assertNotIn("PRIVATE", json.dumps(events))

    def test_runtime_retains_discord_detail_and_new_auth_failure_diagnostics(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "runtime.json"
            status = RuntimeStatus(path, [{"id": "123456789012345678"}])
            status.event({"component": "discord", "channel_id": "123456789012345678", "state": "reconnecting", "detail": "Discord navigation timed out [timeout]. Check DNS."})
            status.event({"component": "codex", "state": "auth_required"})
            status.event({"component": "codex", "state": "auth_required", "detail": "Session expired [auth_required]. Use Codex sign-in."})
            projected = _safe_runtime({}, path)
            self.assertIn("[timeout]", projected["discord"]["detail"])
            self.assertIn("Session expired", projected["codex"]["detail"])
