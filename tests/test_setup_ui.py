"""Synthetic browser checks for the authenticated setup surface.

These tests mock every API response in Playwright. They do not sign in to a
provider, start a model, inspect a broker, or place an order.
"""

from __future__ import annotations

import json
import threading
import unittest
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

try:
    from playwright.sync_api import expect, sync_playwright
except ImportError:  # pragma: no cover - optional local browser dependency
    expect = None
    sync_playwright = None


ROOT = Path(__file__).resolve().parents[1]
STATIC_ROOT = ROOT / "relay" / "static"


class StaticHandler(SimpleHTTPRequestHandler):
    """Serve the dashboard's two static URL roots without an API server."""

    def translate_path(self, path: str) -> str:
        route = urlparse(path).path
        if route in {"", "/"}:
            route = "/index.html"
        elif route.startswith("/static/"):
            route = route[len("/static") :]
        return str(STATIC_ROOT / route.lstrip("/"))

    def log_message(self, *_args):
        return


class SetupUITests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if sync_playwright is None:
            raise unittest.SkipTest("Playwright required for setup UI checks")

    def setUp(self):
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), StaticHandler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=3)

    @staticmethod
    def status_payload(**overrides):
        status = {
            "csrf_token": "synthetic-csrf",
            "configured": False,
            "paused": False,
            "poll_seconds": 3,
            "trading": {
                "mode": "shadow",
                "live_enabled": False,
                "worker_mode": "shadow",
                "pending": False,
            },
            "channels": [
                {
                    "url": "https://discord.com/channels/111/222",
                    "name": "Options room",
                    "role": "signals",
                    "authors": ["111111111111111111", "222222222222222222"],
                },
                {
                    "url": "https://discord.com/channels/333/444",
                    "name": "Context room",
                    "role": "context",
                    "authors": ["333333333333333333"],
                },
            ],
            "discord": {
                "state": "not_connected",
                "detail": "Open Browser login.",
                "discovery": {
                    "state": "idle",
                    "request_id": "",
                    "guild_id": "",
                    "channel_id": "",
                    "guilds": [],
                    "channels": [],
                    "authors": [],
                    "authors_limited": False,
                    "detail": "Sign in to Discord, then refresh servers to load the directory.",
                },
            },
            "codex": {"state": "not_connected", "detail": "No device sign-in active."},
            "robinhood": {"state": "not_connected", "detail": "No account connected."},
            "risk": {
                "entry_risk_min_fraction": "0.05",
                "entry_risk_max_fraction": "0.10",
                "min_confidence": 0.8,
                "allow_same_day_expiry": False,
            },
        }
        status.update(overrides)
        return status

    def route_api(self, page, state):
        def fulfill(route, payload, status=200):
            route.fulfill(
                status=status,
                content_type="application/json",
                body=json.dumps(payload),
            )

        def handle(route, request):
            path = urlparse(request.url).path
            if path == "/api/status":
                fulfill(route, state.get("runtime_status", {"mode": "shadow", "live_orders_enabled": False, "counts": {}}))
                return
            if path in {"/api/messages", "/api/orders", "/api/events"}:
                fulfill(route, {"items": [], "next_offset": None})
                return
            if path == "/api/positions":
                fulfill(route, {"items": []})
                return
            if path == "/api/setup" and request.method == "GET":
                if state.pop("hold_next_setup", False):
                    state["held_setup"] = (route, json.loads(json.dumps(state["status"])))
                    return
                if state.get("setup_error"):
                    error = state["setup_error"]
                    fulfill(route, {"detail": error["detail"]}, error["status"])
                    return
                if state.get("setup_responses"):
                    response = state["setup_responses"].pop(0)
                    if response.get("__error__"):
                        state["setup_error"] = response["__error__"]
                        error = state["setup_error"]
                        fulfill(route, {"detail": error["detail"]}, error["status"])
                        return
                    state["status"] = response
                if state.get("discovery_responses"):
                    discovery = state["discovery_responses"].pop(0)
                    state["status"].setdefault("discord", {})["discovery"] = discovery
                fulfill(route, state["status"])
                return
            if path.startswith("/api/setup/") and request.method == "POST":
                state["headers"].append(dict(request.headers))
                body = request.post_data_json or {}
                state["requests"].append((path, body))
                if path == "/api/setup/discord/discover":
                    state.setdefault("discovery_requests", []).append(body)
                    if state.get("discovery_error"):
                        fulfill(route, {"error": state["discovery_error"]}, 400)
                        return
                    state.setdefault("discovery_responses", [])
                    current = state["status"].setdefault("discord", {})
                    current["discovery"] = {
                        "state": "waiting",
                        "request_id": f"discovery-{len(state['discovery_requests'])}",
                        "guild_id": body.get("guild_id", ""),
                        "channel_id": body.get("channel_id", ""),
                        "guilds": [],
                        "channels": [],
                        "authors": [],
                        "authors_limited": True,
                        "detail": "Discovery request accepted.",
                    }
                    if state.get("discovery_result_queue"):
                        state["discovery_responses"].append(state["discovery_result_queue"].pop(0))
                    fulfill(route, dict(state["status"], accepted="discovery_requested"))
                    return
                if path == "/api/setup/auth/codex/start":
                    state["status"].update(
                        codex={
                            "state": "waiting",
                            "detail": "Complete device sign-in.",
                            "user_code": "SYNTHETIC-CODE",
                            "verification_url": "https://auth.openai.com/codex/device",
                        },
                    )
                elif path == "/api/setup/auth/codex/cancel":
                    state["status"]["codex"] = {"state": "cancelled", "detail": "Device sign-in cancelled."}
                elif path == "/api/setup/auth/robinhood/start":
                    state["status"].update(
                        robinhood={
                            "state": "waiting",
                            "detail": "Continue Robinhood sign-in in your browser.",
                            "last_four": "4321",
                            "account": {"type": "cash", "state": "active", "option_level": "option_level_2", "agentic_allowed": True},
                            "authorization_url": state.get("robinhood_authorization_url", "https://robinhood.com/oauth/authorize?state=synthetic-state"),
                        },
                    )
                elif path == "/api/setup/auth/robinhood/cancel":
                    state["status"]["robinhood"] = {"state": "cancelled", "detail": "Robinhood sign-in cancelled."}
                elif path == "/api/setup/auth/robinhood/callback":
                    callback_error = state.get("callback_error")
                    if callback_error:
                        fulfill(route, {"error": {"detail": callback_error["detail"]}}, callback_error.get("status", 400))
                        return
                    state["status"]["robinhood"] = {"state": "connected", "detail": "Robinhood sign-in completed."}
                elif path == "/api/setup/channels":
                    state["status"]["channels"] = body.get("channels", [])
                    state["status"]["poll_seconds"] = body.get("poll_seconds", 3)
                elif path == "/api/setup/mode":
                    state.setdefault("mode_requests", []).append(body)
                    if state.get("mode_error"):
                        error = state["mode_error"]
                        fulfill(route, {"detail": error["detail"]}, error["status"])
                        return
                    response = state.pop("mode_response", None)
                    if response and response.get("__error__"):
                        error = response["__error__"]
                        fulfill(route, {"detail": error["detail"]}, error["status"])
                        return
                    if response:
                        state["status"] = response
                    else:
                        mode = body.get("mode")
                        trading = state["status"].setdefault("trading", {})
                        trading.update(
                            mode=mode,
                            live_enabled=mode == "live",
                            worker_mode=state.get("mode_worker_mode", mode),
                            pending=state.get("mode_pending", False),
                        )
                elif path == "/api/setup/evaluation":
                    state.setdefault("evaluation_requests", []).append(body)
                    if state.get("evaluation_error"):
                        fulfill(route, {"detail": "Synthetic save failure"}, 409)
                        return
                    state["status"]["evaluation"] = body
                    state["status"]["risk"]["max_chase_fraction"] = body["max_chase_fraction"]
                elif path == "/api/setup/expiry-policy":
                    state.setdefault("expiry_policy_requests", []).append(body)
                    if state.get("expiry_policy_error"):
                        error = state["expiry_policy_error"]
                        fulfill(route, {"detail": error["detail"]}, error["status"])
                        return
                    if state.get("hold_expiry_policy"):
                        state["held_expiry_policy"] = route
                        return
                    state["status"].setdefault("risk", {})["allow_same_day_expiry"] = body.get("allow_same_day_expiry")
                elif path == "/api/setup/pause":
                    state["status"]["paused"] = bool(body.get("paused"))
                elif path == "/api/setup/reconnect":
                    pass
                fulfill(route, state["status"])
                return
            route.continue_()

        page.route("**/*", handle)

    def new_page(self, playwright, state):
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 1280, "height": 1000})
        page.add_init_script("window.open = () => ({ focus() {} });")
        self.route_api(page, state)
        page.goto(f"http://127.0.0.1:{self.server.server_address[1]}/", wait_until="domcontentloaded")
        expect(page.get_by_role("heading", name="Connections and channels")).to_be_visible()
        return browser, page

    def test_dirty_channel_inputs_survive_auth_progress(self):
        state = {
            "status": self.status_payload(),
            "headers": [],
            "requests": [],
            "discovery_requests": [],
        }
        with sync_playwright() as playwright:
            browser, page = self.new_page(playwright, state)
            try:
                name = page.locator('[data-channel-index="0"] input[data-channel-field="name"]')
                expect(name).to_have_value("Options room")
                expect(page.locator("#setup-state-stamp")).to_have_text("SETUP REQUIRED")
                expect(page.locator("#risk-context-text")).to_contain_text("5–10%")
                expect(page.locator("#risk-context-text")).to_contain_text("80%")
                name.fill("Draft room")
                with page.expect_response(lambda response: response.url.endswith("/api/setup/auth/codex/start")):
                    page.get_by_role("button", name="Start Codex sign-in").click()
                expect(page.locator("#codex-device-code")).to_have_text("SYNTHETIC-CODE")
                expect(page.locator("#codex-device-url")).to_have_attribute("href", "https://auth.openai.com/codex/device")
                expect(name).to_have_value("Draft room")
                self.assertTrue(any(headers.get("x-relay-csrf") == "synthetic-csrf" for headers in state["headers"]))
            finally:
                browser.close()

    def test_robinhood_auth_link_pause_resume_and_reconnect_use_fixed_routes(self):
        state = {
            "status": self.status_payload(),
            "headers": [],
            "requests": [],
            "discovery_requests": [],
        }
        with sync_playwright() as playwright:
            browser, page = self.new_page(playwright, state)
            try:
                page.locator("#robinhood-account-number").fill("12344321")
                with page.expect_response(lambda response: response.url.endswith("/api/setup/auth/robinhood/start")):
                    page.get_by_role("button", name="Start Robinhood sign-in").click()
                expect(page.locator("#robinhood-account-summary")).to_be_visible()
                expect(page.locator("#robinhood-account-label")).to_have_text("Account ending 4321")
                expect(page.locator("#robinhood-account-detail")).to_contain_text("Agentic enabled")
                authorization_link = page.locator("#robinhood-authorization-url")
                expect(authorization_link).to_be_visible()
                expect(authorization_link).to_have_attribute("href", "https://robinhood.com/oauth/authorize?state=synthetic-state")
                expect(authorization_link).to_have_attribute("target", "_blank")
                expect(authorization_link).to_have_attribute("rel", "noopener noreferrer")
                expect(page.locator("#browser-login-dialog")).to_be_hidden()
                self.assertIn(("/api/setup/auth/robinhood/start", {"account_number": "12344321"}), state["requests"])

                callback_form = page.locator("#robinhood-remote-callback")
                callback_form.locator("summary").click()
                callback_input = page.locator("#robinhood-callback-url")
                callback_input.fill("https://127.0.0.1:8766/callback?code=cancelled&state=synthetic-state")

                with page.expect_response(lambda response: response.url.endswith("/api/setup/auth/robinhood/cancel")):
                    page.locator("#robinhood-cancel").click()
                expect(page.locator("#robinhood-authorization-url")).to_be_hidden()
                self.assertIsNone(page.locator("#robinhood-authorization-url").get_attribute("href"))
                expect(callback_input).to_have_value("")
                expect(callback_form).to_be_hidden()

                with page.expect_response(lambda response: response.url.endswith("/api/setup/pause")):
                    page.get_by_role("button", name="Pause relay").click()
                expect(page.get_by_role("button", name="Resume relay")).to_be_visible()

                with page.expect_response(lambda response: response.url.endswith("/api/setup/pause")):
                    page.get_by_role("button", name="Resume relay").click()
                expect(page.get_by_role("button", name="Pause relay")).to_be_visible()

                with page.expect_response(lambda response: response.url.endswith("/api/setup/reconnect")):
                    page.get_by_role("button", name="Reconnect").click()
                self.assertIn(("/api/setup/reconnect", {}), state["requests"])
            finally:
                browser.close()

    def test_robinhood_remote_callback_posts_once_and_clears_on_completion(self):
        callback_url = "http://127.0.0.1:8766/callback?code=approved&state=synthetic-state"
        authorization_url = "https://robinhood.com/oauth/authorize?redirect_uri=http%3A%2F%2F127.0.0.1%3A8766%2Fcallback&state=synthetic-state"
        state = {
            "status": self.status_payload(),
            "headers": [],
            "requests": [],
            "discovery_requests": [],
            "robinhood_authorization_url": authorization_url,
        }
        with sync_playwright() as playwright:
            browser, page = self.new_page(playwright, state)
            try:
                callback_form = page.locator("#robinhood-remote-callback")
                callback_input = page.locator("#robinhood-callback-url")
                callback_submit = page.locator("#robinhood-callback-submit")
                expect(callback_form).to_be_hidden()
                expect(callback_submit).to_be_disabled()

                page.locator("#robinhood-start").click()
                expect(callback_form).to_be_visible()
                expect(callback_input).to_have_attribute("type", "password")
                expect(callback_input).to_have_attribute("autocomplete", "off")
                expect(callback_input).to_have_attribute("spellcheck", "false")
                page.wait_for_timeout(250)
                self.assertFalse(any(path == "/api/setup/auth/robinhood/callback" for path, _body in state["requests"]))

                callback_form.locator("summary").click()
                callback_input.fill(callback_url)
                expect(callback_submit).to_be_enabled()
                with page.expect_response(lambda response: response.url.endswith("/api/setup/auth/robinhood/callback")):
                    callback_submit.click()

                self.assertIn(("/api/setup/auth/robinhood/callback", {"callback_url": callback_url}), state["requests"])
                expect(callback_input).to_have_value("")
                expect(callback_form).to_be_hidden()
                expect(callback_submit).to_be_disabled()
            finally:
                browser.close()

    def test_robinhood_remote_callback_error_is_shown_without_retaining_url(self):
        callback_url = "http://127.0.0.1:8766/callback?code=bad&state=synthetic-state"
        authorization_url = "https://robinhood.com/oauth/authorize?redirect_uri=http%3A%2F%2F127.0.0.1%3A8766%2Fcallback&state=synthetic-state"
        state = {
            "status": self.status_payload(),
            "headers": [],
            "requests": [],
            "discovery_requests": [],
            "robinhood_authorization_url": authorization_url,
            "callback_error": {"detail": "The returned URL must match the callback address for this sign-in attempt"},
        }
        with sync_playwright() as playwright:
            browser, page = self.new_page(playwright, state)
            try:
                page.locator("#robinhood-start").click()
                page.locator("#robinhood-remote-callback summary").click()
                callback_input = page.locator("#robinhood-callback-url")
                callback_input.fill(callback_url)
                with page.expect_response(lambda response: response.url.endswith("/api/setup/auth/robinhood/callback")):
                    page.locator("#robinhood-callback-submit").click()
                expect(callback_input).to_have_value("")
                expect(page.locator("#robinhood-setup-detail")).to_contain_text("Could not update setup: The returned URL must match")
            finally:
                browser.close()

    def test_robinhood_authorization_link_rejects_untrusted_host(self):
        state = {
            "status": self.status_payload(),
            "headers": [],
            "requests": [],
            "discovery_requests": [],
            "robinhood_authorization_url": "https://evil.example/oauth/authorize?state=synthetic-state",
        }
        with sync_playwright() as playwright:
            browser, page = self.new_page(playwright, state)
            try:
                with page.expect_response(lambda response: response.url.endswith("/api/setup/auth/robinhood/start")):
                    page.get_by_role("button", name="Start Robinhood sign-in").click()
                expect(page.locator("#robinhood-authorization-url")).to_be_hidden()
                expect(page.locator("#robinhood-progress")).to_be_hidden()
                expect(page.locator("#browser-login-dialog")).to_be_hidden()
            finally:
                browser.close()

    def test_browser_login_dialog_tracks_discord_and_preserves_draft(self):
        state = {
            "status": self.status_payload(
                discord={"state": "waiting", "detail": "Complete Discord sign-in."},
            ),
            "headers": [],
            "requests": [],
            "discovery_requests": [],
        }
        with sync_playwright() as playwright:
            browser, page = self.new_page(playwright, state)
            try:
                name = page.locator('[data-channel-index="0"] input[data-channel-field="name"]')
                name.fill("Draft room")
                browser_link = page.get_by_role("link", name="Open Browser login")
                browser_link.click()
                dialog = page.locator("#browser-login-dialog")
                expect(dialog).to_be_visible()
                self.assertNotIn("/browser/vnc.html", page.url)
                expect(page.locator("#browser-login-frame")).to_have_attribute(
                    "src", "/browser/vnc.html?autoconnect=true&resize=scale&path=browser/websockify"
                )
                expect(page.locator("#browser-login-status-label")).to_have_text("Waiting")
                expect(page.locator("#browser-login-detail")).to_have_text("Complete Discord sign-in.")

                state["setup_responses"] = [
                    self.status_payload(
                        discord={
                            "state": "connected",
                            "detail": "Discord is signed in. You can return to Setup.",
                        },
                    ),
                ]
                expect(page.locator("#browser-login-status-label")).to_have_text("Signed in", timeout=5000)
                expect(page.locator("#browser-login-detail")).to_have_text(
                    "Discord is signed in. You can return to Setup."
                )
                expect(name).to_have_value("Draft room")

                page.locator("#browser-login-close").click()
                expect(dialog).to_be_hidden()
                expect(browser_link).to_be_focused()
                browser_link.click()
                expect(dialog).to_be_visible()
                page.keyboard.press("Escape")
                expect(dialog).to_be_hidden()
                expect(browser_link).to_be_focused()
            finally:
                browser.close()

    def test_loading_and_provider_diagnostics_are_visible(self):
        loading = "Discord is still loading. The browser will stay open; manual sign-in has no time limit."
        state = {"status": self.status_payload(
            discord={"state": "starting", "detail": loading},
            codex={"state": "failed", "detail": "Codex device login failed [dns_failed]. Check NAS DNS.",
                   "failure": {"phase": "device_login", "code": "dns_failed"}},
            robinhood={"state": "failed", "detail": "Robinhood token exchange failed [http_503]. Retry when service recovers.",
                       "failure": {"phase": "token_exchange", "type": "HTTPStatusError", "http_status": 503, "source": "broker.py", "line": 42}},
        ), "headers": [], "requests": []}
        state["runtime_status"] = {"mode": "shadow", "runtime": {
            "discord": {"state": "starting", "detail": loading},
            "codex": {"state": "unavailable", "detail": "evaluation failed: code=timeout; attempts=2; no order submitted"},
            "broker": {"state": "unavailable", "detail": "Robinhood connection failed [dns_failed]. Check NAS DNS."},
        }}
        with sync_playwright() as playwright:
            browser, page = self.new_page(playwright, state)
            try:
                expect(page.locator("#codex-setup-detail")).to_contain_text("Code: dns_failed")
                expect(page.locator("#robinhood-setup-detail")).to_contain_text("HTTP: 503")
                expect(page.locator("#robinhood-setup-detail")).to_contain_text("Source: broker.py:42")
                page.get_by_role("link", name="Open Browser login").click()
                expect(page.locator("#browser-login-detail")).to_have_text(loading)
                expect(page.locator("#browser-login-status-label")).not_to_have_text("Failed")
                page.locator("#browser-login-close").click()
                expect(page.locator("#browser-login-dialog")).to_be_hidden()
                expect(page.locator("#runtime-discord-detail")).to_have_text(loading)
                expect(page.locator("#runtime-codex-detail")).to_contain_text("attempts=2")
                expect(page.locator("#runtime-broker-detail")).to_contain_text("dns_failed")
                self.assertEqual(state["requests"], [])
            finally:
                browser.close()

    def test_browser_login_dialog_clears_success_on_poll_error(self):
        state = {
            "status": self.status_payload(
                discord={
                    "state": "connected",
                    "detail": "Discord is signed in. You can return to Setup.",
                },
            ),
            "headers": [],
            "requests": [],
            "discovery_requests": [],
        }
        with sync_playwright() as playwright:
            browser, page = self.new_page(playwright, state)
            try:
                browser_link = page.get_by_role("link", name="Open Browser login")
                browser_link.click()
                expect(page.locator("#browser-login-status-label")).to_have_text("Signed in")
                state["setup_responses"] = [
                    {"__error__": {"status": 503, "detail": "status backend unavailable"}},
                ]
                expect(page.locator("#browser-login-status-label")).to_have_text("Unknown", timeout=5000)
                expect(page.locator("#browser-login-detail")).to_have_text(
                    "Setup status unavailable: status backend unavailable"
                )
                page.locator("#browser-login-close").click()
                expect(page.locator("#browser-login-dialog")).to_be_hidden()
                expect(browser_link).to_be_focused()
            finally:
                browser.close()

    def test_discovery_selection_and_save_accept_all_authors(self):
        guild_id = "100000000000000001"
        channel_id = "200000000000000001"
        state = {
            "status": self.status_payload(
                channels=[
                    {"url": "", "name": "Channel 1", "role": "", "authors": []},
                    {"url": "", "name": "Channel 2", "role": "", "authors": []},
                ],
                discord={
                    "state": "connected",
                    "detail": "Discord is signed in.",
                    "discovery": {
                        "state": "idle",
                        "guilds": [],
                        "channels": [],
                        "authors": [],
                        "authors_limited": False,
                    },
                },
            ),
            "headers": [],
            "requests": [],
            "discovery_requests": [],
            "discovery_result_queue": [
                {
                    "state": "ready",
                    "request_id": "discovery-1",
                    "guild_id": "",
                    "channel_id": "",
                    "guilds": [{"id": guild_id, "name": "Demo Server"}],
                    "channels": [],
                    "authors": [],
                    "authors_limited": False,
                    "detail": "Found one server.",
                },
                {
                    "state": "ready",
                    "request_id": "discovery-2",
                    "guild_id": guild_id,
                    "channel_id": "",
                    "guilds": [{"id": guild_id, "name": "Demo Server"}],
                    "channels": [{"id": channel_id, "guild_id": guild_id, "name": "options-trading", "url": f"https://discord.com/channels/{guild_id}/{channel_id}"}],
                    "authors": [],
                    "authors_limited": False,
                    "detail": "Found one channel.",
                },
                {
                    "state": "ready",
                    "request_id": "discovery-3",
                    "guild_id": guild_id,
                    "channel_id": channel_id,
                    "guilds": [{"id": guild_id, "name": "Demo Server"}],
                    "channels": [{"id": channel_id, "guild_id": guild_id, "name": "options-trading", "url": f"https://discord.com/channels/{guild_id}/{channel_id}"}],
                    "authors": [{"id": "300000000000000001", "name": "Signal Author"}],
                    "authors_limited": True,
                    "detail": "Observed authors are partial.",
                },
            ],
        }
        with sync_playwright() as playwright:
            browser, page = self.new_page(playwright, state)
            try:
                guild = page.locator('[data-channel-index="0"] select[data-channel-field="guild_id"]')
                channel = page.locator('[data-channel-index="0"] select[data-channel-field="channel_id"]')
                expect(guild.locator(f'option[value="{guild_id}"]')).to_have_text("Demo Server", timeout=6000)
                guild.select_option(guild_id)
                expect(channel.locator(f'option[value="{channel_id}"]')).to_have_text("options-trading", timeout=6000)
                channel.select_option(channel_id)
                restriction = page.locator('[data-channel-index="0"] input[data-channel-field="restrict_authors"]')
                expect(page.locator('[data-channel-index="0"] select[data-channel-field="author_choices"] option')).to_have_text("Signal Author · 300000000000000001", timeout=6000)
                self.assertFalse(restriction.is_checked())
                page.locator('[data-channel-index="0"] input[data-channel-field="name"]').fill("Trading alerts")
                page.locator('[data-channel-index="0"] select[data-channel-field="role"]').select_option("signals")
                page.locator('[data-channel-index="1"] input[data-channel-field="name"]').fill("Context")
                page.locator('[data-channel-index="1"] select[data-channel-field="role"]').select_option("context")
                with page.expect_response(lambda response: response.url.endswith("/api/setup/channels")):
                    page.get_by_role("button", name="Save channels").click()
                self.assertEqual(state["requests"][-1][0], "/api/setup/channels")
                self.assertEqual(len(state["requests"][-1][1]["channels"]), 2)
                self.assertEqual(state["requests"][-1][1]["channels"][0]["authors"], [])
                self.assertEqual(state["requests"][-1][1]["channels"][0]["url"], f"https://discord.com/channels/{guild_id}/{channel_id}")
                expect(page.locator("#channel-form-feedback")).to_have_text("Channels saved.")
            finally:
                browser.close()

    def test_saved_author_removal_manual_url_and_stale_poll(self):
        guild, channel = "100000000000000005", "200000000000000005"
        first, second = "300000000000000005", "300000000000000006"
        discovery = {"state": "ready", "request_id": "old", "guild_id": guild, "channel_id": channel,
                     "guilds": [{"id": guild, "name": "Saved server"}],
                     "channels": [{"id": channel, "guild_id": guild, "name": "Saved channel"}],
                     "authors": [{"id": first, "name": "First author"}, {"id": second, "name": "Second author"}]}
        status = self.status_payload(discord={"state": "not_connected", "discovery": discovery})
        status["channels"][0].update(url=f"https://discord.com/channels/{guild}/{channel}", authors=[first, second])
        state = {"status": status, "headers": [], "requests": [], "setup_responses": []}
        with sync_playwright() as playwright:
            browser, page = self.new_page(playwright, state)
            try:
                row = page.locator('[data-channel-index="0"]')
                choices = row.locator('[data-channel-field="author_choices"]')
                expect(choices).to_have_values([first, second])
                expect(row.locator('[data-channel-field="authors"]')).to_have_value("")
                choices.select_option(second)
                with page.expect_response(lambda response: response.url.endswith("/api/setup/channels")):
                    page.get_by_role("button", name="Save channels").click()
                self.assertEqual(state["requests"][-1][1]["channels"][0]["authors"], [second])
                new_url = "https://discord.com/channels/100000000000000009/200000000000000009"
                row.locator(".advanced-channel summary").click()
                row.locator('[data-channel-field="url"]').fill(new_url)
                expect(row.locator('[data-channel-field="guild_id"]')).to_have_value("100000000000000009")
                expect(row.locator('[data-channel-field="channel_id"]')).to_have_value("200000000000000009")
                expect(row.locator('[data-channel-field="restrict_authors"]')).not_to_be_checked()
                with page.expect_response(lambda response: response.url.endswith("/api/setup/channels")):
                    page.get_by_role("button", name="Save channels").click()
                saved = state["requests"][-1][1]["channels"][0]
                self.assertEqual((saved["url"], saved["authors"]), (new_url, []))

                state["hold_next_setup"] = True
                page.get_by_role("link", name="Open Browser login").click()
                page.wait_for_timeout(1900)
                self.assertIn("held_setup", state)
                page.get_by_role("button", name="Back to setup").click()
                state["discovery_result_queue"] = [{"state": "ready", "request_id": "discovery-1",
                    "guild_id": None, "channel_id": None, "guilds": [], "channels": [], "authors": [], "detail": "Fresh directory"}]
                page.get_by_role("button", name="Refresh servers").click()
                expect(page.locator("#discord-discovery-detail")).to_have_text("Fresh directory", timeout=6000)
                route, stale = state.pop("held_setup")
                route.fulfill(status=200, content_type="application/json", body=json.dumps(stale))
                page.wait_for_timeout(200)
                expect(page.locator("#discord-discovery-detail")).to_have_text("Fresh directory")
            finally:
                browser.close()

    def test_author_restriction_requires_a_selection_and_discovery_errors_are_visible(self):
        guild_id = "100000000000000002"
        channel_id = "200000000000000002"
        state = {
            "status": self.status_payload(
                channels=[
                    {"url": f"https://discord.com/channels/{guild_id}/{channel_id}", "name": "Saved", "role": "signals", "authors": []},
                    {"url": "https://discord.com/channels/333/444", "name": "Context", "role": "context", "authors": []},
                ],
                discord={
                    "state": "not_connected",
                    "detail": "Open Browser login.",
                    "discovery": {
                        "state": "ready",
                        "request_id": "",
                        "guild_id": guild_id,
                        "channel_id": channel_id,
                        "guilds": [{"id": guild_id, "name": "Saved server"}],
                        "channels": [{"id": channel_id, "guild_id": guild_id, "name": "Saved channel", "url": f"https://discord.com/channels/{guild_id}/{channel_id}"}],
                        "authors": [],
                        "authors_limited": True,
                    },
                },
            ),
            "headers": [],
            "requests": [],
            "discovery_requests": [],
            "discovery_error": "",
            "discovery_result_queue": [],
        }
        with sync_playwright() as playwright:
            browser, page = self.new_page(playwright, state)
            try:
                restriction = page.locator('[data-channel-index="0"] input[data-channel-field="restrict_authors"]')
                restriction.check()
                page.get_by_role("button", name="Save channels").click()
                expect(page.locator("#channel-form-feedback")).to_have_text("Choose at least one observed or manual author, or turn off author restriction.")
                restriction.uncheck()

                state["discovery_error"] = "Discord browser is not signed in"
                page.get_by_role("button", name="Refresh servers").click()
                expect(page.locator("#discord-discovery-feedback")).to_contain_text("Discord browser is not signed in", timeout=3000)
            finally:
                browser.close()

    def test_evaluation_settings_pause_save_retry_and_reload(self):
        state = {"status": self.status_payload(configured=True, paused=False,
            evaluation={"model": None, "reasoning_effort": "low", "service_tier": "standard", "max_chase_fraction": "0.05"},
            trading={"mode": "live", "live_enabled": True, "worker_mode": None, "pending": True}),
            "headers": [], "requests": [], "discovery_requests": [], "evaluation_error": True}
        with sync_playwright() as playwright:
            browser, page = self.new_page(playwright, state)
            try:
                page.locator("#evaluation-model").fill("gpt-6-astra")
                page.locator("#evaluation-effort").select_option("medium")
                page.locator("#evaluation-tier").select_option("fast")
                page.locator("#evaluation-chase").fill("10")
                save = page.locator("#save-evaluation")
                expect(save).to_have_text("Pause and save evaluation settings")
                save.click()
                expect(page.locator("#evaluation-feedback")).to_contain_text("Synthetic save failure")
                expect(page.locator("#evaluation-model")).to_have_value("gpt-6-astra")
                expect(save).to_be_enabled()
                self.assertTrue(state["status"]["paused"])
                state.pop("evaluation_error")
                save.click()
                expect(page.locator("#evaluation-feedback")).to_contain_text("Evaluation settings saved")
                payload = {"model": "gpt-6-astra", "reasoning_effort": "medium", "service_tier": "fast", "max_chase_fraction": "0.1"}
                relevant = [request for request in state["requests"] if request[0] in {"/api/setup/pause", "/api/setup/evaluation"}]
                self.assertEqual(relevant, [("/api/setup/pause", {"paused": True}), ("/api/setup/evaluation", payload), ("/api/setup/evaluation", payload)])
                page.reload(wait_until="domcontentloaded")
                expect(page.locator("#evaluation-model")).to_have_value("gpt-6-astra")
                expect(page.locator("#evaluation-effort")).to_have_value("medium")
                expect(page.locator("#evaluation-tier")).to_have_value("fast")
                expect(page.locator("#evaluation-chase")).to_have_value("10")
                expect(page.locator("#save-evaluation")).to_be_enabled()
            finally:
                browser.close()

    def test_same_day_expiry_policy_is_editable_until_saved_and_retries_on_error(self):
        state = {
            "status": self.status_payload(
                configured=True,
                paused=False,
                discord={"state": "connected", "detail": "Discord is signed in."},
                trading={"mode": "live", "live_enabled": True, "worker_mode": "live", "pending": False},
            ),
            "headers": [],
            "requests": [],
            "discovery_requests": [],
            "expiry_policy_requests": [],
        }
        with sync_playwright() as playwright:
            browser, page = self.new_page(playwright, state)
            try:
                expiry = page.get_by_role("checkbox", name="Allow same-day (0DTE) entries")
                save = page.locator("#save-expiry-policy")
                expect(expiry).to_be_enabled()
                expect(expiry).not_to_be_checked()
                expect(save).to_have_text("Pause and save")
                expect(save).to_be_enabled()

                save.click()
                expect(page.locator("#expiry-policy-feedback")).to_have_text("Same-day entry permission saved.")
                expect(save).to_be_enabled()
                self.assertEqual(
                    [request for request in state["requests"] if request[0] in {"/api/setup/pause", "/api/setup/expiry-policy"}],
                    [
                        ("/api/setup/pause", {"paused": True}),
                        ("/api/setup/expiry-policy", {"allow_same_day_expiry": False}),
                    ],
                )
                page.reload(wait_until="domcontentloaded")
                expect(expiry).not_to_be_checked()
                expect(save).to_have_text("Save permission")
                expect(save).to_be_enabled()

                expiry.check()
                expect(expiry).to_be_checked()
                expect(page.locator("#expiry-policy-feedback")).to_have_text("Unsaved changes.")
                expect(save).to_be_enabled()
                self.assertEqual(state["expiry_policy_requests"], [{"allow_same_day_expiry": False}])
                self.assertTrue(page.evaluate("() => { const event = new Event('beforeunload', {cancelable: true}); window.dispatchEvent(event); return event.defaultPrevented; }"))

                state["hold_next_setup"] = True
                page.get_by_role("link", name="Open Browser login").click()
                page.wait_for_timeout(1900)
                self.assertIn("held_setup", state)
                stale = state["held_setup"][1]
                self.assertFalse(stale["risk"]["allow_same_day_expiry"])
                stale_route, stale = state.pop("held_setup")
                stale_route.fulfill(status=200, content_type="application/json", body=json.dumps(stale))
                page.wait_for_timeout(200)
                expect(expiry).to_be_checked()
                expect(page.locator("#expiry-policy-feedback")).to_have_text("Unsaved changes.")
                self.assertEqual(state["expiry_policy_requests"], [{"allow_same_day_expiry": False}])
                page.get_by_role("button", name="Back to setup").click()

                state["expiry_policy_error"] = {"status": 409, "detail": "policy refused"}
                save.click()
                expect(page.locator("#expiry-policy-feedback")).to_contain_text("Could not update setup")
                expect(expiry).to_be_checked()
                expect(save).to_have_text("Save permission")
                expect(save).to_be_enabled()
                self.assertEqual(
                    [request for request in state["requests"] if request[0] in {"/api/setup/pause", "/api/setup/expiry-policy"}],
                    [
                        ("/api/setup/pause", {"paused": True}),
                        ("/api/setup/expiry-policy", {"allow_same_day_expiry": False}),
                        ("/api/setup/expiry-policy", {"allow_same_day_expiry": True}),
                    ],
                )
                self.assertTrue(page.evaluate("() => { const event = new Event('beforeunload', {cancelable: true}); window.dispatchEvent(event); return event.defaultPrevented; }"))

                state.pop("expiry_policy_error")
                save.click()
                expect(page.locator("#expiry-policy-feedback")).to_have_text("Same-day entry permission saved.")
                expect(save).to_be_enabled()
                self.assertEqual(
                    [request for request in state["requests"] if request[0] in {"/api/setup/pause", "/api/setup/expiry-policy"}],
                    [
                        ("/api/setup/pause", {"paused": True}),
                        ("/api/setup/expiry-policy", {"allow_same_day_expiry": False}),
                        ("/api/setup/expiry-policy", {"allow_same_day_expiry": True}),
                        ("/api/setup/expiry-policy", {"allow_same_day_expiry": True}),
                    ],
                )
                self.assertFalse(page.evaluate("() => { const event = new Event('beforeunload', {cancelable: true}); window.dispatchEvent(event); return event.defaultPrevented; }"))

                page.reload(wait_until="domcontentloaded")
                expect(page.locator("#allow-same-day-expiry")).to_be_checked()
                expect(page.locator("#save-expiry-policy")).to_be_enabled()
            finally:
                browser.close()

    def test_same_day_expiry_save_blocks_resume_and_ignores_stale_poll(self):
        state = {
            "status": self.status_payload(
                configured=True,
                paused=True,
                discord={"state": "connected", "detail": "Discord is signed in."},
                trading={"mode": "live", "live_enabled": True, "worker_mode": "live", "pending": False},
            ),
            "headers": [],
            "requests": [],
            "discovery_requests": [],
            "expiry_policy_requests": [],
            "hold_expiry_policy": True,
        }
        with sync_playwright() as playwright:
            browser, page = self.new_page(playwright, state)
            try:
                name = page.locator('[data-channel-index="0"] input[data-channel-field="name"]')
                name.fill("Draft room")
                expiry = page.get_by_role("checkbox", name="Allow same-day (0DTE) entries")
                save = page.locator("#save-expiry-policy")
                expect(expiry).to_be_enabled()
                expiry.check()
                expect(page.locator("#expiry-policy-feedback")).to_have_text("Unsaved changes.")
                expect(save).to_be_enabled()
                self.assertEqual(state["expiry_policy_requests"], [])
                save.click()
                expect(page.locator("#expiry-policy-feedback")).to_have_text("Saving…")
                expect(expiry).to_be_disabled()
                expect(save).to_be_disabled()
                expect(page.get_by_role("button", name="Resume relay")).to_be_disabled()
                expect(page.get_by_role("button", name="Use Shadow")).to_be_disabled()
                self.assertTrue(page.evaluate("() => { const event = new Event('beforeunload', {cancelable: true}); window.dispatchEvent(event); return event.defaultPrevented; }"))
                self.assertEqual(state["expiry_policy_requests"], [{"allow_same_day_expiry": True}])

                state["hold_next_setup"] = True
                page.get_by_role("link", name="Open Browser login").click()
                page.wait_for_timeout(1900)
                self.assertIn("held_setup", state)
                stale = state["held_setup"][1]
                self.assertFalse(stale["risk"]["allow_same_day_expiry"])

                expiry_route = state.pop("held_expiry_policy")
                state["status"]["risk"]["allow_same_day_expiry"] = True
                expiry_route.fulfill(status=200, content_type="application/json", body=json.dumps(state["status"]))
                expect(expiry).to_be_checked()
                expect(page.locator("#expiry-policy-feedback")).to_have_text("Same-day entry permission saved.")
                expect(save).to_be_enabled()
                expect(page.get_by_role("button", name="Resume relay")).to_be_enabled()
                self.assertFalse(page.evaluate("() => { const event = new Event('beforeunload', {cancelable: true}); window.dispatchEvent(event); return event.defaultPrevented; }"))
                expect(name).to_have_value("Draft room")

                stale_route, stale = state.pop("held_setup")
                stale_route.fulfill(status=200, content_type="application/json", body=json.dumps(stale))
                page.wait_for_timeout(200)
                expect(expiry).to_be_checked()
                expect(save).to_be_enabled()
                expect(name).to_have_value("Draft room")
            finally:
                browser.close()

    def test_same_day_expiry_policy_pending_reload_is_clickable_and_explains(self):
        state = {
            "status": self.status_payload(
                configured=True,
                paused=True,
                discord={"state": "connected", "detail": "Discord is signed in."},
                trading={"mode": "live", "live_enabled": True, "worker_mode": "live", "pending": True},
            ),
            "headers": [],
            "requests": [],
            "discovery_requests": [],
            "expiry_policy_requests": [],
        }
        with sync_playwright() as playwright:
            browser, page = self.new_page(playwright, state)
            try:
                save = page.locator("#save-expiry-policy")
                expect(save).to_have_text("Save permission")
                expect(save).to_be_enabled()
                save.click()
                expect(page.locator("#expiry-policy-feedback")).to_have_text(
                    "Waiting for the worker to load the previous change. Try saving again shortly."
                )
                self.assertEqual(state["expiry_policy_requests"], [])
                self.assertEqual(
                    [request for request in state["requests"] if request[0] in {"/api/setup/pause", "/api/setup/expiry-policy"}],
                    [],
                )
            finally:
                browser.close()

    def test_live_mode_confirmation_cancel_and_pending_resume_guard(self):
        state = {
            "status": self.status_payload(
                configured=True,
                paused=True,
                discord={"state": "connected", "detail": "Discord is signed in."},
                trading={"mode": "shadow", "live_enabled": False, "worker_mode": "shadow", "pending": False},
            ),
            "headers": [],
            "requests": [],
            "mode_requests": [],
            "mode_worker_mode": "shadow",
            "mode_pending": True,
        }
        with sync_playwright() as playwright:
            browser, page = self.new_page(playwright, state)
            try:
                enable = page.get_by_role("button", name="Enable Live")
                expect(enable).to_be_enabled()
                enable.click()
                expect(page.locator("#live-mode-dialog")).to_be_visible()
                page.locator("#live-mode-cancel").click()
                expect(page.locator("#live-mode-dialog")).to_be_hidden()
                self.assertEqual(state["mode_requests"], [])

                enable.click()
                with page.expect_response(lambda response: response.url.endswith("/api/setup/mode")):
                    page.locator('#live-mode-dialog button[type="submit"]').click()
                self.assertEqual(state["mode_requests"], [{"mode": "live", "confirm_live": True}])
                expect(page.locator("#trading-mode-status")).to_have_text("Pending reload")
                expect(page.locator("#trading-mode-detail")).to_contain_text("Configured Live; worker Shadow.")
                expect(page.locator("#pause-relay")).to_be_disabled()
            finally:
                browser.close()

    def test_shadow_mode_posts_exact_payload(self):
        state = {
            "status": self.status_payload(
                configured=True,
                paused=True,
                discord={"state": "connected", "detail": "Discord is signed in."},
                trading={"mode": "live", "live_enabled": True, "worker_mode": "live", "pending": False},
            ),
            "headers": [],
            "requests": [],
            "mode_requests": [],
            "mode_worker_mode": "shadow",
        }
        with sync_playwright() as playwright:
            browser, page = self.new_page(playwright, state)
            try:
                with page.expect_response(lambda response: response.url.endswith("/api/setup/mode")):
                    page.get_by_role("button", name="Use Shadow").click()
                self.assertEqual(state["mode_requests"], [{"mode": "shadow"}])
                expect(page.locator("#trading-mode-status")).to_have_text("Paused")
                expect(page.locator("#trading-mode-detail")).to_contain_text("Configured Shadow; worker Shadow.")
            finally:
                browser.close()

    def test_live_confirmation_dialog_reset_prevents_escape_replay(self):
        state = {
            "status": self.status_payload(
                configured=True,
                paused=True,
                discord={"state": "connected", "detail": "Discord is signed in."},
                trading={"mode": "shadow", "live_enabled": False, "worker_mode": "shadow", "pending": False},
            ),
            "headers": [],
            "requests": [],
            "mode_requests": [],
        }
        with sync_playwright() as playwright:
            browser, page = self.new_page(playwright, state)
            try:
                page.get_by_role("button", name="Enable Live").click()
                with page.expect_response(lambda response: response.url.endswith("/api/setup/mode")):
                    page.locator('#live-mode-dialog button[type="submit"]').click()
                page.get_by_role("button", name="Use Shadow").click()
                expect(page.locator("#trading-mode-status")).to_have_text("Paused")
                self.assertEqual(
                    state["mode_requests"],
                    [{"mode": "live", "confirm_live": True}, {"mode": "shadow"}],
                )

                page.get_by_role("button", name="Enable Live").click()
                expect(page.locator("#live-mode-dialog")).to_be_visible()
                page.keyboard.press("Escape")
                expect(page.locator("#live-mode-dialog")).to_be_hidden()
                page.wait_for_timeout(200)
                self.assertEqual(
                    state["mode_requests"],
                    [{"mode": "live", "confirm_live": True}, {"mode": "shadow"}],
                )
            finally:
                browser.close()

    def test_pending_live_mode_can_roll_back_to_shadow_while_paused(self):
        state = {
            "status": self.status_payload(
                configured=True,
                paused=True,
                discord={"state": "connected", "detail": "Discord is signed in."},
                trading={"mode": "live", "live_enabled": True, "worker_mode": "shadow", "pending": True},
            ),
            "headers": [],
            "requests": [],
            "mode_requests": [],
            "mode_worker_mode": "live",
            "mode_pending": True,
        }
        with sync_playwright() as playwright:
            browser, page = self.new_page(playwright, state)
            try:
                expect(page.get_by_role("button", name="Use Shadow")).to_be_enabled()
                with page.expect_response(lambda response: response.url.endswith("/api/setup/mode")):
                    page.get_by_role("button", name="Use Shadow").click()
                self.assertEqual(state["mode_requests"], [{"mode": "shadow"}])
                expect(page.locator("#trading-mode-status")).to_have_text("Pending reload")
                expect(page.locator("#pause-relay")).to_be_disabled()
            finally:
                browser.close()

    def test_trading_mode_backend_errors_are_visible(self):
        state = {
            "status": self.status_payload(
                configured=True,
                paused=True,
                discord={"state": "connected", "detail": "Discord is signed in."},
                trading={"mode": "shadow", "live_enabled": False, "worker_mode": "shadow", "pending": False},
            ),
            "headers": [],
            "requests": [],
            "mode_requests": [],
            "mode_response": {"__error__": {"status": 409, "detail": "worker reload refused"}},
        }
        with sync_playwright() as playwright:
            browser, page = self.new_page(playwright, state)
            try:
                page.get_by_role("button", name="Enable Live").click()
                with page.expect_response(lambda response: response.url.endswith("/api/setup/mode")):
                    page.locator('#live-mode-dialog button[type="submit"]').click()
                expect(page.locator("#trading-mode-feedback")).to_contain_text("Could not update setup: worker reload refused")
                self.assertEqual(state["mode_requests"], [{"mode": "live", "confirm_live": True}])
            finally:
                browser.close()

    def test_initial_setup_poll_preserves_dirty_channel_draft(self):
        state = {
            "status": self.status_payload(),
            "headers": [],
            "requests": [],
            "discovery_requests": [],
            "hold_next_setup": True,
        }
        with sync_playwright() as playwright:
            browser, page = self.new_page(playwright, state)
            try:
                name = page.locator('[data-channel-index="0"] input[data-channel-field="name"]')
                name.fill("Draft before initial status")
                self.assertIn("held_setup", state)
                route, held_status = state.pop("held_setup")
                route.fulfill(status=200, content_type="application/json", body=json.dumps(held_status))
                page.wait_for_timeout(250)
                expect(name).to_have_value("Draft before initial status")
            finally:
                browser.close()


if __name__ == "__main__":
    unittest.main()
