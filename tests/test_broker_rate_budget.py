"""Offline request-budget and watch-to-dispatch cache regressions."""
import asyncio
from types import SimpleNamespace
import time
import unittest
from unittest.mock import AsyncMock, Mock

import httpx

from relay.broker import (
    BrokerError, RobinhoodMCP, _BrokerBudgetHold, _BrokerReadBudget,
    _READ_BUDGET_BACKGROUND, _READ_BUDGET_TOTAL, _WATCH_QUOTE_REFRESH_DEADLINE_SECONDS,
    _rate_limited, _retry_after_delay,
)
from relay.watch import (
    WATCH_MARKET_REQUEST_INTERVAL_SECONDS, WATCH_PREPARATION_REFRESH_MAX_SECONDS,
    WATCH_PREPARATION_REFRESH_MIN_SECONDS,
)
from tests import test_watch_broker as watch_fixtures


class BudgetChecks(unittest.IsolatedAsyncioTestCase):
    async def test_four_watch_budget_keeps_quote_and_account_data_inside_ten_seconds(self):
        now = [0.0]

        async def advance(seconds):
            now[0] += seconds

        budget = _BrokerReadBudget(clock=lambda: now[0], sleep=advance)
        events = []
        quote_index = 0
        while quote_index * WATCH_MARKET_REQUEST_INTERVAL_SECONDS < 120:
            due = quote_index * WATCH_MARKET_REQUEST_INTERVAL_SECONDS
            events.extend(((due, "snapshot_start", None), (due, "quote_batch", quote_index)))
            events.extend((due, "snapshot", read_index) for read_index in range(4))
            quote_index += 1
        for index in range(4):
            interval = WATCH_PREPARATION_REFRESH_MIN_SECONDS + index / 3 * (
                WATCH_PREPARATION_REFRESH_MAX_SECONDS - WATCH_PREPARATION_REFRESH_MIN_SECONDS)
            due = interval
            while due < 120:
                events.extend((due, "metadata", index) for _ in range(2))
                due += interval
        events.sort(key=lambda event: (event[0], {
            "snapshot_start": 0, "quote_batch": 1, "snapshot": 2, "metadata": 3,
        }[event[1]], -1 if event[2] is None else event[2]))

        last_quote = None
        last_snapshot = None
        max_quote_gap = 0.0
        max_snapshot_age_at_quote = 0.0
        max_quote_admission_wait = 0.0
        for due, kind, _index in events:
            now[0] = max(now[0], due)
            if kind == "snapshot_start":
                last_snapshot = float(due)
                continue
            admitted_at = await budget.acquire(background=True)
            if kind == "quote_batch":
                if last_quote is not None:
                    max_quote_gap = max(max_quote_gap, admitted_at - last_quote)
                if last_snapshot is not None:
                    max_snapshot_age_at_quote = max(max_snapshot_age_at_quote, admitted_at - last_snapshot)
                max_quote_admission_wait = max(max_quote_admission_wait, admitted_at - due)
                last_quote = admitted_at
                now[0] += 2.0  # Two-second transport tolerance, counted against the 8s deadline.
            stats = await budget.stats()
            self.assertLessEqual(stats["background_calls"], _READ_BUDGET_BACKGROUND)
            self.assertLessEqual(stats["total_calls"], _READ_BUDGET_TOTAL)

        self.assertLessEqual(max_quote_gap, 10)
        self.assertLessEqual(max_snapshot_age_at_quote, 10)
        self.assertLess(max_quote_admission_wait, _WATCH_QUOTE_REFRESH_DEADLINE_SECONDS - 2.0)

    async def test_held_position_load_exposes_capacity_shortfall(self):
        now = [0.0]

        async def advance(seconds):
            now[0] += seconds

        budget = _BrokerReadBudget(clock=lambda: now[0], sleep=advance)
        events = []
        for due in range(0, 120, int(WATCH_MARKET_REQUEST_INTERVAL_SECONDS)):
            events.extend(((float(due), "snapshot_start", None), (float(due), "quote_batch", None)))
            # Four base snapshot reads plus contract and quote reads for one
            # held position exceed the remaining background request budget.
            events.extend((float(due), "snapshot", index) for index in range(6))
        for index in range(4):
            interval = WATCH_PREPARATION_REFRESH_MIN_SECONDS + index / 3 * (
                WATCH_PREPARATION_REFRESH_MAX_SECONDS - WATCH_PREPARATION_REFRESH_MIN_SECONDS)
            due = interval
            while due < 120:
                events.extend((due, "metadata", index) for _ in range(2))
                due += interval
        events.sort(key=lambda event: (event[0], {
            "snapshot_start": 0, "quote_batch": 1, "snapshot": 2, "metadata": 3,
        }[event[1]], -1 if event[2] is None else event[2]))

        last_quote = None
        last_snapshot = None
        stale_quotes = stale_snapshots = deadline_shortfalls = 0
        max_quote_wait = 0.0
        for due, kind, _index in events:
            now[0] = max(now[0], due)
            if kind == "snapshot_start":
                last_snapshot = float(due)
                continue
            admitted_at = await budget.acquire(background=True)
            if kind == "quote_batch":
                wait = admitted_at - due
                max_quote_wait = max(max_quote_wait, wait)
                if wait + 2.0 > _WATCH_QUOTE_REFRESH_DEADLINE_SECONDS:
                    deadline_shortfalls += 1
                if last_quote is not None and admitted_at - last_quote > 10:
                    stale_quotes += 1
                if last_snapshot is not None and admitted_at - last_snapshot > 10:
                    stale_snapshots += 1
                last_quote = admitted_at
                now[0] += 2.0
            stats = await budget.stats()
            self.assertLessEqual(stats["background_calls"], _READ_BUDGET_BACKGROUND)
            self.assertLessEqual(stats["total_calls"], _READ_BUDGET_TOTAL)

        self.assertGreater(max_quote_wait, 0)
        self.assertGreater(stale_quotes + stale_snapshots, 0)
        self.assertGreater(deadline_shortfalls, 0)

    async def test_background_ceiling_preserves_foreground_burst_and_rolls_over(self):
        now = [100.0]
        budget = _BrokerReadBudget(clock=lambda: now[0])
        for _ in range(60):
            budget.background_tokens = 1  # Exercise the rolling ceiling independently of pacing.
            await budget.acquire(background=True, wait=False)
        with self.assertRaises(_BrokerBudgetHold):
            await budget.acquire(background=True, wait=False)
        for _ in range(30):
            await budget.acquire(wait=False)
        with self.assertRaises(_BrokerBudgetHold):
            await budget.acquire(wait=False)
        now[0] += 60
        await budget.acquire(background=True, wait=False)
        self.assertEqual(len(budget.requests), 1)

    async def test_background_pacing_and_priority_do_not_delay_foreground(self):
        now = [100.0]
        budget = _BrokerReadBudget(clock=lambda: now[0])
        for _ in range(4):
            await budget.acquire(background=True, wait=False)
        with self.assertRaises(_BrokerBudgetHold):
            await budget.acquire(background=True, wait=False)
        await budget.acquire(wait=False)
        now[0] += 1
        budget.foreground_waiters = 1
        with self.assertRaises(_BrokerBudgetHold):
            await budget.acquire(background=True, wait=False)
        budget.foreground_waiters = 0
        await budget.acquire(background=True, wait=False)

    async def test_transport_429_honors_retry_after_and_never_retries_mutation(self):
        now = [100.0]
        budget = _BrokerReadBudget(clock=lambda: now[0], random_value=lambda: .5)
        client = RobinhoodMCP({"account_number": "TEST-BUDGET"})
        client._request_budget = Mock(return_value=budget)
        client._connection_failed = Mock()
        response = httpx.Response(429, headers={"Retry-After": "5"}, request=httpx.Request("GET", "https://example.invalid"))
        error = httpx.HTTPStatusError("rate limited", request=response.request, response=response)
        client.session = SimpleNamespace(call_tool=AsyncMock(side_effect=error))
        with self.assertRaises(httpx.HTTPStatusError):
            await client._call_tool("get_option_quotes", {})
        self.assertGreaterEqual(budget.cooldown_until, 105)
        with self.assertRaises(_BrokerBudgetHold):
            await client._call_tool("place_option_order", {})
        client.session.call_tool.assert_awaited_once()
        self.assertTrue(_rate_limited(SimpleNamespace(structuredContent={"error": {"code": "rate_limited"}})))
        self.assertIsNone(_retry_after_delay(SimpleNamespace(headers={"Retry-After": "inf"})))

    async def test_budget_is_shared_by_clients_bound_to_same_account(self):
        left = RobinhoodMCP({"account_number": "TEST-SHARED"})
        right = RobinhoodMCP({"account_number": "TEST-SHARED"})
        other = RobinhoodMCP({"account_number": "TEST-OTHER"})
        self.assertIs(left._request_budget(), right._request_budget())
        self.assertIsNot(left._request_budget(), other._request_budget())


