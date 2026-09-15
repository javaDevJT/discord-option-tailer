"""Offline checks for browser-only Discord directory discovery."""

from __future__ import annotations

import asyncio
import contextlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import tempfile
import time
import unittest
from unittest.mock import AsyncMock, patch

from relay.browser import AUTH_REQUIRED_JS, requires_manual_auth
from relay.discovery import (
    DISCOVERY_START_URL,
    EXTRACT_AUTHORS_JS,
    EXTRACT_CHANNELS_JS,
    EXTRACT_GUILDS_JS,
    SCROLL_CHANNELS_JS,
    REQUEST_FILENAME,
    RESULT_FILENAME,
    _atomic_json_write,
    _collect_channels,
    _close_discovery_page,
    _directory_snapshot,
    _discover_page,
    _ensure_page,
    _observed_channels,
    _public_projection,
    discovery_status,
    request_discovery,
    serve_discovery,
)


GUILD = "111111111111111111"
GUILD_TWO = "222222222222222222"
CHANNEL = "333333333333333333"
AUTHOR = "444444444444444444"


def _runtime(root: Path) -> Path:
    return root / "state" / "runtime-status.json"


def _wait_for(predicate, timeout: float = 3.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.01)
    raise AssertionError("timed out waiting for discovery state")


class DiscoveryFileTests(unittest.TestCase):
    def test_payload_is_strict_and_validated_before_busy_check(self):
        with tempfile.TemporaryDirectory() as directory:
            runtime = _runtime(Path(directory))
            request_discovery(runtime, {})
            with self.assertRaises(ValueError):
                request_discovery(runtime, {"unexpected": "field"})
            with self.assertRaises(ValueError):
                request_discovery(runtime, {"channel_id": CHANNEL})
            with self.assertRaises(ValueError):
                request_discovery(runtime, {"guild_id": "short"})
            with self.assertRaisesRegex(RuntimeError, "already running"):
                request_discovery(runtime, {})

    def test_request_and_result_are_atomic_owner_only_and_correlated(self):
        with tempfile.TemporaryDirectory() as directory:
            runtime = _runtime(Path(directory))
            waiting = request_discovery(runtime, {"guild_id": GUILD})
            self.assertEqual(waiting["state"], "waiting")
            self.assertEqual(waiting["guild_id"], GUILD)
            request_path = runtime.parent / REQUEST_FILENAME
            self.assertEqual(stat.S_IMODE(request_path.stat().st_mode), 0o600)
            self.assertEqual(discovery_status(runtime)["state"], "waiting")

            _atomic_json_write(runtime.parent / RESULT_FILENAME, {
                "state": "ready",
                "request_id": waiting["request_id"],
                "guild_id": GUILD,
                "channel_id": None,
                "guilds": [{"id": GUILD, "name": "  Alpha\nServer  ", "secret": "drop"}],
                "channels": [],
                "authors": [],
                "detail": "directory is ready",
                "credential": "drop",
            })
            status = discovery_status(runtime)
            self.assertEqual(status["state"], "ready")
            self.assertEqual(status["guilds"], [{"id": GUILD, "name": "Alpha Server"}])
            self.assertNotIn("credential", status)
            self.assertTrue(status["authors_limited"])
            self.assertEqual(stat.S_IMODE((runtime.parent / RESULT_FILENAME).stat().st_mode), 0o600)

    def test_expired_request_is_reported_and_can_be_replaced(self):
        with tempfile.TemporaryDirectory() as directory:
            runtime = _runtime(Path(directory))
            waiting = request_discovery(runtime, {})
            stale = {
                "request_id": waiting["request_id"],
                "created_at": "2020-01-01T00:00:00+00:00",
                "guild_id": None,
                "channel_id": None,
            }
            _atomic_json_write(runtime.parent / REQUEST_FILENAME, stale)
            timed_out = discovery_status(runtime)
            self.assertEqual(timed_out["state"], "failed")
            self.assertEqual(timed_out["request_id"], waiting["request_id"])
            replacement = request_discovery(runtime, {"guild_id": GUILD, "channel_id": CHANNEL})
            self.assertEqual(replacement["state"], "waiting")
            self.assertNotEqual(replacement["request_id"], waiting["request_id"])
            self.assertEqual(discovery_status(runtime)["request_id"], replacement["request_id"])

    def test_completed_result_expires_instead_of_presenting_old_choices_as_ready(self):
        with tempfile.TemporaryDirectory() as directory:
            runtime = _runtime(Path(directory))
            waiting = request_discovery(runtime, {})
            _atomic_json_write(runtime.parent / RESULT_FILENAME, {
                "state": "ready",
                "request_id": waiting["request_id"],
                "guild_id": None,
                "channel_id": None,
                "guilds": [{"id": GUILD, "name": "Old server"}],
                "channels": [],
                "authors": [],
                "detail": "old",
                "completed_at": "2020-01-01T00:00:00+00:00",
            })
            status = discovery_status(runtime)
            self.assertEqual(status["state"], "failed")
            self.assertIn("stale", status["detail"])
            self.assertEqual(status["guilds"], [])


    def test_channel_projection_does_not_silently_truncate_over_300_rows(self):
        channels = []
        for index in range(301):
            channel_id = str(555555555555555555 + index)
            channels.append({
                "id": channel_id,
                "guild_id": GUILD,
                "name": f"channel {index}",
                "url": f"https://discord.com/channels/{GUILD}/{channel_id}",
            })

        observed = _observed_channels({"channels": channels}, GUILD)
        projected = _public_projection({
            "state": "ready",
            "guild_id": GUILD,
            "channel_id": None,
            "guilds": [],
            "channels": channels,
            "authors": [],
        })

        self.assertEqual(len(observed), len(channels))
        self.assertEqual(len(projected["channels"]), len(channels))
        self.assertEqual(observed[-1]["id"], channels[-1]["id"])
        self.assertEqual(projected["channels"][-1]["id"], channels[-1]["id"])


