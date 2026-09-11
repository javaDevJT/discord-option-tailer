"""Offline boundary checks: python -m unittest discover -s tests -p test_interpreter.py."""

import asyncio
import copy
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
from relay.interpreter import _record

from relay.interpreter import (
    DECISION_SCHEMA, RECOVERY_SCHEMA, CodexInterpreter, InterpretationError,
    _strict_json, _recovery_context, safe_interpretation_reason, validate_decision, validate_recovery,
)


MESSAGE = {
    "id": "new", "content": "Buy TSLA 352.5 put expiring 2026-09-04 at .87",
    "author_id": "trader", "source_group": "approved", "timestamp": "2026-09-04T14:00:00Z",
}
DECISION = {
    "action": "OPEN", "origin_message_id": "new",
    "contract": {"symbol": "TSLA", "expiry": "2026-09-04", "strike": "352.5", "option_type": "put"},
    "quantity": None, "fraction": None, "alert_price": ".87", "stop_price": None,
    "confidence": .99, "ambiguous": False, "reason": "Explicit entry with complete contract.",
    "evidence": [{"message_id": "new", "quote": MESSAGE["content"]}],
}
RECOVERY_FACTS = {
    "evaluated_at": "2026-09-04T14:00:10Z",
    "original_timestamp": MESSAGE["timestamp"],
    "signal_age_seconds": 10,
    "context_truncated": False,
    "market_open": True,
    "snapshot_timestamp": "2026-09-04T14:00:08Z",
    "equity": "10000",
    "buying_power": "1000",
    "quote": {
        "contract": DECISION["contract"], "bid": ".80", "ask": ".90",
        "timestamp": "2026-09-04T14:00:09Z", "tradable": True, "multiplier": 100,
        "currency": "USD", "asset_type": "equity_option", "tick_size": ".01",
    },
    "affordable_quantity": 1,
    "blockers": [],
    "context_changed": False,
}
LATER = {
    "id": "later", "content": "Still tracking the TSLA put.", "author_id": "trader",
    "source_group": "approved", "timestamp": "2026-09-04T14:00:05Z",
}


