"""Reassess missed alerts and catch up exits for currently relay-owned positions."""
from __future__ import annotations

import asyncio
import json
import hashlib
import time
from decimal import ROUND_CEILING
from pathlib import Path

from .core import EASTERN, Hold, RetryHold, canonical_contract, contract_key, channel_allows_author, entry_size, instant, money, entry_chase_evaluation, entry_chase_reason, entry_chase_within_cap
from .interpreter import safe_interpretation_reason


class SourceContextChanged(Hold):
    """Discard a cached assessment and consider the newly observed source context."""


class RecoveryEvaluator:
    # ponytail: assess browser-observed alerts once per revision; full history sync is separate work.

    def __init__(self, engine):
        self.engine = engine
        self.store = engine.store
        self.scan_cursor = 0
        self.scan_complete = False
        self.exit_scan_cursor = 0
        self.exit_scan_complete = False
        self.next_reconcile = 0
        self.retry_due = {}
        self.deferred_exits = {}
        with self.store.db:
            self.store.db.execute("UPDATE events SET state='recovery_pending', reason='Recovery assessment interrupted; awaiting evaluation' WHERE state='recovery_evaluating'")

    def eligible(self, message):
        channel = self.engine.channels.get(message.get("channel_id"))
        if (not channel or channel.get("role") != "signals"
                or not channel_allows_author(channel, message.get("author_id", ""))
                or message.get("source") not in {"browser", "gateway"} or message.get("edited_timestamp")
                or message.get("ingestion") not in {"live", "baseline"}
                or message.get("ingestion_reason") in {"history", "backscroll", "edit"}):
            return False
        try:
            return (self.engine.clock() - instant(message["timestamp"])).total_seconds() >= 0
        except (Hold, KeyError):
            return False

    def enqueue(self, message, observed):
        if self.engine.interpreter is None or observed == "edit" or not self.eligible(message):
            return None
        event = self.store.db.execute("SELECT state,reason,decision FROM events WHERE message_id=? AND revision=?", (message["id"], message["revision"])).fetchone()
        if event is None or event["state"].startswith("recovery_"):
            return None
        baseline = message.get("ingestion") == "baseline" and event["state"] in {"context", "observed"}
        stale = event["state"] == "held" and ("signal is stale" in event["reason"] or "Worker stopped before interpretation" in event["reason"])
        if not (baseline or stale):
            return None
        if (self.store.db.execute("SELECT 1 FROM orders WHERE message_id=?", (message["id"],)).fetchone()
                or self.store.db.execute("SELECT 1 FROM events WHERE message_id=? AND revision!=?", (message["id"], message["revision"])).fetchone()):
            return None
        return self.store.record(message, "recovery_pending", "Missed alert queued for assessment; entries are review-only, verified owned exits may execute")

    def recover_saved(self):
        """Walk interrupted observations in bounded batches, including rows no longer in the DOM."""
        if self.scan_complete or self.engine.interpreter is None:
            return
        rows = self.store.db.execute("""SELECT e.id,e.state,m.body FROM events e JOIN messages m
            ON m.id=e.message_id AND m.revision=e.revision WHERE e.id>? AND e.state IN ('observed','context','held')
            AND NOT EXISTS (SELECT 1 FROM orders o WHERE o.message_id=m.id)
            AND NOT EXISTS (SELECT 1 FROM events other WHERE other.message_id=m.id AND other.revision!=m.revision)
            ORDER BY e.id LIMIT 100""", (self.scan_cursor,)).fetchall()
        for row in rows:
            self.scan_cursor = row["id"]
            message = json.loads(row["body"])
            if row["state"] == "observed" and self.eligible(message):
                self.store.record(message, "held", "Worker stopped before interpretation completed; recovery assessment only")
            self.enqueue(message, "same")
        self.scan_complete = len(rows) < 100

    def current_entries(self, group):
        """Latest filled entry identifies each currently owned position's lifetime."""
        rows = self.store.db.execute("""SELECT o.* FROM orders o JOIN positions p
            ON p.source_group=o.source_group AND p.contract=o.contract
            WHERE o.source_group=? AND o.action='OPEN' AND o.filled_quantity>0 AND p.quantity>0
            ORDER BY julianday(o.created_at) DESC, o.rowid DESC""", (group,)).fetchall()
        entries = {}
        for row in rows:
            entries.setdefault(row["contract"], dict(row))
        return entries

    def recover_position_context(self):
        if self.exit_scan_complete or self.engine.interpreter is None:
            return
        rows = self.store.db.execute("""SELECT e.id,e.state,m.body FROM events e JOIN messages m
            ON m.id=e.message_id AND m.revision=e.revision WHERE e.id>?
            AND EXISTS (SELECT 1 FROM positions p WHERE p.source_group=m.source_group AND p.quantity>0)
            AND NOT EXISTS (SELECT 1 FROM orders o WHERE o.message_id=m.id)
            AND NOT EXISTS (SELECT 1 FROM events x WHERE x.message_id=m.id AND x.revision!=m.revision)
            ORDER BY e.id LIMIT 100""", (self.exit_scan_cursor,)).fetchall()
        for row in rows:
            self.exit_scan_cursor = row["id"]
            message = json.loads(row["body"])
            if not self.eligible(message) or row["state"] in {"recovery_pending", "recovery_evaluating"}:
                continue
            group = self.engine.channels[message["channel_id"]]["source_group"]
            if any(instant(entry["created_at"]) <= instant(message["timestamp"])
                   for entry in self.current_entries(group).values()):
                self.store.record(message, "recovery_pending", "Checking owned positions for a missed exit; historical entries remain review-only")
        self.exit_scan_complete = len(rows) < 100

    async def reconcile_orders(self):
        if time.monotonic() < self.next_reconcile:
            return
        self.next_reconcile = time.monotonic() + 30
        rows = self.store.db.execute("""SELECT id,broker_id FROM orders
            WHERE status NOT IN ('filled','canceled','rejected','expired') LIMIT 100""").fetchall()
        for row in rows:
            try:
                options = {} if self.engine.mode == "paper" else {"broker_order_id": row["broker_id"]}
                result = await self.engine.broker.order_status(row["id"], **options)
                self.store.apply_result(row["id"], result)
            except Exception:
                # An uncertain submission keeps the existing unresolved-order gate.
                continue

    def exit_guard(self, message, decision, context, positions):
        engine = self.engine
        key = contract_key(decision["contract"])
        group = message["source_group"]
        entry = self.current_entries(group).get(key)
        if entry is None:
            raise Hold("missed exit has no identifiable currently owned entry")
        origin = engine.origin(message, decision)
        if instant(origin["timestamp"]) < instant(entry["created_at"]):
            raise Hold("missed exit predates the current position")
        signature = self.signature(context)
        latest_id = max(int(m["id"]) for m in [*context, message])
        order_id = hashlib.sha256((message["id"] + ":" + message["revision"]).encode()).hexdigest()

        def guard():
            if decision["action"] not in {"REDUCE", "CLOSE"} or not self.eligible(message):
                raise Hold("only verified missed exits can use catch-up execution")
            if Path(engine.config["kill_switch"]).exists():
                raise Hold("kill switch is present")
            current, truncated = self.context(message, engine.clock())
            if truncated:
                raise Hold("source context is incomplete during exit catch-up")
            if self.signature(current) != signature:
                raise SourceContextChanged("source context changed during exit catch-up")
            if engine.source_latest.get(group, latest_id) > latest_id:
                raise SourceContextChanged("a newer source message must be considered before this exit")
            row = self.store.db.execute("SELECT revision FROM messages WHERE id=?", (message["id"],)).fetchone()
            if row is None or row[0] != message["revision"] or engine.origin(message, decision)["revision"] != origin["revision"]:
                raise Hold("missed exit source was changed or removed")
            if self.current_entries(group).get(key, {}).get("id") != entry["id"]:
                raise Hold("the owned position changed during exit catch-up")
            current_positions = [p for p in self.store.positions() if p["source_group"] == group]
            if current_positions != positions:
                raise Hold("owned quantities changed during exit catch-up")
            for previous in self.store.db.execute("""SELECT o.*,m.timestamp FROM orders o
                LEFT JOIN messages m ON m.id=o.message_id WHERE o.source_group=? AND o.contract=?
                AND o.action IN ('REDUCE','CLOSE') AND julianday(o.created_at)>=julianday(?)""",
                (group, key, entry["created_at"])).fetchall():
                if previous["id"] == order_id and previous["status"] == "submitting":
                    continue
                body = json.loads(previous["body"])
                if (body.get("origin_message_id", previous["message_id"]) == origin["id"]
                        or previous["message_id"] == message["id"]
                        or (previous["timestamp"] and instant(previous["timestamp"]) >= instant(origin["timestamp"]))):
                    raise Hold("this exit or a later exit already has an order; it will not be replayed")
            return str(latest_id)

        guard()
        return guard

    def context(self, message, cutoff):
        channel = self.engine.channels[message["channel_id"]]
        rows = self.store.db.execute("""SELECT body FROM messages WHERE source_group=? AND id!=?
            AND julianday(timestamp)<=julianday(?) ORDER BY julianday(timestamp) DESC, length(id) DESC, id DESC LIMIT 241""",
            (channel["source_group"], message["id"], cutoff.isoformat())).fetchall()
        records = []
        for row in rows:
            record = json.loads(row[0])
            source = self.engine.channels.get(record.get("channel_id"))
            if (not source or source["source_group"] != channel["source_group"]
                    or not channel_allows_author(source, record.get("author_id", ""))):
                continue
            if record.get("edited_timestamp") and instant(record["edited_timestamp"]) > cutoff:
                continue
            records.append(record)
        original_time = instant(message["timestamp"])
        # Older background being omitted does not mean a later invalidation was omitted.
        truncated = any(instant(record["timestamp"]) >= original_time for record in records[60:])
        if len(rows) == 241 and instant(json.loads(rows[-1][0])["timestamp"]) >= original_time:
            truncated = True
        return list(reversed(records[:60])), truncated

    @staticmethod
    def signature(context):
        return [(m["id"], m["revision"]) for m in context]

    def defer(self, message, decision, reason, delay=30):
        self.retry_due[message["id"]] = time.monotonic() + delay
        return self.store.record(message, "recovery_pending", f"Missed exit waiting: {reason}; retry in {delay}s", decision)

    async def execute_exit(self, message, decision, context, positions):
        async with self.engine.lock:
            guard = self.exit_guard(message, decision, context, positions)
            decision["recovery"]["execution"] = "exit"
            try:
                return await self.engine.execute_decision(message, decision, recovery_guard=guard)
            except RetryHold as exc:
                # Reuse the semantic assessment only while the captured source and position stay identical.
                self.deferred_exits[message["id"]] = (decision, context, positions)
                return self.defer(message, decision, str(exc))

    async def facts(self, message, decision):
        engine = self.engine
        now = engine.clock()
        origin = engine.origin(message, decision) if decision["action"] in {"OPEN", "REDUCE", "CLOSE"} else message
        facts = dict(evaluated_at=now.isoformat(), original_timestamp=origin["timestamp"],
                     signal_age_seconds=max(0, (now - instant(origin["timestamp"])).total_seconds()),
                     market_open=None, snapshot_timestamp=None, quote=None, affordable_quantity=None,
                     context_truncated=False, context_changed=False, blockers=[])
        blockers = facts["blockers"]
        contract = canonical_contract(decision.get("contract"))
        expired = contract["expiry"] < now.astimezone(EASTERN).date().isoformat()
        if expired:
            blockers.append("The original option contract has expired")
            return facts
        snapshot = None
        try:
            snapshot = await engine.broker.snapshot()
            if engine.mode != "paper" and snapshot.get("account_id") != engine.account:
                blockers.append("Account snapshot does not match the bound account")
                return facts
            facts.update({key: snapshot.get(key) for key in ("equity", "buying_power")})
            facts["market_open"] = snapshot.get("market_open")
            facts["snapshot_timestamp"] = snapshot.get("timestamp")
            if snapshot.get("market_open") is not True:
                blockers.append("An open options session is not confirmed")
            if snapshot.get("restrictions", []) != []:
                blockers.append("Account restrictions prevent trading")
            engine.check_quote_age(snapshot, engine.clock(), "account snapshot")
        except Hold as exc:
            blockers.append(str(exc))
        except Exception:
            blockers.append("Current account data is unavailable")
            snapshot = None
        try:
            quote = await engine.broker.quote(contract)
            if canonical_contract(quote.get("contract")) != contract:
                raise Hold("Quote does not match the original contract")
            facts["quote"] = {key: quote.get(key) for key in (
                "contract", "bid", "ask", "timestamp", "tradable", "multiplier", "currency", "asset_type", "tick_size")}
            if quote.get("tradable") is not True or quote.get("multiplier") != 100 or quote.get("currency") != "USD" or quote.get("asset_type") != "equity_option":
                blockers.append("The original contract is not confirmed as a tradable standard USD option")
            engine.check_quote_age(quote, engine.clock())
            bid, ask = money(quote.get("bid"), positive=True), money(quote.get("ask"), positive=True)
            if ask < bid or (ask - bid) / ask > money(engine.config["risk"]["max_spread_fraction"]):
                blockers.append("Current option spread exceeds the configured limit")
            if decision["action"] == "OPEN":
                reference = money(decision.get("alert_price"), positive=True)
                tick = money(quote.get("tick_size"), positive=True)
                limit = (ask / tick).to_integral_value(rounding=ROUND_CEILING) * tick
                evaluation = entry_chase_evaluation(ask, reference, limit, engine.config["risk"]["max_chase_fraction"])
                decision["entry_evaluation"] = evaluation
                if not entry_chase_within_cap(evaluation):
                    blockers.append(entry_chase_reason("Current ask or rounded limit exceeds permitted chase from the original alert premium", evaluation))
                if snapshot is not None:
                    quantity, _ = entry_size(engine.config["risk"], snapshot, contract, decision["confidence"], limit, message["source_group"])
                    facts["affordable_quantity"] = quantity
        except Hold as exc:
            blockers.append(str(exc))
        except Exception:
            blockers.append("Current option quote or affordability is unavailable")
        return facts

    async def assess(self, message):
        engine = self.engine
        decision = None
        try:
            if not self.eligible(message):
                return self.store.record(message, "context", "No longer an eligible recovery source or timestamp; no execution")
            if self.store.db.execute("SELECT 1 FROM orders WHERE message_id=?", (message["id"],)).fetchone():
                return {"message_id": message["id"], "state": "duplicate", "reason": "an order already exists for this message"}
            message = dict(message, source_group=engine.channels[message["channel_id"]]["source_group"])
            if self.store.db.execute("SELECT 1 FROM events WHERE message_id=? AND revision!=?", (message["id"], message["revision"])).fetchone():
                return self.store.record(message, "context", "A revised alert remains context only; no execution")
            deferred = self.deferred_exits.pop(message["id"], None)
            self.retry_due.pop(message["id"], None)
            if deferred is not None:
                decision, context, positions = deferred
                return await self.execute_exit(message, decision, context, positions)
            self.store.record(message, "recovery_evaluating", "Evaluating missed alert against current conditions; only verified owned exits may execute")
            older, _ = self.context(message, instant(message["timestamp"]))
            all_positions = self.store.positions()
            positions = [p for p in all_positions if p["source_group"] == message["source_group"]]
            decision = await engine.interpreter.interpret(message, older, positions)
            if isinstance(decision.get("evaluation_timing"), dict):
                decision["evaluation_timing"]["path"] = "recovery"
            decision = await engine.resolve_expiry(message, decision)
            now = engine.clock()
            if decision["action"] in {"IGNORE", "WAIT", "UPDATE_STOP"}:
                assessment = dict(status="not_actionable" if decision["action"] == "IGNORE" else "uncertain",
                                  confidence=decision["confidence"], reason=decision["reason"], evidence=decision["evidence"])
                facts = dict(evaluated_at=now.isoformat(), original_timestamp=message["timestamp"],
                             signal_age_seconds=max(0, (now - instant(message["timestamp"])).total_seconds()), blockers=[])
                if decision["action"] == "UPDATE_STOP":
                    assessment["reason"] = "Stop changes require broker-native order support and review; no stop was installed"
                    facts["blockers"].append(assessment["reason"])
            else:
                facts = await self.facts(message, decision)
                context, truncated = self.context(message, engine.clock())
                facts["context_truncated"] = truncated
                if (decision["action"] in {"REDUCE", "CLOSE"} and not truncated
                        and facts["blockers"] and "The original option contract has expired" not in facts["blockers"]):
                    # Check ownership/replay before retaining a delayed exit during a market/provider outage.
                    self.exit_guard(message, decision, context, positions)
                    decision["recovery"] = dict(status="uncertain", reason="; ".join(facts["blockers"]), facts=facts)
                    return self.defer(message, decision, decision["recovery"]["reason"], delay=300)
                assessment = await engine.interpreter.assess_recovery(message, context, positions, decision, facts)
                decision["recovery"] = assessment
                current, _ = self.context(message, engine.clock())
                row = self.store.db.execute("SELECT revision FROM messages WHERE id=?", (message["id"],)).fetchone()
                facts["context_changed"] = self.signature(context) != self.signature(current) or row is None or row[0] != message["revision"]
                if facts["context_changed"]:
                    facts["blockers"].append("Source messages changed during assessment")
                    assessment["status"] = "uncertain"
                    if decision["action"] in {"REDUCE", "CLOSE"}:
                        raise SourceContextChanged("source context changed during exit assessment")
                if sorted(json.dumps(p, sort_keys=True) for p in all_positions) != sorted(json.dumps(p, sort_keys=True) for p in self.store.positions()):
                    facts["blockers"].append("Relay positions changed during assessment")
                    assessment["status"] = "uncertain"
                for label, timestamp in (("account snapshot", facts.get("snapshot_timestamp")), ("option quote", (facts.get("quote") or {}).get("timestamp"))):
                    if timestamp:
                        try:
                            engine.check_quote_age({"timestamp": timestamp}, engine.clock(), label)
                        except Hold as exc:
                            facts["blockers"].append(str(exc))
                if "The original option contract has expired" in facts["blockers"]:
                    assessment["status"] = "invalidated"
                elif facts["blockers"] and assessment["status"] == "viable":
                    assessment["status"] = "uncertain"
            facts["evaluated_at"] = engine.clock().isoformat()
            recovery = assessment | {key: facts[key] for key in ("evaluated_at", "original_timestamp", "signal_age_seconds")} | {"facts": facts}
            decision["recovery"] = recovery
            if (decision["action"] in {"REDUCE", "CLOSE"} and assessment["status"] == "viable"
                    and assessment["confidence"] >= engine.config["risk"]["min_confidence"]
                    and not facts.get("blockers") and not facts.get("context_truncated")):
                return await self.execute_exit(message, decision, context, positions)
            return self.store.record(message, "recovery_review", "Recovery assessment only; no order submitted", decision | {"recovery": recovery})
        except asyncio.CancelledError:
            self.store.record(message, "recovery_pending", "Recovery assessment interrupted; awaiting evaluation")
            raise
        except SourceContextChanged as exc:
            self.deferred_exits.pop(message["id"], None)
            return self.defer(message, decision, str(exc))
        except Hold as exc:
            if decision and isinstance(decision.get("recovery"), dict):
                decision["recovery"].update(status="uncertain", reason=str(exc))
            return self.store.record(message, "recovery_review", "Missed exit held: " + str(exc), decision)
        except Exception as exc:
            timing = getattr(exc, "evaluation_timing", None)
            if isinstance(timing, dict):
                timing["path"] = "recovery"
                if decision is None:
                    decision = {"evaluation_timing": timing}
                else:
                    decision = decision | {"recovery": {"evaluation_timing": timing}}
            return self.store.record(message, "recovery_error", safe_interpretation_reason(exc), decision)

    async def consume(self, fresh_queue, emit):
        while True:
            # Assessments run alongside fresh interpretation; only final exit dispatch takes the lock.
            await asyncio.sleep(1)
            if not fresh_queue.empty() or self.engine.lock.locked() or Path(self.engine.config["kill_switch"]).exists():
                continue
            async with self.engine.lock:
                await self.reconcile_orders()
            self.recover_saved()
            self.recover_position_context()
            rows = self.store.db.execute("""SELECT m.id,m.body FROM events e JOIN messages m ON m.id=e.message_id AND m.revision=e.revision
                WHERE e.state='recovery_pending' ORDER BY julianday(m.timestamp), e.id LIMIT 100""").fetchall()
            for row in rows:
                if self.retry_due.get(row["id"], 0) <= time.monotonic():
                    emit(await self.assess(json.loads(row["body"])))
                    break