class _FakePage:
    def __init__(self, *, slow: bool = False, auth_required: bool = False, auth_probe_error: bool = False):
        self.url = "about:blank"
        self.goto_calls: list[str] = []
        self.navigation_options: list[dict] = []
        self.clicked_links = []
        self.closed = False
        self.slow = slow
        self.auth_required = auth_required
        self.auth_probe_error = auth_probe_error

    def is_closed(self):
        return self.closed

    async def goto(self, url, **kwargs):
        self.navigation_options.append(kwargs)
        self.goto_calls.append(url)
        self.url = url

    async def wait_for_timeout(self, milliseconds):
        await __import__("asyncio").sleep(milliseconds / 1000)

    def locator(self, selector):
        page = self

        class Link:
            @property
            def first(self):
                return self

            async def click(self, **kwargs):
                page.clicked_links.append(selector)
                page.url = f"https://discord.com/channels/{GUILD}/{CHANNEL}"

        return Link()

    async def evaluate(self, expression, argument=None):
        if expression == AUTH_REQUIRED_JS:
            if self.auth_probe_error:
                raise RuntimeError("fixture auth probe failed")
            return self.auth_required
        import asyncio
        if self.slow:
            await asyncio.sleep(2)
        if expression == SCROLL_CHANNELS_JS:
            return {"top": 0, "before": 0, "end": True}
        if expression == EXTRACT_GUILDS_JS:
            return {"sidebar_present": True, "guilds": [
                {"id": GUILD, "name": "Alpha", "href": f"https://discord.com/channels/{GUILD}/@home"},
            ]}
        if expression == EXTRACT_CHANNELS_JS:
            self.assert_argument(argument, GUILD)
            return {"sidebar_present": True, "channels": [{
                "id": CHANNEL, "guild_id": GUILD, "name": "signals",
                "url": f"https://discord.com/channels/{GUILD}/{CHANNEL}",
            }]}
        if expression == EXTRACT_AUTHORS_JS:
            self.assert_argument(argument, CHANNEL)
            return {"chat_present": True, "authors": [{"id": AUTHOR, "name": "Analyst"}]}
        raise AssertionError("unexpected evaluate expression")

    @staticmethod
    def assert_argument(actual, expected):
        if str(actual) != str(expected):
            raise AssertionError((actual, expected))

    async def close(self):
        self.closed = True


