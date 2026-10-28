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


WATCH_POLL_SECONDS = 0.25
WATCH_POLL_WINDOW_SECONDS = 600
WATCH_MARKET_CONCURRENCY = 1
WATCH_MARKET_REQUEST_INTERVAL_SECONDS = 6.0
WATCH_MARKET_MAX_CONTRACTS = 8
WATCH_PREPARATION_REFRESH_MIN_SECONDS = 45.0
WATCH_PREPARATION_REFRESH_MAX_SECONDS = 55.0
WATCH_REFRESH_TIMEOUT_SECONDS = 8.0


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
        self.market_tasks = set()
        self.market_inflight_contracts = set()
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
                     market_polled_at=0, market_success_at=0)
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

    def _preparation_refresh_interval(self, entry):
        identities = list(self.entries)
        try:
            index = identities.index(entry["message"]["id"])
        except ValueError:
            index = 0
        fraction = index / max(1, len(identities) - 1)
        return WATCH_PREPARATION_REFRESH_MIN_SECONDS + fraction * (
            WATCH_PREPARATION_REFRESH_MAX_SECONDS - WATCH_PREPARATION_REFRESH_MIN_SECONDS)

    async def _prepare(self, entry):
        previous_resolution = entry.get("diagnostic", {}).get("expiry_resolution")
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
                    if entry["contract"] is not None and entry["contract"]["expiry"] == entry["day"]:
                        contract = canonical_contract(entry["contract"])
                        if previous_resolution:
                            diagnostic["expiry_resolution"] = copy.deepcopy(previous_resolution)
                    else:
                        # A later default must not become a permanent choice:
                        # today's contract can be missing from earlier discovery.
                        # Do not expose the old fallback while rechecking it.
                        entry["contract"] = None
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
            entry["refresh_at"] = time.monotonic() + self._preparation_refresh_interval(entry)
            entry["busy"] = False

    def _next_market_entries(self):
        now = time.monotonic()
        # Duplicate notices for the same contract share one slot in the rotation.
        candidates = {}
        for entry in self.entries.values():
            # A refresh can still be using the previous contract while its
            # metadata is being renewed. Do not let that busy, possibly stale
            # contract enter a batch.
            if (entry.get("busy") or not entry["contract"]
                    or now >= entry["hot_until"] or not self._current(entry)):
                continue
            # Broker.prepared_entry is a synchronous local-cache check. Use it
            # when available so an expired/unprepared option cannot make the
            # broker reject an otherwise healthy quote batch. Keep brokers and
            # test doubles without this optional cache API compatible.
            metadata_check = getattr(self.engine.broker, "prepared_entry", None)
            if callable(metadata_check) and not asyncio.iscoroutinefunction(metadata_check):
                try:
                    prepared = metadata_check(entry["contract"], "1")
                except Exception:
                    continue
                if asyncio.iscoroutine(prepared):
                    prepared.close()
                elif prepared is None:
                    continue
            key = tuple(entry["contract"][key] for key in ("symbol", "expiry", "strike", "option_type"))
            if key in self.market_inflight_contracts:
                continue
            previous = candidates.get(key)
            if previous is not None:
                entry["market_polled_at"] = max(entry["market_polled_at"], previous["market_polled_at"])
            candidates[key] = entry
        batch_method = getattr(self.engine.broker, "refresh_watch_market_batch", None)
        limit_getter = getattr(self.engine.broker, "watch_market_batch_limit", None)
        if callable(batch_method) and callable(limit_getter):
            try:
                limit = max(1, min(WATCH_MARKET_MAX_CONTRACTS, int(limit_getter())))
            except (TypeError, ValueError):
                limit = 1
        else:
            limit = 1
        return sorted(candidates.values(), key=lambda entry: entry["market_polled_at"])[:limit]

    def _next_market_entry(self):
        return next(iter(self._next_market_entries()), None)

    async def _refresh_market(self, entry):
        await self._refresh_market_batch([entry])

    async def _refresh_market_batch(self, entries):
        if not entries:
            return
        started = time.monotonic()
        active = []
        for entry in entries:
            contract_key = tuple(entry["contract"][key] for key in ("symbol", "expiry", "strike", "option_type"))
            remaining = entry["hot_until"] - started
            if remaining <= 0 or not self._current(entry) or contract_key in self.market_inflight_contracts:
                continue
            self.market_inflight_contracts.add(contract_key)
            active.append((contract_key, entry, min(remaining, WATCH_REFRESH_TIMEOUT_SECONDS)))
        if not active:
            return

        timeout = min(deadline for _, _, deadline in active)
        schedule = {
            "poll_interval_seconds": WATCH_MARKET_REQUEST_INTERVAL_SECONDS,
            "concurrency_limit": WATCH_MARKET_CONCURRENCY,
            "batch_size": len(active),
            "last_attempt_age_seconds": round(min(
                max(0, started - entry["market_polled_at"]) for _, entry, _ in active), 6),
            "last_success_age_seconds": (round(min(
                max(0, started - entry["market_success_at"]) for _, entry, _ in active
                if entry["market_success_at"]), 6)
                if any(entry["market_success_at"] for _, entry, _ in active) else None),
        }
        for _, entry, deadline in active:
            entry["market_diagnostic"] = {
                "state": "refreshing", "started_at": self.engine.clock().isoformat(),
                "deadline_seconds": round(deadline, 6), "schedule": copy.deepcopy(schedule),
            }

        try:
            result = await asyncio.wait_for(
                self._call_watch_market_batch([entry for _, entry, _ in active]), timeout=timeout)
            quote_rows = result.get("quotes") if isinstance(result, dict) else None
            quote_by_contract = {}
            if isinstance(quote_rows, list):
                for quote in quote_rows:
                    if not isinstance(quote, dict) or not isinstance(quote.get("contract"), dict):
                        continue
                    key = tuple(quote["contract"].get(name)
                                for name in ("symbol", "expiry", "strike", "option_type"))
                    if key in quote_by_contract:
                        raise RuntimeError("Watch quote batch returned a duplicate contract")
                    quote_by_contract[key] = quote

            completed = time.monotonic()
            for contract_key, entry, _ in active:
                quote = quote_by_contract.get(contract_key)
                if quote is None:
                    error = RuntimeError("Watch quote batch omitted a prepared contract")
                    error._watch_market_diagnostic = {
                        "capacity_state": "shortfall", "batch_size": len(active),
                    }
                    self._set_market_failure(entry, error, schedule, started, completed)
                    continue
                quote_data = {key: value for key, value in result.items() if key != "quotes"}
                quote_data["quote"] = copy.deepcopy(quote)
                entry["market_diagnostic"].update(
                    state="ready", completed_at=self.engine.clock().isoformat(), data=quote_data,
                    duration_seconds=round(completed - started, 6))
                entry["market_success_at"] = completed
        except asyncio.CancelledError:
            for _, entry, _ in active:
                entry["market_diagnostic"]["state"] = "cancelled"
            raise
        except Exception as error:
            completed = time.monotonic()
            for _, entry, _ in active:
                self._set_market_failure(entry, error, schedule, started, completed)
        finally:
            completed = time.monotonic()
            for contract_key, entry, _ in active:
                entry["market_polled_at"] = completed
                self.market_inflight_contracts.discard(contract_key)

    async def _call_watch_market_batch(self, entries):
        method = getattr(self.engine.broker, "refresh_watch_market_batch", None)
        if callable(method):
            return await method([dict(entry["contract"]) for entry in entries])
        method = getattr(self.engine.broker, "refresh_watch_market", None)
        if not callable(method):
            raise RuntimeError("Broker does not support watch market refresh")
        results = await asyncio.gather(*(method(dict(entry["contract"])) for entry in entries))
        return {"quotes": [
            {"contract": dict(entry["contract"]), **result}
            for entry, result in zip(entries, results)
        ]}

    def _set_market_failure(self, entry, error, schedule, started, completed):
        diagnostic = entry.setdefault("market_diagnostic", {})
        diagnostic.update(state="failed", schedule=copy.deepcopy(schedule),
                          duration_seconds=round(completed - started, 6))
        _, diagnostic["failure"] = execution_failure(error, stage="watch_market_refresh")
        details = getattr(error, "_watch_market_diagnostic", None)
        if isinstance(details, dict):
            for key in ("deadline_seconds", "capacity_state", "request_budget", "batch_size", "batch_limit"):
                if key in details:
                    diagnostic[key] = copy.deepcopy(details[key])

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
                    diagnostic["market_success_age_seconds"] = (
                        round(max(0, time.monotonic() - entry["market_success_at"]), 6)
                        if entry["market_success_at"] else None)
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
            if (entry["busy"] and contract and entry["requested"]["expiry"] == "nearest"
                    and contract["expiry"] != entry["day"]):
                report(entry, "watch_preparing")
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
                self.market_tasks.intersection_update(task for task in self.market_tasks if not task.done())
                if ((callable(getattr(self.engine.broker, "refresh_watch_market", None))
                     or callable(getattr(self.engine.broker, "refresh_watch_market_batch", None)))
                        and len(self.market_tasks) < WATCH_MARKET_CONCURRENCY
                        and time.monotonic() >= self.market_refresh_at):
                    entries = self._next_market_entries()
                    if entries:
                        task = asyncio.create_task(self._refresh_market_batch(entries))
                        self.market_tasks.add(task)
                        self.tasks.add(task)
                        task.add_done_callback(self.tasks.discard)
                        task.add_done_callback(self.market_tasks.discard)
                        self.market_refresh_at = time.monotonic() + WATCH_MARKET_REQUEST_INTERVAL_SECONDS
                delay = WATCH_POLL_SECONDS
                if len(self.market_tasks) < WATCH_MARKET_CONCURRENCY and self.market_refresh_at > time.monotonic():
                    delay = min(delay, self.market_refresh_at - time.monotonic())
                await asyncio.sleep(min(1, max(.01, delay)))
        finally:
            await self.close()

    async def close(self):
        pending = list(self.tasks)
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        close_market = getattr(self.engine.broker, "close_watch_market", None)
        if callable(close_market):
            await close_market()
