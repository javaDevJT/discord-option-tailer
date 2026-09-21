import asyncio
import copy
import json
import os
import unittest
from unittest.mock import AsyncMock, patch

from relay.evaluation import EvaluationRouter, synthetic_test
from relay.interpreter import InterpretationError


MESSAGE = {
    "id": "message-2",
    "content": "Buy TSLA 352.5 put expiring 2026-09-04 at .87",
    "author_id": "trader",
    "source_group": "approved",
    "timestamp": "2026-09-04T14:00:00Z",
}
DECISION = {
    "action": "OPEN",
    "origin_message_id": "message-2",
    "contract": {"symbol": "TSLA", "expiry": "2026-09-04", "strike": "352.5", "option_type": "put"},
    "quantity": None,
    "fraction": None,
    "alert_price": ".87",
    "stop_price": None,
    "confidence": .99,
    "ambiguous": False,
    "reason": "Fixture explicit entry.",
    "evidence": [{"message_id": "message-2", "quote": MESSAGE["content"]}],
    "profit_only": False,
}


class Codex:
    def __init__(self, result=None):
        self.result = result or copy.deepcopy(DECISION)
        self.calls = []
        self.recovery_calls = 0

    async def interpret(self, message, context, positions):
        self.calls.append(message["id"])
        return copy.deepcopy(self.result)

    async def assess_recovery(self, *args):
        self.recovery_calls += 1
        return {"status": "uncertain", "confidence": .9, "reason": "Fixture", "evidence": DECISION["evidence"]}


def jev_response():
    result = {
        "model": "jev-latest",
        "answers": {
            "candidate": {"choice": "candidate_0", "probabilities": {"candidate_0": .99, "candidate_abstain": .01}, "confidence": .99},
            "actionable": {"choice": "yes", "probabilities": {"yes": .99, "no": .01}, "confidence": .99},
            "evidence": {"choice": "yes", "probabilities": {"yes": .99, "no": .01}, "confidence": .99},
            "profit_only": {"choice": "no", "probabilities": {"yes": .01, "no": .99}, "confidence": .99},
        },
    }
    for answer in result["answers"].values():
        answer["type"] = "choice"
    return result


