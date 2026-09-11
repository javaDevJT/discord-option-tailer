"""Real-browser dashboard smoke check and explicit synthetic container fixture."""

import importlib.util
import json
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from relay.core import Store
from relay.dashboard import DashboardApp, create_server
from relay.ingest import normalize

ROOT = Path(__file__).resolve().parents[1]


def seed_dashboard(base, template):
    base = Path(base)
    config = json.loads(Path(template).read_text())
    config.update(mode="paper", database=str(base / "state/demo.sqlite3"),
                  kill_switch=str(base / "state/STOP"), runtime_status_file=str(base / "state/runtime-status.json"))
    config["robinhood"].update(account_number=None, enable_live_orders=False,
                                token_store=str(base / "state/robinhood-oauth.json"))
    config["browser"]["profile_dir"] = str(base / "state/discord-browser")
    for index, channel in enumerate(config["channels"]):
        channel["name"] = "Demo signals" if index == 0 else "Demo context"
    now = datetime.now(timezone.utc).isoformat()
    recovery_timestamp = (datetime.now(timezone.utc) - timedelta(hours=2, minutes=3)).isoformat()
    contract = {"symbol": "SPY", "expiry": "2026-09-18", "strike": "600", "option_type": "call"}
    store = Store(config["database"])
    try:
        store.bind_execution("paper", None)
        for index, (content, action, state, reason) in enumerate([
            ("DEMO: bought SPY 600 calls at 0.50", "OPEN", "paper_order", "Synthetic entry; one simulated contract."),
            ("DEMO: waiting for confirmation before the next entry", "WAIT", "wait", "Watch-only message; no entry instruction."),
            ("DEMO: <img src=x onerror=alert(1)> stays plain text", "IGNORE", "ignore", "Untrusted markup remains inert text."),
            ("DEMO: missed SPY 600 call during downtime", "OPEN", "recovery_review", "Recovery assessment only; no order submitted."),
            ("DEMO: missed signal queued for recovery", "OPEN", "recovery_pending", "Recovery assessment queued; no order submitted."),
            ("DEMO: missed signal is being evaluated", "OPEN", "recovery_evaluating", "Recovery assessment in progress; no order submitted."),
            ("DEMO: missed signal recovery failed", "OPEN", "recovery_error", "Recovery assessment failed; no order submitted."),
            ("DEMO: evaluation failed before a decision", None, "error", "evaluation failed: code=invalid_output; attempts=2; no order submitted"),
        ]):
            channel = config["channels"][index % 2]
            timestamp = recovery_timestamp if state.startswith("recovery_") else now
            message = normalize({"id": str(1545700000000000001 + index), "channel_id": channel["id"],
                                 "author": {"id": channel["authors"][0], "username": "Demo analyst"},
                                 "timestamp": timestamp, "content": content,
                                 "embeds": [
                                     {"description": "Comment\nStopped out\n@demo-trader"},
                                     {"title": "Structured embed", "text": "Additional text",
                                      "fields": [{"name": "Contract", "value": "SPY 600 call"}],
                                      "footer": {"text": "Synthetic footer"},
                                      "description": "<img src=x onerror=alert(1)>"},
                                 ] if index == 2 else []})
            message["source_group"] = channel["source_group"]
            decision = dict(action=action, contract=contract if action == "OPEN" else None, confidence=.9,
                            reason=reason, evidence=[{"message_id": message["id"], "quote": content}])
            if state == "recovery_review":
                decision["recovery"] = {
                    "status": "viable",
                    "confidence": .62,
                    "reason": "Current quote remains within the recorded recovery bounds.",
                    "evidence": [{"message_id": message["id"], "quote": content}],
                    "evaluated_at": now,
                    "original_timestamp": timestamp,
                    "signal_age_seconds": 7380,
                    "facts": {
                        "evaluated_at": now,
                        "original_timestamp": timestamp,
                        "signal_age_seconds": 7380,
                        "context_truncated": False,
                        "market_open": True,
                        "snapshot_timestamp": now,
                        "quote": {
                            "contract": contract, "bid": "0.78", "ask": "0.82", "timestamp": now,
                            "tradable": True, "multiplier": 100, "currency": "USD",
                            "asset_type": "equity_option", "tick_size": "0.01",
                        },
                        "equity": "10000", "buying_power": "2500", "affordable_quantity": 2,
                        "blockers": [], "context_changed": False,
                        "account_number": "999999999999999999", "provider_payload": {"secret": "omit"},
                    },
                    "account_number": "999999999999999999",
                }
            store.observe(message)
            store.record(message, state, reason, decision if action else None)
            if action == "OPEN" and not state.startswith("recovery_"):
                body = dict(client_order_id="demo-order", contract=contract, side="buy", quantity=1,
                            limit_price="0.50", position_effect="open", mode="paper",
                            sizing={"budget": "75", "risk_fraction": "0.075", "method": "confidence_allocation_cap"})
                store.reserve(message, decision, body, datetime.now(timezone.utc))
                with store.db:
                    store.db.execute("UPDATE orders SET status='filled',broker_id='paper-demo',filled_quantity=1,filled_notional='50' WHERE id='demo-order'")
                    store.db.execute("INSERT INTO positions VALUES (?,?,?,?)", (channel["source_group"], json.dumps(contract), 1, "0.50"))
    finally:
        store.close()
    config_path = base / "config.json"
    config_path.write_text(json.dumps(config))
    config_path.chmod(0o600)
    return config_path


