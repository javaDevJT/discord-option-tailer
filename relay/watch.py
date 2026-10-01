"""Prepare bounded, source-bound watch candidates without authorizing an entry."""
from __future__ import annotations

import asyncio
import copy
from contextlib import nullcontext
import re
import time

from .core import EASTERN, Hold, canonical_contract, channel_allows_author, instant
from .entry_rules import _normalize_expiry, _source_date, option_matches, visible_text
from .status import execution_failure


WATCH_POLL_SECONDS = 3
WATCH_POLL_WINDOW_SECONDS = 600
WATCH_REFRESH_TIMEOUT_SECONDS = 10


def watch_candidate(message):
    text = visible_text(message)
    if (not re.search(r"\b(?:on\s+watch|eyes\s+on|watching)\b", text, re.I)
            or re.search(r"\b(?:test|example|cancel(?:led)?|no\s+longer|entry|open|bought|bto)\b", text, re.I)):
        return None
    # Watch shorthand such as QQQ740P only prepares metadata, never an order.
    contract_text = re.sub(r"\b([A-Z]{1,6})(\$?\d+(?:\.\d+)?[CP])\b", r"\1 \2", text)
    contracts = option_matches(re.sub(r"\b(calls|puts)\b", lambda m: m.group(0)[:-1], contract_text, flags=re.I))
    # "At the ask today" dates the observed purchase, not the option's expiry.
    expiry_text = re.sub(r"\bat\s+(?:the\s+)?ask\s+today\b", "at the ask", text, flags=re.I)
    expiry, invalid = _normalize_expiry(expiry_text, message)
    if len(contracts) != 1 or invalid or not _source_date(message):
        return None
    return contracts[0] | {"expiry": expiry or "nearest"}