class EvaluationTests(unittest.IsolatedAsyncioTestCase):
    async def test_codex_mode_preserves_validated_metadata(self):
        codex = Codex(copy.deepcopy(DECISION) | {"evaluation_timing": {"path": "direct", "attempts": 1}})
        router = EvaluationRouter({"evaluation": {"mode": "codex", "direct_entries": False}, "llm": {"model": "codex-fixture"}}, codex=codex)
        result = await router.interpret(MESSAGE, [], [])
        self.assertEqual(result["action"], "OPEN")
        self.assertEqual(result["evaluation_timing"]["route"], "codex")
        self.assertEqual(result["evaluation_timing"]["model"], "codex-fixture")
        self.assertIn("model_duration_seconds", result["evaluation_timing"])

    async def test_jev_mode_falls_back_on_image_ambiguity_with_reason(self):
        codex = Codex()
        seen = []

        def request(_request, _timeout):
            seen.append(True)
            return 200, {}, json.dumps(jev_response()).encode()

        config = {"evaluation": {"mode": "jev", "direct_entries": False, "api_key_file": "/does/not/exist"}}
        with patch.dict(os.environ, {"TYPESAFE_API_KEY": "abcdefgh"}, clear=False):
            router = EvaluationRouter(config, codex=codex)
            router.jev._request = request
            result = await router.interpret({**MESSAGE, "content": MESSAGE["content"] + ". See attached image for the expiry.", "attachments": [{"filename": "chart.png", "content_type": "image/png"}]}, [], [])
        self.assertEqual(result["evaluation_timing"]["route"], "fallback")
        self.assertEqual(result["evaluation_timing"]["fallback_reason"], "image_ambiguity")
        self.assertEqual(seen, [])
        self.assertEqual(codex.calls, ["message-2"])

    async def test_jev_success_has_confidence_mapping_and_no_codex_call(self):
        codex = Codex()

        def request(_request, _timeout):
            return 200, {}, json.dumps(jev_response()).encode()

        config = {"evaluation": {"mode": "jev", "direct_entries": False, "api_key_file": "/does/not/exist"}}
        with patch.dict(os.environ, {"TYPESAFE_API_KEY": "abcdefgh"}, clear=False):
            router = EvaluationRouter(config, codex=codex)
            router.jev._request = request
            result = await router.interpret(MESSAGE, [], [])
        timing = result["evaluation_timing"]
        self.assertEqual(timing["route"], "jev")
        self.assertEqual(timing["evaluator"], "jev")
        self.assertEqual(timing["semantic_confidence"], .99)
        self.assertEqual(timing["allocation_confidence"], .99)
        self.assertEqual(timing["selected_probability"], .99)
        self.assertEqual(codex.calls, [])

    async def test_shadow_returns_codex_and_calls_jev(self):
        codex = Codex()
        calls = []

        def request(_request, _timeout):
            calls.append(True)
            return 200, {}, json.dumps(jev_response()).encode()

        config = {"evaluation": {"mode": "jev_shadow", "api_key_file": "/does/not/exist"}, "llm": {"model": "codex-fixture"}}
        with patch.dict(os.environ, {"TYPESAFE_API_KEY": "abcdefgh"}, clear=False):
            router = EvaluationRouter(config, codex=codex)
            router.jev._request = request
            result = await router.interpret(MESSAGE, [], [])
        self.assertEqual(result["evaluation_timing"]["route"], "shadow")
        self.assertEqual(result["evaluation_timing"]["evaluator"], "codex")
        self.assertEqual(result["evaluation_timing"]["model"], "codex-fixture")
        self.assertEqual(result["evaluation_timing"]["jev_model"], "jev-latest")
        self.assertIsNotNone(result["evaluation_timing"]["codex_duration_seconds"])
        self.assertEqual(result["evaluation_timing"]["allocation_confidence"], result["confidence"])
        self.assertEqual(result["evaluation_timing"]["jev_shadow_action"], "OPEN")
        self.assertEqual(len(calls), 1)
        self.assertEqual(len(codex.calls), 1)

    async def test_recovery_always_serialized_codex(self):
        codex = Codex()
        router = EvaluationRouter({"evaluation": {"mode": "jev"}}, codex=codex)
        result = await router.assess_recovery(MESSAGE, [], [], DECISION, {"evaluated_at": MESSAGE["timestamp"]})
        self.assertEqual(result["status"], "uncertain")
        self.assertEqual(codex.recovery_calls, 1)

    async def test_synthetic_test_uses_one_benign_provider_request(self):
        reply = {"model": "jev-latest", "answers": {"connectivity": {"type": "choice", "choice": "online",
                 "confidence": .99, "probabilities": {"online": .99, "offline": .01}}}}
        with patch("relay.evaluation.JEVInterpreter._api_key", return_value="synthetic-key"), \
             patch("relay.evaluation.JEVInterpreter._request_once", new_callable=AsyncMock, return_value=reply) as request:
            result = await synthetic_test({"evaluation": {"mode": "jev"}})
        request.assert_awaited_once()
        payload = json.loads(request.call_args.args[0])
        self.assertEqual(set(payload["questions"]), {"connectivity"})
        self.assertNotIn("positions", payload["state"])
        self.assertTrue(result["synthetic"])
        self.assertEqual(result["state"], "connected")
        self.assertGreater(result["latency_ms"], 0)
        self.assertNotIn("api_key", str(result).lower())

    async def test_synthetic_test_sanitizes_malformed_answers(self):
        for answers in (None, [], "invalid"):
            with self.subTest(answers=answers), \
                 patch("relay.evaluation.JEVInterpreter._api_key", return_value="synthetic-key"), \
                 patch("relay.evaluation.JEVInterpreter._request_once", new_callable=AsyncMock, return_value={"answers": answers}):
                result = await synthetic_test({"evaluation": {"mode": "jev"}})
                self.assertEqual(result["state"], "unavailable")

    async def test_provider_slot_wait_is_included_in_deadline(self):
        codex = Codex()
        router = EvaluationRouter({"evaluation": {"mode": "jev", "direct_entries": False, "timeout_ms": 15}}, codex=codex)
        await router._jev_lock.acquire()
        await router._jev_lock.acquire()
        try:
            result = await asyncio.wait_for(router.interpret(MESSAGE, [], []), .2)
            self.assertEqual(result["evaluation_timing"]["fallback_reason"], "jev_timeout")
            self.assertEqual(len(codex.calls), 1)
        finally:
            router._jev_lock.release()
            router._jev_lock.release()
            await router.aclose()


if __name__ == "__main__":
    unittest.main()