class _RedirectingAuthPage(_FakePage):
    async def goto(self, url, **kwargs):
        self.navigation_options.append(kwargs)
        self.goto_calls.append(url)
        self.url = DISCOVERY_START_URL
        self.auth_required = True


class _SlowNavigationPage(_FakePage):
    async def goto(self, url, **kwargs):
        del kwargs
        self.goto_calls.append(url)
        self.url = url
        await asyncio.sleep(2)


class _LoginRetryPage(_FakePage):
    def __init__(self):
        super().__init__()
        self.url = "https://discord.com/login"

    async def goto(self, url, **kwargs):
        del kwargs
        self.goto_calls.append(url)
        self.url = DISCOVERY_START_URL


class _ErrorNavigationPage(_FakePage):
    async def goto(self, url, **kwargs):
        del kwargs
        self.goto_calls.append(url)
        raise RuntimeError("fixture navigation failure")


class _DelayedDirectoryPage:
    def __init__(self):
        self.calls = 0
        self.url = DISCOVERY_START_URL

    def is_closed(self):
        return False

    async def evaluate(self, expression, argument=None):
        if expression == AUTH_REQUIRED_JS:
            return False
        del argument
        self.calls += 1
        if self.calls < 3:
            return {"sidebar_present": False, "guilds": []}
        return {"sidebar_present": True, "guilds": [{"id": GUILD, "name": "Alpha"}]}

    async def wait_for_timeout(self, milliseconds):
        del milliseconds


class _FakeContext:
    def __init__(self, page, *, pages=None):
        self.page = page
        self.pages = list(pages) if pages is not None else [page]
        self.new_page_calls = 0

    async def new_page(self):
        self.new_page_calls += 1
        self.pages.append(self.page)
        return self.page