class InterpreterChecks(unittest.TestCase):
    def test_default_expiry_only_for_entries_and_dates_follow_source_timezone(self):
        message = MESSAGE | {"content": "Buy TSLA 352.5 put at .87", "timestamp": "2026-09-05T00:30:00Z"}
        default = DECISION | {"contract": DECISION["contract"] | {"expiry": "nearest"},
            "evidence": [{"message_id": message["id"], "quote": message["content"]}]}
        self.assertEqual(validate_decision(default, message, []), default)
        self.assertEqual(_record(message)["market_date"], "2026-09-04")
        self.assertEqual(_record(message | {"timestamp": "2026-01-06T04:30:00Z"})["market_date"], "2026-01-05")
        for action in ("CLOSE", "REDUCE", "UPDATE_STOP", "WAIT"):
            with self.subTest(action=action), self.assertRaises(InterpretationError):
                validate_decision(default | {"action": action}, message, [])

    def test_status_sink_failure_preserves_codex_result_and_original_error(self):
        async def scenario():
            def failed_sink(_event):
                raise OSError("private status path")
            interpreter = CodexInterpreter({"llm": {"executable": sys.executable}}, on_status=failed_sink)
            with patch.object(interpreter, "_interpret", return_value=DECISION):
                self.assertEqual(await interpreter.interpret(MESSAGE, [], []), DECISION)
            original = InterpretationError("ChatGPT authentication needs attention")
            with patch.object(interpreter, "_interpret", side_effect=original):
                with self.assertRaises(InterpretationError) as raised:
                    await interpreter.interpret(MESSAGE, [], [])
            self.assertIs(raised.exception, original)
        asyncio.run(scenario())

    def test_retry_revalidates_structured_output_then_publishes_ready(self):
        invalid = DECISION | {"evidence": [{"message_id": "invented", "quote": "not supplied"}]}
        events = []
        interpreter = CodexInterpreter({"llm": {"executable": sys.executable}}, on_status=events.append)
        with patch.object(interpreter, "_interpret", side_effect=[invalid, DECISION]) as run:
            result = asyncio.run(interpreter.interpret(MESSAGE, [], []))
        self.assertEqual(result, DECISION)
        self.assertEqual(run.call_count, 2)
        self.assertEqual(interpreter.last_attempts, 2)
        self.assertEqual(events, [{"component": "codex", "state": "ready"}])

    def test_retry_exhaustion_has_safe_code_and_attempt_count(self):
        invalid = DECISION | {"evidence": [{"message_id": "invented", "quote": "private-token"}]}
        interpreter = CodexInterpreter({"llm": {"executable": sys.executable}})
        with patch.object(interpreter, "_interpret", side_effect=[invalid, invalid]) as run:
            with self.assertRaises(InterpretationError) as raised:
                asyncio.run(interpreter.interpret(MESSAGE, [], []))
        self.assertEqual(run.call_count, 2)
        self.assertEqual((raised.exception.code, raised.exception.attempts), ("invalid_evidence", 2))
        reason = safe_interpretation_reason(raised.exception)
        self.assertIn("code=invalid_evidence", reason)
        self.assertIn("attempts=2", reason)
        self.assertNotIn("private-token", reason)

    def test_auth_and_quota_failures_do_not_retry(self):
        for error, code in (
            ("ChatGPT authentication needs attention", "auth_required"),
            ("subscription usage limit reached", "quota_exhausted"),
        ):
            with self.subTest(code=code):
                interpreter = CodexInterpreter({"llm": {"executable": sys.executable}})
                with patch.object(interpreter, "_interpret", side_effect=InterpretationError(error)) as run:
                    with self.assertRaises(InterpretationError) as raised:
                        asyncio.run(interpreter.interpret(MESSAGE, [], []))
                self.assertEqual(run.call_count, 1)
                self.assertEqual((raised.exception.code, raised.exception.attempts), (code, 1))

    def test_fast_and_standard_cli_preferences_are_explicit_and_bounded(self):
        for tier, expected_service, expected_fast in (("fast", 'service_tier="fast"', 'features.fast_mode=true'),
                                                       ("standard", None, 'features.fast_mode=false')):
            with self.subTest(tier=tier), tempfile.TemporaryDirectory() as directory:
                home = Path(directory) / "source-home"
                home.mkdir()
                (home / "auth.json").write_text("test credential placeholder")
                (home / "config.toml").write_text('model = "user-default-model"\nmodel_provider = "private-proxy"\n')
                interpreter = CodexInterpreter({"llm": {"executable": sys.executable, "model": "fixture-model",
                                                          "reasoning_effort": "medium", "service_tier": tier}})
                interpreter.source_home = home
                captured = []

                def execute(*args, **kwargs):
                    captured.append(args)
                    command, env, cwd = args[:3]
                    input_bytes = kwargs.get("input_bytes")
                    if "login" in command:
                        return subprocess.CompletedProcess(command, 0, b"Logged in using ChatGPT\n", b"")
                    output = Path(command[command.index("--output-last-message") + 1])
                    output.write_text(json.dumps(DECISION))
                    events = [
                        {"type": "thread.started", "thread_id": "ephemeral"},
                        {"type": "turn.started"},
                        {"type": "item.completed", "item": {"type": "agent_message", "text": json.dumps(DECISION)}},
                        {"type": "turn.completed", "usage": {}},
                    ]
                    return subprocess.CompletedProcess(command, 0, "\n".join(json.dumps(e) for e in events).encode(), b"")

                with patch.object(interpreter, "_run_process", side_effect=execute):
                    asyncio.run(interpreter.interpret(MESSAGE, [], []))
                args = captured[1][0]
                self.assertIn('model_reasoning_effort="medium"', args)
                self.assertIn(expected_fast, args)
                if expected_service:
                    self.assertIn(expected_service, args)
                else:
                    self.assertNotIn('service_tier="standard"', args)

    def test_normal_and_recovery_requests_publish_provider_health(self):
        async def scenario():
            events = []
            interpreter = CodexInterpreter({"llm": {"executable": sys.executable}}, on_status=events.append)
            assessment = {"status": "uncertain", "confidence": .9, "reason": "Fixture assessment",
                          "evidence": DECISION["evidence"]}
            operations = (
                (lambda: interpreter.interpret(MESSAGE, [], []), DECISION),
                (lambda: interpreter.assess_recovery(MESSAGE, [LATER], [], DECISION, RECOVERY_FACTS), assessment),
            )
            for operation, result in operations:
                for error, state in (("ChatGPT authentication needs attention", "auth_required"),
                                     ("network connection unavailable", "unavailable")):
                    with patch.object(interpreter, "_interpret", side_effect=InterpretationError(error)):
                        with self.assertRaises(InterpretationError):
                            await operation()
                    self.assertEqual(events[-1], {"component": "codex", "state": state})
                with patch.object(interpreter, "_interpret", return_value=copy.deepcopy(result)):
                    await operation()
                self.assertEqual(events[-1], {"component": "codex", "state": "ready"})
            self.assertTrue(all(set(event) == {"component", "state"} for event in events))
        asyncio.run(scenario())

    def test_recovery_keeps_old_origin_when_context_is_full(self):
        origin = dict(MESSAGE, id="origin", edited_timestamp="2026-09-04T14:00:02Z")
        context = [origin] + [dict(LATER, id=str(i)) for i in range(65)]
        _, history = _recovery_context(MESSAGE, context, "origin")
        self.assertEqual(len(history), 60)
        self.assertEqual(history[0]["id"], "origin")
        self.assertEqual(history[0]["edited_timestamp"], origin["edited_timestamp"])
        self.assertEqual(history[-1]["id"], "64")

    def test_valid_contract_and_boundary_quotes(self):
        self.assertEqual(validate_decision(copy.deepcopy(DECISION), MESSAGE, []), DECISION)
        message = {"id": "new", "content": "", "embeds": [{"title": MESSAGE["content"]}]}
        self.assertEqual(validate_decision(copy.deepcopy(DECISION), message, []), DECISION)

    def test_rejects_malformed_decisions(self):
        alterations = [
            {"extra": True}, {"action": "BUY"}, {"action": ["OPEN"]},
            {"origin_message_id": None}, {"origin_message_id": 1}, {"origin_message_id": ""},
            {"origin_message_id": "missing"},
            {"quantity": True}, {"quantity": 0}, {"quantity": 1.5},
            {"fraction": 0}, {"fraction": 1.1}, {"fraction": True},
            {"confidence": float("nan")}, {"confidence": True}, {"confidence": 2},
            {"alert_price": 1.5}, {"alert_price": "Infinity"}, {"stop_price": "-1"},
            {"ambiguous": "false"}, {"ambiguous": True}, {"contract": None},
            {"reason": ""}, {"evidence": []},
            {"evidence": [{"message_id": "new", "quote": "invented quote"}]},
            {"evidence": [{"message_id": "unknown", "quote": MESSAGE["content"]}]},
            {"evidence": [{"message_id": "new", "quote": " "}]},
            {"evidence": [{"message_id": "new", "quote": MESSAGE["content"], "extra": 1}]},
        ]
        for change in alterations:
            with self.subTest(change=change), self.assertRaises(InterpretationError):
                validate_decision(DECISION | change, MESSAGE, [])
        incomplete = copy.deepcopy(DECISION)
        del incomplete["quantity"]
        with self.assertRaises(InterpretationError):
            validate_decision(incomplete, MESSAGE, [])

    def test_invalid_contract_shapes(self):
        for change in ({"expiry": "2026-02-30"}, {"expiry": "20260904"}, {"symbol": "tsla"},
                       {"strike": "0"}, {"strike": 352.5}, {"option_type": "bullish"}, {"extra": 1}):
            with self.subTest(change=change), self.assertRaises(InterpretationError):
                validate_decision(DECISION | {"contract": DECISION["contract"] | change}, MESSAGE, [])

    def test_context_alone_cannot_authorize_current_trade(self):
        old = MESSAGE | {"id": "old"}
        output = DECISION | {"evidence": [{"message_id": "old", "quote": MESSAGE["content"]}]}
        with self.assertRaises(InterpretationError):
            validate_decision(output, MESSAGE, [old])
        output["evidence"].append({"message_id": "new", "quote": "Buy TSLA"})
        self.assertEqual(validate_decision(output, MESSAGE, [old]), output)

    def test_edits_and_cross_source_context_cannot_supply_trade_evidence(self):
        edited = MESSAGE | {"content": "Cancelled that entry"}
        with self.assertRaises(InterpretationError):
            validate_decision(DECISION, edited, [MESSAGE])
        other = MESSAGE | {"id": "other", "source_group": "different-trader"}
        decision = DECISION | {"evidence": DECISION["evidence"] + [{"message_id": "other", "quote": "Buy TSLA"}]}
        with self.assertRaises(InterpretationError):
            validate_decision(decision, MESSAGE, [other])

    def test_wait_ignore_and_stop_semantics(self):
        wait = DECISION | {"action": "WAIT", "origin_message_id": None, "contract": None, "ambiguous": True}
        self.assertEqual(validate_decision(wait, MESSAGE, []), wait)
        ignore = wait | {"action": "IGNORE", "evidence": []}
        self.assertEqual(validate_decision(ignore, MESSAGE, []), ignore)
        with self.assertRaises(InterpretationError):
            validate_decision(DECISION | {"action": "UPDATE_STOP"}, MESSAGE, [])

    def test_correction_retains_original_entry_trigger(self):
        entry = MESSAGE | {"id": "entry", "content": "Buy TSLA 352.5 expiring 2026-09-04 at .87"}
        correction = MESSAGE | {"id": "correction", "content": "put*", "timestamp": "2026-09-04T14:00:03Z"}
        decision = DECISION | {
            "origin_message_id": "entry",
            "evidence": [
                {"message_id": "entry", "quote": entry["content"]},
                {"message_id": "correction", "quote": correction["content"]},
            ],
        }
        result = validate_decision(decision, correction, [entry])
        self.assertEqual(result["origin_message_id"], "entry")
        with self.assertRaises(InterpretationError):
            validate_decision(decision | {"evidence": decision["evidence"][1:]}, correction, [entry])
        with self.assertRaises(InterpretationError):
            validate_decision(decision, correction, [entry | {"source_group": "other"}])

    def test_exit_requires_origin_but_stop_update_may_use_null(self):
        for action in ("REDUCE", "CLOSE"):
            with self.subTest(action=action), self.assertRaises(InterpretationError):
                validate_decision(DECISION | {"action": action, "origin_message_id": None}, MESSAGE, [])
        stop = DECISION | {"action": "UPDATE_STOP", "origin_message_id": None, "stop_price": ".60"}
        self.assertEqual(validate_decision(stop, MESSAGE, []), stop)

    def test_duplicate_fields_nonfinite_and_nonjson_rejected(self):
        for content in ('{"a": 1, "a": 2}', '{"confidence": NaN}', '```json\n{}\n```', '{} trailing'):
            with self.subTest(content=content), self.assertRaises(InterpretationError):
                _strict_json(content)

