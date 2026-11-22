from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import relay.notifications as notifications
from relay.notifications import DeliveryError, NotificationWorker, notification_status, validate_webhook_url


WEBHOOK = "https://discord.com/api/webhooks/123456789012345678/" + "a" * 68
UTC = timezone.utc


def embed_text(payload):
    return json.dumps(payload["embeds"][0])


class NotificationTests(unittest.TestCase):
    def test_expiry_exit_and_unfilled_risk_reach_sanitized_notifications(self):
        decision = {"action": "CLOSE", "expiry_exit": {"status": "unfilled", "provider_response": "SECRET"}}
        event = {"id": 1, "state": "held", "message_id": "expiry:example", "decision": json.dumps(decision)}
        with patch.dict(notifications.os.environ, {"RELAY_DISCORD_ALERT_MENTION": "here"}):
            alert = notifications._event_candidate(event, mode="live", order_ids=set(), order_message_ids=set())
        self.assertEqual(alert.payload["content"], "@here")
        self.assertEqual(alert.payload["allowed_mentions"], {"parse": ["everyone"]})
        self.assertIn("Expiry protection", embed_text(alert.payload))
        self.assertIn("check broker immediately", embed_text(alert.payload))
        self.assertNotIn("SECRET", json.dumps(alert.payload))
        order = {"id": "order", "status": "open", "body": json.dumps(decision), "action": "CLOSE", "filled_quantity": 0}
        alert = notifications._order_candidate(order, mode="live")
        self.assertIn("Expiry protection", embed_text(alert.payload))

    def test_chase_diagnostics_are_numeric_and_reach_hold_and_order_notifications(self):
        evaluation = {"ask": "1.16", "reference_price": "1.00", "ask_deviation_percent": "16",
                      "limit_price": "1.20", "limit_deviation_percent": "20", "max_chase_percent": "15",
                      "provider_response": "SECRET"}
        decision = {"action": "OPEN", "entry_evaluation": evaluation}
        event = {"id": 1, "state": "held", "message_id": "1", "decision": json.dumps(decision)}
        held = notifications._event_candidate(event, mode="live", order_ids=set(), order_message_ids=set())
        self.assertIn("Evaluated ask $1.16", embed_text(held.payload))
        self.assertIn("Chase cap 15%", embed_text(held.payload))
        self.assertIn("Limit $1.2 (+20%)", embed_text(held.payload))
        order = {"id": "order", "status": "filled", "body": json.dumps({"side": "buy", "position_effect": "open", "entry_evaluation": evaluation}),
                 "action": "OPEN", "filled_quantity": 1}
        entered = notifications._order_candidate(order, mode="live")
        self.assertIn("+16% vs alert", embed_text(entered.payload))
        self.assertNotIn("SECRET", json.dumps(entered.payload))
        evaluation["ask_deviation_percent"] = "-5"
        candidate = notifications._event_candidate(
            {**event, "decision": json.dumps(decision)}, mode="live", order_ids=set(), order_message_ids=set(),
        )
        self.assertIn("-5% vs alert", embed_text(candidate.payload))
        evaluation["ask"] = "NaN"
        self.assertNotIn("entry_evaluation", notifications._projection(decision))

    def test_worker_failure_keeps_specific_provider_reauthentication_reason(self):
        path = self.state / "runtime-status.json"
        path.write_text(json.dumps({"state": "error", "heartbeat_at": datetime.now(UTC).isoformat(),
                                    "broker": {"state": "auth_required"}}))
        self.assertEqual(notifications._runtime_issues(path), {"broker": "reauth"})

    def test_codex_auth_attention_overrides_ready_runtime_snapshot(self):
        now = datetime.now(UTC)
        path = self.state / "runtime-status.json"
        runtime = {"state": "running", "heartbeat_at": now.isoformat(),
                   "codex": {"state": "ready", "updated_at": now.isoformat()}}
        path.write_text(json.dumps(runtime), encoding="utf-8")
        with patch.object(notifications, "codex_auth_attention", return_value=True) as attention:
            self.assertEqual(notifications._runtime_issues(path, now=now), {"codex": "reauth"})
        attention.assert_called_once_with(path, runtime, now)

    def test_missing_credential_state_aliases_are_reauthentication_issues(self):
        now = datetime.now(UTC)
        path = self.state / "runtime-status.json"
        for state in ("credential_missing", "credentials_missing"):
            with self.subTest(state=state):
                path.write_text(json.dumps({"state": "running", "heartbeat_at": now.isoformat(),
                                            "codex": {"state": state}}), encoding="utf-8")
                with patch.object(notifications, "codex_auth_attention", return_value=False):
                    self.assertEqual(notifications._runtime_issues(path, now=now), {"codex": "reauth"})

    def test_action_update_uses_sanitized_structured_embed_without_ping(self):
        decision = {
            "action": "OPEN",
            "contract": {"symbol": "SPY", "expiry": "2026-09-18", "strike": "600", "option_type": "call"},
            "quantity": 2,
            "alert_price": "0.80",
            "entry_evaluation": {
                "ask": "0.92", "reference_price": "0.80", "ask_deviation_percent": "15",
                "max_chase_percent": "10", "provider_response": "PRIVATE RESPONSE",
            },
            "reason": "PRIVATE REASON",
        }
        row = {"id": 4, "state": "held", "message_id": "4", "decision": json.dumps(decision)}
        candidate = notifications._event_candidate(row, mode="live", order_ids=set(), order_message_ids=set())
        payload = candidate.payload
        self.assertEqual(payload["allowed_mentions"], {"parse": []})
        embed = payload["embeds"][0]
        self.assertEqual(embed["title"], "Action held for review")
        self.assertEqual(embed["color"], notifications._EMBED_COLORS["warning"])
        fields = {field["name"]: field["value"] for field in embed["fields"]}
        self.assertIn("SPY", fields["Contract"])
        self.assertIn("2 contracts", fields["Quantity"])
        self.assertIn("Evaluated ask $0.92", fields["Entry evaluation"])
        encoded = json.dumps(payload)
        self.assertNotIn("PRIVATE RESPONSE", encoded)
        self.assertNotIn("PRIVATE REASON", encoded)
        self.assertNotIn("@here", payload["content"])

    def test_runtime_incident_mention_configuration_is_scoped_and_fails_closed(self):
        with patch.dict(notifications.os.environ, {}, clear=True):
            default = notifications._runtime_payload("codex", "reauth")
        self.assertEqual(default["content"], "@here")
        self.assertEqual(default["allowed_mentions"], {"parse": ["everyone"]})
        self.assertIn("reauthentication required", default["embeds"][0]["title"])

        configurations = (
            ("none", "", {"parse": []}),
            ("everyone", "@everyone", {"parse": ["everyone"]}),
            ("123456789012345678", "<@&123456789012345678>",
             {"parse": [], "roles": ["123456789012345678"]}),
            ("@here", "", {"parse": []}),
        )
        for value, expected_content, allowed in configurations:
            with self.subTest(value=value), patch.dict(notifications.os.environ, {"RELAY_DISCORD_ALERT_MENTION": value}):
                payload = notifications._runtime_payload("broker", "unavailable")
            self.assertEqual(payload["content"], expected_content)
            self.assertEqual(payload["allowed_mentions"], allowed)

        with patch.dict(notifications.os.environ, {"RELAY_DISCORD_ALERT_MENTION": "here"}):
            action = notifications._event_candidate(
                {"id": 1, "state": "held", "message_id": "1", "decision": "{}"},
                mode="live", order_ids=set(), order_message_ids=set(),
            ).payload
        self.assertNotIn("@here", action["content"])
        self.assertEqual(action["allowed_mentions"], {"parse": []})

    def test_legacy_outbox_replay_keeps_its_saved_unpinged_payload(self):
        now = datetime.now(UTC)
        (self.state / "runtime-status.json").write_text(
            json.dumps({"state": "running", "heartbeat_at": now.isoformat()}), encoding="utf-8",
        )
        sent = []
        worker = NotificationWorker(self.config_path, sender=sent.append, clock=lambda: now)
        worker.poll()
        legacy = {"content": "Previously queued issue", "allowed_mentions": {"parse": []}}
        db = notifications._open_state(worker.db_path)
        try:
            db.execute(
                "INSERT INTO outbox(dedup_key,payload,created_at) VALUES (?,?,?)",
                ("legacy:issue", json.dumps(legacy, separators=(",", ":")), now.isoformat()),
            )
            db.commit()
        finally:
            db.close()

        with patch.dict(notifications.os.environ, {"RELAY_DISCORD_ALERT_MENTION": "here"}):
            result = worker.poll()
        self.assertEqual(result["sent"], 1)
        self.assertEqual(sent, [legacy])

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="notifications-")
        self.root = Path(self.temporary.name)
        self.state = self.root / "state"
        self.state.mkdir()
        self.ledger = self.state / "relay.sqlite3"
        self.config_path = self.root / "config.json"
        self.config = {
            "mode": "shadow",
            "database": "state/relay.sqlite3",
            "runtime_status_file": "state/runtime-status.json",
            "notifications": {"enabled": True, "webhook_url": WEBHOOK},
        }
        self._create_ledger()
        self.config_path.write_text(json.dumps(self.config), encoding="utf-8")

    def tearDown(self):
        self.temporary.cleanup()

    def _create_ledger(self):
        connection = sqlite3.connect(self.ledger)
        connection.executescript(
            """
            CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
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
            """
        )
        connection.commit()
        connection.close()

    def _event(self, event_id, state="shadow_order", decision=None):
        decision = decision or {
            "action": "OPEN",
            "contract": {"symbol": "SPY", "expiry": "2026-09-18", "strike": "600", "option_type": "call"},
            "quantity": 2,
            "alert_price": "0.80",
            "reason": "do not disclose",
            "evidence": [{"quote": "secret source text"}],
        }
        connection = sqlite3.connect(self.ledger)
        connection.execute(
            "INSERT INTO events VALUES (?,?,?,?,?,?,?)",
            (event_id, str(event_id), "rev", state, "private reason", json.dumps(decision), datetime.now(UTC).isoformat()),
        )
        connection.commit()
        connection.close()

    def test_webhook_validation_rejects_unsafe_destinations(self):
        with self.assertRaisesRegex(ValueError, "invalid Discord webhook URL"):
            validate_webhook_url(" " + WEBHOOK + " ")
        self.assertEqual(validate_webhook_url(WEBHOOK.replace("/api/", "/api/v10/")), WEBHOOK.replace("/api/", "/api/v10/"))
        for value in (
            "http://discord.com/api/webhooks/123/token",
            "https://discord.com.evil/api/webhooks/123/token",
            "https://user:pass@discord.com/api/webhooks/123/token",
            "https://discord.com:443/api/webhooks/123/token",
            "https://discord.com/api/webhooks/not-numeric/token",
            "https://discord.com/api/webhooks/123/token?thread_id=1",
            "https://discord.com/api/webhooks/123/token#fragment",
            "https://discord.com/api/webhooks/123/",
        ):
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, "invalid Discord webhook URL"):
                validate_webhook_url(value)

    def test_http_sender_requests_saved_message_acknowledgement(self):
        class Response:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def getcode(self):
                return 200

            def read(self, _limit):
                return b'{"id":"123456789012345678"}'

        class Opener:
            def __init__(self):
                self.request = None

            def open(self, request, timeout):
                self.request = request
                self.timeout = timeout
                return Response()

        opener = Opener()
        worker = NotificationWorker(self.config_path)
        worker._active_url = WEBHOOK
        with patch.object(notifications, "build_opener", return_value=opener):
            worker.send({"content": "safe", "allowed_mentions": {"parse": []}})
        self.assertEqual(opener.request.full_url, WEBHOOK + "?wait=true")

    def test_http_sender_does_not_mark_invalid_acknowledgement_as_sent(self):
        class Response:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def getcode(self):
                return 204

            def read(self, _limit):
                return b""

        class Opener:
            def open(self, _request, timeout):
                self.timeout = timeout
                return Response()

        worker = NotificationWorker(self.config_path)
        worker._active_url = WEBHOOK
        with patch.object(notifications, "build_opener", return_value=Opener()):
            with self.assertRaisesRegex(DeliveryError, "acknowledgement unavailable"):
                worker.send({"content": "safe", "allowed_mentions": {"parse": []}})

    def test_first_enable_baselines_history_but_alerts_current_reauth(self):
        self._event(1)
        self._event(2, "held")
        self._event(3, "paper_order")
        status = self.state / "runtime-status.json"
        status.write_text(json.dumps({"state": "running", "codex": {"state": "auth_required"}}), encoding="utf-8")
        sent = []
        worker = NotificationWorker(self.config_path, sender=sent.append)
        first = worker.poll()
        self.assertEqual(first["state"], "running")
        self.assertEqual(len(sent), 1)
        self.assertIn("Codex reauthentication", embed_text(sent[0]))
        self.assertEqual(sent[0]["content"], "@here")
        self.assertEqual(sent[0]["allowed_mentions"], {"parse": ["everyone"]})
        encoded = json.dumps(sent[0])
        self.assertNotIn("secret source text", encoded)
        self.assertNotIn("do not disclose", encoded)
        self.assertNotIn(WEBHOOK, encoded)

        self.assertEqual(worker.poll()["sent"], 0)
        self.assertIsNotNone(notification_status(self.config_path).get("last_sent_at"))

    def test_auth_alert_is_not_suppressed_when_ledger_is_absent(self):
        self.ledger.unlink()
        status = self.state / "runtime-status.json"
        status.write_text(json.dumps({"state": "running", "codex": {"state": "auth_required"}}), encoding="utf-8")
        sent = []
        result = NotificationWorker(self.config_path, sender=sent.append).poll()
        self.assertEqual(result["state"], "running")
        self.assertEqual(len(sent), 1)
        self.assertIn("Codex reauthentication", embed_text(sent[0]))

    def test_ledger_read_failure_keeps_runtime_worker_alive(self):
        status = self.state / "runtime-status.json"
        status.write_text(json.dumps({"state": "running", "codex": {"state": "auth_required"}}), encoding="utf-8")
        sent = []
        worker = NotificationWorker(self.config_path, sender=sent.append)
        with patch.object(notifications, "_read_ledger", return_value=([], [], False)):
            result = worker.poll()
        self.assertEqual(result["state"], "retrying")
        self.assertEqual(len(sent), 1)
        self.assertIn("Codex reauthentication", embed_text(sent[0]))

    def test_unknown_event_with_order_row_is_not_duplicated(self):
        now = datetime(2026, 9, 8, tzinfo=UTC)
        decision = {
            "action": "OPEN",
            "contract": {"symbol": "SPY", "expiry": "2026-09-18", "strike": "600", "option_type": "call"},
            "quantity": 1,
            "limit_price": "1.20",
            "order_proposal": {"client_order_id": "client-1"},
        }
        (self.state / "runtime-status.json").write_text(
            json.dumps({"state": "running", "heartbeat_at": now.isoformat()}),
            encoding="utf-8",
        )
        sent = []
        worker = NotificationWorker(self.config_path, sender=sent.append, clock=lambda: now)
        worker.poll()
        connection = sqlite3.connect(self.ledger)
        connection.execute(
            "INSERT INTO events VALUES (?,?,?,?,?,?,?)",
            (1, "message-1", "rev", "unknown", "private", json.dumps(decision), now.isoformat()),
        )
        connection.execute(
            "INSERT INTO orders VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (
                "client-1", "message-1", "source", json.dumps(decision["contract"]), "OPEN",
                json.dumps(decision), "unknown", None, now.isoformat(), 0, "0",
            ),
        )
        connection.commit()
        connection.close()
        result = worker.poll()
        self.assertEqual(result["sent"], 1)
        self.assertIn("needs reconciliation", embed_text(sent[0]))

    def test_new_events_are_paginated_and_orders_are_read_only(self):
        worker = NotificationWorker(self.config_path, sender=lambda payload: None)
        worker.poll()  # establish the baseline
        for event_id in range(1, 405):
            self._event(event_id)
        before = self.ledger.stat().st_mtime_ns
        result = worker.poll()
        self.assertEqual(result["sent"], 404)
        self.assertEqual(self.ledger.stat().st_mtime_ns, before)
        self.assertFalse(self.ledger.with_suffix(self.ledger.suffix + ".lock").exists())
        self.assertEqual(worker.poll()["sent"], 0)

    def test_delivery_retries_are_durable_and_invalid_webhook_is_delayed(self):
        calls = []

        (self.state / "runtime-status.json").write_text(json.dumps({"state": "running"}), encoding="utf-8")

        def fail(payload):
            calls.append(payload)
            raise DeliveryError("temporary", delay=5)

        worker = NotificationWorker(self.config_path, sender=fail)
        worker.poll()
        self._event(1)
        result = worker.poll()
        self.assertEqual(result["state"], "retrying")
        self.assertEqual(len(calls), 1)
        self.assertEqual(worker.poll()["sent"], 0)
        connection = sqlite3.connect(self.state / "notifications.sqlite3")
        try:
            row = connection.execute("SELECT attempts, state FROM outbox").fetchone()
        finally:
            connection.close()
        self.assertEqual(row, (1, "pending"))

    def test_rate_limit_body_must_be_an_object(self):
        class Error:
            def read(self, _limit):
                return b"[]"

        failure = NotificationWorker._http_failure(429, error=Error())
        self.assertIsInstance(failure, DeliveryError)
        self.assertTrue(failure.retryable)
        self.assertEqual(failure.delay, 5.0)

    def test_rate_limit_cooldown_applies_to_destination(self):
        now = datetime(2026, 9, 8, tzinfo=UTC)
        calls = []

        (self.state / "runtime-status.json").write_text(
            json.dumps({"state": "running"}), encoding="utf-8"
        )

        def send(payload):
            calls.append(payload)
            if len(calls) == 1:
                raise DeliveryError("rate limited", delay=60)

        worker = NotificationWorker(self.config_path, sender=send, clock=lambda: now)
        worker.poll()
        self._event(1)
        self._event(2)

        result = worker.poll()
        self.assertEqual(result["state"], "retrying")
        self.assertEqual(len(calls), 1)
        self.assertEqual(worker.poll()["sent"], 0)
        self.assertEqual(len(calls), 1)
        restarted = NotificationWorker(self.config_path, sender=send, clock=lambda: now)
        self.assertEqual(restarted.poll()["sent"], 0)
        self.assertEqual(len(calls), 1)
        now += timedelta(seconds=60)
        self.assertEqual(restarted.poll()["sent"], 2)
        self.assertEqual(len(calls), 3)

    def test_pending_outbox_survives_same_destination_mode_and_ledger_change(self):
        now = datetime(2026, 9, 8, tzinfo=UTC)
        calls = []

        (self.state / "runtime-status.json").write_text(
            json.dumps({"state": "running", "heartbeat_at": now.isoformat()}),
            encoding="utf-8",
        )

        def send(payload):
            calls.append(payload)
            if len(calls) == 1:
                raise DeliveryError("temporary", delay=60)

        worker = NotificationWorker(self.config_path, sender=send, clock=lambda: now)
        worker.poll()
        self._event(1)
        first = worker.poll()
        self.assertEqual(first["state"], "retrying")
        self.assertEqual(len(calls), 1)

        new_ledger = self.state / "paper.sqlite3"
        sqlite3.connect(new_ledger).close()
        updated = dict(self.config)
        updated["mode"] = "paper"
        updated["database"] = "state/paper.sqlite3"
        self.config_path.write_text(json.dumps(updated), encoding="utf-8")

        restarted = NotificationWorker(self.config_path, sender=send, clock=lambda: now)
        result = restarted.poll()
        self.assertEqual(result["sent"], 0)
        now += timedelta(seconds=60)
        result = restarted.poll()
        self.assertEqual(result["sent"], 1)
        self.assertEqual(len(calls), 2)
        self.assertIn("Shadow proposal", embed_text(calls[1]))

    def test_status_defaults_disabled_and_hides_invalid_or_private_state(self):
        disabled = self.root / "disabled.json"
        disabled.write_text(json.dumps({}), encoding="utf-8")
        self.assertEqual(notification_status(disabled), {
            "enabled": False, "configured": False, "state": "disabled", "detail": "Notifications are disabled.",
        })
        self.config["notifications"]["webhook_url"] = "https://discord.com/api/webhooks/1/bad?secret=1"
        self.config_path.write_text(json.dumps(self.config), encoding="utf-8")
        result = notification_status(self.config_path)
        self.assertTrue(result["enabled"])
        self.assertFalse(result["configured"])
        self.assertNotIn("secret=1", json.dumps(result))

    def test_reauthentication_alert_dedupes_across_restart_and_reopens_after_recovery(self):
        now = datetime(2026, 9, 11, tzinfo=UTC)
        runtime = self.state / "runtime-status.json"
        sent = []
        worker = NotificationWorker(self.config_path, sender=sent.append, clock=lambda: now)
        runtime.write_text(json.dumps({"state": "running", "broker": {"state": "reauth_required"}}), encoding="utf-8")
        self.assertEqual(worker.poll()["sent"], 1)
        runtime.write_text(json.dumps({"state": "running", "broker": {"state": "connected"}}), encoding="utf-8")
        self.assertEqual(worker.poll()["sent"], 0)
        runtime.write_text(json.dumps({"state": "running", "broker": {"auth_required": True}}), encoding="utf-8")
        self.assertEqual(worker.poll()["sent"], 1)
        restarted = NotificationWorker(self.config_path, sender=sent.append, clock=lambda: now)
        self.assertEqual(restarted.poll()["sent"], 0)
        self.assertEqual(len(sent), 2)
        self.assertTrue(all("Robinhood reauthentication" in embed_text(payload) for payload in sent))


if __name__ == "__main__":
    unittest.main()