class DiscoveryWorkerTests(unittest.IsolatedAsyncioTestCase):
    async def test_virtualized_channel_sidebar_is_scanned_and_scroll_restored(self):
        class ScrollingPage(_FakePage):
            top = 1

            async def evaluate(self, expression, argument=None):
                if expression == SCROLL_CHANNELS_JS:
                    argument = argument["action"]
                    before = self.top
                    if argument == "next":
                        self.top = 1
                    elif isinstance(argument, int):
                        self.top = argument
                    return {"top": self.top, "before": before, "end": self.top == 1}
                if expression == EXTRACT_CHANNELS_JS:
                    identifier = str(int(CHANNEL) + self.top)
                    return {"sidebar_present": True, "channels": [{"id": identifier, "guild_id": GUILD,
                        "name": f"channel {self.top}", "url": f"https://discord.com/channels/{GUILD}/{identifier}"}]}
                return await super().evaluate(expression, argument)

        page = ScrollingPage()
        with patch("relay.discovery._wait_briefly", new=AsyncMock()):
            result = await _collect_channels(page, GUILD)
        self.assertEqual({item["id"] for item in result["channels"]}, {CHANNEL, str(int(CHANNEL) + 1)})
        self.assertEqual(page.top, 1)
        with patch("relay.discovery._wait_briefly", new=AsyncMock()):
            await _collect_channels(page, GUILD, CHANNEL)
        self.assertEqual(page.top, 0)

    async def test_delayed_virtualized_sidebar_is_not_ready_partial(self):
        class DelayedScrollingPage(_FakePage):
            def __init__(self):
                super().__init__()
                self.top = 0
                self.clock_ms = 0
                self.visible_top = None
                self.last_scroll_ms = 0
                self.total_rows = 45

            async def wait_for_timeout(self, milliseconds):
                self.clock_ms += milliseconds
                if self.clock_ms - self.last_scroll_ms >= 400:
                    self.visible_top = self.top

            async def evaluate(self, expression, argument=None):
                if expression == SCROLL_CHANNELS_JS:
                    action = argument["action"]
                    before = self.top
                    if action == "next":
                        self.top = min(self.total_rows - 1, self.top + 1)
                    elif isinstance(action, int):
                        self.top = max(0, min(self.total_rows - 1, action))
                    if self.top != before:
                        self.last_scroll_ms = self.clock_ms
                    return {
                        "top": self.top,
                        "before": before,
                        "end": self.top >= self.total_rows - 1,
                    }
                if expression == EXTRACT_CHANNELS_JS:
                    if self.visible_top is None:
                        rows = []
                    else:
                        channel_id = str(int(CHANNEL) + self.visible_top)
                        rows = [{
                            "id": channel_id,
                            "guild_id": GUILD,
                            "name": f"channel {self.visible_top}",
                            "url": f"https://discord.com/channels/{GUILD}/{channel_id}",
                        }]
                    return {"sidebar_present": True, "channels": rows}
                return await super().evaluate(expression, argument)

        page = DelayedScrollingPage()
        result = await _collect_channels(page, GUILD)
        expected = {str(int(CHANNEL) + index) for index in range(page.total_rows)}
        observed = {item["id"] for item in result["channels"]}
        self.assertIn("complete", result)
        if result["complete"]:
            self.assertEqual(observed, expected)
        else:
            self.assertLess(len(observed), len(expected))

        page = DelayedScrollingPage()
        discovered = await _discover_page(page, {
            "request_id": "00000000-0000-0000-0000-000000000001",
            "guild_id": GUILD,
            "channel_id": None,
        })
        self.assertEqual(discovered["state"], "ready" if result["complete"] else "partial")

    async def test_pending_auth_blocks_discovery_without_new_page_or_navigation(self):
        with tempfile.TemporaryDirectory() as directory:
            runtime = _runtime(Path(directory))
            request = request_discovery(runtime, {})
            page = _FakePage(auth_required=True)
            page.url = DISCOVERY_START_URL
            context = _FakeContext(page)
            task = asyncio.create_task(serve_discovery(context, runtime))
            try:
                status = await self._wait_async(lambda: discovery_status(runtime))
            finally:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task

            self.assertEqual(status["state"], "login_required")
            self.assertEqual(status["request_id"], request["request_id"])
            self.assertEqual(context.new_page_calls, 0)
            self.assertEqual(page.goto_calls, [])
            self.assertFalse(page.closed)

    async def test_redirected_discovery_auth_tab_is_preserved_during_worker_cleanup(self):
        with tempfile.TemporaryDirectory() as directory:
            runtime = _runtime(Path(directory))
            request_discovery(runtime, {})
            page = _RedirectingAuthPage()
            context = _FakeContext(page)
            task = asyncio.create_task(serve_discovery(context, runtime))
            try:
                status = await self._wait_async(lambda: discovery_status(runtime))
            finally:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task

            self.assertEqual(status["state"], "login_required")
            self.assertEqual(context.new_page_calls, 1)
            self.assertEqual(page.goto_calls, [DISCOVERY_START_URL])
            self.assertFalse(page.closed)

    async def test_unknown_auth_probe_closes_owned_discovery_tab(self):
        page = _FakePage(auth_probe_error=True)
        page.url = DISCOVERY_START_URL
        context = _FakeContext(page)

        await _close_discovery_page(context, page)

        self.assertTrue(page.closed)

    async def test_known_challenge_survives_later_auth_probe_error(self):
        page = _FakePage(auth_required=True)
        page.url = DISCOVERY_START_URL
        context = _FakeContext(page)

        self.assertTrue(await requires_manual_auth(page))
        page.auth_required = False
        page.auth_probe_error = True
        await _close_discovery_page(context, page)

        self.assertFalse(page.closed)

    async def test_setup_discovery_reuses_open_tab_without_closing_it(self):
        with tempfile.TemporaryDirectory() as directory:
            runtime = _runtime(Path(directory))
            request_discovery(runtime, {})
            page = _FakePage()
            page.url = DISCOVERY_START_URL
            context = _FakeContext(page)
            task = __import__("asyncio").create_task(serve_discovery(context, runtime, setup_page=page))
            try:
                status = await self._wait_async(lambda: discovery_status(runtime))
            finally:
                task.cancel()
                with contextlib.suppress(__import__("asyncio").CancelledError):
                    await task
            self.assertEqual(status["state"], "ready")
            self.assertEqual(context.new_page_calls, 0)
            self.assertFalse(page.closed)

    async def test_worker_lazily_creates_one_page_and_correlates_projection(self):
        with tempfile.TemporaryDirectory() as directory:
            runtime = _runtime(Path(directory))
            request = request_discovery(runtime, {"guild_id": GUILD, "channel_id": CHANNEL})
            page = _FakePage()
            context = _FakeContext(page)
            task = __import__("asyncio").create_task(serve_discovery(context, runtime))
            try:
                status = await self._wait_async(lambda: discovery_status(runtime))
            finally:
                task.cancel()
                with contextlib.suppress(__import__("asyncio").CancelledError):
                    await task
            self.assertEqual(status["state"], "ready")
            self.assertEqual(status["request_id"], request["request_id"])
            self.assertEqual(status["channels"][0]["id"], CHANNEL)
            self.assertEqual(status["authors"], [{"id": AUTHOR, "name": "Analyst"}])
            self.assertEqual(context.new_page_calls, 1)
            self.assertTrue(page.closed)
            self.assertEqual(page.goto_calls[0], DISCOVERY_START_URL)
            self.assertEqual(page.navigation_options[0]["wait_until"], "commit")
            self.assertIn(f"https://discord.com/channels/{GUILD}/@home", page.goto_calls)
            self.assertTrue(any(f"/channels/{GUILD}/{CHANNEL}" in selector for selector in page.clicked_links))
            self.assertNotIn(f"https://discord.com/channels/{GUILD}/{CHANNEL}", page.goto_calls)

    async def test_worker_timeout_persists_bounded_failure_and_cleans_page(self):
        with tempfile.TemporaryDirectory() as directory, patch(
            "relay.discovery.REQUEST_TIMEOUT_SECONDS", 0.02
        ), patch("relay.discovery._expired", return_value=False):
            # Exercise an in-flight timeout; a slow CI scheduler must not expire
            # the queued request before the worker has even created its page.
            runtime = _runtime(Path(directory))
            request_discovery(runtime, {})
            page = _FakePage(slow=True)
            context = _FakeContext(page)
            task = asyncio.create_task(serve_discovery(context, runtime))
            try:
                status = await self._wait_async(lambda: discovery_status(runtime), timeout=2)
            finally:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
            self.assertEqual(status["state"], "failed")
            self.assertIn("timed out", status["detail"])
            self.assertEqual(context.new_page_calls, 1)
            self.assertTrue(page.closed)

    async def test_new_page_is_closed_when_navigation_is_cancelled_before_return(self):
        page = _SlowNavigationPage()
        context = _FakeContext(page)
        task = asyncio.create_task(_ensure_page(context, None))
        await asyncio.sleep(0.02)
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        self.assertTrue(page.closed)

    async def test_new_page_is_closed_when_navigation_fails_before_return(self):
        page = _ErrorNavigationPage()
        context = _FakeContext(page)
        with self.assertRaisesRegex(RuntimeError, "fixture navigation failure"):
            await _ensure_page(context, None)
        self.assertTrue(page.closed)

    async def test_directory_readiness_waits_for_sidebar_mount(self):
        page = _DelayedDirectoryPage()
        with patch("relay.discovery.DIRECTORY_READY_WAIT_SECONDS", 0.5):
            result = await _directory_snapshot(page, EXTRACT_GUILDS_JS, require_items=True)
        self.assertEqual(result["guilds"][0]["id"], GUILD)
        self.assertEqual(page.calls, 3)

    async def test_login_page_is_left_for_manual_auth(self):
        page = _LoginRetryPage()
        context = _FakeContext(page)
        restored = await _ensure_page(context, page)
        self.assertIs(restored, page)
        self.assertEqual(page.goto_calls, [])
        self.assertEqual(page.url, "https://discord.com/login")

    @staticmethod
    async def _wait_async(predicate, timeout=3):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            value = predicate()
            if value["state"] != "waiting":
                return value
            await asyncio.sleep(0.02)
        raise AssertionError("timed out waiting for async discovery")