class CodexInterpreterChecks(unittest.TestCase):
    def test_pictured_expiry_reaches_codex_without_signed_url_and_is_preserved(self):
        message = MESSAGE | {"content": "Buy TSLA 352.5 put at .87", "attachments": [{
            "filename": "alert.png", "content_type": "image/png",
            "url": "https://cdn.discordapp.com/attachments/123456789012345678/234567890123456789/alert.png?ex=private-fixture",
        }]}
        decision = DECISION | {"contract": DECISION["contract"] | {"expiry": "2026-09-18"},
            "evidence": [{"message_id": message["id"], "quote": message["content"]}]}
        with tempfile.TemporaryDirectory() as directory:
            instance = self.interpreter(directory)
            def response(args, env, cwd, input_bytes=None):
                if "exec" in args:
                    self.assertIn("--image", args)
                    payload = json.loads(input_bytes)
                    self.assertEqual(payload["image_inputs"], [{"message_id": "new"}])
                    self.assertNotIn("private-fixture", input_bytes.decode())
                    self.assertNotIn("cdn.discordapp.com", input_bytes.decode())
                return self.response(args, env, cwd, input_bytes, decision=decision)
            with patch.object(instance, "_run_process", side_effect=response), patch("relay.interpreter.download_images", return_value=[Path(directory) / "image.png"]):
                result = asyncio.run(instance.interpret(message, [], []))
            self.assertEqual(result["contract"]["expiry"], "2026-09-18")
            with patch.object(instance, "_run_process", side_effect=response), patch("relay.interpreter.download_images", side_effect=ValueError("unreadable")):
                with self.assertRaisesRegex(InterpretationError, "no expiry was assumed"):
                    asyncio.run(instance.interpret(message, [], []))

    def interpreter(self, directory, **llm):
        home = Path(directory) / "source-home"
        home.mkdir()
        (home / "auth.json").write_text("test credential placeholder")
        (home / "config.toml").write_text('model = "user-default-model"\nmodel_provider = "private-proxy"\n')
        instance = CodexInterpreter({"llm": {"executable": sys.executable, **llm}})
        instance.source_home = home
        return instance

    def response(self, args, env, cwd, input_bytes=None, *, events=None, decision=None, assessment=None):
        if "login" in args:
            self.assertIsNone(input_bytes)
            runtime_auth = Path(env["CODEX_HOME"]) / "auth.json"
            self.assertTrue(runtime_auth.is_symlink())
            return subprocess.CompletedProcess(args, 0, b"Logged in using ChatGPT\n", b"")
        output = Path(args[args.index("--output-last-message") + 1])
        result = decision if decision is not None else assessment if assessment is not None else DECISION
        output.write_text(json.dumps(result))
        default_events = [
            {"type": "thread.started", "thread_id": "ephemeral"},
            {"type": "turn.started"},
            {"type": "item.completed", "item": {"type": "agent_message", "text": json.dumps(result)}},
            {"type": "turn.completed", "usage": {"input_tokens": 500, "cached_input_tokens": 100, "output_tokens": 100}},
        ]
        return subprocess.CompletedProcess(args, 0, "\n".join(json.dumps(e) for e in (events if events is not None else default_events)).encode(), b"")

    def test_subscription_uses_isolated_auth_and_current_default(self):
        with tempfile.TemporaryDirectory() as directory:
            interpreter = self.interpreter(directory)
            captured = []

            def execute(*args, **kwargs):
                captured.append(args)
                return self.response(*args, **kwargs)

            with patch.dict(os.environ, {"OPENAI_API_KEY": "do-not-use", "OPENAI_BASE_URL": "http://private-proxy", "CODEX_APP_TOOLS_PIPE_PATH": "/private/tool-socket", "HTTPS_PROXY": "http://transport-proxy"}), patch.object(interpreter, "_run_process", side_effect=execute):
                result = asyncio.run(interpreter.interpret(MESSAGE, [], []))
            args, env, cwd, data = captured[1]
            self.assertEqual(result, DECISION)
            self.assertEqual(interpreter.last_authentication, "chatgpt_subscription")
            self.assertEqual(interpreter.last_model, "user-default-model")
            self.assertEqual(interpreter.last_usage["output_tokens"], 100)
            self.assertEqual(args[args.index("--model") + 1], "user-default-model")
            self.assertEqual(args[args.index("--sandbox") + 1], "read-only")
            for required in ("--ignore-user-config", "--ignore-rules", "--ephemeral", "--strict-config", "--output-schema", 'forced_login_method="chatgpt"', "features.shell_tool=false", "features.plugins=false", "features.skip_host_skill_discovery=true", "project_doc_max_bytes=0"):
                self.assertIn(required, args)
            self.assertEqual(args[-1], "-")
            self.assertNotIn(MESSAGE["content"], " ".join(args))
            self.assertEqual(json.loads(data)["current_message"]["content"], MESSAGE["content"])
            self.assertNotIn("OPENAI_API_KEY", env)
            self.assertNotIn("OPENAI_BASE_URL", env)
            self.assertNotIn("CODEX_APP_TOOLS_PIPE_PATH", env)
            self.assertEqual(env["HTTPS_PROXY"], "http://transport-proxy")
            self.assertFalse(Path(env["CODEX_HOME"]).exists())
            self.assertEqual((interpreter.source_home / "auth.json").read_text(), "test credential placeholder")

    def test_explicit_model_and_no_api_auth_fallback(self):
        with tempfile.TemporaryDirectory() as directory:
            interpreter = self.interpreter(directory, model="explicit-model")
            with patch.object(interpreter, "_run_process", side_effect=self.response):
                asyncio.run(interpreter.interpret(MESSAGE, [], []))
            self.assertEqual(interpreter.last_model, "explicit-model")
            with patch.object(interpreter, "_run_process", return_value=subprocess.CompletedProcess([], 0, b"Logged in using an API key", b"")) as run, self.assertRaises(InterpretationError):
                asyncio.run(interpreter.interpret(MESSAGE, [], []))
            self.assertEqual(run.call_count, 1)

    def test_subscription_payload_is_bounded_and_omits_private_metadata(self):
        image_url = "https://cdn.discordapp.com/attachments/123456789012345678/234567890123456789/chart.png?token=private-fixture"
        message = MESSAGE | {
            "attachments": [{"filename": "chart.png", "content_type": "image/png", "url": image_url}],
            "embeds": [{"title": "Entry", "image": {"url": image_url}}],
        }
        context = [{"id": str(i), "content": "x" * 7000, "source_group": "approved"} for i in range(70)]
        positions = [{"contract": DECISION["contract"], "quantity": 1, "bot_owned": True, "account_number": "private", "source_group": "approved"}]
        payloads = []

        def execute(args, env, cwd, input_bytes=None):
            if "exec" in args:
                self.assertEqual(json.loads(Path(args[args.index("--output-schema") + 1]).read_text()), DECISION_SCHEMA)
                payloads.append(json.loads(input_bytes))
            return self.response(args, env, cwd, input_bytes)

        with tempfile.TemporaryDirectory() as directory:
            interpreter = self.interpreter(directory)
            with patch.object(interpreter, "_run_process", side_effect=execute), patch("relay.interpreter.download_images", return_value=[Path(directory) / "image.png"]):
                result = asyncio.run(interpreter.interpret(message, context, positions))
        self.assertEqual(result, DECISION)
        self.assertEqual(len(payloads[0]["context"]), 60)
        self.assertEqual(len(payloads[0]["context"][0]["content"]), 6000)
        self.assertEqual(payloads[0]["current_message"]["source_group"], "approved")
        self.assertNotIn("private", json.dumps(payloads[0]))

    def test_recovery_uses_alternate_schema_and_sanitizes_facts(self):
        assessment = {
            "status": "viable", "confidence": .8, "reason": "No later exit was observed; broker facts are current.",
            "evidence": [
                {"message_id": MESSAGE["id"], "quote": MESSAGE["content"]},
                {"message_id": LATER["id"], "quote": LATER["content"]},
            ],
        }
        foreign = LATER | {"id": "foreign", "source_group": "other", "content": "secret provider token"}
        facts = RECOVERY_FACTS | {"account_id": "private-account", "provider_metadata": {"token": "secret"}}
        captured = []
        schemas = []

        def execute(*args, **kwargs):
            captured.append(args)
            if "exec" in args[0]:
                schemas.append(json.loads(Path(args[0][args[0].index("--output-schema") + 1]).read_text()))
            return self.response(*args, **kwargs, assessment=assessment)

        with tempfile.TemporaryDirectory() as directory:
            interpreter = self.interpreter(directory)
            with patch.object(interpreter, "_run_process", side_effect=execute):
                result = asyncio.run(interpreter.assess_recovery(MESSAGE, [LATER, foreign], [], DECISION, facts))
        self.assertEqual(result, assessment)
        args, _env, _cwd, data = captured[1]
        self.assertEqual(schemas, [RECOVERY_SCHEMA])
        payload = json.loads(data)
        self.assertEqual([record["id"] for record in payload["context"]], ["later"])
        self.assertEqual(payload["facts"]["original_timestamp"], MESSAGE["timestamp"])
        self.assertNotIn("private-account", json.dumps(payload))
        self.assertNotIn("provider_metadata", json.dumps(payload))

    def test_recovery_retries_output_and_evidence_validation_once(self):
        assessment = {
            "status": "uncertain", "confidence": .5, "reason": "Insufficient later evidence.",
            "evidence": [
                {"message_id": MESSAGE["id"], "quote": MESSAGE["content"]},
                {"message_id": LATER["id"], "quote": LATER["content"]},
            ],
        }
        invalid = assessment | {"evidence": [{"message_id": LATER["id"], "quote": LATER["content"]}]}
        with tempfile.TemporaryDirectory() as directory:
            interpreter = self.interpreter(directory)
            with patch.object(interpreter, "_interpret", side_effect=[invalid, assessment]) as run:
                result = asyncio.run(interpreter.assess_recovery(MESSAGE, [LATER], [], DECISION, RECOVERY_FACTS))
        self.assertEqual(result, assessment)
        self.assertEqual(run.call_count, 2)
        self.assertEqual(interpreter.last_attempts, 2)

    def test_recovery_requires_original_and_current_same_source_evidence(self):
        invalid = {
            "status": "invalidated", "confidence": .9, "reason": "The source later cancelled the proposal.",
            "evidence": [{"message_id": MESSAGE["id"], "quote": MESSAGE["content"]}],
        }
        foreign = LATER | {"id": "foreign", "source_group": "other"}
        invalid["evidence"].append({"message_id": "foreign", "quote": foreign["content"]})
        with tempfile.TemporaryDirectory() as directory:
            interpreter = self.interpreter(directory)
            with patch.object(interpreter, "_run_process", side_effect=lambda *args, **kwargs: self.response(*args, **kwargs, assessment=invalid)), self.assertRaises(InterpretationError):
                asyncio.run(interpreter.assess_recovery(MESSAGE, [LATER, foreign], [], DECISION, RECOVERY_FACTS))

    def test_recovery_cannot_claim_viable_with_blocked_facts(self):
        assessment = {
            "status": "viable", "confidence": .95, "reason": "The proposal still looks actionable.",
            "evidence": [{"message_id": MESSAGE["id"], "quote": MESSAGE["content"]}],
        }
        facts = RECOVERY_FACTS | {"blockers": ["The current quote is stale", "secret-token"]}
        with tempfile.TemporaryDirectory() as directory:
            interpreter = self.interpreter(directory)
            with patch.object(interpreter, "_run_process", side_effect=lambda *args, **kwargs: self.response(*args, **kwargs, assessment=assessment)):
                result = asyncio.run(interpreter.assess_recovery(MESSAGE, [LATER], [], DECISION, facts))
        self.assertEqual(result["status"], "uncertain")
        self.assertNotIn("secret-token", json.dumps(result))

    def test_recovery_validator_requires_current_message_quote(self):
        assessment = {
            "status": "uncertain", "confidence": .5, "reason": "Insufficient evidence.",
            "evidence": [{"message_id": LATER["id"], "quote": LATER["content"]}],
        }
        with self.assertRaises(InterpretationError):
            validate_recovery(assessment, MESSAGE, [LATER])

    def test_status_checks_login_without_invoking_a_model(self):
        with tempfile.TemporaryDirectory() as directory:
            interpreter = self.interpreter(directory)
            with patch.object(interpreter, "_run_process", return_value=subprocess.CompletedProcess([], 0, b"Logged in using ChatGPT", b"")) as run:
                status = asyncio.run(interpreter.subscription_status())
            self.assertTrue(status["authenticated"])
            self.assertTrue(status["isolated_execution_available"])
            self.assertEqual(status["method"], "chatgpt_subscription")
            self.assertEqual(run.call_args.args[0][1:], ["login", "status"])

    def test_timeout_kills_child_group_and_redacts_diagnostics(self):
        with tempfile.TemporaryDirectory() as directory:
            interpreter = self.interpreter(directory)
            with patch("relay.interpreter.subprocess.Popen") as popen, patch("relay.interpreter.os.killpg") as kill:
                process = popen.return_value
                process.pid = 123
                process.poll.return_value = None
                process.communicate.side_effect = [subprocess.TimeoutExpired(["codex"], 1), (b"", b"error sending request private-token")]
                with self.assertRaises(InterpretationError) as error:
                    interpreter._run_process(["codex"], {}, directory)
            kill.assert_called_once()
            self.assertIn("network connection unavailable", str(error.exception))
            self.assertNotIn("private-token", str(error.exception))

    def test_tools_failed_turn_and_unknown_events_are_rejected(self):
        cases = [
            [{"type": "item.completed", "item": {"type": "command_execution", "command": "unsafe"}}],
            [{"type": "item.started", "item": {"type": "mcp_tool_call"}}],
            [{"type": "turn.failed"}], [{"type": "error"}], [{"type": "new_tool_event"}], [],
        ]
        for events in cases:
            with self.subTest(events=events), tempfile.TemporaryDirectory() as directory:
                interpreter = self.interpreter(directory)
                with patch.object(interpreter, "_run_process", side_effect=lambda *args, **kwargs: self.response(*args, **kwargs, events=events)), self.assertRaises(InterpretationError):
                    asyncio.run(interpreter.interpret(MESSAGE, [], []))

    def test_reauth_is_classified_from_cli_stderr_or_failed_json_without_retry(self):
        for mode in ("stderr", "json"):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as directory:
                events = []
                interpreter = self.interpreter(directory, service_tier="standard")
                interpreter.on_status = events.append

                def execute(args, env, cwd, input_bytes=None):
                    if "login" in args:
                        return self.response(args, env, cwd, input_bytes)
                    if mode == "stderr":
                        return subprocess.CompletedProcess(args, 1, b"", b"ChatGPT authentication failed; run codex login")
                    failed = [{"type": "turn.started"}, {"type": "turn.failed", "error": {"message": "Please reauthenticate with ChatGPT"}}]
                    return self.response(args, env, cwd, input_bytes, events=failed)

                with patch.object(interpreter, "_run_process", side_effect=execute) as run:
                    with self.assertRaises(InterpretationError) as raised:
                        asyncio.run(interpreter.interpret(MESSAGE, [], []))
                self.assertEqual(run.call_count, 2)  # login status plus one model request
                self.assertEqual((raised.exception.code, raised.exception.attempts), ("auth_required", 1))
                self.assertEqual(events[-1], {"component": "codex", "state": "auth_required"})

    def test_only_exact_disabled_code_mode_startup_notice_is_accepted(self):
        notice = {"type": "item.completed", "item": {"type": "error", "message": CodexInterpreter._DISABLED_CODE_MODE_NOTICE}}
        events = [notice, {"type": "turn.started"}, {"type": "turn.completed", "usage": {}}]
        with tempfile.TemporaryDirectory() as directory:
            interpreter = self.interpreter(directory)
            with patch.object(interpreter, "_run_process", side_effect=lambda *args, **kwargs: self.response(*args, **kwargs, events=events)):
                self.assertEqual(asyncio.run(interpreter.interpret(MESSAGE, [], [])), DECISION)
            self.assertEqual(interpreter.last_notices, ["code_mode_disabled"])
            for invalid in ([{"type": "turn.started"}, notice, {"type": "turn.completed"}], [{"type": "item.completed", "item": {"type": "error", "message": "other failure"}}, {"type": "turn.started"}, {"type": "turn.completed"}]):
                with patch.object(interpreter, "_run_process", side_effect=lambda *args, **kwargs: self.response(*args, **kwargs, events=invalid)), self.assertRaises(InterpretationError):
                    asyncio.run(interpreter.interpret(MESSAGE, [], []))

    def test_codex_output_uses_same_evidence_validator(self):
        with tempfile.TemporaryDirectory() as directory:
            interpreter = self.interpreter(directory)
            invalid = DECISION | {"evidence": [{"message_id": "new", "quote": "invented"}]}
            with patch.object(interpreter, "_run_process", side_effect=lambda *args, **kwargs: self.response(*args, **kwargs, decision=invalid)), self.assertRaises(InterpretationError):
                asyncio.run(interpreter.interpret(MESSAGE, [], []))

    def test_missing_subscription_and_failed_execution_hold(self):
        with tempfile.TemporaryDirectory() as directory:
            interpreter = self.interpreter(directory)

            def failure(args, env, cwd, input_bytes=None):
                if "login" in args:
                    return self.response(args, env, cwd)
                return subprocess.CompletedProcess(args, 1, b"", b"private request contents and private-token")

            with patch.object(interpreter, "_run_process", side_effect=failure), self.assertRaises(InterpretationError) as error:
                asyncio.run(interpreter.interpret(MESSAGE, [], []))
            self.assertNotIn("private", str(error.exception))
            (interpreter.source_home / "auth.json").unlink()
            with patch.object(interpreter, "_run_process") as run, self.assertRaises(InterpretationError):
                asyncio.run(interpreter.interpret(MESSAGE, [], []))
            run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