class WatchMarketChecks(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        watch_fixtures.WatchBrokerChecks.setUp(self)

    async def warm_market(self):
        await self.broker.prewarm_entry(self.contract)
        result = await self.broker.refresh_watch_market(self.contract)
        task = self.broker._watch_snapshot_task
        if task is not None:
            await task
        return result

    async def test_shared_account_refresh_and_cached_snapshot_reach_submission(self):
        await self.warm_market()
        self.calls.clear()
        refreshed = await self.broker.refresh_watch_market(self.contract)
        self.assertEqual(refreshed["account_snapshot"], "cached")
        self.assertEqual([name for name, _ in self.calls], ["get_option_quotes"])
        started, generation, snapshot = self.broker._watch_snapshot
        self.broker._watch_snapshot = (started - 4, generation, snapshot)
        self.calls.clear()
        with self.broker.execution_reads():
            diagnostic = {}
            cached = self.broker.watch_entry_snapshot(self.contract, diagnostic=diagnostic)
            self.assertIsNotNone(cached)
            self.assertNotIn("read_timing", cached)
            self.assertEqual(diagnostic["source"], "watch_cache")
            self.assertIn("background_read_timing", diagnostic)
            result = await self.broker.submit(self.order | {"prepared_entry": True}, before_submit=AsyncMock())
        self.assertEqual([name for name, _ in self.calls], ["review_option_order", "place_option_order"])
        self.assertEqual(result["status"], "filled")

    async def test_expired_and_changed_watch_data_are_not_reused(self):
        await self.warm_market()
        started, generation, snapshot = self.broker._watch_snapshot
        self.broker._watch_snapshot = (started - 11, generation, snapshot)
        diagnostic = {}
        self.assertIsNone(self.broker.watch_entry_snapshot(self.contract, diagnostic=diagnostic))
        self.assertEqual(diagnostic["reason"], "watch_snapshot_expired")
        self.broker._watch_snapshot = (started, generation, snapshot)
        with self.broker.execution_reads():
            self.assertIsNotNone(self.broker.watch_entry_snapshot(self.contract))
            scope = self.broker._active_execution_scope()
            self.assertAlmostEqual(scope.snapshot_deadline, started + 10)
            self.broker._invalidate_watch_market()
            self.assertIsNone(self.broker._execution_snapshot())
        self.assertIsNone(self.broker.watch_entry_snapshot(self.contract))

    async def test_inflight_refresh_cannot_publish_after_mutation(self):
        await self.broker.prewarm_entry(self.contract)
        started, cancelled = asyncio.Event(), asyncio.Event()

        async def held_snapshot():
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

        self.broker.snapshot = held_snapshot
        result = await self.broker.refresh_watch_market(self.contract)
        self.assertTrue(result["quote_timestamp"])
        await started.wait()
        snapshot_task = self.broker._watch_snapshot_task
        self.broker._invalidate_watch_market()
        await asyncio.gather(snapshot_task, return_exceptions=True)
        self.assertTrue(cancelled.is_set())
        self.assertIsNone(self.broker._watch_snapshot)
        self.assertEqual(self.broker._watch_quotes, {})

    async def test_slow_read_age_is_not_reset_when_handed_to_execution(self):
        await self.warm_market()
        started, generation, snapshot = self.broker._watch_snapshot
        with self.broker.execution_reads():
            self.assertIsNotNone(self.broker.watch_entry_snapshot(self.contract))
            scope = self.broker._active_execution_scope()
            scope.snapshot_deadline = time.monotonic() - 1
            self.assertIsNone(self.broker._execution_snapshot())
        self.broker.account_changed.set()
        self.assertIsNone(self.broker.watch_entry_snapshot(self.contract))

    async def test_budget_rejection_is_definitively_unsubmitted(self):
        await self.broker.prewarm_entry(self.contract)
        original = self.broker._data.side_effect

        async def limited(name, args):
            if name == "place_option_order":
                raise _BrokerBudgetHold("no request sent")
            return await original(name, args)

        self.broker._data.side_effect = limited
        order = self.order | {"prepared_entry": True}
        with self.assertRaises(_BrokerBudgetHold):
            await self.broker.submit(order, before_submit=AsyncMock())
        self.assertFalse(order["broker_submission"]["transport_attempted"])
        self.assertNotIn("submitted_at", order["broker_submission"])
        self.assertNotIn(order["client_order_id"], self.broker.attempted)
