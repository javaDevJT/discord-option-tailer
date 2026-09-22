import asyncio
import json
import time
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

from relay.recovery import RecoveryEvaluator
from tests import test_core

CONTRACT = test_core.CONTRACT
NOW = test_core.NOW


class OrderReconciliationChecks(unittest.IsolatedAsyncioTestCase):
    setUp = test_core.CoreChecks.setUp
    message = test_core.CoreChecks.message

    def order(self, order_id, *, quantity=2, deadline=None, after_seconds=None, side="buy", effect="open"):
        order = {
            "client_order_id": order_id,
            "contract": CONTRACT,
            "side": side,
            "quantity": quantity,
            "limit_price": "1.00",
            "position_effect": effect,
            "quote_timestamp": NOW.isoformat(),
            "account_timestamp": NOW.isoformat(),
        }
        if deadline is not None:
            order["entry_cancel_at"] = deadline
        if after_seconds is not None:
            order["entry_cancel_after_seconds"] = after_seconds
        return order

    def reserve(self, order, action="OPEN"):
        message = self.message(content=f"{action} {order['client_order_id']}")
        message["source_group"] = "source-a"
        self.store.observe(message)
        self.store.record(message, "paper_order", "submitted", {"action": action})
        self.store.reserve(message, {"action": action}, order, NOW)
        return message

    def recovery(self):
        evaluator = RecoveryEvaluator(self.engine)
        self.engine.stops.maintain = AsyncMock()
        return evaluator

    async def test_observe_only_reconciles_without_canceling_or_arming_orders(self):
        self.engine.observe_only = True
        self.engine.interpreter = None
        self.reserve(self.order("observed-entry", deadline=(NOW - timedelta(seconds=1)).isoformat()))
        self.broker.cancel_order = AsyncMock(side_effect=AssertionError("observe-only cancellation"))
        self.broker.order_status = AsyncMock(return_value={
            "id": "paper-observed-entry", "status": "open", "filled_quantity": 0, "fill_price": None,
        })
        evaluator = self.recovery()
        await evaluator.reconcile_orders(force=True)
        self.broker.order_status.assert_awaited_once()
        self.broker.cancel_order.assert_not_awaited()
        self.engine.stops.maintain.assert_not_awaited()

    async def test_reservation_persists_cancel_deadline_and_restarts_without_submit(self):
        order = self.order("entry-1", after_seconds=3)
        self.reserve(order)
        row = self.store.db.execute("SELECT body,status FROM orders WHERE id=?", ("entry-1",)).fetchone()
        body = json.loads(row["body"])
        self.assertEqual(body["entry_cancel_at"], (NOW + timedelta(seconds=3)).isoformat())

        self.store.mark_unknown("entry-1")
        self.broker.order_status = AsyncMock(return_value={
            "id": "paper-entry-1",
            "status": "filled",
            "filled_quantity": 2,
            "fill_price": "1.00",
        })
        evaluator = self.recovery()
        await evaluator.reconcile_orders(force=True)
        self.assertEqual(self.store.positions()[0]["quantity"], 2)
        self.assertEqual(self.broker.order_status.await_count, 1)

        restarted = RecoveryEvaluator(self.engine)
        await restarted.reconcile_orders(force=True)
        self.assertEqual(self.broker.order_status.await_count, 1)

        close = self.order("close-1", quantity=2, side="sell", effect="close")
        self.reserve(close, "CLOSE")
        self.store.apply_result("close-1", {
            "id": "paper-close-1",
            "status": "filled",
            "filled_quantity": 2,
            "fill_price": "1.00",
        })
        self.assertEqual(self.store.positions(), [])

    async def test_unknown_fill_updates_originating_event_and_never_resubmits(self):
        message = self.reserve(self.order("entry-2"))
        self.store.mark_unknown("entry-2")
        event = self.store.db.execute(
            "SELECT reason,decision FROM events WHERE message_id=? ORDER BY id DESC LIMIT 1",
            (message["id"],),
        ).fetchone()
        self.assertIn("unknown", event["reason"])

        self.broker.order_status = AsyncMock(return_value={
            "id": "paper-entry-2",
            "status": "filled",
            "filled_quantity": 2,
            "fill_price": "1.00",
        })
        self.broker.submit = AsyncMock(side_effect=AssertionError("reconciliation resubmitted"))
        await self.recovery().reconcile_orders(force=True)
        event = self.store.db.execute(
            "SELECT reason,decision FROM events WHERE message_id=? ORDER BY id DESC LIMIT 1",
            (message["id"],),
        ).fetchone()
        self.assertIn("filled", event["reason"])
        self.assertEqual(json.loads(event["decision"])["order_reconciliation"]["filled_quantity"], 2)
        self.broker.submit.assert_not_awaited()

    async def test_partial_cancel_fill_is_applied_once(self):
        order = self.order("entry-3", deadline=(NOW - timedelta(seconds=1)).isoformat())
        self.reserve(order)
        self.store.db.execute("UPDATE orders SET status='open' WHERE id=?", ("entry-3",))
        self.broker.cancel_order = AsyncMock(return_value={
            "id": "paper-entry-3",
            "status": "canceled",
            "filled_quantity": 1,
            "fill_price": "1.00",
        })
        evaluator = self.recovery()
        await evaluator.reconcile_orders(force=True)
        await evaluator.reconcile_orders(force=True)
        row = self.store.db.execute("SELECT status,filled_quantity FROM orders WHERE id=?", ("entry-3",)).fetchone()
        self.assertEqual((row["status"], row["filled_quantity"]), ("canceled", 1))
        self.assertEqual(self.store.positions()[0]["quantity"], 1)
        self.assertEqual(self.broker.cancel_order.await_count, 1)

    async def test_cancel_race_polls_authoritative_fill(self):
        order = self.order("entry-4", deadline=(NOW - timedelta(seconds=1)).isoformat())
        self.reserve(order)
        self.store.db.execute("UPDATE orders SET status='open' WHERE id=?", ("entry-4",))
        self.broker.cancel_order = AsyncMock(side_effect=RuntimeError("fill won cancel race"))
        self.broker.order_status = AsyncMock(return_value={
            "id": "paper-entry-4",
            "status": "filled",
            "filled_quantity": 2,
            "fill_price": "1.00",
        })
        await self.recovery().reconcile_orders(force=True)
        row = self.store.db.execute("SELECT status,filled_quantity FROM orders WHERE id=?", ("entry-4",)).fetchone()
        self.assertEqual((row["status"], row["filled_quantity"]), ("filled", 2))
        self.assertEqual(self.store.positions()[0]["quantity"], 2)
        self.broker.order_status.assert_awaited_once()

    async def test_live_lookup_receives_persisted_creation_anchor_for_legacy_identity(self):
        self.engine.mode = "live"
        self.reserve(self.order("entry-5"))
        self.store.mark_unknown("entry-5")
        self.broker.order_status = AsyncMock(return_value={
            "id": "broker-entry-5",
            "status": "open",
            "filled_quantity": 0,
            "fill_price": None,
        })
        await self.recovery().reconcile_orders(force=True)
        expected = self.broker.order_status.await_args.kwargs["expected_order"]
        self.assertEqual(expected["created_at"], NOW.isoformat())
        self.assertEqual(expected["client_order_id"], "entry-5")

    async def test_idle_reconcile_loop_wakes_for_new_expired_entry(self):
        evaluator = self.recovery()
        self.engine.clock = lambda: datetime.now(timezone.utc)
        evaluator.next_reconcile = time.monotonic() + 30
        cancelled = asyncio.Event()

        async def cancel_order(order_id, **kwargs):
            cancelled.set()
            return {"id": "paper-entry-6", "status": "canceled", "filled_quantity": 0, "fill_price": None}

        self.broker.cancel_order = cancel_order
        task = asyncio.create_task(evaluator.reconcile_loop())
        await asyncio.sleep(0.05)
        order = self.order("entry-6", deadline=(datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat())
        self.reserve(order)
        self.store.db.execute("UPDATE orders SET status='open' WHERE id=?", ("entry-6",))
        try:
            await asyncio.wait_for(cancelled.wait(), 2)
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        self.assertEqual(self.store.db.execute("SELECT status FROM orders WHERE id=?", ("entry-6",)).fetchone()[0], "canceled")

    async def test_holdings_without_relay_order_are_not_attributed(self):
        self.store.db.execute(
            "INSERT INTO positions VALUES (?,?,?,?)",
            ("source-a", json.dumps(CONTRACT, sort_keys=True, separators=(",", ":")), 2, "1.00"),
        )
        evaluator = self.recovery()
        self.assertEqual(evaluator.current_entries("source-a"), {})


if __name__ == "__main__":
    unittest.main()
