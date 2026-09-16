import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from relay.browser import AUTH_REQUIRED_JS, EXTRACT_MESSAGES_JS, SnapshotTracker, login, monitor
from relay.discovery import EXTRACT_GUILDS_JS


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
            page = SimpleNamespace(url="https://discord.com/login", sidebar_ready=False, guilds=[], is_closed=lambda: False)
            page.goto = AsyncMock()
            context = SimpleNamespace(pages=[page], close=AsyncMock())

            class Playwright:
                async def __aenter__(self):
                    return SimpleNamespace(chromium=SimpleNamespace(
                        launch_persistent_context=AsyncMock(return_value=context)))

                async def __aexit__(self, *args):
                    return False

            async def evaluate(expression):
                if expression == EXTRACT_GUILDS_JS:
                    return {"sidebar_present": page.sidebar_ready, "login_required": False, "guilds": page.guilds}
                self.assertEqual(expression, AUTH_REQUIRED_JS)
                return False

            page.evaluate = evaluate

            async def sign_in_then_stop(seconds):
                if page.url.endswith("/login"):
                    page.url = "https://discord.com/channels/@me"
                elif not page.sidebar_ready:
                    self.assertNotIn("connected", [row["state"] for row in statuses])
                    page.sidebar_ready = True
                elif not page.guilds:
                    self.assertNotIn("connected", [row["state"] for row in statuses])
                    page.guilds = [{"id": "3000000000000000010"}]
                else:
                    raise RuntimeError("fixture complete")

            with patch("relay.browser._playwright", return_value=Playwright), patch(
                "relay.browser.asyncio.sleep", sign_in_then_stop
            ):
                with self.assertRaisesRegex(RuntimeError, "fixture complete"):
                    await login(directory, keep_open=True, on_status=statuses.append)
            self.assertEqual([row["state"] for row in statuses], ["starting", "login_required", "reconnecting", "reconnecting", "connected"])
            self.assertIn("sign-in detected", statuses[-1]["detail"])
            self.assertIn("Return to Setup", statuses[-1]["detail"])
            page.goto.assert_not_awaited()
            context.close.assert_awaited_once()

    async def test_login_does_not_navigate_visible_captcha_on_channels_me(self):
        with tempfile.TemporaryDirectory() as directory:
            statuses = []
            page = SimpleNamespace(
                url="https://discord.com/channels/@me",
                auth_required=True,
                is_closed=lambda: False,
                goto=AsyncMock(),
            )
            context = SimpleNamespace(pages=[page], close=AsyncMock())

            async def evaluate(expression):
                if expression == EXTRACT_GUILDS_JS:
                    return {"sidebar_present": True, "login_required": False, "guilds": [{"id": "3000000000000000010"}]}
                self.assertEqual(expression, AUTH_REQUIRED_JS)
                return page.auth_required

            page.evaluate = evaluate

            class Playwright:
                async def __aenter__(self):
                    return SimpleNamespace(chromium=SimpleNamespace(
                        launch_persistent_context=AsyncMock(return_value=context)))

                async def __aexit__(self, *args):
                    return False

            async def finish_challenge(seconds):
                del seconds
                page.auth_required = False

            with patch("relay.browser._playwright", return_value=Playwright), patch(
                "relay.browser.asyncio.sleep", finish_challenge
            ):
                await login(directory, on_status=statuses.append)

            self.assertEqual([row["state"] for row in statuses], ["starting", "login_required", "connected"])
            page.goto.assert_not_awaited()
            context.close.assert_awaited_once()

    async def _monitor_fixture(self, snapshot, *, page_specs=None, sleep_hook=None):
        with tempfile.TemporaryDirectory() as directory:
            config = _config(Path(directory))
            statuses = []
            events = []
            goto_calls = []

            class Page:
                def __init__(self, index, spec=None):
                    spec = spec or {}
                    self.index = index
                    self.url = spec.get("url", "about:blank")
                    self.auth_required = spec.get("auth_required", snapshot.get("auth_required", False))
                    self.sidebar_ready = spec.get("sidebar_ready", True)
                    self.snapshot = spec.get("snapshot", snapshot)
                    self.closed = False

                def is_closed(self):
                    return False

                async def goto(self, url, **kwargs):
                    goto_calls.append(url)
                    self.url = url

                async def evaluate(self, expression, channel_id=None):
                    if expression == EXTRACT_GUILDS_JS:
                        return {"sidebar_present": self.sidebar_ready, "login_required": False, "guilds": [{"id": "3000000000000000010"}]}
                    if expression == AUTH_REQUIRED_JS:
                        return self.auth_required
                    if expression == EXTRACT_MESSAGES_JS:
                        result = self.snapshot | {"url": self.url}
                        if result.get("messages") and channel_id is not None:
                            result["messages"] = [
                                message | {"channel_id": str(channel_id)}
                                for message in result["messages"]
                            ]
                        return result
                    raise AssertionError(f"unexpected browser expression: {expression!r}")

            class Context:
                def __init__(self):
                    specs = page_specs or [{}, {}]
                    self.pages = [Page(index, spec) for index, spec in enumerate(specs)]
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
                if sleep_hook is not None:
                    await sleep_hook(context, seconds)
                    return
                raise RuntimeError("fixture complete")

            async def record(message):
                events.append(message)

            with patch("relay.browser._playwright", return_value=Playwright), patch(
                "relay.browser.asyncio.sleep", stop_after_one_sleep
            ):
                with self.assertRaisesRegex(RuntimeError, "fixture complete"):
                    await monitor(config, record, on_status=statuses.append)
            self._last_monitor_context = context
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
        self.assertEqual(len(goto_calls), 1)
        self.assertTrue(closed)

    async def test_loading_snapshot_does_not_qualify_as_empty_or_connected(self):
        statuses, events, goto_calls, closed = await self._monitor_fixture(
            {"ready": False, "loading": True, "messages": [], "at_bottom": True, "connection_epoch": "fixture"}
        )
        self.assertNotIn("connected", [item["state"] for item in statuses])
        self.assertEqual(events, [])
        self.assertEqual(len(goto_calls), 2)
        self.assertTrue(any("without reloading" in item.get("detail", "") for item in statuses))
        self.assertTrue(closed)

    async def test_third_tab_challenge_is_retained_and_pauses_both_channels(self):
        statuses, events, goto_calls, closed = await self._monitor_fixture(
            {"ready": True, "empty_ready": True, "messages": [], "at_bottom": True, "connection_epoch": "fixture"},
            page_specs=[
                {},
                {},
                {"url": "https://discord.com/channels/@me", "auth_required": True},
            ],
        )
        context = self._last_monitor_context
        self.assertEqual(goto_calls, [])
        self.assertEqual([item["state"] for item in statuses], ["login_required", "login_required"])
        self.assertEqual(events, [])
        self.assertTrue(closed)
        self.assertEqual(len(context.pages), 3)
        self.assertFalse(context.pages[2].closed)

    async def test_all_tabs_pause_then_resume_with_context_only_baseline(self):
        messages = [_message(1, "2026-09-15T12:00:00Z")]
        snapshot = {
            "ready": True,
            "empty_ready": False,
            "messages": messages,
            "at_bottom": True,
            "connection_epoch": "fixture",
        }
        first_channel_url = "https://discord.com/channels/3000000000000000010/1000000000000000001"
        second_channel_url = "https://discord.com/channels/3000000000000000010/1000000000000000002"
        sleeps = 0

        async def clear_auth_then_stop(context, seconds):
            nonlocal sleeps
            del seconds
            sleeps += 1
            if sleeps == 1:
                context.pages[0].auth_required = False
                return
            raise RuntimeError("fixture complete")

        statuses, events, goto_calls, closed = await self._monitor_fixture(
            snapshot,
            page_specs=[
                {"url": first_channel_url, "auth_required": True},
                {"url": second_channel_url, "auth_required": False},
            ],
            sleep_hook=clear_auth_then_stop,
        )
        states = [item["state"] for item in statuses]
        self.assertGreaterEqual(states.count("login_required"), 2)
        self.assertGreaterEqual(states.count("connected"), 2)
        self.assertEqual(len(events), 2)
        self.assertTrue(all(message["ingestion"] == "baseline" for message in events))
        self.assertEqual(goto_calls, [])
        self.assertTrue(closed)

    async def test_loading_account_ui_then_captcha_pauses_navigation_and_keeps_error(self):
        snapshot = {
            "ready": True, "messages": [_message(1, "2026-09-16T12:00:00Z")],
            "at_bottom": True, "connection_epoch": "fixture",
        }
        sleeps = 0
        diagnostic = SimpleNamespace(
            failure="", detail=lambda fallback: diagnostic.failure or fallback,
            close=AsyncMock(),
        )

        def clear():
            diagnostic.failure = ""

        diagnostic.clear = clear

        async def progress(context, seconds):
            nonlocal sleeps
            sleeps += 1
            if sleeps == 1:
                self.assertEqual(context.pages[0].url, "https://discord.com/channels/@me")
                context.pages[0].auth_required = True
                diagnostic.failure = "Discord CAPTCHA rejected [captcha_rejected]; HTTP 400."
            elif sleeps == 2:
                # Discord returns to login; the last response must remain visible.
                context.pages[0].url = "https://discord.com/login"
                context.pages[0].auth_required = False
            elif sleeps == 3:
                context.pages[0].url = "https://discord.com/channels/@me"
                context.pages[0].sidebar_ready = True
            elif sleeps >= 5:
                raise RuntimeError("fixture complete")

        with patch("relay.browser.DiscordLoginDiagnostics", return_value=diagnostic):
            statuses, events, goto_calls, closed = await self._monitor_fixture(
                snapshot,
                page_specs=[{"url": "https://discord.com/channels/@me", "sidebar_ready": False}, {}],
                sleep_hook=progress,
            )
        self.assertEqual([row["state"] for row in statuses[:6]],
                         ["reconnecting"] * 2 + ["login_required"] * 4)
        self.assertTrue(all("captcha_rejected" in row["detail"] for row in statuses[2:6]))
        self.assertEqual(len(goto_calls), 2)
        self.assertEqual(len(events), 2)
        self.assertTrue(all(message["ingestion"] == "baseline" for message in events))
        self.assertEqual(statuses[-1]["state"], "connected")
        self.assertNotIn("captcha_rejected", statuses[-1]["detail"])
        diagnostic.close.assert_awaited_once()
        self.assertTrue(closed)

    async def test_healthy_channel_does_not_clear_other_channels_login_diagnostic(self):
        snapshot = {"ready": True, "empty_ready": True, "messages": [], "at_bottom": True}
        failure = "Discord CAPTCHA rejected [captcha_rejected]; HTTP 400."
        diagnostic = SimpleNamespace(failure=failure, close=AsyncMock())
        diagnostic.detail = lambda fallback: diagnostic.failure or fallback
        diagnostic.clear = lambda: setattr(diagnostic, "failure", "")
        sleeps = 0

        async def recover_then_stop(context, seconds):
            nonlocal sleeps
            sleeps += 1
            if sleeps == 1:
                self.assertEqual(diagnostic.failure, failure)
                context.pages[1].snapshot = snapshot
            else:
                self.assertEqual(diagnostic.failure, "")
                raise RuntimeError("fixture complete")

        with patch("relay.browser.DiscordLoginDiagnostics", return_value=diagnostic):
            statuses, _, _, _ = await self._monitor_fixture(
                snapshot,
                page_specs=[
                    {"url": "https://discord.com/channels/3000000000000000010/1000000000000000001"},
                    {"url": "https://discord.com/channels/3000000000000000010/1000000000000000002",
                     "snapshot": snapshot | {"ready": False}},
                ],
                sleep_hook=recover_then_stop,
            )
        self.assertEqual(statuses[1]["state"], "reconnecting")
        self.assertEqual(statuses[1]["detail"], failure)
        diagnostic.close.assert_awaited_once()

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