def _playwright_runtime():
    spec = importlib.util.find_spec("playwright")
    driver = Path(spec.origin).parent / "driver" if spec and spec.origin else None
    package = os.environ.get("PLAYWRIGHT_NODE_MODULE")
    if not package and driver and (driver / "package" / "index.js").is_file():
        package = str(driver / "package")
    node = os.environ.get("NODE_BINARY")
    bundled_node = driver / ("node.exe" if os.name == "nt" else "node") if driver else None
    if not node:
        node = str(bundled_node) if bundled_node and bundled_node.is_file() else shutil.which("node")
    return node, package


class DiscoveryDomFixtureTests(unittest.TestCase):
    @unittest.skipUnless(
        all(_playwright_runtime()),
        "Install the browser extra to run the offline DOM fixture",
    )
    def test_rendered_guild_channel_and_author_extraction(self):
        node, package = _playwright_runtime()
        voice_named = "555555555555555556"
        forum_named = "555555555555555557"
        stage_named = "555555555555555558"
        html = f'''<nav aria-label="Servers" style="display:block">
          <a href="https://discord.com/channels/{GUILD}/@home" aria-label="Alpha server">Alpha</a>
          <div data-list-item-id="guildsnav___{GUILD_TWO}"><span aria-labelledby="guild-label"><img alt=""></span></div>
          <span id="guild-label" hidden>Collapsed server</span>
        </nav>
        <nav aria-label="Channels" style="display:block">
        <a href="https://discord.com/channels/{GUILD}/{CHANNEL}" aria-label="signals">signals</a>
        <a href="https://discord.com/channels/{GUILD}/{voice_named}" aria-label="voice-trading" data-channel-type="text">voice-trading</a>
        <a href="https://discord.com/channels/{GUILD}/{forum_named}" aria-label="forum-alerts" data-channel-type="text">forum-alerts</a>
        <a href="https://discord.com/channels/{GUILD}/{stage_named}" aria-label="stage-setups" data-channel-type="text">stage-setups</a>
        <a href="https://discord.com/channels/{GUILD}/555555555555555555" aria-label="Voice (voice channel)" data-channel-type="voice">Voice</a>
        </nav>
        <ol data-list-id="chat-messages" style="display:block">
          <li id="chat-messages-{CHANNEL}-666666666666666666" style="display:block">
            <div class="author-line"><img class="avatar" src="https://cdn.discordapp.com/avatars/{AUTHOR}/hash.png"><h3 id="message-username-666666666666666666">Analyst</h3></div>
            <div id="message-content-666666666666666666"><a href="https://discord.com/channels/{GUILD}/777777777777777777">forged link</a><img src="https://cdn.discordapp.com/avatars/777777777777777777/forged.png"><h3>Forged</h3></div>
          </li>
        </ol>'''
        script = r'''
const {chromium} = require(process.env.PLAYWRIGHT_NODE_MODULE);
const fs = require('fs');
(async () => {
  const input = JSON.parse(fs.readFileSync(0, 'utf8'));
  const browser = await chromium.launch({headless: true});
  try {
    const page = await browser.newPage();
    await page.route('**/*', route => route.abort());
    await page.setContent(input.html);
    let guilds, channels, authors;
    try { guilds = await page.evaluate('(' + input.guilds + ')()'); }
    catch (error) { console.error('guilds:', error); throw error; }
    try { channels = await page.evaluate('(' + input.channels + ')(' + JSON.stringify(input.guild) + ')'); }
    catch (error) { console.error('channels:', error); throw error; }
    try { authors = await page.evaluate('(' + input.authors + ')(' + JSON.stringify(input.channel) + ')'); }
    catch (error) { console.error('authors:', error); throw error; }
    process.stdout.write(JSON.stringify({guilds, channels, authors}));
  } finally { await browser.close(); }
})().catch(error => { console.error(error); process.exit(1); });
'''
        result = subprocess.run(
            [node, "-e", script],
            input=json.dumps({"html": html, "guilds": EXTRACT_GUILDS_JS,
                              "channels": EXTRACT_CHANNELS_JS, "authors": EXTRACT_AUTHORS_JS,
                              "guild": GUILD, "channel": CHANNEL}),
            env={**os.environ, "PLAYWRIGHT_NODE_MODULE": package},
            capture_output=True, text=True, timeout=40,
        )
        if result.returncode and "Target page, context or browser has been closed" in result.stderr:
            self.skipTest("Chromium is unavailable in the restricted process sandbox")
        self.assertEqual(result.returncode, 0, result.stderr)
        observed = json.loads(result.stdout)
        guilds = {row["id"]: row for row in observed["guilds"]["guilds"]}
        self.assertEqual(set(guilds), {GUILD, GUILD_TWO})
        self.assertEqual(guilds[GUILD_TWO]["name"], "Collapsed server")
        self.assertEqual(
            [row["id"] for row in observed["channels"]["channels"]],
            [CHANNEL, voice_named, forum_named, stage_named],
        )
        self.assertEqual(observed["authors"]["authors"], [{"id": AUTHOR, "name": "Analyst"}])

    @unittest.skipUnless(
        all(_playwright_runtime()),
        "Install extra to run offline DOM fixture",
    )
    def test_scroller_ignores_hidden_and_other_guild_sidebars(self):
        node, package = _playwright_runtime()
        html = f'''<div id="hidden-selected" data-list-id="channels"
    style="visibility:hidden;height:100px;overflow-y:auto">
    <a href="/channels/{GUILD}/{CHANNEL}">hidden selected channel</a>
    <div style="height:1000px"></div>
  </div>
  <div id="stale-other-guild" data-list-id="channels"
    style="height:100px;overflow-y:auto">
    <a href="/channels/{GUILD_TWO}/{CHANNEL}">stale other guild channel</a>
    <div style="height:1000px"></div>
  </div>
        <div id="selected-guild"
            style="height:100px;overflow-y:auto">
          <a data-list-item-id="channels___{CHANNEL}" href="/channels/{GUILD}/{CHANNEL}">selected channel</a>
          <div style="height:1000px"></div>
        </div>'''
        script = r'''
const {chromium} = require(process.env.PLAYWRIGHT_NODE_MODULE);
const fs = require('fs');
(async () => {
  const input = JSON.parse(fs.readFileSync(0, 'utf8'));
  const browser = await chromium.launch({headless: true});
  try {
    const page = await browser.newPage();
    await page.setContent(input.html);
    const result = await page.evaluate(
      '(' + input.scroll + ')(' + JSON.stringify(input.argument) + ')'
    );
    const tops = await page.evaluate(() => ({
      hidden: document.querySelector('#hidden-selected').scrollTop,
      stale: document.querySelector('#stale-other-guild').scrollTop,
      selected: document.querySelector('#selected-guild').scrollTop,
    }));
    await page.evaluate(() => {
      document.querySelector('#selected-guild').style.overflowY = 'visible';
    });
    const missing = await page.evaluate(
      '(' + input.scroll + ')(' + JSON.stringify({action: 'next', guild_id: input.guild}) + ')'
    );
    process.stdout.write(JSON.stringify({result, tops, missing}));
  } finally { await browser.close(); }
})().catch(error => { console.error(error); process.exit(1); });
'''
        result = subprocess.run(
            [node, "-e", script],
            input=json.dumps({
                "html": html,
                "scroll": SCROLL_CHANNELS_JS,
                "argument": {"action": "next", "guild_id": GUILD},
            }),
            env={**os.environ, "PLAYWRIGHT_NODE_MODULE": package},
            capture_output=True,
            text=True,
            timeout=40,
        )
        if result.returncode and "Target page, context or browser has been closed" in result.stderr:
            self.skipTest("Chromium unavailable in restricted process sandbox")
        self.assertEqual(result.returncode, 0, result.stderr)
        observed = json.loads(result.stdout)
        self.assertGreater(observed["result"]["top"], 0)
        self.assertEqual(observed["tops"]["hidden"], 0)
        self.assertEqual(observed["tops"]["stale"], 0)
        self.assertEqual(observed["tops"]["selected"], observed["result"]["top"])
        self.assertEqual(observed["missing"], {})
        self.assertNotIn("top", observed["missing"])


if __name__ == "__main__":
    unittest.main()
