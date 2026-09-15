import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from relay.browser import EXTRACT_MESSAGES_JS, SnapshotTracker, login, monitor


CHANNEL = "1000000000000000001"
AUTHOR = "2000000000000000002"


def _config(root):
    config = json.loads(Path("config.example.json").read_text())
    config["browser"]["profile_dir"] = str(root / "profile")
    for channel in config["channels"]:
        channel["guild_id"] = "3000000000000000010"
    return config


def _message(number, timestamp):
    return {
        "id": str(1545000000000000000 + number),
        "channel_id": CHANNEL,
        "author": {"id": AUTHOR, "username": "fixture"},
        "timestamp": timestamp,
        "content": str(number),
    }


class BrowserRecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_login_reports_success_while_browser_remains_open(self):
        with tempfile.TemporaryDirectory() as directory:
            statuses = []
            page = SimpleNamespace(url="https://discord.com/login", is_closed=lambda: False)
            page.goto = AsyncMock()
            context = SimpleNamespace(pages=[page], close=AsyncMock())

            class Playwright:
                async def __aenter__(self):
                    return SimpleNamespace(chromium=SimpleNamespace(
                        launch_persistent_context=AsyncMock(return_value=context)))

                async def __aexit__(self, *args):
                    return False

            async def sign_in_then_stop(seconds):
                if page.url.endswith("/login"):
                    page.url = "https://discord.com/channels/@me"
                else:
                    raise RuntimeError("fixture complete")

            with patch("relay.browser._playwright", return_value=Playwright), patch(
                "relay.browser.asyncio.sleep", sign_in_then_stop
            ):
                with self.assertRaisesRegex(RuntimeError, "fixture complete"):
                    await login(directory, keep_open=True, on_status=statuses.append)
            self.assertEqual([row["state"] for row in statuses], ["starting", "login_required", "connected"])
            self.assertIn("sign-in detected", statuses[-1]["detail"])
            self.assertIn("Return to Setup", statuses[-1]["detail"])
            page.goto.assert_awaited_once()
            context.close.assert_awaited_once()

    async def _monitor_fixture(self, snapshot):
        with tempfile.TemporaryDirectory() as directory:
            config = _config(Path(directory))
            statuses = []
            events = []
            goto_calls = []

            class Page:
                def __init__(self, index):
                    self.index = index
                    self.url = "about:blank"

                def is_closed(self):
                    return False

                async def goto(self, url, **kwargs):
                    goto_calls.append(url)
                    self.url = url

                async def evaluate(self, expression, channel_id):
                    return snapshot | {"url": self.url}

            class Context:
                def __init__(self):
                    self.pages = [Page(0), Page(1)]
                    self.closed = False

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

            async def stop_after_one_sleep(seconds):
                raise RuntimeError("fixture complete")

            async def record(message):
                events.append(message)

            with patch("relay.browser._playwright", return_value=Playwright), patch(
                "relay.browser.asyncio.sleep", stop_after_one_sleep
            ):
                with self.assertRaisesRegex(RuntimeError, "fixture complete"):
                    await monitor(config, record, on_status=statuses.append)
            return statuses, events, goto_calls, context.closed

    async def test_empty_ready_channel_connects_without_emitting(self):
        statuses, events, goto_calls, closed = await self._monitor_fixture(
            {"ready": True, "empty_ready": True, "messages": [], "at_bottom": True, "connection_epoch": "fixture"}
        )
        self.assertEqual([item["state"] for item in statuses], ["connected", "connected"])
        self.assertEqual(events, [])
        self.assertEqual(len(goto_calls), 2)
        self.assertTrue(closed)

    async def test_auth_control_on_channel_page_pauses_without_navigation_loop(self):
        statuses, events, goto_calls, closed = await self._monitor_fixture(
            {"ready": False, "auth_required": True, "messages": [], "at_bottom": False, "connection_epoch": "fixture"}
        )
        self.assertEqual([item["state"] for item in statuses], ["login_required", "login_required"])
        self.assertEqual(events, [])
        self.assertEqual(len(goto_calls), 2)
        self.assertTrue(closed)

    async def test_loading_snapshot_does_not_qualify_as_empty_or_connected(self):
        statuses, events, goto_calls, closed = await self._monitor_fixture(
            {"ready": False, "loading": True, "messages": [], "at_bottom": True, "connection_epoch": "fixture"}
        )
        self.assertNotIn("connected", [item["state"] for item in statuses])
        self.assertEqual(events, [])
        self.assertGreater(len(goto_calls), 2)
        self.assertTrue(closed)

    def test_tracker_accepts_confirmed_empty_start_without_replaying_delayed_rows(self):
        cutoff = [datetime(2026, 9, 6, 12, 0, tzinfo=timezone.utc)]
        tracker = SnapshotTracker(CHANNEL, clock=lambda: cutoff[0])
        self.assertEqual(tracker.observe([], empty_ready=True), [])
        self.assertFalse(tracker.needs_baseline)

        delayed = tracker.observe([_message(1, "2026-09-06T11:59:59Z")])
        self.assertEqual(delayed[0]["ingestion"], "baseline")

        cutoff[0] = datetime(2026, 9, 6, 12, 0, 1, tzinfo=timezone.utc)
        current = tracker.observe(
            [_message(1, "2026-09-06T11:59:59Z"), _message(2, "2026-09-06T12:00:01Z")]
        )
        self.assertEqual(current[0]["ingestion"], "live")

    async def test_browser_structural_states_against_actual_dom(self):
        try:
            from playwright.async_api import async_playwright
        except ImportError:
            self.skipTest("Playwright is required for the DOM check")
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=True)
            try:
                page = await browser.new_page()
                await page.route("**/*", lambda route: route.abort())
                async def snapshot(content="", *, attributes="", extra=""):
                    await page.set_content(f'<div data-list-id="chat-messages" style="height:100px;overflow:auto" {attributes}>{content}</div>{extra}')
                    return await page.evaluate(EXTRACT_MESSAGES_JS, CHANNEL)
                empty = await snapshot()
                self.assertTrue(empty["ready"] and empty["empty_ready"] and empty["at_bottom"])
                loading = await snapshot(attributes='aria-busy="true"')
                self.assertTrue(loading["loading"])
                self.assertFalse(loading["ready"] or loading["empty_ready"])
                malformed = await snapshot('<div id="chat-messages-loading">Loading</div>')
                self.assertTrue(malformed["unparseable_rows"])
                self.assertFalse(malformed["ready"])
                auth = await snapshot(extra='<input type="password">')
                self.assertTrue(auth["auth_required"])
                mfa = await snapshot(extra='<div role="dialog"><input autocomplete="one-time-code"></div>')
                self.assertTrue(mfa["auth_required"])
                captcha = await snapshot(extra='<iframe src="https://hcaptcha.com/fixture"></iframe>')
                self.assertTrue(captcha["auth_required"])
                content = await snapshot('<p>Please login and verify this trade</p><input type="password">')
                self.assertFalse(content["auth_required"])
            finally:
                await browser.close()


if __name__ == "__main__":
    unittest.main()
