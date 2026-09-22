import asyncio
import copy
import json
import os
import tempfile
import unittest
import httpx
from pathlib import Path
from unittest.mock import patch

from relay.jev import (
    JEVError,
    JEVInterpreter,
    bounded_entry_candidate,
    credential_configured,
    evaluation_settings,
    extract_candidates,
    prepare_candidates,
)


MESSAGE = {
    "id": "message-1",
    "content": "Buy TSLA 352.5 put expiring 2026-09-04 at .87",
    "author_id": "trader",
    "source_group": "approved",
    "timestamp": "2026-09-04T14:00:00Z",
}


def response_for(choice="candidate_0"):
    return {
        "model": "jev-latest",
        "answers": {
            "candidate": {"type": "choice", "choice": choice, "probabilities": {choice: .99, "candidate_abstain": .01}, "confidence": .99},
            "actionable": {"type": "choice", "choice": "yes", "probabilities": {"yes": .99, "no": .01}, "confidence": .99},
            "evidence": {"type": "choice", "choice": "yes", "probabilities": {"yes": .99, "no": .01}, "confidence": .99},
            "profit_only": {"type": "choice", "choice": "no", "probabilities": {"yes": .01, "no": .99}, "confidence": .99},
        },
    }


class Broker:
    def __init__(self):
        self.calls = []

    async def nearest_expiry(self, contract):
        self.calls.append(contract)
        return contract | {"expiry": "2026-09-04"}


