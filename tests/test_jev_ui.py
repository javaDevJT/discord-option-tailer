"""Offline browser checks for the JEV setup and interpretation trace UI."""

from __future__ import annotations

import json
import tempfile
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
    def translate_path(self, path: str) -> str:
        route = urlparse(path).path
        if route in {"", "/"}:
            route = "/index.html"
        elif route.startswith("/static/"):
            route = route[len("/static") :]
        return str(STATIC_ROOT / route.lstrip("/"))

    def log_message(self, *_args):
        return


class JevUITests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if sync_playwright is None:
            raise unittest.SkipTest("Playwright required for JEV UI checks")

    def setUp(self):
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), StaticHandler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=3)

    @staticmethod
    def setup_status():
        return {
            "csrf_token": "synthetic-csrf",
            "configured": True,
            "paused": True,
            "poll_seconds": 30,
            "channels": [],
            "discord": {"state": "connected", "detail": "Synthetic Discord session."},
            "codex": {"state": "connected", "detail": "Synthetic Codex session."},
            "robinhood": {"state": "connected", "detail": "Synthetic account session."},
            "trading": {"mode": "shadow", "live_enabled": False, "worker_mode": "shadow", "pending": False},
            "risk": {"allow_same_day_expiry": False},
        "evaluation": {
            "model": "gpt-5.5",
            "reasoning_effort": "low",
            "service_tier": "standard",
            "max_chase_fraction": "0.05",
            "direct_entries": True,
            "jev": {
                "mode": "jev_shadow",
                "direct_entries": True,
                    "model": "jev-latest",
                    "timeout_ms": 1200,
                    "min_confidence": 0.95,
                    "min_probability": 0.95,
                    "min_eligibility": 0.98,
                    "credential_configured": True,
                    "state": "ready",
                    "detail": "Synthetic JEV evaluator ready.",
                },
            },
        }

    @staticmethod
    def artifact_path(name):
        path = ROOT / "artifacts" / "private" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        return path

    def route_api(self, page, state):
        def fulfill(route, payload, status=200):
            route.fulfill(status=status, content_type="application/json", body=json.dumps(payload))

        def handle(route, request):
            path = urlparse(request.url).path
            if path == "/api/status":
                fulfill(route, state.get("runtime_status", {"mode": "shadow", "live_orders_enabled": False, "counts": {}}))
                return
            if path in {"/api/messages", "/api/orders", "/api/positions", "/api/account"}:
                if path == "/api/messages":
                    fulfill(route, {"items": state["messages"], "next_offset": None})
                elif path == "/api/account":
                    fulfill(route, {"available": False, "status": "unavailable"})
                else:
                    fulfill(route, {"items": [], "next_offset": None})
                return
            if path == "/api/events":
                fulfill(route, {"items": state["events"], "next_offset": None})
                return
            if path == "/api/setup" and request.method == "GET":
                fulfill(route, state["setup"])
                return
            if path == "/api/setup/evaluation" and request.method == "POST":
                body = request.post_data_json or {}
                state["evaluation_requests"].append(body)
                state["setup"]["evaluation"].update(body)
                if isinstance(body.get("evaluation"), dict):
                    state["setup"]["evaluation"]["jev"] = body["evaluation"]
                    if "direct_entries" in body["evaluation"]:
                        state["setup"]["evaluation"]["direct_entries"] = body["evaluation"]["direct_entries"]
                    state["setup"]["evaluation"]["jev"]["credential_configured"] = "typesafe_api_key" in body or state["setup"]["evaluation"]["jev"].get("credential_configured") is True
                fulfill(route, state["setup"])
                return
            if path == "/api/setup/evaluation/test" and request.method == "POST":
                state["test_requests"].append(request.post_data_json or {})
                fulfill(route, {"state": "passed", "latency_ms": 23, "detail": "Synthetic classification accepted."})
                return
            if path == "/api/setup/pause" and request.method == "POST":
                state["setup"]["paused"] = bool((request.post_data_json or {}).get("paused"))
                fulfill(route, state["setup"])
                return
            fulfill(route, {"detail": f"Unhandled synthetic route: {path}"}, 404)

        page.route("**/api/**", handle)

    def new_page(self, playwright, state):
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page()
        self.route_api(page, state)
        page.goto(f"http://127.0.0.1:{self.server.server_port}/", wait_until="domcontentloaded")
        expect(page.locator("#evaluation-form")).to_be_visible()
        expect(page.locator("#evaluation-mode")).to_have_value("jev_shadow")
        return browser, page

    def test_jev_save_includes_nested_settings_and_preserves_write_only_key(self):
        state = {
            "setup": self.setup_status(),
            "messages": [],
            "events": [],
            "evaluation_requests": [],
            "test_requests": [],
        }
        with sync_playwright() as playwright:
            browser, page = self.new_page(playwright, state)
            try:
                page.locator("#evaluation-form").screenshot(path=str(self.artifact_path("jev-20260918-setup.png")))
                page.locator("#evaluation-mode").select_option("jev")
                page.locator("#evaluation-typesafe-key").fill("synthetic-typesafe-key")
                page.locator("#evaluation-timeout").fill("1200")
                page.locator("#evaluation-min-confidence").fill("96")
                page.locator("#evaluation-min-probability").fill("97")
                page.locator("#evaluation-min-eligibility").fill("99")
                page.locator("#save-evaluation").click()
                expect(page.locator("#evaluation-feedback")).to_contain_text("saved")
                self.assertEqual(len(state["evaluation_requests"]), 1)
                payload = state["evaluation_requests"][0]
                self.assertEqual(payload["model"], "gpt-5.5")
                self.assertEqual(payload["evaluation"]["mode"], "jev")
                self.assertEqual(payload["evaluation"]["timeout_ms"], 1200)
                self.assertEqual(payload["evaluation"]["min_confidence"], 0.96)
                self.assertEqual(payload["evaluation"]["min_probability"], 0.97)
                self.assertEqual(payload["evaluation"]["min_eligibility"], 0.99)
                self.assertEqual(payload["typesafe_api_key"], "synthetic-typesafe-key")
                expect(page.locator("#evaluation-typesafe-key")).to_have_value("")
                expect(page.locator("#evaluation-mode")).to_have_value("jev")
            finally:
                browser.close()

    def test_direct_entries_toggle_saves_persists_and_survives_status_refresh(self):
        state = {
            "setup": self.setup_status(),
            "messages": [],
            "events": [],
            "evaluation_requests": [],
            "test_requests": [],
        }
        with sync_playwright() as playwright:
            browser, page = self.new_page(playwright, state)
            try:
                direct = page.locator("#evaluation-direct-entries")
                expect(direct).to_be_checked()
                direct.uncheck()
                # The normal setup poll must not clobber an unsaved checkbox.
                page.wait_for_timeout(9300)
                expect(direct).not_to_be_checked()

                page.locator("#save-evaluation").click()
                expect(page.locator("#evaluation-feedback")).to_contain_text("saved")
                self.assertFalse(state["evaluation_requests"][0]["evaluation"]["direct_entries"])

                page.reload(wait_until="domcontentloaded")
                expect(page.locator("#evaluation-direct-entries")).not_to_be_checked()
            finally:
                browser.close()

    def test_render_metrics_from_real_dashboard_fixture(self):
        from relay.dashboard import create_server
        from tests.test_dashboard_ui import seed_dashboard

        with tempfile.TemporaryDirectory() as temporary:
            config_path = seed_dashboard(temporary, ROOT / "config.example.json")
            server = create_server(config_path, port=0)
            status = server.app.status()
            self.assertIn("evaluation_metrics", status)
            self.assertEqual(set(status["evaluation_metrics"]["buckets"]), {"live", "historical", "recovery"})
            self.assertEqual(status["evaluation_metrics"]["buckets"]["live"]["total"], 1)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                origin = f"http://127.0.0.1:{server.server_address[1]}"
                with sync_playwright() as playwright:
                    browser = playwright.chromium.launch(headless=True)
                    page = browser.new_page(viewport={"width": 1440, "height": 1100})
                    page.route("**/*", lambda route: route.continue_() if route.request.url.startswith(origin + "/") else route.abort())
                    page.goto(origin, wait_until="networkidle")
                    expect(page.locator("#evaluation-summary-window")).to_have_text("LIVE")
                    expect(page.locator("#evaluation-summary-count")).to_have_text("1")
                    expect(page.locator("#evaluation-summary-count-detail")).to_contain_text("1 evaluated")
                    expect(page.locator("#evaluation-summary-detail")).to_contain_text("Bounded live aggregate")
                    expect(page.locator("#evaluation-summary-interpretation")).to_have_text("— / — / —")
                    expect(page.locator("#evaluation-summary-broker")).to_have_text("— / —")
                    page.locator("#evaluation-summary").screenshot(path=str(self.artifact_path("jev-20260918-metrics.png")))
                    browser.close()
            finally:
                server.shutdown()
                server.server_close()

    def test_test_button_and_message_trace_separate_interpretation_from_broker_result(self):
        event = {
            "message_id": "message-jev-1",
            "state": "evaluated",
            "decision": {"action": "open", "contract": {"symbol": "SPY", "expiry": "2026-09-18", "strike": 500, "right": "call"}},
            "evaluation_timing": {
                "evaluator": "jev",
                "route": "fallback",
                "fallback_reason": "deadline",
                "model": "jev-latest",
                "jev_duration_seconds": 1.2,
                "codex_duration_seconds": 0.4,
                "semantic_confidence": 0.96,
                "allocation_confidence": 0.96,
                "selected_probability": 0.97,
                "native_confidence": 0.96,
                "eligibility_probability": 0.99,
                "received_at": "2026-09-18T12:00:00Z",
                "queue_wait_seconds": 0.02,
                "preparation_seconds": 0.04,
                "interpretation_ready_at": "2026-09-18T12:00:01Z",
                "interpretation_seconds": 1.6,
                "execution_seconds": 0.8,
                "submission_seconds": 0.2,
            },
        }
        state = {
            "setup": self.setup_status(),
            "messages": [{"id": "message-jev-1", "source_group": "synthetic", "channel_id": "1", "author": {"name": "Tester"}, "timestamp": "2026-09-18T12:00:00Z", "content": "SPY 500 call", "latest_event": event}],
            "events": [event],
            "evaluation_requests": [],
            "test_requests": [],
        }
        with sync_playwright() as playwright:
            browser, page = self.new_page(playwright, state)
            try:
                expect(page.locator("#message-list")).to_contain_text("Model interpretation")
                expect(page.locator("#message-list")).to_contain_text("Evaluator Jev")
                expect(page.locator("#message-list")).to_contain_text("Fallback Deadline")
                expect(page.locator("#message-list")).to_contain_text("Final broker result is recorded separately")
                page.locator("#test-evaluation").click()
                expect(page.locator("#evaluation-jev-feedback")).to_contain_text("JEV test Passed")
                self.assertEqual(state["test_requests"], [{}])
            finally:
                browser.close()

    def test_render_aggregate_evaluation_summary(self):
        state = {
            "setup": self.setup_status(),
            "messages": [],
            "events": [],
            "evaluation_requests": [],
            "test_requests": [],
            "runtime_status": {
                "mode": "shadow",
                "live_orders_enabled": False,
                "counts": {},
                "evaluation_metrics": {
                    "window": "recent",
                    "recent": {
                        "total": 24,
                        "evaluated": 18,
                        "eligible": 16,
                        "fast_path": 12,
                        "fallback": 3,
                        "failures": 1,
                        "timeouts": 2,
                        "timings": {
                            "interpretation_seconds": {"count": 18, "p50": 1.2, "p95": 1.9, "p99": 2.4},
                            "broker_ack_seconds": {"count": 8, "p50": 0.5, "p95": 0.9, "p99": 1.3},
                            "fill_seconds": {"count": 6, "p50": 1.1, "p95": 1.8, "p99": 2.7},
                        },
                    },
                },
            },
        }
        with sync_playwright() as playwright:
            browser, page = self.new_page(playwright, state)
            try:
                expect(page.locator("#evaluation-summary-count")).to_have_text("24")
                expect(page.locator("#evaluation-summary-count-detail")).to_contain_text("18 evaluated")
                expect(page.locator("#evaluation-summary-count-detail")).to_contain_text("16 eligible")
                expect(page.locator("#evaluation-summary-fast-path")).to_have_text("50%")
                expect(page.locator("#evaluation-summary-fast-path-detail")).to_contain_text("Eligible coverage 75%")
                expect(page.locator("#evaluation-summary-fallback")).to_have_text("3")
                expect(page.locator("#evaluation-summary-fallback-detail")).to_contain_text("Errors 1")
                expect(page.locator("#evaluation-summary-fallback-detail")).to_contain_text("timeouts 2")
                expect(page.locator("#evaluation-summary-interpretation")).to_have_text("1.2s / 1.9s / 2.4s")
                expect(page.locator("#evaluation-summary-broker")).to_have_text("500ms / 1.1s")
            finally:
                browser.close()


if __name__ == "__main__":
    unittest.main()
