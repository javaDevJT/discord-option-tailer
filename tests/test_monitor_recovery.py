"""Position monitors reuse recovery without replaying historical entry orders."""

import unittest
from relay.recovery import RecoveryEvaluator
from tests import test_exit_recovery as fixtures


class MonitorRecoveryChecks(unittest.IsolatedAsyncioTestCase):
    setUp = fixtures.ExitRecoveryChecks.setUp
    message = fixtures.ExitRecoveryChecks.message
    open_position = fixtures.ExitRecoveryChecks.open_position
    missed = fixtures.ExitRecoveryChecks.missed

    def request_monitor(self):
        self.interpreter.decision.update(
            action="WAIT", alert_price=None,
            monitor={"duration_seconds": 3600, "poll_interval_seconds": 15,
                     "reassess_after_seconds": 300,
                     "conditions": [{"metric": "option_bid", "comparison": "lte", "threshold": ".50"}]},
            reason="Monitor the held position and reconsider below the stated level")

    async def test_recovery_arms_and_restart_scan_does_not_replay_monitor(self):
        recovery = await self.open_position()
        message = await self.missed(content="Watch my position; reconsider if premium drops below .50")
        self.request_monitor()
        result = await recovery.assess(message)
        self.assertEqual(result["state"], "monitoring", result)
        self.assertEqual(len(self.broker.submissions), 1)
        row = self.store.db.execute("SELECT id,state FROM position_monitors").fetchone()
        self.assertEqual(row["state"], "active")
        RecoveryEvaluator(self.engine).recover_position_context()
        state = self.store.db.execute("SELECT state FROM events WHERE message_id=?", (message["id"],)).fetchone()[0]
        self.assertEqual(state, "monitoring")
        self.assertEqual(self.store.db.execute("SELECT count(*) FROM position_monitors").fetchone()[0], 1)

    async def test_live_wait_arms_without_submitting_a_second_order(self):
        await self.open_position()
        self.request_monitor()
        result = await self.engine.handle(self.message(content="Watch this position", timestamp=self.now.isoformat()))
        self.assertEqual(result["state"], "monitoring", result)
        self.assertEqual(len(self.broker.submissions), 1)
        self.assertEqual(self.store.db.execute("SELECT count(*) FROM position_monitors").fetchone()[0], 1)

    async def test_monitor_requires_owned_position(self):
        self.request_monitor()
        result = await self.engine.handle(self.message(content="Watch this position"))
        self.assertEqual(result["state"], "held", result)
        self.assertEqual(self.store.db.execute("SELECT count(*) FROM position_monitors").fetchone()[0], 0)
        self.assertEqual(self.broker.submissions, [])

    def test_monitor_diagnostics_keep_correlation_and_remove_secrets(self):
        from relay.status import execution_failure, project_execution_context
        fields = {"monitor_id": "a" * 64, "poll_interval_seconds": 15,
                  "owned_quantity": 2, "action": "WAIT", "state": "active",
                  "access_token": "private", "raw_provider_response": {"secret": "private"}}
        safe = project_execution_context(fields)
        self.assertEqual(safe, {k: fields[k] for k in ("monitor_id", "poll_interval_seconds", "owned_quantity", "action", "state")})
        _, diagnostic = execution_failure(ValueError("private"), stage="monitor_poll", context=fields)
        self.assertEqual(diagnostic["stage"], "monitor_poll")
        self.assertNotIn("private", str(diagnostic))


if __name__ == "__main__":
    unittest.main()