class JEVTests(unittest.IsolatedAsyncioTestCase):
    def adapter(self, *, reply=None, timeout_ms=1200, broker=None, events=None):
        patcher = patch.dict(os.environ, {"TYPESAFE_API_KEY": "synthetic-test-key"})
        patcher.start()
        self.addCleanup(patcher.stop)
        calls = []
        def request(req, _timeout):
            calls.append(json.loads(req.data))
            return 200, {}, json.dumps(reply if reply is not None else response_for()).encode()
        adapter = JEVInterpreter({"evaluation": {"timeout_ms": timeout_ms, "api_key_file": "/does/not/exist"}},
                                 broker=broker, request=request, on_status=events.append if events is not None else None)
        self.addAsyncCleanup(adapter.aclose)
        return adapter, calls


    async def test_unsafe_effects_context_and_conditional_text_bypass_provider(self):
        for prefix in ("Sell to open", "Buy to close", "Write", "Short", "Do not buy", "Yesterday I bought", "If it dips buy"):
            adapter, calls = self.adapter()
            with self.subTest(prefix=prefix), self.assertRaises(JEVError):
                await adapter.interpret(dict(MESSAGE, content=f"{prefix} TSLA 352.5 put expiring 2026-09-04 at .87"), [], [])
            self.assertFalse(calls)
        for attachment in ({"filename": "chart.gif"}, {"filename": "unknown-file"}):
            adapter, calls = self.adapter()
            image_message = dict(
                MESSAGE,
                content="Buy TSLA 352.5 put at .87; see attached image for the expiry",
                attachments=[attachment],
            )
            with self.subTest(attachment=attachment), self.assertRaises(JEVError):
                await adapter.interpret(image_message, [], [])
            self.assertFalse(calls)
    async def test_fraction_exit_and_at_price_have_exact_candidates(self):
        for text, action, fraction in (
            ("Sell 50% TSLA 352.5 put expiring 2026-09-04", "REDUCE", .5),
            ("Close 50% TSLA 352.5 put expiring 2026-09-04", "REDUCE", .5),
            ("Close all TSLA 352.5 put expiring 2026-09-04", "CLOSE", None),
            ("Buy TSLA 352.5 put expiring 2026-09-04 @ .87", "OPEN", None),
        ):
            adapter, calls = self.adapter()
            with self.subTest(text=text):
                decision = await adapter.interpret(dict(MESSAGE, content=text), [], [])
                self.assertEqual(decision["action"], action)
                self.assertEqual(decision["fraction"], fraction)
                for question in calls[0]["questions"].values():
                    self.assertEqual(set(question), {"type", "instructions", "criteria"})
                self.assertNotIn("win_probability", json.dumps(decision["_jev_result"].decision))

    def test_market_close_commentary_is_not_an_exit_candidate(self):
        message = dict(
            MESSAGE,
            id="dram-commentary",
            content=(
                "DRAM 65 call expiring 2026-10-16 up +$50 per contract "
                "heading into market close. I am looking for $65 as my first target"
            ),
        )
        result = extract_candidates(message, [])
        self.assertIsNone(result.reason)
        self.assertEqual(result.candidates[0]["action"], "WAIT")
        self.assertFalse(result.candidates[0]["profit_only"])

    def test_explicit_close_forms_remain_exit_candidates(self):
        for content in (
            "Close SPY 500 call expiring 2026-09-04",
            "Closed SPY 500 call expiring 2026-09-04",
            "Close remaining SPY 500 call expiring 2026-09-04",
            "Time to close SPY 500 call expiring 2026-09-04",
            "All out SPY 500 call expiring 2026-09-04",
            "SPY 500 call expiring 2026-09-04 - closed",
            "SPY 500 call expiring 2026-09-04\nCLOSED",
        ):
            with self.subTest(content=content):
                result = extract_candidates(dict(MESSAGE, content=content), [])
                self.assertIsNone(result.reason)
                self.assertEqual(result.candidates[0]["action"], "CLOSE")

    async def test_invalid_native_answers_never_accept_an_action(self):
        malformed = []
        response = response_for(); response["answers"]["candidate"].pop("type"); malformed.append(response)
        response = response_for(); response["answers"]["candidate"].update(choice="OPEN", probabilities={"OPEN": 0., "candidate_0": 1.}); malformed.append(response)
        response = response_for(); response["answers"]["candidate"]["probabilities"]["candidate_abstain"] = .3; malformed.append(response)
        response = response_for(); response["answers"]["actionable"]["choice"] = "no"; malformed.append(response)
        response = response_for(); response["answers"]["evidence"]["confidence"] = .1; malformed.append(response)
        response = response_for(); response["answers"]["candidate"].update(choice="candidate_abstain", probabilities={"candidate_0": .01, "candidate_abstain": .99}); malformed.append(response)
        for index, reply in enumerate(malformed):
            adapter, _ = self.adapter(reply=reply)
            with self.subTest(index=index), self.assertRaises(JEVError):
                await adapter.interpret(MESSAGE, [], [])


    async def test_open_nearest_expiry_stays_for_engine_resolution(self):
        class Broker:
            def __init__(self):
                self.calls = []

            async def nearest_expiry(self, contract):
                self.calls.append(contract)
                return dict(contract, expiry="2026-09-04")

        broker = Broker()
        adapter, calls = self.adapter(broker=broker)
        try:
            decision = await adapter.interpret(dict(MESSAGE, content="Buy TSLA 352.5 put at .87"), [], [])
            self.assertEqual(decision["contract"]["expiry"], "nearest")
            self.assertFalse(broker.calls)
            self.assertEqual(len(calls), 1)
        finally:
            await adapter.aclose()

    async def test_current_expiry_and_auth_failures_are_fail_closed(self):
        class EarlierBroker:
            def __init__(self):
                self.calls = []

            async def nearest_expiry(self, contract):
                self.calls.append(contract)
                return dict(contract, expiry="2026-09-03")

        broker = EarlierBroker()
        adapter, calls = self.adapter(broker=broker)
        try:
            decision = await adapter.interpret(dict(MESSAGE, content="Buy TSLA 352.5 put at .87"), [], [])
            self.assertEqual(decision["contract"]["expiry"], "nearest")
            self.assertFalse(broker.calls)
            self.assertTrue(calls)
        finally:
            await adapter.aclose()

        events = []
        adapter, _ = self.adapter(events=events)
        adapter._request = lambda *_args: (401, {}, b"private provider payload")
        with self.assertRaises(JEVError):
            await adapter.interpret(MESSAGE, [], [])
        self.assertEqual(events[-1]["component"], "jev")
        self.assertEqual(events[-1]["state"], "auth_required")
        self.assertNotIn("private provider payload", json.dumps(events))
    async def test_real_async_transport_contract_and_pool_reuse(self):
        adapter, _ = self.adapter()
        adapter._request = None
        requests = []
        async def serve(request):
            requests.append(request)
            return httpx.Response(200, json=response_for())
        client = httpx.AsyncClient(transport=httpx.MockTransport(serve))
        adapter._http_client = client
        for _ in range(2):
            decision = await adapter.interpret(MESSAGE, [], [])
            self.assertEqual(decision["action"], "OPEN")
            self.assertIs(adapter._http_client, client)
        self.assertEqual(len(requests), 2)
        self.assertEqual(str(requests[0].url), "https://api.typesafe.ai/v1/systemone")
        self.assertEqual(requests[0].method, "POST")

    def test_extracts_embed_prose_and_ignores_attachment_url(self):
        message = {
            **MESSAGE,
            "content": "Buy",
            "embeds": [{"title": "TSLA 352.5 put", "description": "expiring 2026-09-04 at .87"}],
            "attachments": [{"filename": "not-a-contract.txt", "url": "https://example.invalid/TSLA-999-call"}],
        }
        result = extract_candidates(message)
        self.assertIsNone(result.reason)
        self.assertEqual(result.candidates[0]["contract"]["symbol"], "TSLA")
        self.assertEqual(result.candidates[0]["evidence"][1]["quote"], "TSLA 352.5 put\nexpiring 2026-09-04 at .87")

    def test_image_evidence_routes_without_model_request(self):
        result = extract_candidates({**MESSAGE, "content": "Buy TSLA 352.5 put at .87; see attached image for the expiry", "attachments": [{"filename": "chart.png", "content_type": "image/png"}]})
        self.assertEqual(result.reason, "image_ambiguity")
        self.assertTrue(result.image_ambiguous)

    def test_multiple_contracts_and_missing_contract_are_bounded(self):
        self.assertEqual(extract_candidates({**MESSAGE, "content": "Buy TSLA 352.5 put and AAPL 200 call expiring 2026-09-04"}).reason, "candidate_ambiguity")
        self.assertEqual(extract_candidates({**MESSAGE, "content": "Buy TSLA expiring 2026-09-04"}).reason, "contract_missing")

    async def test_nearest_expiry_resolves_before_request(self):
        broker = Broker()
        calls = []

        def request(_request, timeout):
            calls.append(timeout)
            return 200, {}, json.dumps(response_for()).encode()

        config = {"evaluation": {"mode": "jev", "api_key_file": "/does/not/exist"}}
        with patch.dict(os.environ, {"TYPESAFE_API_KEY": "abcdefgh"}, clear=False):
            interpreter = JEVInterpreter(config, broker=broker, request=request)
            message = {**MESSAGE, "content": "Buy TSLA 352.5 put at .87"}
            result = await interpreter.interpret(message, [], [])
        self.assertFalse(broker.calls)
        self.assertEqual(result["contract"]["expiry"], "nearest")
        self.assertEqual(len(calls), 1)
        self.assertLessEqual(calls[0], 1.2)

    async def test_native_request_has_single_bounded_payload(self):
        seen = {}

        def request(request, timeout):
            seen["body"] = json.loads(request.data.decode())
            seen["auth"] = request.headers.get("Authorization")
            seen["timeout"] = timeout
            return 200, {}, json.dumps(response_for()).encode()

        config = {"evaluation": {"mode": "jev", "api_key_file": "/does/not/exist", "timeout_ms": 1200}}
        with patch.dict(os.environ, {"TYPESAFE_API_KEY": "abcdefgh"}, clear=False):
            await JEVInterpreter(config, request=request).interpret(MESSAGE, [], [])
        self.assertEqual(seen["body"]["model"], "jev-latest")
        self.assertEqual(set(seen["body"]["questions"]), {"candidate", "actionable", "evidence", "profit_only"})
        self.assertEqual(seen["auth"], "Bearer abcdefgh")
        self.assertLessEqual(seen["timeout"], 1.2)

    async def test_timeout_has_no_retry(self):
        calls = 0

        def request(_request, timeout):
            nonlocal calls
            calls += 1
            import time
            time.sleep(timeout * 4)
            return 200, {}, json.dumps(response_for()).encode()

        config = {"evaluation": {"mode": "jev", "api_key_file": "/does/not/exist", "timeout_ms": 20}}
        with patch.dict(os.environ, {"TYPESAFE_API_KEY": "abcdefgh"}, clear=False):
            with self.assertRaises(JEVError) as raised:
                await JEVInterpreter(config, request=request).interpret(MESSAGE, [], [])
        self.assertEqual(raised.exception.code, "jev_timeout")
        self.assertEqual(calls, 1)

    def test_settings_reject_invalid_mode_and_zero_thresholds(self):
        self.assertEqual(evaluation_settings({})["mode"], "codex")
        for values in ({"mode": []}, {"mode": {}}, {"min_confidence": 0}, {"min_probability": 0}, {"min_eligibility": 0}):
            with self.subTest(values=values), self.assertRaises(ValueError):
                evaluation_settings({"evaluation": values})

    def test_settings_and_credential_status_do_not_expose_secret(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "typesafe.key"
            path.write_text("abcdefgh\n")
            path.chmod(0o600)
            config = {"evaluation": {"api_key_file": str(path), "mode": "jev_shadow"}}
            settings = evaluation_settings(config)
            self.assertEqual(settings["mode"], "jev_shadow")
            self.assertEqual(settings["timeout_ms"], 1200)
            self.assertTrue(credential_configured(config))
            self.assertNotIn("abcdefgh", repr(settings))


class JEVParserEntryTests(unittest.TestCase):
    def test_stop_bearing_complete_entry_falls_back_to_codex(self):
        for instruction in ("S/L BE", "SL .80", "S.L. .80", "B/E"):
            with self.subTest(instruction=instruction):
                result = extract_candidates({
                    **MESSAGE,
                    "content": "Buy TSLA 352.5 put expiring 2026-09-04 at .87; " + instruction,
                })
                self.assertEqual(result.candidates, ())
                self.assertEqual(result.reason, "message_ambiguous")
        from relay.jev import _has_stop_instruction
        self.assertFalse(_has_stop_instruction("this may be quick"))

    def test_dollar_prefixed_entry_is_a_complete_candidate(self):
        message = {
            "id": "entry-1",
            "content": "OPEN **$BAC $63 call 9/18 @ $0.96** (swing)",
            "source_group": "approved",
            "timestamp": "2026-09-03T19:03:04Z",
        }
        candidate = bounded_entry_candidate(message)
        self.assertIsNotNone(candidate)
        self.assertEqual(candidate["contract"]["symbol"], "BAC")
        self.assertEqual(candidate["contract"]["expiry"], "2026-09-18")

    def test_unrelated_context_image_does_not_block_complete_dated_entry(self):
        message = {
            "id": "entry-2",
            "content": "OPEN $BAC $63 call 9/18 @ $0.96",
            "source_group": "approved",
            "timestamp": "2026-09-03T19:03:04Z",
        }
        context = [{
            "id": "old-image",
            "content": "old chart",
            "attachments": [{"filename": "chart.png", "content_type": "image/png"}],
        }]
        self.assertEqual(extract_candidates(message, context).reason, None)

    def test_missing_expiry_exit_does_not_use_nearest(self):
        message = {
            "id": "exit-1",
            "content": "Sell TSLA 352.5 put",
            "source_group": "approved",
            "timestamp": "2026-09-04T14:00:00Z",
        }
        result = extract_candidates(message)
        self.assertIsNone(result.reason)
        self.assertEqual(result.candidates[0]["contract"]["expiry"], "nearest")

    def test_exit_context_binds_one_owned_source_contract(self):
        import asyncio

        message = {
            "id": "exit-2",
            "content": "Sell TSLA 352.5 put",
            "source_group": "approved",
            "timestamp": "2026-09-04T14:00:00Z",
        }
        positions = [{
            "source_group": "approved",
            "contract": {"symbol": "TSLA", "expiry": "2026-09-04", "strike": "352.5", "option_type": "put"},
            "quantity": 1,
        }]
        prepared = asyncio.run(prepare_candidates(message, [], positions))
        self.assertEqual(prepared.candidates[0]["contract"]["expiry"], "2026-09-04")


if __name__ == "__main__":
    unittest.main()
