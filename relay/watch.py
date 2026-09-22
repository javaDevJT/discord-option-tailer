"""Prepare bounded, source-bound watch candidates without authorizing an entry."""
from __future__ import annotations

import asyncio
import re
import time

from .core import EASTERN, Hold, canonical_contract, channel_allows_author, instant
from .entry_rules import _normalize_expiry, _source_date, option_matches, visible_text


def watch_candidate(message):
    text = visible_text(message)
    if (not re.search(r"\b(?:on\s+watch|eyes\s+on|watching)\b", text, re.I)
            or re.search(r"\b(?:test|example|cancel(?:led)?|no\s+longer|entry|open|bought|bto)\b", text, re.I)):
        return None
    contracts = option_matches(re.sub(r"\b(calls|puts)\b", lambda m: m.group(0)[:-1], text, flags=re.I))
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
                     expires=time.monotonic() + 3600, refresh_at=0, busy=False)
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
        try:
            if not self._current(entry):
                return
            requested = entry["requested"]
            if requested["expiry"] == "nearest":
                anchored = canonical_contract(requested | {"expiry": entry["day"]})
                contract = canonical_contract(await self.engine.broker.nearest_expiry(anchored))
                if (any(contract[k] != anchored[k] for k in ("symbol", "strike", "option_type"))
                        or contract["expiry"] < anchored["expiry"]):
                    raise Hold("watch expiry resolution changed the intended contract")
            else:
                contract = canonical_contract(requested)
            await self.engine.broker.prewarm_entry(contract)
            if self._current(entry):
                entry["contract"] = contract
        except asyncio.CancelledError:
            raise
        except Exception:
            # Preparation failure falls back to the existing fresh execution path.
            entry["contract"] = None
        finally:
            entry["refresh_at"] = time.monotonic() + 30
            entry["busy"] = False

    def match(self, message, decision, *, with_source=False):
        if decision.get("action") != "OPEN":
            return None
        requested = decision.get("contract") or {}
        group = self.engine.channels.get(str(message.get("channel_id")), {}).get("source_group")
        for entry in reversed(list(self.entries.values())):
            contract = entry["contract"]
            if (not contract or entry["group"] != group or not self._current(entry)
                    or entry["day"] != _source_date(message)
                    or instant(entry["message"]["timestamp"]) > instant(message["timestamp"])):
                continue
            try:
                normalized = canonical_contract(requested | {"expiry": contract["expiry"]})
            except Hold:
                continue
            if normalized != contract:
                continue
            expiry = requested.get("expiry")
            if expiry == "nearest" and entry["requested"]["expiry"] != "nearest":
                continue
            if expiry != "nearest" and expiry != contract["expiry"]:
                continue
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
                await asyncio.sleep(1)
        finally:
            await self.close()

    async def close(self):
        pending = list(self.tasks)
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
