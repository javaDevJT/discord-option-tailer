"""Persist display-only account values; trading never consumes this cache."""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import json
import logging

from .broker import is_auth_required

REFRESH_SECONDS = 3600
CACHE_KEY = "account_overview"
REFRESH_ERROR = "Account refresh failed. Check the Robinhood connection in Setup."
AUTH_ERROR = "Robinhood needs reauthentication. Reconnect in Setup."


def timestamp(value):
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed if parsed.tzinfo is not None else None
    except (AttributeError, TypeError, ValueError):
        return None


class AccountCache:
    def __init__(self, store, broker, *, clock=None):
        self.store, self.broker = store, broker
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        row = store.db.execute("SELECT value FROM metadata WHERE key=?", (CACHE_KEY,)).fetchone()
        try:
            value = json.loads(row[0]) if row else {}
        except (TypeError, ValueError):
            value = {}
        self.value = value if isinstance(value, dict) and value.get("account_id") == broker.account_number else {}

    def seconds_until_due(self):
        last = timestamp(self.value.get("last_attempt_at"))
        elapsed = (self.clock() - last).total_seconds() if last else REFRESH_SECONDS
        # A clock correction must not defer the next refresh indefinitely.
        return max(0, REFRESH_SECONDS - elapsed) if elapsed >= 0 else 0

    def save(self):
        with self.store.db:
            self.store.db.execute("INSERT OR REPLACE INTO metadata(key,value) VALUES (?,?)",
                                  (CACHE_KEY, json.dumps(self.value)))

    async def refresh(self, *, force=False):
        if not force and self.seconds_until_due() > 0:
            return False
        self.value.update(account_id=self.broker.account_number, last_attempt_at=self.clock().isoformat())
        # Persist the attempt too: restarts and failures cannot create a polling storm.
        self.save()
        try:
            overview = await asyncio.wait_for(self.broker.account_overview(), timeout=60)
        except Exception as exc:
            self.value["error"] = AUTH_ERROR if is_auth_required(exc) else REFRESH_ERROR
        else:
            self.value = dict(overview, account_id=self.broker.account_number,
                              last_attempt_at=self.value["last_attempt_at"],
                              updated_at=self.clock().isoformat(), error=None)
        self.save()
        return True

    async def run(self):
        while True:
            force = self.broker.account_changed.is_set()
            self.broker.account_changed.clear()
            try:
                await self.refresh(force=force)
            except Exception as exc:
                # Display telemetry must not stop the execution worker on a disk error.
                logging.getLogger(__name__).warning("Account cache unavailable (%s)", type(exc).__name__)
            try:
                await asyncio.wait_for(self.broker.account_changed.wait(), timeout=self.seconds_until_due())
            except asyncio.TimeoutError:
                pass
