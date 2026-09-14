"""Synthetic chase boundaries and latest-quote diagnostics; no provider requests."""
import copy
from decimal import Decimal
import json
from pathlib import Path
import unittest

from relay.core import Engine, Hold, Store
from relay.recovery import RecoveryEvaluator
from tests import test_core as fixtures


class ChaseTests(unittest.IsolatedAsyncioTestCase):
    setUp = fixtures.CoreChecks.setUp
    message = fixtures.CoreChecks.message
    position = fixtures.CoreChecks.owned

    def prices(self, ask, tick=".01"):
        self.interpreter.decision["alert_price"] = "1.00"
        self.config["risk"].update(max_chase_fraction=".15", entry_risk_min_fraction=".10", entry_risk_max_fraction=".10")
        self.broker.quotes.update(ask=ask, bid=str(Decimal(ask) * Decimal(".99")), tick_size=tick)

    def live(self):
        config = copy.deepcopy(self.config)
        config["mode"] = "live"
        config["robinhood"].update(account_number="00012345", enable_live_orders=True)
        store = Store(Path(self.temp.name) / "live.sqlite3")
        self.addCleanup(store.close)
        self.broker.account["account_id"] = "00012345"
        return Engine(config, store, self.interpreter, self.broker, lambda: self.now)

    async def test_inclusive_cap_zero_cap_and_tick_rounding(self):
        for ask, tick, cap, allowed in (
            ("1.14", ".01", ".15", True), ("1.15", ".01", ".15", True),
            ("1.16", ".01", ".15", False), ("1.14", ".10", ".15", False),
            ("1.00", ".01", "0", True), ("1.01", ".01", "0", False),
            (".95", ".01", "0", True),
        ):
            with self.subTest(ask=ask, tick=tick, cap=cap):
                self.prices(ask, tick)
                self.config["risk"]["max_chase_fraction"] = cap
                message = self.message()
                message["source_group"] = self.config["channels"][0]["source_group"]
                decision = await self.interpreter.interpret(message, [], [])
                if allowed:
                    order = await self.engine.plan(message, decision)
                    self.assertEqual(Decimal(order["entry_evaluation"]["ask"]), Decimal(ask))
                else:
                    with self.assertRaisesRegex(Hold, "chase.*evaluated ask"):
                        await self.engine.plan(message, decision)
                self.assertEqual(Decimal(decision["entry_evaluation"]["max_chase_percent"]), Decimal(cap) * 100)

    async def test_chase_hold_and_paper_entry_print_evaluated_ask_and_deviation(self):
        self.prices("1.16")
        held = await self.engine.handle(self.message())
        self.assertEqual(held["state"], "held")
        self.assertIn("evaluated ask=$1.16", held["reason"])
        self.assertIn("ask deviation=+16%", held["reason"])
        self.assertEqual(self.broker.submissions, [])
        self.prices(".95")
        entered = await self.engine.handle(self.message())
        self.assertEqual(entered["state"], "paper_order", entered)
        self.assertIn("evaluated ask=$0.95", entered["reason"])
        self.assertIn("ask deviation=-5%", entered["reason"])

    async def test_final_review_crossing_cap_holds_and_persists_latest_ask(self):
        self.prices("1.10")
        engine = self.live()
        async def move_quote():
            self.broker.quotes.update(ask="1.16", bid="1.15")
        self.broker.review_hook = move_quote
        result = await engine.handle(self.message())
        self.assertEqual(result["state"], "held", result)
        self.assertIn("chase during broker review", result["reason"])
        self.assertIn("evaluated ask=$1.16", result["reason"])
        self.assertIn("ask deviation=+16%", result["reason"])
        self.assertEqual(self.broker.submissions, [])
        row = engine.store.db.execute("SELECT status,body FROM orders").fetchone()
        self.assertEqual(row["status"], "rejected")
        self.assertEqual(json.loads(row["body"])["entry_evaluation"]["ask"], "1.16")
        self.assertEqual(engine.store.unresolved(), 0)

    async def test_live_entry_prints_and_persists_final_review_ask(self):
        self.prices("1.10")
        engine = self.live()
        async def move_quote():
            self.broker.quotes.update(ask="1.14", bid="1.13")
        self.broker.review_hook = move_quote
        result = await engine.handle(self.message())
        self.assertEqual(result["state"], "broker_order", result)
        self.assertIn("evaluated ask=$1.14", result["reason"])
        self.assertIn("ask deviation=+14%", result["reason"])
        body = json.loads(engine.store.db.execute("SELECT body FROM orders").fetchone()[0])
        self.assertEqual(body["entry_evaluation"]["ask"], "1.14")
        self.assertEqual(len(self.broker.submissions), 1)

    async def test_exits_ignore_entry_chase(self):
        self.position()
        self.prices("1.20")
        self.config["risk"]["max_chase_fraction"] = "0"
        self.interpreter.decision.update(action="CLOSE", alert_price=".01")
        result = await self.engine.handle(self.message())
        self.assertEqual(result["state"], "paper_order", result)
        self.assertNotIn("entry_evaluation", self.broker.submissions[0])

    async def test_recovery_chase_includes_rounded_limit_and_numeric_context(self):
        self.prices("1.14", ".10")
        message = self.message()
        message["source_group"] = self.config["channels"][0]["source_group"]
        self.store.observe(message)
        decision = await self.interpreter.interpret(message, [], [])
        facts = await RecoveryEvaluator(self.engine).facts(message, decision)
        blockers = " ".join(facts["blockers"])
        self.assertIn("chase", blockers)
        self.assertIn("evaluated ask=$1.14", blockers)
        self.assertIn("limit deviation=+20%", blockers)
        self.assertEqual(self.broker.submissions, [])

    async def test_recovery_affordability_uses_rounded_limit(self):
        self.prices("1.14", ".10")
        self.config["risk"].update(max_chase_fraction=".25", entry_risk_min_fraction=".10",
                                   entry_risk_max_fraction=".10", buying_power_reserve_fraction="0",
                                   fee_reserve_per_contract="0")
        self.broker.account["buying_power"] = "117"
        message = self.message()
        message["source_group"] = self.config["channels"][0]["source_group"]
        decision = await self.interpreter.interpret(message, [], [])
        facts = await RecoveryEvaluator(self.engine).facts(message, decision)
        self.assertIn("one whole contract", " ".join(facts["blockers"]))
        self.assertIsNone(facts.get("affordable_quantity"))
        self.assertEqual(self.broker.submissions, [])
