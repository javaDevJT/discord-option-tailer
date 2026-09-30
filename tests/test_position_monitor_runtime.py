"""Runtime checks for durable position monitor context."""

import json
import unittest
from datetime import datetime, timedelta

from tests import test_exit_recovery as fixtures


class PositionMonitorRuntimeChecks(unittest.IsolatedAsyncioTestCase):
    setUp = fixtures.ExitRecoveryChecks.setUp
    message = fixtures.ExitRecoveryChecks.message
    open_position = fixtures.ExitRecoveryChecks.open_position
    missed = fixtures.ExitRecoveryChecks.missed

    async def test_older_group_history_does_not_truncate_source_context(self):
        await self.open_position()
        source = await self.missed(content="Watch my position")
        source_time = datetime.fromisoformat(source["timestamp"])
        group = source["source_group"]

        with self.store.db:
            for index in range(61):
                timestamp = (source_time - timedelta(seconds=index + 1)).isoformat()
                old_message = dict(source)
                old_message.update(
                    id=f"old-history-{index}",
                    timestamp=timestamp,
                    revision=f"old-revision-{index}",
                    content="unrelated older discussion",
                    ingestion="history",
                    ingestion_reason="backscroll",
                )
                self.store.db.execute(
                    "INSERT INTO messages (id, channel_id, source_group, timestamp, revision, body) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        old_message["id"],
                        old_message["channel_id"],
                        group,
                        timestamp,
                        old_message["revision"],
                        json.dumps(old_message),
                    ),
                )

        context, truncated = self.engine.monitors.context(source)

        self.assertEqual(context, [])
        self.assertFalse(truncated)


if __name__ == "__main__":
    unittest.main()