class WatchEntries:
    # ponytail: eight candidates for one hour; larger watchlists need explicit scheduling.
    def __init__(self, engine):
        self.engine = engine
        self.entries = {}
        self.tasks = set()
        self.market_task = None
        self.market_refresh_at = 0

    def note(self, message):
        channel = self.engine.channels.get(str(message.get("channel_id")))
        if (self.engine.mode == "paper" or not callable(getattr(self.engine.broker, "prewarm_entry", None))
                or not channel or channel.get("role") != "signals"
                or not channel_allows_author(channel, message.get("author_id", ""))
                or message.get("source") not in {"browser", "gateway"}
                or message.get("ingestion") != "live" or message.get("edited_timestamp")):
            return
        try:
            self.engine.fresh(message, self.engine.clock())
        except Hold:
            return
        candidate = watch_candidate(message)
        if candidate is None:
            return
        entry = dict(message=dict(message), requested=candidate, contract=None,
                     group=channel["source_group"], day=_source_date(message),
                     expires=time.monotonic() + 3600, refresh_at=0, busy=False,
                     hot_until=time.monotonic() + max(0, WATCH_POLL_WINDOW_SECONDS -
                         max(0, (self.engine.clock() - instant(message["timestamp"])).total_seconds())),
                     market_polled_at=0)
        self.entries.pop(message["id"], None)
        self.entries[message["id"]] = entry
        while len(self.entries) > 8:
            self.entries.pop(next(iter(self.entries)))
        self._schedule(entry)

    def _current(self, entry):
        message = entry["message"]
        row = self.engine.store.db.execute("SELECT revision FROM messages WHERE id=?", (message["id"],)).fetchone()
        return (time.monotonic() < entry["expires"]
                and entry["day"] == self.engine.clock().astimezone(EASTERN).date().isoformat()
                and row is not None and row[0] == message["revision"]
                and self.engine.observations.get(message["id"], message["revision"]) == message["revision"])

    def _schedule(self, entry):
        if entry["busy"]:
            return
        entry["busy"] = True
        task = asyncio.create_task(self._prepare(entry))
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)

    async def _prepare(self, entry):
        diagnostic = {"route": "fresh", "reason": "watch_preparing",
                      "watch_message_id": entry["message"]["id"],
                      "attempts": entry.get("diagnostic", {}).get("attempts", 0) + 1,
                      "attempted_at": self.engine.clock().isoformat()}
        entry["diagnostic"] = diagnostic
        try:
            if not self._current(entry):
                return
            async with getattr(self.engine.broker, "background_reads", nullcontext)():
                requested = entry["requested"]
                if requested["expiry"] == "nearest":
                    anchored = canonical_contract(requested | {"expiry": entry["day"]})
                    resolution = diagnostic.setdefault("expiry_resolution", {})
                    contract = canonical_contract(await self.engine.broker.nearest_expiry(anchored, diagnostic=resolution))
                    if (any(contract[k] != anchored[k] for k in ("symbol", "strike", "option_type"))
                            or contract["expiry"] < anchored["expiry"]):
                        raise Hold("watch expiry resolution changed the intended contract")
                else:
                    contract = canonical_contract(requested)
                await self.engine.broker.prewarm_entry(contract)
            if self._current(entry):
                entry["contract"] = contract
                diagnostic["reason"] = "prepared"
        except asyncio.CancelledError:
            raise
        except Exception as error:
            # Preparation failure falls back to the existing fresh execution path.
            entry["contract"] = None
            diagnostic["reason"] = "watch_preparation_failed"
            _, diagnostic["failure"] = execution_failure(error, stage="watch_preparation")
        finally:
            entry["refresh_at"] = time.monotonic() + 30
            entry["busy"] = False

    def _next_market_entry(self):
        now = time.monotonic()
        # Duplicate notices for the same contract share one slot in the rotation.
        candidates = {}
        for entry in self.entries.values():
            if not entry["contract"] or now >= entry["hot_until"] or not self._current(entry):
                continue
            key = tuple(entry["contract"][key] for key in ("symbol", "expiry", "strike", "option_type"))
            previous = candidates.get(key)
            if previous is not None:
                entry["market_polled_at"] = max(entry["market_polled_at"], previous["market_polled_at"])
            candidates[key] = entry
        return min(candidates.values(), key=lambda entry: entry["market_polled_at"], default=None)

    async def _refresh_market(self, entry):
        started = time.monotonic()
        remaining = entry["hot_until"] - started
        if remaining <= 0 or not self._current(entry):
            return
        diagnostic = {"state": "refreshing", "started_at": self.engine.clock().isoformat()}
        entry["market_diagnostic"] = diagnostic
        try:
            result = await asyncio.wait_for(
                self.engine.broker.refresh_watch_market(dict(entry["contract"])),
                timeout=min(remaining, WATCH_REFRESH_TIMEOUT_SECONDS))
            diagnostic.update(state="ready", completed_at=self.engine.clock().isoformat(), data=result)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            diagnostic["state"] = "failed"
            _, diagnostic["failure"] = execution_failure(error, stage="watch_market_refresh")
        finally:
            completed = time.monotonic()
            diagnostic["duration_seconds"] = round(completed - started, 6)
            entry["market_polled_at"] = completed
            # Network and budget waits do not accumulate a catch-up burst.
            self.market_refresh_at = completed + WATCH_POLL_SECONDS

    def match(self, message, decision, *, with_source=False, diagnostic=None):
        if decision.get("action") != "OPEN":
            return None
        requested = decision.get("contract") or {}
        group = self.engine.channels.get(str(message.get("channel_id")), {}).get("source_group")
        if diagnostic is not None:
            diagnostic.clear()
            diagnostic.update(route="fresh", reason="no_matching_watch" if self.entries else "no_watch_cached")

        def report(entry, reason=None):
            if diagnostic is not None:
                diagnostic.clear()
                diagnostic.update(copy.deepcopy(entry.get("diagnostic", {})))
                if "market_diagnostic" in entry:
                    diagnostic["market_refresh"] = copy.deepcopy(entry["market_diagnostic"])
                    diagnostic["market_poll_age_seconds"] = round(max(0, time.monotonic() - entry["market_polled_at"]), 6)
                diagnostic["market_poll_window_remaining_seconds"] = round(max(0, entry["hot_until"] - time.monotonic()), 3)
                diagnostic.update(route="fresh", watch_message_id=entry["message"]["id"],
                                  watch_age_seconds=max(0, (instant(message["timestamp"]) - instant(entry["message"]["timestamp"])).total_seconds()))
                diagnostic.setdefault("reason", "watch_preparing")
                if reason:
                    diagnostic["reason"] = reason

        for entry in reversed(list(self.entries.values())):
            contract = entry["contract"]
            if (entry["group"] != group
                    or entry["day"] != _source_date(message)
                or instant(entry["message"]["timestamp"]) > instant(message["timestamp"])):
                continue
            expiry = requested.get("expiry")
            if expiry == "nearest" and entry["requested"]["expiry"] != "nearest":
                continue
            if contract and expiry != "nearest" and expiry != contract["expiry"]:
                continue
            if not contract and expiry != "nearest" and entry["requested"]["expiry"] not in {"nearest", expiry}:
                continue
            try:
                candidate = contract or canonical_contract(entry["requested"] | {"expiry": entry["day"]})
                normalized = canonical_contract(requested | {"expiry": candidate["expiry"]})
            except Hold:
                continue
            if normalized != candidate:
                continue
            # Retain the newest relevant failure unless an older usable watch wins.
            if diagnostic is not None and "watch_message_id" not in diagnostic:
                report(entry)
            if not self._current(entry):
                if diagnostic is not None and diagnostic.get("watch_message_id") == entry["message"]["id"]:
                    diagnostic["reason"] = "watch_expired" if time.monotonic() >= entry["expires"] else "watch_invalidated"
                continue
            if not contract:
                continue
            report(entry, "prepared")
            if with_source:
                return {"message_id": entry["message"]["id"], "revision": entry["message"]["revision"]}
            return dict(contract)
        return None

    async def run(self):
        try:
            while True:
                for identity, entry in list(self.entries.items()):
                    if not self._current(entry):
                        self.entries.pop(identity, None)
                    elif time.monotonic() >= entry["refresh_at"]:
                        self._schedule(entry)
                if (callable(getattr(self.engine.broker, "refresh_watch_market", None))
                        and (self.market_task is None or self.market_task.done())
                        and time.monotonic() >= self.market_refresh_at):
                    entry = self._next_market_entry()
                    if entry is not None:
                        self.market_task = asyncio.create_task(self._refresh_market(entry))
                        self.tasks.add(self.market_task)
                        self.market_task.add_done_callback(self.tasks.discard)
                await asyncio.sleep(1)
        finally:
            await self.close()

    async def close(self):
        pending = list(self.tasks)
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
