"""Persistent monitor API projection and rendered dashboard coverage."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import importlib.util
import json
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest
from urllib.request import urlopen

from relay.dashboard import create_server
from tests.test_dashboard_ui import seed_dashboard

ROOT = Path(__file__).resolve().parents[1]


def _timestamp(delta=timedelta(0)):
    return (datetime.now(timezone.utc) + delta).isoformat()


class MonitorUITests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="monitor-ui-")
        self.config_path = seed_dashboard(Path(self.temp.name), ROOT / "config.example.json")
        self.config = json.loads(Path(self.config_path).read_text(encoding="utf-8"))
        self.server = create_server(self.config_path, port=0)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.url = f"http://127.0.0.1:{self.server.server_port}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=3)
        self.temp.cleanup()

    def insert_monitor(self, identity, state="active", body=None):
        deadline = _timestamp(timedelta(hours=1))
        record = (
            identity,
            "demo",
            json.dumps({"symbol": "SPY", "expiry": "2026-10-16", "strike": "600", "option_type": "call", "account_number": "private-account"}),
            "demo-order",
            state,
            _timestamp(),
            deadline,
            _timestamp(timedelta(minutes=1)),
            json.dumps(body or {}),
        )
        with sqlite3.connect(self.config["database"]) as connection:
            connection.execute("INSERT INTO position_monitors VALUES (?,?,?,?,?,?,?,?,?)", record)
        return deadline

    def get_monitors(self):
        with urlopen(f"{self.url}/api/monitors", timeout=3) as response:
            self.assertEqual(response.status, 200)
            return json.loads(response.read())

    def test_api_projection_drops_private_and_malformed_monitor_fields(self):
        deadline = self.insert_monitor(
            "safe-monitor",
            body={
                "source_message_id": "1545700000000000001",
                "source_revision": "rev-1",
                "source_market_date": datetime.now(timezone.utc).date().isoformat(),
                "plan": {
                    "duration_seconds": 3600,
                    "poll_interval_seconds": 60,
                    "reassess_after_seconds": 300,
                    "conditions": [{"metric": "option_bid", "comparison": "lte", "threshold": "0.25", "api_key": "plan-secret"}],
                    "credential": "plan-private",
                },
                "generation": 2,
                "observation": {
                    "observed_at": _timestamp(),
                    "source_market_date": datetime.now(timezone.utc).date().isoformat(),
                    "option_bid": "0.40",
                    "underlying_price": "599.25",
                    "triggered_conditions": [0],
                    "trigger_reason": "condition",
                    "access_token": "observation-secret",
                    "raw_provider_response": "provider-private-text",
                },
                "triggered_at": _timestamp(),
                "evaluated_at": _timestamp(),
                "result": "No position change",
                "decision": {"action": "WAIT", "reason": "Conditions remain unresolved", "private": "decision-secret"},
                "diagnostic": {"status": "error", "api_key": "diagnostic-secret", "raw_provider_text": "provider-secret-text"},
                "raw_account_id": "private-account",
            },
        )
        self.insert_monitor("retry-monitor", state="error", body={"plan": {"duration_seconds": True, "conditions": []}})

        payload = self.get_monitors()

        self.assertEqual(payload["active_count"], 2)
        self.assertEqual(len(payload["monitors"]), 2)
        projected = next(item for item in payload["monitors"] if item["id"] == "safe-monitor")
        self.assertEqual(projected["state"], "active")
        self.assertEqual(projected["expires_at"], deadline)
        self.assertEqual(projected["plan"]["poll_interval_seconds"], 60)
        self.assertNotIn("account_number", projected["contract"])
        self.assertIsNone(next(item for item in payload["monitors"] if item["id"] == "retry-monitor")["plan"])
        encoded = json.dumps(payload)
        for private in ("private-account", "plan-secret", "plan-private", "observation-secret", "provider-private-text", "decision-secret", "diagnostic-secret", "provider-secret-text"):
            self.assertNotIn(private, encoded)

    def test_old_database_without_monitor_table_returns_empty_projection(self):
        with sqlite3.connect(self.config["database"]) as connection:
            connection.execute("DROP TABLE position_monitors")
        self.assertEqual(self.get_monitors(), {"monitors": [], "active_count": 0})

    @unittest.skipUnless(importlib.util.find_spec("playwright"), "Playwright required monitor UI check")
    def test_browser_renders_monitor_state_deadline_and_source(self):
        deadline = self.insert_monitor(
            "visible-monitor",
            body={
                "source_message_id": "1545700000000000001",
                "source_revision": "rev-1",
                "source_market_date": datetime.now(timezone.utc).date().isoformat(),
                "plan": {"duration_seconds": 3600, "poll_interval_seconds": 60, "reassess_after_seconds": 300, "conditions": []},
                "reason": "Reassess the held position periodically",
                "observation": {"observed_at": _timestamp(), "option_bid": "0.40", "trigger_reason": "timer"},
                "result": "No action required",
            },
        )
        from playwright.sync_api import expect, sync_playwright

        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            page = browser.new_page()
            page.goto(self.url, wait_until="networkidle")
            expect(page.locator("#monitor-active-count")).to_have_text("1 pending reassessments")
            expect(page.locator("#monitor-list")).to_contain_text("Active")
            expect(page.locator("#monitor-list")).to_contain_text("Deadline")
            expect(page.locator("#monitor-list")).to_contain_text("Source message 1545700000000000001")
            expect(page.locator("#monitor-list")).to_contain_text("Poll interval 1m")
            expect(page.locator("#monitor-boundary")).to_contain_text("do not place a protective broker stop")
            deadline_node = page.locator("#monitor-list time").filter(has_text="Deadline")
            expect(deadline_node).to_be_visible()
            self.assertEqual(deadline_node.get_attribute("datetime"), deadline)
            browser.close()


if __name__ == "__main__":
    unittest.main()
