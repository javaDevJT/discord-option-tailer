"""Focused offline checks for persistent monitor interpretation."""

import unittest

from relay.evaluation import EvaluationRouter
from relay.interpreter import (
    DECISION_SCHEMA,
    MONITOR_DECISION_SCHEMA,
    CodexInterpreter,
    InterpretationError,
    validate_decision,
)
from relay.monitor_contract import project_monitor_facts, validate_monitor_plan


CONTRACT = {"symbol": "SPY", "expiry": "2026-10-02", "strike": "500", "option_type": "call"}
POSITION = {
    "contract": CONTRACT,
    "quantity": 1,
    "average_price": "4.50",
    "bot_owned": True,
    "source_group": "signals",
}
MESSAGE = {
    "id": "monitor-source",
    "content": "If SPY falls below 500, I want out of my 500 call; watch this position.",
    "source_group": "signals",
    "author_id": "trader",
    "timestamp": "2026-09-29T14:00:00-04:00",
}
PLAN = {
    "duration_seconds": 3600,
    "poll_interval_seconds": 30,
    "reassess_after_seconds": 300,
    "conditions": [{"metric": "underlying_price", "comparison": "lte", "threshold": "500"}],
}


def make_decision(action, *, monitor=None, message=MESSAGE, fraction=None, origin=None):
    return {
        "action": action,
        "origin_message_id": origin,
        "contract": CONTRACT,
        "quantity": None,
        "fraction": fraction,
        "profit_only": False,
        "alert_price": None,
        "stop_price": None,
        "confidence": 0.9,
        "ambiguous": False,
        "reason": "Original position-management condition reassessed.",
        "evidence": [{"message_id": message["id"], "quote": message["content"]}],
        "monitor": monitor,
    }


class MonitorContractTests(unittest.TestCase):
    def test_plan_bounds_symbols_and_signed_decimal(self):
        signed = PLAN | {
            "conditions": [
                {"metric": "unrealized_return_fraction", "comparison": "lte", "threshold": "-0.25"}
            ]
        }
        self.assertEqual(validate_monitor_plan(signed)["conditions"][0]["threshold"], "-0.25")
        with self.assertRaises(ValueError):
            validate_monitor_plan(signed | {"duration_seconds": True})
        with self.assertRaises(ValueError):
            validate_monitor_plan(PLAN | {"reassess_after_seconds": 4})
        with self.assertRaises(ValueError):
            validate_monitor_plan(PLAN | {"conditions": [{"metric": "option_bid", "comparison": "lte", "threshold": "source_day_low"}]})

    def test_empty_conditions_need_timer_and_facts_are_allowlisted(self):
        self.assertEqual(validate_monitor_plan(PLAN | {"conditions": [], "reassess_after_seconds": 60})["conditions"], [])
        with self.assertRaises(ValueError):
            validate_monitor_plan(PLAN | {"conditions": [], "reassess_after_seconds": None})
        facts = project_monitor_facts(
            {
                "observed_at": "2026-09-30T14:01:00Z",
                "source_market_date": "2026-09-29",
                "triggered_at": "2026-09-30T14:01:00Z",
                "trigger_reason": "resume",
                "triggered_conditions": [0],
                "option_bid": "1.25",
                "api_key": "never expose",
            }
        )
        self.assertEqual(facts["trigger_reason"], "resume")
        self.assertEqual(facts["triggered_conditions"], [0])
        self.assertEqual(facts["triggered_at"], "2026-09-30T14:01:00Z")
        self.assertNotIn("api_key", facts)


