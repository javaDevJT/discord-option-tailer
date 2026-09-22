from __future__ import annotations

from datetime import datetime, timezone, timedelta
import http.client
import json
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest

from relay.dashboard import DashboardApp, DashboardHTTPServer


UTC = timezone.utc


class DashboardTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="dashboard-test-")
        self.root = Path(self.temp.name)
        self.state = self.root / "state"
        self.state.mkdir()
        self.database = self.state / "relay.sqlite3"
        self._create_fixture()
        self.config_path = self.root / "config.json"
        self.config_path.write_text(json.dumps({
            "mode": "shadow",
            "database": "state/relay.sqlite3",
            "kill_switch": "state/STOP",
            "runtime_status_file": "state/runtime-status.json",
            "channels": [{
                "id": "1000000000000000001", "name": "signals", "role": "signals", "source_group": "source-a",
            }],
            "robinhood": {"account_number": "123456789", "enable_live_orders": False},
        }), encoding="utf-8")
        self.status_path = self.state / "runtime-status.json"
        self.status_path.write_text(json.dumps({
            "updated_at": datetime.now(UTC).isoformat(),
            "state": "running",
            "detail": "Robinhood authorization in progress",
            "discord": {"state": "ready", "channels": [{
                "id": "1000000000000000001", "state": "ready",
                "updated_at": datetime.now(UTC).isoformat(),
                "last_seen_at": datetime.now(UTC).isoformat(),
            }]},
            "codex": {"state": "ready"},
            "broker": {"state": "disabled"},
            "account_number": "999999999999999999",
        }), encoding="utf-8")
        self.app = DashboardApp(self.config_path)
        self.server = DashboardHTTPServer(("127.0.0.1", 0), self.app)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.temp.cleanup()

    def _create_fixture(self):
        connection = sqlite3.connect(self.database)
        connection.executescript("""
            CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE messages (
                id TEXT PRIMARY KEY, channel_id TEXT NOT NULL, source_group TEXT NOT NULL,
                timestamp TEXT NOT NULL, revision TEXT NOT NULL, body TEXT NOT NULL
            );
            CREATE TABLE events (
                id INTEGER PRIMARY KEY, message_id TEXT NOT NULL, revision TEXT NOT NULL,
                state TEXT NOT NULL, reason TEXT NOT NULL, decision TEXT, created_at TEXT NOT NULL,
                UNIQUE(message_id, revision)
            );
            CREATE TABLE orders (
                id TEXT PRIMARY KEY, message_id TEXT NOT NULL, source_group TEXT NOT NULL,
                contract TEXT NOT NULL, action TEXT NOT NULL, body TEXT NOT NULL,
                status TEXT NOT NULL, broker_id TEXT, created_at TEXT NOT NULL,
                filled_quantity INTEGER NOT NULL DEFAULT 0, filled_notional TEXT NOT NULL DEFAULT '0'
            );
            CREATE TABLE positions (
                source_group TEXT NOT NULL, contract TEXT NOT NULL, quantity INTEGER NOT NULL,
                average_price TEXT NOT NULL, PRIMARY KEY(source_group, contract)
            );
        """)
        now = datetime.now(UTC)
        contract = {"symbol": "SPY", "expiry": "2026-09-18", "strike": "600", "option_type": "call"}
        decision = {
            "action": "OPEN", "origin_message_id": "1545700000000000001", "contract": contract,
            "quantity": 1, "fraction": None, "alert_price": "0.80", "stop_price": None,
            "confidence": 0.99, "ambiguous": False, "reason": "explicit entry",
            "evidence": [{"message_id": "1545700000000000001", "quote": "BUY SPY 600 call"}],
            "account_number": "999999999999999999",
        }
        messages = [
            ("1545700000000000001", now - timedelta(seconds=3), "entry one", "alice"),
            ("1545700000000000002", now - timedelta(seconds=2), "entry two", "bob"),
            ("1545700000000000003", now - timedelta(seconds=1), "watch only", "carol"),
        ]
        for message_id, timestamp, content, author_name in messages:
            body = {
                "id": message_id, "channel_id": "1000000000000000001", "source_group": "source-a",
                "author_id": "2000000000000000001", "author_name": author_name, "content": content,
                "embeds": [{"title": "ENTRY", "description": content, "url": "https://secret.invalid/token"}],
            }
            connection.execute("INSERT INTO messages VALUES (?,?,?,?,?,?)", (
                message_id, body["channel_id"], body["source_group"], timestamp.isoformat(), "rev-" + message_id[-1], json.dumps(body),
            ))
        connection.execute("INSERT INTO events VALUES (?,?,?,?,?,?,?)", (
            1, "1545700000000000001", "rev-1", "held", "fixture hold", json.dumps(decision), now.isoformat(),
        ))
        connection.execute("INSERT INTO events VALUES (?,?,?,?,?,?,?)", (
            2, "1545700000000000002", "rev-2", "paper_order", "filled", json.dumps(decision), now.isoformat(),
        ))
        order_body = {
            "client_order_id": "client-1", "contract": contract, "side": "buy", "quantity": 1,
            "limit_price": "0.80", "position_effect": "open", "quote_timestamp": now.isoformat(),
            "account_timestamp": now.isoformat(), "sizing": {"method": "confidence_allocation_cap", "budget": "100"},
            "account_number": "999999999999999999",
        }
        connection.execute("INSERT INTO orders VALUES (?,?,?,?,?,?,?,?,?,?,?)", (
            "client-1", "1545700000000000002", "source-a", json.dumps(contract, sort_keys=True), "OPEN",
            json.dumps(order_body), "filled", "broker-1", now.isoformat(), 1, "0.80",
        ))
        connection.execute("INSERT INTO orders VALUES (?,?,?,?,?,?,?,?,?,?,?)", (
            "client-2", "1545700000000000001", "source-a", json.dumps(contract, sort_keys=True), "OPEN",
            json.dumps(order_body | {"client_order_id": "client-2"}), "pending", None, now.isoformat(), 0, "0",
        ))
        connection.execute("INSERT INTO positions VALUES (?,?,?,?)", ("source-a", json.dumps(contract, sort_keys=True), 1, "0.80"))
        connection.commit()
        connection.close()

    def request(self, path, method="GET"):
        client = http.client.HTTPConnection("127.0.0.1", self.server.server_address[1], timeout=3)
        client.request(method, path)
        response = client.getresponse()
        body = response.read()
        headers = {key.lower(): value for key, value in response.getheaders()}
        client.close()
        try:
            payload = json.loads(body)
        except json.JSONDecodeError:
            payload = body.decode("utf-8")
        return response.status, headers, payload

    def test_account_endpoint_is_cached_read_only_and_rejects_refresh_parameters(self):
        status, headers, account = self.request("/api/account")
        self.assertEqual(status, 200)
        self.assertEqual(headers["cache-control"], "no-store")
        self.assertFalse(account["available"])
        self.assertIsNone(account["equity"])
        self.assertEqual(self.request("/api/account?refresh=true")[0], 400)
        self.assertEqual(self.request("/api/account", method="HEAD")[0], 200)

    def test_entry_evaluation_projection_keeps_only_finite_numeric_context(self):
        from relay.dashboard import _project_decision, _project_order_proposal
        evaluation = {"ask": "0.95", "reference_price": "1", "ask_deviation_percent": "-5",
                      "limit_price": "1.00", "limit_deviation_percent": "0", "max_chase_percent": "15",
                      "provider_response": "SECRET"}
        decision = _project_decision({"action": "OPEN", "entry_evaluation": evaluation})
        self.assertEqual(decision["entry_evaluation"]["ask_deviation_percent"], "-5")
        self.assertNotIn("SECRET", json.dumps(decision))
        proposal = _project_order_proposal({"entry_evaluation": evaluation | {"ask": "NaN"}})
        self.assertNotIn("ask", proposal["entry_evaluation"])

    def test_evaluation_timing_projection_keeps_valid_durations_and_recovery_path(self):
        from relay.dashboard import _project_decision
        timing = {
            "model_duration_seconds": 1.25,
            "posted_to_decision_seconds": 12.5,
            "attempts": 2,
            "decision_at": "2026-09-17T12:00:00+00:00",
            "path": "direct",
            "delayed": True,
            "provider_response": "SECRET",
            "negative": -1,
        }
        recovery_timing = timing | {"path": "recovery", "model_duration_seconds": 2.5}
        decision = _project_decision({"evaluation_timing": timing, "recovery": {"evaluation_timing": recovery_timing}})
        self.assertEqual(decision["evaluation_timing"]["model_duration_seconds"], 1.25)
        self.assertEqual(decision["evaluation_timing"]["attempts"], 2)
        self.assertTrue(decision["evaluation_timing"]["delayed"])
        self.assertEqual(decision["recovery"]["evaluation_timing"]["path"], "recovery")
        self.assertNotIn("provider_response", json.dumps(decision))
        self.assertNotIn("negative", json.dumps(decision))

    def test_status_reports_counts_and_stale_detection_without_sensitive_fields(self):
        status, headers, payload = self.request("/api/status")
        self.assertEqual(status, 200)
        self.assertEqual(headers["cache-control"], "no-store")
        self.assertEqual(payload["mode"], "shadow")
        self.assertFalse(payload["live_orders_enabled"])
        self.assertTrue(payload["ledger_available"])
        self.assertEqual(payload["counts"], {"messages": 3, "events": 2, "orders": 2, "positions": 1, "held": 1, "errors": 0})
        self.assertTrue(payload["runtime"]["available"])
        self.assertFalse(payload["runtime"]["stale"])
        self.assertEqual(payload["runtime"]["detail"], "Robinhood authorization in progress")
        channel = payload["runtime"]["discord"]["channels"][0]
        self.assertIn("updated_at", channel)
        self.assertIn("last_seen_at", channel)
        self.assertNotIn("account_number", json.dumps(payload))

        self.status_path.write_text(json.dumps({"updated_at": (datetime.now(UTC) - timedelta(minutes=5)).isoformat(), "state": "running"}), encoding="utf-8")
        _, _, stale = self.request("/api/status")
        self.assertTrue(stale["runtime"]["stale"])

        self.status_path.write_text(json.dumps({"updated_at": (datetime.now(UTC) + timedelta(seconds=10)).isoformat(), "state": "running"}), encoding="utf-8")
        _, _, future = self.request("/api/status")
        self.assertTrue(future["runtime"]["stale"])

        self.status_path.write_text(json.dumps({
            "updated_at": datetime.now(UTC).isoformat(), "state": "running",
            "detail": "authorization: Bearer abcdefghijklmnop",
        }), encoding="utf-8")
        _, _, redacted = self.request("/api/status")
        self.assertEqual(redacted["runtime"]["detail"], "details withheld")

    def test_messages_use_normalized_author_fields_join_latest_event_and_filters(self):
        status, _, payload = self.request("/api/messages?limit=1&q=entry&state=held")
        self.assertEqual(status, 200)
        self.assertEqual(len(payload["items"]), 1)
        item = payload["items"][0]
        self.assertEqual(item["author"], {"id": "2000000000000000001", "name": "alice"})
        self.assertEqual(item["content"], "entry one")
        self.assertEqual(item["embeds"], [{"title": "ENTRY", "description": "entry one"}])
        self.assertEqual(item["latest_event"]["state"], "held")
        self.assertEqual(item["latest_event"]["decision"]["action"], "OPEN")
        self.assertNotIn("account_number", json.dumps(item))
        self.assertIsNone(payload["next_offset"])

        status, _, payload = self.request("/api/messages?limit=2")
        self.assertEqual(status, 200)
        self.assertEqual(len(payload["items"]), 2)
        self.assertEqual(payload["next_offset"], 2)

    def test_reconciled_unknown_event_projects_terminal_state_without_duplicate_message(self):
        connection = sqlite3.connect(self.database)
        decision = json.loads(connection.execute("SELECT decision FROM events WHERE id=2").fetchone()[0])
        decision["order_reconciliation"] = {
            "status": "filled",
            "filled_quantity": 1,
            "requested_quantity": 1,
            "broker_order_id": "broker-1",
            "fill_price": "0.80",
            "secret": "must not be projected",
        }
        connection.execute(
            "UPDATE events SET state=?, reason=?, decision=? WHERE id=2",
            ("unknown", "Order reconciled: filled; filled 1/1", json.dumps(decision)),
        )
        connection.commit()
        connection.close()

        status, _, payload = self.request("/api/messages?limit=5&q=entry%20two")
        self.assertEqual(status, 200)
        self.assertEqual(len(payload["items"]), 1)
        self.assertIsNone(payload["next_offset"])
        event = payload["items"][0]["latest_event"]
        self.assertEqual(event["state"], "filled")
        self.assertEqual(event["decision"]["action"], "OPEN")
        self.assertEqual(event["decision"]["order_reconciliation"]["status"], "filled")
        self.assertNotIn("secret", json.dumps(event))

        status, _, payload = self.request("/api/messages?limit=5&q=entry%20two&state=filled")
        self.assertEqual(status, 200)
        self.assertEqual(len(payload["items"]), 1)
        self.assertEqual(payload["items"][0]["latest_event"]["state"], "filled")

        status, _, payload = self.request("/api/messages?limit=5&q=entry%20two&state=unknown")
        self.assertEqual(status, 200)
        self.assertEqual(payload["items"], [])

        status, _, payload = self.request("/api/messages?state=unknown")
        self.assertEqual(status, 200)
        self.assertEqual(payload["items"], [])  # Uninterpreted messages have no event, not an unknown order.

        status, _, payload = self.request("/api/events?limit=1")
        self.assertEqual(status, 200)
        self.assertEqual(payload["items"][0]["state"], "filled")

        from relay.dashboard import _project_decision
        self.assertNotIn(
            "order_reconciliation",
            _project_decision({"order_reconciliation": {"status": "not-a-state"}}),
        )

    def test_orders_events_positions_are_safe_projections_and_filterable(self):
        status, _, payload = self.request("/api/orders?status=pending")
        self.assertEqual(status, 200)
        self.assertEqual(len(payload["items"]), 1)
        pending = payload["items"][0]
        self.assertEqual(pending["status"], "pending")
        self.assertEqual(pending["filled_quantity"], 0)
        self.assertEqual(pending["mode"], "shadow")
        self.assertNotIn("account_number", json.dumps(pending))

        status, _, payload = self.request("/api/events?limit=1")
        self.assertEqual(status, 200)
        self.assertEqual(payload["items"][0]["state"], "paper_order")
        self.assertEqual(payload["next_offset"], 1)

        status, _, payload = self.request("/api/positions")
        self.assertEqual(status, 200)
        self.assertEqual(payload["items"][0]["quantity"], 1)
        self.assertEqual(payload["items"][0]["contract"]["symbol"], "SPY")

    def test_missing_database_is_empty_setup_state_and_reader_does_not_create_lock(self):
        missing = self.root / "missing.json"
        missing.write_text(json.dumps({"mode": "shadow", "database": "state/not-yet-created.sqlite3"}), encoding="utf-8")
        app = DashboardApp(missing)
        self.assertFalse(app.status()["ledger_available"])
        self.assertEqual(app.messages(""), {"items": [], "next_offset": None})
        self.assertFalse((self.state / "not-yet-created.sqlite3").exists())
        self.assertFalse((self.state / "not-yet-created.sqlite3.lock").exists())

        before = self.database.stat().st_mtime_ns
        self.request("/api/status")
        self.request("/api/messages")
        self.assertEqual(self.database.stat().st_mtime_ns, before)
        self.assertFalse((self.database.with_suffix(self.database.suffix + ".lock")).exists())

    def test_config_reload_switches_mode_and_database_and_invalid_config_fails_closed(self):
        alternate = self.state / "alternate.sqlite3"
        alternate.write_bytes(self.database.read_bytes())
        connection = sqlite3.connect(alternate)
        for table in ("messages", "events", "orders", "positions"):
            connection.execute(f"DELETE FROM {table}")
        connection.commit()
        connection.close()

        updated = json.loads(self.config_path.read_text(encoding="utf-8"))
        updated["mode"] = "paper"
        updated["database"] = "state/alternate.sqlite3"
        self.config_path.write_text(json.dumps(updated), encoding="utf-8")
        status, _, payload = self.request("/api/status")
        self.assertEqual(status, 200)
        self.assertEqual(payload["mode"], "paper")
        self.assertTrue(payload["ledger_available"])
        self.assertEqual(payload["counts"], {"messages": 0, "events": 0, "orders": 0, "positions": 0, "held": 0, "errors": 0})

        self.config_path.write_text("{invalid", encoding="utf-8")
        status, _, payload = self.request("/api/status")
        self.assertEqual(status, 503)
        self.assertIn("configuration", payload["error"])

    def test_invalid_queries_path_traversal_and_mutations_are_rejected(self):
        for path in ("/api/messages?limit=0", "/api/messages?limit=201", "/api/messages?state=not-a-state", "/api/status?token=secret", "/api/messages?limit=1&limit=2"):
            status, headers, payload = self.request(path)
            self.assertEqual(status, 400, path)
            self.assertEqual(headers["x-content-type-options"], "nosniff")
            self.assertIn("error", payload)
        for path in ("/../relay/core.py", "/%2e%2e/relay/core.py", "/relay/core.py", "/api/nope"):
            status, _, _ = self.request(path)
            self.assertEqual(status, 404, path)
        status, headers, payload = self.request("/api/orders", method="POST")
        self.assertEqual(status, 405)
        self.assertEqual(headers["allow"], "GET, HEAD")
        self.assertIn("error", payload)


if __name__ == "__main__":
    unittest.main()
