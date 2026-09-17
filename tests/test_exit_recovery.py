"""Missed exits use synthetic holdings, quotes and source verification only."""
import json
import unittest
from datetime import timedelta
from unittest.mock import AsyncMock

from relay.recovery import RecoveryEvaluator
from relay.core import Engine
from tests import test_core as fixtures


class ExitRecoveryChecks(unittest.IsolatedAsyncioTestCase):
    setUp = fixtures.CoreChecks.setUp
    message = fixtures.CoreChecks.message

    async def open_position(self, quantity=2):
        fraction = str((81 * quantity + 1) / 10000)
        self.config["risk"].update(entry_risk_min_fraction=fraction, entry_risk_max_fraction=fraction)
        self.config["require_source_verification"] = True
        self.engine.verify_current = AsyncMock(return_value=True)
        result = await self.engine.handle(self.message(content="Fixture entry"))
        self.assertEqual(result["state"], "broker_order" if self.engine.mode == "live" else "paper_order", result)
        self.assertEqual(self.store.positions()[0]["quantity"], quantity)
        self.now = fixtures.NOW + timedelta(hours=2)
        self.broker.account["timestamp"] = self.now.isoformat()
        self.broker.quotes.update(timestamp=self.now.isoformat(), bid="1.00", ask="1.03")

        async def assess(message, context, positions, decision, facts):
            return dict(status="viable", confidence=.99, reason="Fixture missed exit remains applicable",
                        evidence=[{"message_id": message["id"], "quote": message["content"]}])

        self.interpreter.assess_recovery = assess
        return RecoveryEvaluator(self.engine)

    async def missed(self, *, content="Took 50% here", **changes):
        message = self.message(content=content, timestamp=(self.now - timedelta(hours=1)).isoformat(), **changes)
        message.update(ingestion="baseline", source_group=self.config["channels"][0]["source_group"])
        self.engine.note_observation(message)
        await self.engine.handle(message)
        return message

    async def test_restart_trim_sells_one_of_two_once_and_preserves_record(self):
        recovery = await self.open_position()
        message = await self.missed()
        self.interpreter.decision.update(action="REDUCE", fraction=.5, profit_only=False)
        self.store.record(message, "ignore", "Legacy interpreter ignored optional exits")
        recovery.recover_position_context()
        state = self.store.db.execute("SELECT state FROM events WHERE message_id=?", (message["id"],)).fetchone()[0]
        self.assertEqual(state, "recovery_pending")
        result = await recovery.assess(message)
        self.assertEqual(result["state"], "paper_order", result)
        self.assertEqual(self.broker.submissions[-1]["quantity"], 1)
        self.assertEqual(self.store.positions()[0]["quantity"], 1)
        self.assertTrue(self.engine.verify_current.call_args.kwargs["recovery"])
        restarted = RecoveryEvaluator(self.engine)
        restarted.recover_position_context()
        self.assertEqual((await restarted.assess(message))["state"], "duplicate")
        self.assertEqual(len(self.broker.submissions), 2)
        self.assertEqual(self.store.db.execute("SELECT state FROM events WHERE message_id=?", (message["id"],)).fetchone()[0], "paper_order")

    async def test_full_exit_closes_all_remaining_and_never_opens_from_history(self):
        recovery = await self.open_position(3)
        message = await self.missed(content="All out")
        self.interpreter.decision.update(action="CLOSE", profit_only=False)
        self.assertEqual((await recovery.assess(message))["state"], "paper_order")
        self.assertEqual(self.broker.submissions[-1]["quantity"], 3)
        self.assertEqual(self.store.positions(), [])
        another = await self.missed(content="New entry recap")
        self.interpreter.decision.update(action="OPEN")
        self.assertEqual((await recovery.assess(another))["state"], "recovery_review")
        self.assertEqual(len(self.broker.submissions), 2)

    async def test_optional_exit_checks_current_profit_not_the_old_alert_gain(self):
        recovery = await self.open_position(1)
        message = await self.missed(content="You can take profits if you'd like")
        self.interpreter.decision.update(action="REDUCE", fraction=None, profit_only=True)
        self.broker.quotes.update(bid=".79", ask=".81")
        result = await recovery.assess(message)
        self.assertEqual(result["state"], "recovery_review")
        self.assertIn("round-trip fee reserve", result["reason"])
        self.assertEqual(len(self.broker.submissions), 1)

    async def test_pre_entry_signal_and_unverified_source_do_not_dispatch(self):
        recovery = await self.open_position()
        message = self.message(content="All out", timestamp=(fixtures.NOW - timedelta(minutes=1)).isoformat())
        message.update(ingestion="baseline", source_group="source-a")
        await self.engine.handle(message)
        self.interpreter.decision.update(action="CLOSE")
        result = await recovery.assess(message)
        self.assertIn("predates", result["reason"])
        later = await self.missed(content="Sell the rest")
        self.engine.verify_current.return_value = False
        result = await recovery.assess(later)
        self.assertIn("could not be verified", result["reason"])
        self.assertEqual(len(self.broker.submissions), 1)

    async def test_new_source_context_during_verification_blocks_catchup(self):
        recovery = await self.open_position()
        message = await self.missed(content="Close all")
        self.interpreter.decision.update(action="CLOSE")

        async def changed(target, **options):
            newer = self.message(content="Correction: keep the remaining position", timestamp=self.now.isoformat())
            newer.update(source_group="source-a", ingestion="baseline")
            self.engine.note_observation(newer)
            self.store.observe(newer)
            return True

        self.engine.verify_current = changed
        result = await recovery.assess(message)
        self.assertEqual(result["state"], "recovery_review")
        self.assertIn("context changed", result["reason"])
        self.assertEqual(len(self.broker.submissions), 1)

    async def test_restart_reconciles_pending_fill_without_resubmission(self):
        recovery = await self.open_position()
        order = self.broker.submissions[0]
        with self.store.db:
            self.store.db.execute("UPDATE orders SET status='open' WHERE id=?", (order["client_order_id"],))
        self.broker.order_status = AsyncMock(return_value=dict(
            id="paper-" + order["client_order_id"], status="filled", filled_quantity=2, fill_price=order["limit_price"],
        ))
        await recovery.reconcile_orders()
        self.assertEqual(self.store.unresolved(), 0)
        self.assertEqual(self.store.positions()[0]["quantity"], 2)
        self.assertEqual(len(self.broker.submissions), 1)

    async def test_slow_source_retries_without_another_model_evaluation(self):
        recovery = await self.open_position()
        message = await self.missed(content="All out")
        self.interpreter.decision.update(action="CLOSE")
        self.engine.verify_current.return_value = False
        result = await recovery.assess(message)
        self.assertEqual(result["state"], "recovery_pending", result)
        calls = len(self.interpreter.calls)
        self.assertIn(message["id"], recovery.retry_due)
        self.engine.verify_current.return_value = True
        result = await recovery.assess(message)
        self.assertEqual(result["state"], "paper_order", result)
        self.assertEqual(len(self.interpreter.calls), calls)
        self.assertEqual(self.store.positions(), [])

    async def test_market_closed_exit_remains_pending_until_open(self):
        recovery = await self.open_position()
        message = await self.missed(content="All out")
        self.interpreter.decision.update(action="CLOSE")
        self.broker.account["market_open"] = False
        self.assertEqual((await recovery.assess(message))["state"], "recovery_pending")
        self.assertEqual(len(self.broker.submissions), 1)
        self.broker.account["market_open"] = True
        self.assertEqual((await recovery.assess(message))["state"], "paper_order")

    async def test_exit_for_closed_position_cannot_sell_a_reopened_position(self):
        recovery = await self.open_position()
        old = await self.missed(content="Took 50% here")
        self.interpreter.decision.update(action="CLOSE")
        close = self.message(content="Close now", timestamp=self.now.isoformat())
        self.assertEqual((await self.engine.handle(close))["state"], "paper_order")
        self.now += timedelta(minutes=1)
        self.broker.account["timestamp"] = self.now.isoformat()
        self.broker.quotes.update(timestamp=self.now.isoformat(), bid=".77", ask=".80")
        self.interpreter.decision.update(action="OPEN")
        reopened = self.message(content="New position", timestamp=self.now.isoformat())
        self.assertEqual((await self.engine.handle(reopened))["state"], "paper_order")
        self.interpreter.decision.update(action="REDUCE", fraction=.5)
        result = await recovery.assess(old)
        self.assertEqual(result["state"], "recovery_review", result)
        self.assertIn("predates", result["reason"])
        self.assertEqual(len(self.broker.submissions), 3)

    async def test_later_consumed_exit_blocks_an_older_missed_trim(self):
        recovery = await self.open_position(3)
        old = await self.missed(content="Took 50% here")
        self.interpreter.decision.update(action="REDUCE", fraction=.5)
        fresh = self.message(content="Trim half now", timestamp=self.now.isoformat())
        self.assertEqual((await self.engine.handle(fresh))["state"], "paper_order")
        result = await recovery.assess(old)
        self.assertEqual(result["state"], "recovery_review", result)
        self.assertIn("later exit already", result["reason"])
        self.assertEqual(self.store.positions()[0]["quantity"], 1)

    async def test_final_broker_review_rechecks_optional_profit(self):
        self.config.update(mode="live")
        self.config["robinhood"].update(account_number="12345678", enable_live_orders=True)
        # Synthetic test-only binding; no real account or provider is used.
        self.store.db.execute("DELETE FROM metadata WHERE key='execution_binding'")
        self.engine = Engine(self.config, self.store, self.interpreter, self.broker, lambda: self.now)
        self.engine.verify_current = AsyncMock(return_value=True)
        self.broker.account["account_id"] = "12345678"
        recovery = await self.open_position()
        message = await self.missed(content="You can take profits here")
        self.interpreter.decision.update(action="REDUCE", fraction=.5, profit_only=True)
        async def faded():
            self.broker.quotes.update(bid=".79", ask=".81")
        self.broker.review_hook = faded
        result = await recovery.assess(message)
        self.assertEqual(result["state"], "held", result)
        saved = json.loads(self.store.db.execute("SELECT decision FROM events WHERE message_id=?", (message["id"],)).fetchone()[0])
        self.assertLess(float(saved["exit_evaluation"]["estimated_net_profit"]), 0)
        self.assertEqual(len(self.broker.submissions), 1)
        self.assertEqual(self.store.positions()[0]["quantity"], 2)


if __name__ == "__main__":
    unittest.main()
