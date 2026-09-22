from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from relay.dashboard import (
    DashboardApp,
    _project_decision,
    _project_order,
    _project_order_protection,
    _project_position,
    _project_stop_evaluation,
)
from relay.notifications import _event_candidate, _format_projection, _order_candidate, _projection


CONTRACT = {
    "symbol": "SPY",
    "expiry": "2026-09-18",
    "strike": "600",
    "option_type": "call",
}


class StopDisplayTests(unittest.TestCase):
    def test_dashboard_stop_projection_is_bounded_and_explicit(self):
        decision = _project_decision({
            "action": "REDUCE",
            "contract": CONTRACT,
            "stop_evaluation": {
                "status": "active",
                "stop_price": "0.80",
                "reason": "native stop active",
                "order_id": "stop-1",
                "provider_response": "SECRET",
            },
        })
        self.assertEqual(decision["stop_evaluation"]["status"], "active")
        self.assertEqual(decision["stop_evaluation"]["stop_price"], "0.80")
        self.assertNotIn("SECRET", json.dumps(decision))

        self.assertEqual(
            _project_stop_evaluation({"status": "provider-secret", "stop_price": "-1"}),
            {"status": "unknown"},
        )

    def test_order_and_position_projections_keep_native_stop_fields(self):
        order = _project_order({
            "id": "stop-1",
            "contract": json.dumps(CONTRACT, sort_keys=True),
            "action": "UPDATE_STOP",
            "body": json.dumps({
                "contract": CONTRACT,
                "order_type": "stop_market",
                "stop_price": "0.80",
                "requested_stop_price": "0.80",
                "quantity": 2,
            }),
            "status": "open",
            "filled_quantity": 0,
            "filled_notional": "0",
            "broker_id": None,
            "message_id": "message-1",
            "created_at": "2026-09-22T12:00:00+00:00",
        }, "paper")
        self.assertEqual(order["order_type"], "stop_market")
        self.assertEqual(order["stop_price"], "0.80")
        self.assertEqual(order["requested_stop_price"], "0.80")
        self.assertNotIn("limit_price", order["protection"])
        self.assertIsNone(_project_order_protection({"order_type": "market", "stop_price": "0"}))

        position = _project_position({
            "source_group": "signals",
            "contract": json.dumps(CONTRACT, sort_keys=True),
            "quantity": 2,
            "average_price": "0.75",
            "stop_status": "pending",
            "stop_reason": "placement pending",
            "stop_price": "0.80",
            "stop_order_id": "stop-1",
        })
        self.assertEqual(position["stop_evaluation"]["status"], "pending")
        self.assertEqual(position["stop_evaluation"]["order_id"], "stop-1")

    def test_positions_attach_stop_request_without_provider_access(self):
        with tempfile.TemporaryDirectory(prefix="stop-display-") as temporary:
            root = Path(temporary)
            state = root / "state"
            state.mkdir()
            database = state / "relay.sqlite3"
            connection = sqlite3.connect(database)
            connection.executescript(
                """
                CREATE TABLE positions (
                    source_group TEXT NOT NULL,
                    contract TEXT NOT NULL,
                    quantity INTEGER NOT NULL,
                    average_price TEXT NOT NULL,
                    PRIMARY KEY(source_group, contract)
                );
                CREATE TABLE stop_requests (
                    source_group TEXT NOT NULL,
                    contract TEXT NOT NULL,
                    message TEXT NOT NULL,
                    decision TEXT NOT NULL,
                    stop_price TEXT NOT NULL,
                    entry_id TEXT NOT NULL,
                    generation INTEGER NOT NULL DEFAULT 0,
                    order_id TEXT,
                    status TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    PRIMARY KEY(source_group, contract)
                );
                """
            )
            contract_json = json.dumps(CONTRACT, sort_keys=True)
            connection.execute("INSERT INTO positions VALUES (?,?,?,?)", ("signals", contract_json, 2, "0.75"))
            connection.execute(
                "INSERT INTO stop_requests VALUES (?,?,?,?,?,?,?,?,?,?)",
                ("signals", contract_json, "{}", "{}", "0.80", "entry-1", 1, "stop-1", "blocked", "broker acknowledgement missing"),
            )
            connection.commit()
            connection.close()

            config = root / "config.json"
            config.write_text(json.dumps({"mode": "shadow", "database": "state/relay.sqlite3"}), encoding="utf-8")
            payload = DashboardApp(config).positions()
            self.assertEqual(payload["items"][0]["stop_evaluation"]["status"], "blocked")
            self.assertEqual(payload["items"][0]["stop_evaluation"]["stop_price"], "0.80")

    def test_notifications_label_only_explicit_active_status(self):
        for status in ("shadow", "pending", "complete", "blocked", "provider-secret"):
            projection = _projection({
                "action": "REDUCE",
                "contract": CONTRACT,
                "stop_evaluation": {"status": status, "stop_price": "0.80", "provider_response": "SECRET"},
            })
            content = _format_projection(projection)
            self.assertNotIn("Protection active", content)
            self.assertNotIn("SECRET", content)

        active = _format_projection(_projection({
            "action": "REDUCE",
            "contract": CONTRACT,
            "stop_evaluation": {"status": "active", "stop_price": "0.80"},
        }))
        self.assertIn("Protection active at $0.8", active)

        shadow = _format_projection(_projection({
            "action": "REDUCE",
            "contract": CONTRACT,
            "stop_evaluation": {"status": "shadow", "stop_price": "0.80"},
        }))
        self.assertIn("Protection shadow at $0.8; no order submitted", shadow)

    def test_order_notification_describes_native_stop_without_claiming_active(self):
        alert = _order_candidate({
            "id": "stop-1",
            "status": "open",
            "action": "UPDATE_STOP",
            "filled_quantity": 0,
            "body": json.dumps({
                "contract": CONTRACT,
                "order_type": "stop_market",
                "stop_price": "0.80",
                "requested_stop_price": "0.80",
                "quantity": 2,
            }),
        }, mode="paper")
        self.assertIn("Native stop market order at $0.8", alert.payload["content"])
        self.assertNotIn("Protection active", alert.payload["content"])

    def test_existing_views_contain_bounded_protection_rows(self):
        source = (Path(__file__).parents[1] / "relay" / "static" / "app.js").read_text(encoding="utf-8")
        for marker in (
            "function protectionText(evaluation)",
            "renderProtection(decisionPanel, readDecision(event).stop_evaluation)",
            "renderProtection(contract, order.stop_evaluation)",
            "renderProtection(contract, position.stop_evaluation)",
            "renderProtection(row, readDecision(event).stop_evaluation)",
        ):
            self.assertIn(marker, source)


if __name__ == "__main__":
    unittest.main()
