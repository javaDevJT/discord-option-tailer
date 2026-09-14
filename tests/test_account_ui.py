"""Synthetic browser checks for the cached Robinhood account panel."""

from __future__ import annotations

import json
import threading
import unittest
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

try:
    from playwright.sync_api import expect, sync_playwright
except ImportError:  # pragma: no cover - local browser dependency
    expect = None
    sync_playwright = None


ROOT = Path(__file__).resolve().parents[1]
STATIC_ROOT = ROOT / "relay" / "static"


class StaticHandler(SimpleHTTPRequestHandler):
    def translate_path(self, path: str) -> str:
        route = urlparse(path).path
        if route in {"", "/"}:
            route = "/index.html"
        if route.startswith("/static/"):
            route = route[len("/static"):]
        return str(STATIC_ROOT / route.lstrip("/"))

    def log_message(self, *_args):
        return


@unittest.skipUnless(sync_playwright, "Playwright is required for rendered UI checks")
class AccountUITests(unittest.TestCase):
    def setUp(self):
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), StaticHandler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=3)

    @staticmethod
    def setup_payload():
        return {
            "csrf_token": "synthetic-csrf",
            "configured": False,
            "paused": True,
            "poll_seconds": 3,
            "channels": [],
            "trading": {"mode": "shadow", "worker_mode": "shadow", "pending": False},
            "risk": {"allow_same_day_expiry": False},
        }

    @staticmethod
    def account_payload(**overrides):
        payload = {
            "available": True,
            "status": "ready",
            "updated_at": "2026-09-11T15:00:00Z",
            "last_attempt_at": "2026-09-11T15:00:03Z",
            "error": None,
            "stale": False,
            "currency": "USD",
            "equity": "12345.67",
            "cash": "7000.00",
            "buying_power": "8500.00",
            "unleveraged_buying_power": "7200.00",
            "asset_values": {
                "equity_value": "4000.00",
                "options_value": "8345.67",
                "futures_value": None,
                "event_contracts_value": None,
                "crypto_value": None,
                "mutual_funds_value": None,
                "fixed_income_value": None,
            },
            "positions": [{
                "contract": {"symbol": "AAPL", "expiry": "2026-09-18", "strike": "225", "option_type": "CALL"},
                "quantity": "-2",
                "average_price": "1.25",
                "market_value": "250.00",
                "position_type": "short",
                "multiplier": "100",
                "quote_timestamp": "2026-09-11T14:59:00Z",
            }],
            "scope": "option_positions",
        }
        payload.update(overrides)
        return payload

    def route_api(self, page, state):
        def fulfill(route, payload, status=200):
            route.fulfill(status=status, content_type="application/json", body=json.dumps(payload))

        def handle(route, request):
            path = urlparse(request.url).path
            if path == "/api/status":
                fulfill(route, {"runtime": {"state": "running", "updated_at": "2026-09-11T15:00:00Z"}, "counts": {}})
                return
            if path in {"/api/messages", "/api/orders", "/api/events", "/api/positions"}:
                fulfill(route, {"items": [], "next_offset": None})
                return
            if path == "/api/setup" and request.method == "GET":
                fulfill(route, state["setup"])
                return
            if path == "/api/account" and request.method == "GET":
                state["account_requests"] += 1
                payloads = state["account_payloads"]
                payload = payloads.pop(0) if len(payloads) > 1 else payloads[0]
                fulfill(route, payload)
                return
            route.continue_()

        page.route("**/*", handle)

    def new_page(self, playwright, state, timeout_override_ms=None, hang_account_fetch=False):
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 1440, "height": 1100})
        if timeout_override_ms is not None:
            page.add_init_script(f"""
                const nativeAbortSignalTimeout = AbortSignal.timeout.bind(AbortSignal);
                AbortSignal.timeout = () => nativeAbortSignalTimeout({timeout_override_ms});
            """)
        if hang_account_fetch:
            page.add_init_script("""
                const nativeRelayFetch = window.fetch.bind(window);
                let relayHangAccountOnce = true;
                window.fetch = (input, init = {}) => {
                    const url = new URL(typeof input === "string" ? input : input.url, window.location.href);
                    if (relayHangAccountOnce && url.pathname === "/api/account") {
                        relayHangAccountOnce = false;
                        return new Promise((resolve, reject) => {
                            const signal = init?.signal;
                            const abort = () => reject(new DOMException("Aborted", "AbortError"));
                            if (signal?.aborted) abort();
                            else signal?.addEventListener("abort", abort, { once: true });
                        });
                    }
                    return nativeRelayFetch(input, init);
                };
            """)
        self.route_api(page, state)
        page.goto(f"http://127.0.0.1:{self.server.server_address[1]}/", wait_until="domcontentloaded")
        expect(page.get_by_role("heading", name="Robinhood account")).to_be_visible()
        return browser, page

    def test_account_panel_renders_cached_values_and_option_metadata(self):
        state = {
            "setup": self.setup_payload(),
            "account_payloads": [self.account_payload()],
            "account_requests": 0,
        }
        with sync_playwright() as playwright:
            browser, page = self.new_page(playwright, state)
            try:
                expect(page.locator("#account-status")).to_have_text("Ready")
                expect(page.locator("#account-equity")).to_have_text("$12,345.67")
                expect(page.locator("#account-cash")).to_have_text("$7,000.00")
                expect(page.locator("#account-buying-power")).to_have_text("$8,500.00")
                expect(page.locator("#account-unleveraged-buying-power")).to_have_text("$7,200.00")
                expect(page.locator("#account-assets")).to_be_visible()
                expect(page.locator("#account-asset-values")).to_contain_text("Equities")
                expect(page.locator("#account-position-list")).to_contain_text("AAPL")
                expect(page.locator("#account-position-list")).to_contain_text("Short")
                expect(page.locator("#account-position-list")).to_contain_text("Multiplier ×100")
                expect(page.locator("#account-position-list")).to_contain_text("average price / unit")
                expect(page.locator("#account-position-list")).to_contain_text("Quote")
                page.locator("#account").screenshot(path=str(ROOT / "artifacts" / "private" / "account-dashboard-preview.png"))
                self.assertGreaterEqual(state["account_requests"], 1)
            finally:
                browser.close()

    def test_account_refresh_retains_cached_values_when_broker_refresh_errors(self):
        ready = self.account_payload()
        failed = self.account_payload(
            status="error",
            error="Robinhood refresh failed; showing the last cached snapshot.",
            stale=True,
        )
        state = {
            "setup": self.setup_payload(),
            "account_payloads": [ready, failed],
            "account_requests": 0,
        }
        with sync_playwright() as playwright:
            browser, page = self.new_page(playwright, state)
            try:
                with page.expect_response(lambda response: response.url.endswith("/api/account")):
                    page.locator("#refresh-button").click()
                expect(page.locator("#account-status")).to_have_text("Error")
                expect(page.locator("#account-status-detail")).to_contain_text("last cached snapshot")
                expect(page.locator("#account-equity")).to_have_text("$12,345.67")
                expect(page.locator("#account-position-list")).to_contain_text("AAPL")
                expect(page.locator('[data-error="account"]')).to_be_visible()
                expect(page.locator('[data-error="account"]')).to_contain_text("last cached snapshot")
            finally:
                browser.close()

    def test_hung_read_times_out_and_manual_refresh_recovers(self):
        state = {
            "setup": self.setup_payload(),
            "account_payloads": [self.account_payload()],
            "account_requests": 0,
        }
        with sync_playwright() as playwright:
            browser, page = self.new_page(playwright, state, timeout_override_ms=250, hang_account_fetch=True)
            try:
                expect(page.locator("#account-status")).to_have_text("Error", timeout=3000)
                page.wait_for_timeout(100)
                with page.expect_response(lambda response: response.url.endswith("/api/account")):
                    page.locator("#refresh-button").click()
                expect(page.locator("#account-status")).to_have_text("Ready")
                expect(page.locator("#account-equity")).to_have_text("$12,345.67")
                self.assertEqual(state["account_requests"], 1)
            finally:
                browser.close()


if __name__ == "__main__":
    unittest.main()