@unittest.skipUnless(importlib.util.find_spec("playwright"), "Playwright is required for the rendered UI check")
class DashboardUITests(unittest.TestCase):
    def test_rendered_messages_orders_filtering_and_mobile_layout(self):
        from playwright.sync_api import sync_playwright
        with tempfile.TemporaryDirectory() as temporary:
            config = seed_dashboard(temporary, ROOT / "config.example.json")
            server = create_server(config, port=0)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                origin = f"http://127.0.0.1:{server.server_address[1]}"
                with sync_playwright() as playwright:
                    browser = playwright.chromium.launch(headless=True)
                    page = browser.new_page(viewport={"width": 1440, "height": 1100})
                    errors = []
                    page.on("pageerror", lambda error: errors.append(str(error)))
                    page.route("**/*", lambda route: route.continue_() if route.request.url.startswith(origin + "/") else route.abort())
                    page.goto(origin, wait_until="networkidle")
                    page.locator("#messages-shell .message-content").filter(has_text="DEMO: bought SPY 600 calls at 0.50").wait_for()
                    recovery_card = page.locator("#messages-shell .message-card").filter(has_text="DEMO: missed SPY 600 call during downtime")
                    recovery_card.wait_for()
                    recovery_text = recovery_card.inner_text().lower()
                    self.assertIn("recovery assessment", recovery_text)
                    self.assertIn("potentially viable", recovery_text)
                    self.assertIn("assessment only · no order submitted.", recovery_text)
                    self.assertIn("quote at assessment", recovery_text)
                    self.assertIn("market / open", recovery_text)
                    self.assertIn("recovery pending", page.locator("#messages-shell").inner_text().lower())
                    self.assertIn("recovery evaluating", page.locator("#messages-shell").inner_text().lower())
                    failed_card = page.locator("#messages-shell .message-card").filter(has_text="DEMO: evaluation failed before a decision")
                    self.assertEqual(failed_card.locator(".decision-action").text_content(), "Evaluation failed")
                    failed_event = page.locator("#events-shell .event-row").filter(has_text="code=invalid_output")
                    self.assertEqual(failed_event.locator(".event-action").text_content(), "Evaluation failed")
                    self.assertIn("SPY", page.locator("#orders-shell").inner_text())
                    self.assertIn("filled", page.locator("#orders-shell").inner_text().lower())
                    self.assertIn("SPY", page.locator("#positions-shell").inner_text())
                    self.assertEqual(page.locator("#messages-shell img").count(), 0)
                    embeds = page.locator("#messages-shell .message-embed")
                    self.assertEqual(embeds.count(), 2)
                    self.assertEqual(embeds.nth(0).inner_text(), "Comment\nStopped out\n@demo-trader")
                    self.assertEqual(embeds.nth(1).inner_text(),
                                     "Structured embed\n<img src=x onerror=alert(1)>\nAdditional text\nContract\nSPY 600 call\nSynthetic footer")
                    page.locator("#message-search").fill("waiting for confirmation")
                    page.get_by_role("button", name="Apply filters", exact=True).click()
                    page.wait_for_function("() => document.querySelector('#messages-shell').textContent.includes('waiting for confirmation') && !document.querySelector('#messages-shell').textContent.includes('bought SPY')")
                    page.get_by_role("button", name="Clear", exact=True).click()
                    page.locator("#messages-shell .message-content").filter(has_text="DEMO: bought SPY 600 calls at 0.50").wait_for()
                    page.locator("#message-state").select_option("recovery_review")
                    page.get_by_role("button", name="Apply filters", exact=True).click()
                    page.wait_for_function("() => document.querySelectorAll('#messages-shell .message-card').length === 1 && document.querySelector('#messages-shell').textContent.includes('missed SPY')")
                    self.assertNotIn("bought SPY", page.locator("#messages-shell").inner_text())
                    page.get_by_role("button", name="Clear", exact=True).click()
                    page.set_viewport_size({"width": 390, "height": 844})
                    self.assertTrue(page.evaluate("document.documentElement.scrollWidth <= innerWidth"))
                    self.assertEqual(errors, [])
                    browser.close()
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=3)

    def test_notifications_form_keeps_webhook_secret_and_omits_blank_updates(self):
        from playwright.sync_api import sync_playwright
        with tempfile.TemporaryDirectory() as temporary:
            config = seed_dashboard(temporary, ROOT / "config.example.json")
            server = create_server(config, port=0)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            posts = []
            try:
                origin = f"http://127.0.0.1:{server.server_address[1]}"
                setup_status = {
                    "csrf_token": "fixture-csrf",
                    "configured": False,
                    "paused": True,
                    "mode": "paper",
                    "channels": [],
                    "poll_seconds": 30,
                    "discord": {"state": "connected", "discovery": {"state": "ready", "guilds": [], "channels": [], "authors": []}},
                    "codex": {"state": "connected"},
                    "robinhood": {"state": "not_connected"},
                    "trading": {"mode": "paper", "worker_mode": "paper", "pending": False},
                    "notifications": {"enabled": False, "configured": False, "state": "not_configured", "detail": "No output webhook is configured."},
                }

                def handle_route(route):
                    request = route.request
                    path = request.url.split(origin, 1)[-1]
                    if path == "/api/setup":
                        route.fulfill(status=200, content_type="application/json", body=json.dumps(setup_status))
                    elif path == "/api/setup/notifications" and request.method == "POST":
                        posts.append(json.loads(request.post_data or "{}"))
                        configured = setup_status["notifications"]["configured"]
                        if "webhook_url" in posts[-1]:
                            configured = bool(posts[-1]["webhook_url"])
                        setup_status["notifications"] = {
                            "enabled": posts[-1].get("enabled") is True,
                            "configured": configured,
                            "state": "configured",
                            "detail": "Webhook saved; the URL remains hidden.",
                        }
                        route.fulfill(status=200, content_type="application/json", body=json.dumps(setup_status))
                    elif request.url.startswith(origin + "/"):
                        route.continue_()
                    else:
                        route.abort()

                with sync_playwright() as playwright:
                    browser = playwright.chromium.launch(headless=True)
                    page = browser.new_page(viewport={"width": 1440, "height": 1100})
                    page.route("**/*", handle_route)
                    page.goto(origin, wait_until="networkidle")
                    page.locator("#notifications-status").wait_for()
                    self.assertEqual(page.locator("#notifications-webhook").get_attribute("value"), None)
                    self.assertEqual(page.locator("#notifications-webhook").get_attribute("type"), "password")
                    page.locator("#notifications-enabled").check()
                    page.locator("#notifications-webhook").fill("https://discord.com/api/webhooks/test/secret")
                    page.locator("#save-notifications").click()
                    page.wait_for_function("() => document.querySelector('#notifications-feedback').textContent.includes('saved')")
                    self.assertEqual(posts[0], {"enabled": True, "webhook_url": "https://discord.com/api/webhooks/test/secret"})
                    self.assertEqual(page.locator("#notifications-webhook").input_value(), "")
                    page.locator("#save-notifications").click()
                    page.wait_for_function("() => document.querySelector('#notifications-feedback').textContent.includes('saved')")
                    self.assertEqual(posts[1], {"enabled": True})
                    page.locator("#clear-notifications").click()
                    page.wait_for_function("() => document.querySelector('#notifications-feedback').textContent.includes('removed')")
                    self.assertEqual(posts[2], {"enabled": False, "webhook_url": ""})
                    browser.close()
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=3)


class RecoveryProjectionTests(unittest.TestCase):
    def test_recovery_projection_is_bounded_and_filterable(self):
        with tempfile.TemporaryDirectory() as temporary:
            config = seed_dashboard(temporary, ROOT / "config.example.json")
            app = DashboardApp(config)
            payload = app.messages("")
            item = next(item for item in payload["items"] if "missed SPY 600 call" in item["content"])
            decision = item["latest_event"]["decision"]
            self.assertEqual(decision["action"], "OPEN")
            self.assertEqual(decision["recovery"]["status"], "viable")
            self.assertEqual(decision["recovery"]["facts"]["quote"]["ask"], "0.82")
            self.assertEqual(decision["recovery"]["facts"]["market_open"], True)
            serialized = json.dumps(item)
            self.assertNotIn("account_number", serialized)
            self.assertNotIn("provider_payload", serialized)

            filtered = app.messages("state=recovery_review")
            self.assertEqual(len(filtered["items"]), 1)
            self.assertEqual(filtered["items"][0]["latest_event"]["state"], "recovery_review")


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == "--seed":
        seed_dashboard(sys.argv[2], sys.argv[3])
    else:
        unittest.main()
