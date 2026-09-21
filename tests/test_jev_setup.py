"""Focused offline checks for JEV setup persistence and dashboard projections."""

import http.client
import json
import os
from pathlib import Path
import stat
import sqlite3
import tempfile
import threading
import unittest
from unittest.mock import AsyncMock, patch

from relay.dashboard import DashboardApp, DashboardHTTPServer, _project_evaluation_timing
from relay.setup import SetupManager


ROOT = Path(__file__).resolve().parents[1]


class JEVSetupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        config = json.loads((ROOT / "config.example.json").read_text(encoding="utf-8"))
        config["database"] = "state/relay.sqlite3"
        config["kill_switch"] = "state/STOP"
        config["runtime_status_file"] = "state/runtime-status.json"
        config["browser"]["profile_dir"] = "state/discord-browser"
        config["robinhood"]["token_store"] = "state/robinhood-oauth.json"
        self.path = self.base / "config.json"
        self.path.write_text(json.dumps(config), encoding="utf-8")
        self.manager = SetupManager(self.path)
        self.validate = patch.object(self.manager, "_validate_candidate", return_value=None)
        self.validate.start()
        self.addCleanup(self.validate.stop)
        for name, result in (
            ("_public_codex_locked", {"state": "connected", "detail": "Codex connected."}),
            ("_public_robinhood_locked", {"state": "connected", "detail": "Robinhood connected."}),
            ("_public_discord", {"state": "connected", "detail": "Discord connected."}),
        ):
            mocked = patch.object(self.manager, name, return_value=result)
            mocked.start()
            self.addCleanup(mocked.stop)

    def tearDown(self):
        self.manager.close()
        self.temp.cleanup()

    def _paused(self):
        stop = self.base / "state/STOP"
        stop.parent.mkdir(parents=True, exist_ok=True)
        stop.write_text("", encoding="utf-8")

    def _legacy_payload(self):
        return {
            "model": "gpt-6-astra",
            "reasoning_effort": "medium",
            "service_tier": "fast",
            "max_chase_fraction": "0.10",
        }

    def test_old_four_field_payload_keeps_legacy_shape(self):
        raw = json.loads(self.path.read_text(encoding="utf-8"))
        raw.pop("evaluation", None)
        self.path.write_text(json.dumps(raw), encoding="utf-8")
        self._paused()

        result = self.manager.save_evaluation(self._legacy_payload())
        saved = json.loads(self.path.read_text(encoding="utf-8"))

        self.assertNotIn("evaluation", saved)
        self.assertEqual(result["evaluation"]["model"], "gpt-6-astra")
        self.assertEqual(result["evaluation"]["mode"], "codex")
        self.assertIn("jev", result["evaluation"])
        self.assertTrue(result["evaluation"]["direct_entries"])

    def test_direct_entries_persists_and_rejects_malformed_booleans(self):
        self._paused()
        payload = self._legacy_payload()
        payload["evaluation"] = {"direct_entries": False}
        result = self.manager.save_evaluation(payload)
        saved = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertFalse(saved["evaluation"]["direct_entries"])
        self.assertFalse(result["evaluation"]["direct_entries"])
        for malformed in (None, "false", 0, 1, []):
            with self.subTest(malformed=malformed):
                invalid = self._legacy_payload()
                invalid["evaluation"] = {"direct_entries": malformed}
                with self.assertRaises(ValueError):
                    self.manager.save_evaluation(invalid)

    def test_nested_settings_write_private_key_and_public_projection_is_sanitized(self):
        self._paused()
        payload = self._legacy_payload() | {
            "evaluation": {
                "mode": "jev_shadow",
                "timeout_ms": 1200,
                "min_confidence": 0.95,
                "min_probability": 0.95,
                "min_eligibility": 0.98,
                "typesafe_api_key": "private-typesafe-key",
            }
        }

        result = self.manager.save_evaluation(payload)
        key_path = self.base / "state/typesafe.key"
        saved = json.loads(self.path.read_text(encoding="utf-8"))

        self.assertEqual(key_path.read_text(encoding="utf-8").strip(), "private-typesafe-key")
        self.assertEqual(stat.S_IMODE(key_path.stat().st_mode), 0o600)
        self.assertEqual(saved["evaluation"]["mode"], "jev_shadow")
        self.assertNotIn("typesafe_api_key", json.dumps(saved))
        self.assertNotIn("private-typesafe-key", json.dumps(result))
        self.assertNotIn("api_key_file", result["evaluation"]["jev"])
        self.assertTrue(result["evaluation"]["configured"])
        with patch.object(self.manager, "_read_runtime", return_value={"jev": {"state": "auth_required", "detail": "Reauthenticate TypeSafe."}}):
            public = self.manager._public_evaluation(saved)
        self.assertEqual(public["jev"]["state"], "auth_required")
        for settings in ({"timeout_ms": 1201}, {"min_confidence": 0}, {"mode": []}):
            with self.subTest(settings=settings), self.assertRaises(ValueError):
                self.manager.save_evaluation(self._legacy_payload() | {"evaluation": settings})

    def test_blank_key_preserves_existing_secret(self):
        self._paused()
        first = self._legacy_payload() | {"evaluation": {"typesafe_api_key": "keep-me"}}
        self.manager.save_evaluation(first)
        self.manager.save_evaluation(self._legacy_payload() | {"evaluation": {"typesafe_api_key": ""}})
        self.assertEqual((self.base / "state/typesafe.key").read_text(encoding="utf-8").strip(), "keep-me")

    def test_symlink_key_path_is_rejected(self):
        self._paused()
        state = self.base / "state"
        state.mkdir(parents=True, exist_ok=True)
        target = self.base / "outside.key"
        target.write_text("unchanged", encoding="utf-8")
        (state / "typesafe.key").symlink_to(target)
        with self.assertRaisesRegex(RuntimeError, "must not be a symlink"):
            self.manager.save_evaluation(self._legacy_payload() | {"evaluation": {"typesafe_api_key": "new"}})
        self.assertEqual(target.read_text(encoding="utf-8"), "unchanged")

    def test_synthetic_test_invokes_provider_probe_and_is_timed(self):
        with patch("relay.evaluation.synthetic_test", new=AsyncMock(return_value={
            "state": "ready", "provider": "jev", "synthetic": True,
            "latency_ms": 1.5, "detail": "Synthetic classification completed.",
        })) as probe:
            result = self.manager.test_evaluation({})
        self.assertEqual(result["state"], "ready")
        self.assertEqual(result["provider"], "jev")
        self.assertTrue(result["synthetic"])
        self.assertGreaterEqual(result["latency_ms"], 0)
        probe.assert_awaited_once()

    def test_http_synthetic_endpoint(self):
        app = DashboardApp(self.path, enable_setup=True)
        try:
            server = DashboardHTTPServer(("127.0.0.1", 0), app)
        except PermissionError:
            self.skipTest("network sockets are unavailable in this sandbox")
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.shutdown)
        self.addCleanup(server.server_close)
        self.addCleanup(app.setup.close)
        body = json.dumps({}).encode("utf-8")
        client = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=3)
        client.request("POST", "/api/setup/evaluation/test", body=body, headers={
            "Origin": f"http://127.0.0.1:{server.server_port}",
            "Content-Type": "application/json",
            "X-Relay-CSRF": app.csrf_token,
        })
        response = client.getresponse()
        result = json.loads(response.read())
        client.close()
        self.assertEqual(response.status, 200)
        self.assertEqual(result["provider"], "jev")

    def test_timing_projection_ignores_malformed_shadow_action(self):
        for action in ([], {}, None):
            with self.subTest(action=action):
                self.assertNotIn("jev_shadow_action", _project_evaluation_timing({"jev_shadow_action": action}))

    def test_timing_projection_allowlists_provider_and_stage_fields(self):
        projected = _project_evaluation_timing({
            "evaluator": "JEV",
            "route": "fallback",
            "fallback_reason": "timeout",
            "model": "jev-latest",
            "jev_duration_seconds": 0.12,
            "codex_duration_seconds": 1.4,
            "posted_to_receipt_seconds": 0.04,
            "posted_to_interpretation_seconds": 0.16,
            "source_verification_seconds": 0.03,
            "contract_resolution_seconds": 0.02,
            "broker_result_at": "2026-09-18T12:00:01+00:00",
            "broker_result_status": "partially_filled",
            "clock_uncertain": True,
            "semantic_confidence": 0.97,
            "eligibility_probability": 0.99,
            "received_at": "2026-09-18T12:00:00+00:00",
            "provider_response": "private-typesafe-key",
            "negative": -1,
        })
        self.assertEqual(projected["evaluator"], "jev")
        self.assertEqual(projected["route"], "fallback")
        self.assertEqual(projected["fallback_reason"], "timeout")
        self.assertEqual(projected["jev_duration_seconds"], 0.12)
        self.assertEqual(projected["posted_to_receipt_seconds"], 0.04)
        self.assertEqual(projected["posted_to_interpretation_seconds"], 0.16)
        self.assertEqual(projected["source_verification_seconds"], 0.03)
        self.assertEqual(projected["contract_resolution_seconds"], 0.02)
        self.assertEqual(projected["broker_result_status"], "partially_filled")
        self.assertTrue(projected["clock_uncertain"])
        self.assertNotIn("provider_response", projected)
        self.assertNotIn("negative", projected)

    def test_evaluation_metrics_aggregate_bounded_durable_rows(self):
        database = self.base / "state/relay.sqlite3"
        database.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(database)
        connection.executescript("""
            CREATE TABLE messages (
                id TEXT PRIMARY KEY, channel_id TEXT NOT NULL, source_group TEXT NOT NULL,
                timestamp TEXT NOT NULL, revision TEXT NOT NULL, body TEXT NOT NULL
            );
            CREATE TABLE events (
                id INTEGER PRIMARY KEY, message_id TEXT NOT NULL, revision TEXT NOT NULL,
                state TEXT NOT NULL, reason TEXT NOT NULL, decision TEXT, created_at TEXT NOT NULL
            );
        """)
        rows = [
            (
                "1000000000000000001", "live", "paper_order", "",
                {"route": "jev", "evaluator": "jev", "fast_path": True, "eligible": True,
                 "posted_to_decision_seconds": 1.2, "source_verification_seconds": .01,
                 "contract_resolution_seconds": .02},
            ),
            (
                "1000000000000000002", "live", "error", "deadline exceeded",
                {"route": "fallback", "evaluator": "codex", "fallback_reason": "jev_timeout",
                 "timed_out": True},
            ),
            (
                "1000000000000000003", "baseline", "wait", "",
                {"route": "codex", "path": "historical", "model_duration_seconds": 3.5},
            ),
            (
                "1000000000000000004", "live", "recovery_review", "",
                {"route": "codex", "path": "recovery", "model_duration_seconds": 4.5},
            ),
        ]
        for index, (message_id, ingestion, state, reason, timing) in enumerate(rows, 1):
            connection.execute(
                "INSERT INTO messages VALUES (?,?,?,?,?,?)",
                (message_id, "channel", "source", "2026-09-18T12:00:00+00:00", "r1",
                 json.dumps({"ingestion": ingestion, "source": "browser", "content": "fixture"})),
            )
            decision = {"evaluation_timing": timing}
            if timing.get("path") == "recovery":
                decision["recovery"] = {"evaluation_timing": timing}
            connection.execute(
                "INSERT INTO events VALUES (?,?,?,?,?,?,?)",
                (index, message_id, "r1", state, reason, json.dumps(decision),
                 "2026-09-18T12:00:01+00:00"),
            )
        connection.execute("INSERT INTO events VALUES (?,?,?,?,?,?,?)",
                           (5, "1000000000000000005", "r1", "observed", "", None, "2026-09-18T12:00:01+00:00"))
        connection.commit()
        connection.close()

        metrics = DashboardApp(self.path).evaluation_metrics()
        self.assertTrue(metrics["available"])
        self.assertEqual(metrics["sample_limit"], 1000)
        self.assertEqual(metrics["sample_count"], 5)
        live = metrics["buckets"]["live"]
        self.assertEqual(live["total"], 3)
        self.assertEqual(live["evaluated"], 2)
        self.assertEqual(live["routes"], {"jev": 1, "fallback": 1, "unknown": 1})
        self.assertEqual(live["fallback_reasons"], {"jev_timeout": 1})
        self.assertEqual(live["failures"], 1)
        self.assertEqual(live["timeouts"], 1)
        self.assertEqual(live["under_2s"], {"count": 1, "denominator": 3, "rate": 1 / 3})
        self.assertEqual(live["coverage"]["fast_path"], 1 / 3)
        self.assertEqual(live["coverage"]["eligible"], 1.0)
        self.assertEqual(live["timings"]["source_verification_seconds"]["p50"], .01)
        self.assertEqual(live["timings"]["contract_resolution_seconds"]["p50"], .02)
        self.assertIsNone(live["timings"]["fill_seconds"]["p50"])
        self.assertEqual(metrics["buckets"]["historical"]["total"], 1)
        self.assertEqual(metrics["buckets"]["recovery"]["total"], 1)


if __name__ == "__main__":
    unittest.main()
