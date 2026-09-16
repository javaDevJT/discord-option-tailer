"""Offline browser checks for the Discord gateway setup controls."""

from __future__ import annotations

import copy
import importlib.util
import json
import threading
import unittest
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

try:
    from playwright.sync_api import expect, sync_playwright
except ImportError:  # pragma: no cover - depends on the optional browser extra
    expect = None
    sync_playwright = None


ROOT = Path(__file__).resolve().parents[1]
STATIC_ROOT = ROOT / "relay" / "static"


class StaticHandler(SimpleHTTPRequestHandler):
    """Serve the setup page and its static assets without a live relay."""

    def translate_path(self, path: str) -> str:
        route = urlparse(path).path
        if route in {"", "/"}:
            route = "/index.html"
        if route.startswith("/static/"):
            route = route[len("/static") :]
        return str(STATIC_ROOT / route.lstrip("/"))

    def log_message(self, *_args) -> None:
        return


@unittest.skipUnless(importlib.util.find_spec("playwright"), "Playwright is not installed")
class GatewaySetupUITests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        if sync_playwright is None:
            raise unittest.SkipTest("Playwright is not installed")
        try:
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch(headless=True)
                browser.close()
        except Exception as exc:  # pragma: no cover - host-specific browser setup
            raise unittest.SkipTest(f"Playwright browser unavailable: {exc}") from exc

    def setUp(self) -> None:
        try:
            self.server = ThreadingHTTPServer(("127.0.0.1", 0), StaticHandler)
        except OSError as exc:  # pragma: no cover - restricted CI environments
            raise unittest.SkipTest(f"local test server unavailable: {exc}") from exc
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=3)

    @staticmethod
    def status_payload() -> dict:
        return {
            "csrf_token": "synthetic-csrf",
            "configured": False,
            "paused": True,
            "poll_seconds": 2,
            "discord": {
                "state": "not_connected",
                "transport": "browser",
                "credential_configured": False,
                "browser_fallback": False,
                "detail": "Choose a Discord connection.",
            },
            "codex": {"state": "not_connected", "detail": "Waiting."},
            "robinhood": {"state": "not_connected", "detail": "Waiting."},
            "channels": [],
            "trading": {
                "mode": "shadow",
                "worker_mode": "shadow",
                "live_enabled": False,
                "pending": False,
            },
            "risk": {
                "max_chase_fraction": 0.15,
                "max_signal_age_seconds": 3600,
                "allow_same_day_expiry": False,
            },
            "evaluation": {
                "enabled": False,
                "interval_seconds": 30,
                "lookback_days": 5,
            },
        }

    @staticmethod
    def fulfill(route, payload, status: int = 200) -> None:
        route.fulfill(
            status=status,
            content_type="application/json",
            body=json.dumps(payload),
        )

    def route_api(self, page, state: dict) -> None:
        def handle(route, request) -> None:
            path = urlparse(request.url).path
            if path == "/api/status":
                self.fulfill(route, {"mode": "shadow", "live_orders_enabled": False, "counts": {}})
                return
            if path in {"/api/messages", "/api/orders", "/api/events", "/api/positions"}:
                self.fulfill(route, {"items": [], "next_offset": None})
                return
            if path == "/api/setup" and request.method == "GET":
                self.fulfill(route, state["status"])
                return
            if path == "/api/setup/discord" and request.method == "POST":
                body = request.post_data_json or {}
                state["requests"].append((path, copy.deepcopy(body)))
                discord = state["status"]["discord"]
                discord["transport"] = body.get("transport", "browser")
                if body.get("token"):
                    discord["credential_configured"] = True
                self.fulfill(route, state["status"])
                return
            if path.startswith("/api/setup/") and request.method == "POST":
                self.fulfill(route, state["status"])
                return
            route.continue_()

        page.route("**/*", handle)

    def new_page(self, playwright, state: dict):
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 1280, "height": 1000})
        self.route_api(page, state)
        page.goto(
            f"http://127.0.0.1:{self.server.server_address[1]}/",
            wait_until="domcontentloaded",
        )
        expect(page.locator("#discord-transport")).to_be_visible()
        return browser, page

    def test_gateway_selection_save_and_draft_preservation(self) -> None:
        state = {"status": self.status_payload(), "requests": []}
        console_errors: list[str] = []
        page_errors: list[str] = []

        with sync_playwright() as playwright:
            browser, page = self.new_page(playwright, state)
            page.on("console", lambda message: console_errors.append(message.text) if message.type == "error" else None)
            page.on("pageerror", lambda error: page_errors.append(str(error)))
            try:
                gateway_option = page.locator('#discord-transport option[value="gateway"]')
                expect(gateway_option).to_have_text("Gateway (discord.py-self)")

                browser_link = page.locator('.setup-browser-link[data-provider="discord"]')
                browser_frame = page.locator("#browser-login-frame")
                expect(browser_link).to_be_visible()
                self.assertTrue((browser_frame.get_attribute("src") or "").startswith("/browser/vnc.html"))

                page.locator("#discord-transport").select_option("gateway")
                page.locator("#discord-token").fill("synthetic-gateway-token")
                expect(page.locator("#discord-gateway-fields")).to_be_visible()
                expect(browser_link).to_be_hidden()
                self.assertIsNone(browser_frame.get_attribute("src"))

                # The existing setup poll must preserve the in-progress gateway draft.
                page.wait_for_timeout(2400)
                expect(page.locator("#discord-transport")).to_have_value("gateway")
                expect(page.locator("#discord-token")).to_have_value("synthetic-gateway-token")

                with page.expect_response(
                    lambda response: urlparse(response.url).path == "/api/setup/discord"
                    and response.request.method == "POST"
                ) as response_info:
                    page.locator("#save-discord").click()
                self.assertEqual(response_info.value.status, 200)
                expect(page.locator("#discord-setup-feedback")).to_have_text(
                    "Discord setup saved. Reconnect requested."
                )
                expect(page.locator("#discord-token")).to_have_value("")
                self.assertEqual(
                    state["requests"][0][1],
                    {"transport": "gateway", "token": "synthetic-gateway-token"},
                )
                expect(browser_link).to_be_hidden()
                self.assertIsNone(browser_frame.get_attribute("src"))

                # An empty token must omit the field and preserve the saved credential.
                with page.expect_response(
                    lambda response: urlparse(response.url).path == "/api/setup/discord"
                    and response.request.method == "POST"
                ) as response_info:
                    page.locator("#save-discord").click()
                self.assertEqual(response_info.value.status, 200)
                self.assertEqual(state["requests"][1][1], {"transport": "gateway"})
                self.assertTrue(state["status"]["discord"]["credential_configured"])
                expect(page.locator("#discord-token")).to_have_value("")
            finally:
                browser.close()

        self.assertEqual(console_errors, [])
        self.assertEqual(page_errors, [])


if __name__ == "__main__":
    unittest.main()
