"""Offline checks of monitor dispatch, persisted claims, and concurrent invalidation."""
import asyncio
from datetime import timedelta
import unittest
from unittest.mock import AsyncMock
from relay.core import instant
from relay.monitoring import PositionMonitors
from tests import test_monitor_recovery as fixtures


class MonitorExecutionChecks(unittest.IsolatedAsyncioTestCase):
    setUp = fixtures.MonitorRecoveryChecks.setUp
    message = fixtures.MonitorRecoveryChecks.message
    open_position = fixtures.MonitorRecoveryChecks.open_position
    request_monitor = fixtures.MonitorRecoveryChecks.request_monitor

    async def arm(self):
        await self.open_position()
        self.request_monitor()
        self.interpreter.decision['monitor']['conditions'][0].update(comparison='gte', threshold='.90')
        self.source = self.message(content='Watch this position and close at my risk threshold', timestamp=self.now.isoformat())
        result = await self.engine.handle(self.source)
        self.assertEqual(result['state'], 'monitoring', result)
        self.broker.account['positions'] = self.store.positions()
        self.identity = self.store.db.execute('SELECT id FROM position_monitors').fetchone()[0]
        self.answer = self.interpreter.decision | {
            'action': 'CLOSE', 'monitor': None, 'origin_message_id': self.source['id'],
            'evidence': [{'message_id': self.source['id'], 'quote': self.source['content']}]}
        self.interpreter.assess_monitor = AsyncMock(return_value=self.answer)
        return self.engine.monitors

    def row(self):
        return self.engine.monitors.get(self.identity)

    def cancel(self):
        with self.store.db:
            self.store.db.execute("UPDATE position_monitors SET state='canceled' WHERE id=?", (self.identity,))

    async def test_sells_once_verifies_actual_source_and_never_reads_unneeded_underlying(self):
        monitors = await self.arm()
        self.broker.underlying_quote = AsyncMock(side_effect=RuntimeError('unavailable'))
        await monitors.poll_once(background=False)
        self.assertEqual(self.row()['state'], 'completed', self.row())
        self.assertEqual(len(self.broker.submissions), 2)
        self.assertEqual(self.store.positions(), [])
        self.broker.underlying_quote.assert_not_awaited()
        self.assertEqual(self.engine.verify_current.call_args.args[0]['id'], self.source['id'])
        self.assertIn('position monitor reassessment', self.row()['body']['reason'])
        self.engine.monitors = PositionMonitors(self.engine)
        await self.engine.monitors.poll_once(background=False)
        self.assertEqual(len(self.broker.submissions), 2)

    async def test_restart_resumes_claim_even_after_condition_recovers(self):
        monitors = await self.arm()
        row = self.row()
        event_id = 'monitor:' + self.identity + ':0'
        row['body'].update(event_id=event_id, triggered_at=self.now.isoformat())
        monitors.save(row, state='evaluating')
        self.broker.quotes.update(bid='.70', ask='.75')
        self.answer.update(action='WAIT')
        self.engine.monitors = PositionMonitors(self.engine)
        await self.engine.monitors.poll_once(background=False)
        facts = self.interpreter.assess_monitor.call_args.args[-1]
        self.assertEqual(facts['trigger_reason'], 'resume')
        self.assertEqual(facts['triggered_conditions'], [])
        self.assertEqual(self.row()['body']['event_id'], event_id)
        self.assertEqual(self.row()['state'], 'completed')
        self.assertEqual(len(self.broker.submissions), 1)

    async def test_observation_cannot_resurrect_canceled_plan(self):
        monitors = await self.arm()
        entered, release = asyncio.Event(), asyncio.Event()
        async def snapshot():
            entered.set()
            await release.wait()
            return self.broker.account
        self.broker.snapshot = snapshot
        task = asyncio.create_task(monitors.poll_once(background=False))
        await entered.wait()
        self.cancel()
        release.set()
        await task
        self.assertEqual(self.row()['state'], 'canceled')
        self.interpreter.assess_monitor.assert_not_awaited()
        self.assertEqual(len(self.broker.submissions), 1)

    async def test_model_result_cannot_execute_after_plan_is_canceled(self):
        monitors = await self.arm()
        entered, release = asyncio.Event(), asyncio.Event()
        async def assess(*args):
            entered.set()
            await release.wait()
            return self.answer
        self.interpreter.assess_monitor = assess
        task = asyncio.create_task(monitors.poll_once(background=False))
        await entered.wait()
        self.cancel()
        release.set()
        await task
        self.assertEqual(self.row()['state'], 'canceled')
        self.assertEqual(len(self.broker.submissions), 1)

    async def test_rearm_shortens_deadline_and_clears_generation_claim(self):
        monitors = await self.arm()
        plan = dict(self.row()['body']['plan'], duration_seconds=60, reassess_after_seconds=30)
        self.answer.update(action='WAIT', monitor=plan)
        await monitors.poll_once(background=False)
        row = self.row()
        self.assertEqual(row['state'], 'active', row)
        self.assertEqual(instant(row['expires_at']), self.now + timedelta(seconds=60))
        self.assertEqual(row['body']['generation'], 1)
        self.assertIsNone(row['body']['event_id'])
        self.assertNotIn('triggered_at', row['body'])
        self.assertEqual(len(self.broker.submissions), 1)

    async def test_unexpected_actions_and_stale_data_cannot_dispatch(self):
        monitors = await self.arm()
        for action in ('OPEN', 'UPDATE_STOP', 'ADD'):
            with self.subTest(action=action):
                self.answer['action'] = action
                await monitors.poll_once(background=False)
                self.assertEqual(self.row()['state'], 'error')
                self.assertEqual(len(self.broker.submissions), 1)
                self.now += timedelta(seconds=31)
                self.broker.account['timestamp'] = self.now.isoformat()
                self.broker.quotes['timestamp'] = self.now.isoformat()
        self.interpreter.assess_monitor.reset_mock()
        self.broker.quotes['timestamp'] = (self.now - timedelta(hours=1)).isoformat()
        await monitors.poll_once(background=False)
        self.interpreter.assess_monitor.assert_not_awaited()
        self.assertEqual(self.row()['body']['diagnostic']['stage'], 'monitor_poll')
        self.assertEqual(len(self.broker.submissions), 1)

    async def test_expiry_cancels_waiting_model_without_a_sale(self):
        monitors = await self.arm()
        entered = asyncio.Event()
        async def assess(*args):
            entered.set()
            await asyncio.Event().wait()
        self.interpreter.assess_monitor = assess
        await monitors.poll_once()
        await entered.wait()
        self.now = instant(self.row()['expires_at']) + timedelta(seconds=1)
        runner = asyncio.create_task(monitors.run())
        try:
            await asyncio.sleep(.02)
            self.assertEqual(self.row()['state'], 'expired')
            self.assertEqual(len(self.broker.submissions), 1)
        finally:
            runner.cancel()
            await asyncio.gather(runner, return_exceptions=True)


if __name__ == '__main__':
    unittest.main()