class MonitorDecisionTests(unittest.TestCase):
    def test_legacy_decision_remains_without_added_monitor_key(self):
        message = {"id": "entry", "content": "Buy SPY 500 call", "source_group": "signals"}
        legacy = make_decision("OPEN", message=message, origin="entry")
        legacy.pop("monitor")
        legacy["action"] = "OPEN"
        legacy["alert_price"] = "1.00"
        result = validate_decision(legacy, message, [])
        self.assertNotIn("monitor", result)
        self.assertIn("monitor", DECISION_SCHEMA["required"])
        self.assertEqual(MONITOR_DECISION_SCHEMA["properties"]["action"]["enum"], ["WAIT", "IGNORE", "REDUCE", "CLOSE"])

    def test_tentative_stop_can_wait_with_plan_but_unavailable_low_cannot_exit(self):
        message = MESSAGE | {"content": "Most likely have my stoploss under today's low on my SPY 500 call; keep an eye on it."}
        plan = PLAN | {
            "conditions": [{"metric": "underlying_price", "comparison": "lte", "threshold": "source_day_low"}]
        }
        waiting = make_decision("WAIT", message=message, monitor=plan)
        self.assertEqual(validate_decision(waiting, message, [], positions=[POSITION])["action"], "WAIT")
        exit_decision = make_decision("CLOSE", message=message, monitor=None, origin=message["id"])
        codex = CodexInterpreter.__new__(CodexInterpreter)

        async def fake_request(data, *, validator, **kwargs):
            self.assertEqual(data["monitor_facts"]["source_market_date"], "2026-09-29")
            self.assertNotIn("source_day_low", data["monitor_facts"])
            self.assertEqual(kwargs["timing_path"], "monitor")
            return validator(exit_decision)

        codex._request = fake_request
        facts = {
            "evaluated_at": "2026-09-30T14:00:00Z",
            "source_market_date": "2026-09-29",
            "reference_market_date": "2026-09-29",
            "underlying_price": "499",
            "underlying_quote_at": "2026-09-30T14:00:00Z",
            "trigger_reason": "condition",
        }
        with self.assertRaises(InterpretationError):
            import asyncio
            asyncio.run(codex.assess_monitor(message, [], [POSITION], plan, facts))

    def test_current_condition_allows_exit_and_negative_return_threshold(self):
        message = MESSAGE | {"content": "If the option loses 25%, take half off this SPY 500 call; monitor it."}
        plan = PLAN | {
            "conditions": [
                {"metric": "unrealized_return_fraction", "comparison": "lte", "threshold": "-0.25"}
            ]
        }
        decision = make_decision("REDUCE", message=message, fraction=0.5, origin=message["id"])
        codex = CodexInterpreter.__new__(CodexInterpreter)

        async def fake_request(data, *, validator, **kwargs):
            self.assertEqual(data["monitor_facts"]["source_market_date"], "2026-09-29")
            self.assertEqual(data["monitor_facts"]["triggered_conditions"], [0])
            return validator(decision) | {"evaluation_timing": {"path": kwargs["timing_path"]}}

        codex._request = fake_request
        facts = {
            "evaluated_at": "2026-09-30T14:00:00Z",
            "source_market_date": "2026-09-29",
            "unrealized_return_fraction": "-0.30",
            "option_quote_at": "2026-09-30T14:00:00Z",
            "trigger_reason": "resume",
        }
        import asyncio
        result = asyncio.run(codex.assess_monitor(message, [], [POSITION], plan, facts))
        self.assertEqual(result["action"], "REDUCE")
        self.assertEqual(result["evaluation_timing"]["path"], "monitor")

    def test_profit_status_and_wrong_evidence_do_not_authorize_monitor_exit(self):
        status = {
            "id": "status",
            "content": "$DRAM calls up +$50 per contract heading into market close. I am looking for $65 as my first target",
            "source_group": "signals",
            "timestamp": MESSAGE["timestamp"],
        }
        plan = PLAN | {"conditions": [{"metric": "option_bid", "comparison": "gte", "threshold": "65"}]}
        result = make_decision("WAIT", message=status, monitor=plan)
        result["evidence"] = [{"message_id": "status", "quote": status["content"]}]
        with self.assertRaises(InterpretationError):
            validate_decision(result, status, [], positions=[POSITION])
        wrong = make_decision("CLOSE", message=MESSAGE, origin=MESSAGE["id"])
        wrong["evidence"] = [{"message_id": "other-source", "quote": MESSAGE["content"]}]
        with self.assertRaises(InterpretationError):
            validate_decision(wrong, MESSAGE, [])


class MonitorRouterTests(unittest.IsolatedAsyncioTestCase):
    async def test_serialized_codex_monitor_route_is_annotated(self):
        class FakeCodex:
            async def assess_monitor(self, *args):
                return {"action": "WAIT", "evaluation_timing": {"path": "monitor"}}

        router = EvaluationRouter({}, codex=FakeCodex())
        try:
            result = await router.assess_monitor(MESSAGE, [], [POSITION], PLAN, {})
        finally:
            await router.aclose()
        self.assertEqual(result["evaluation_timing"]["evaluator"], "codex")
        self.assertEqual(result["evaluation_timing"]["route"], "monitor")


if __name__ == "__main__":
    unittest.main()
