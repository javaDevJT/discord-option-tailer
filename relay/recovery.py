"""Read-only reassessment of missed alerts; this module never creates orders."""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

from .core import EASTERN, Hold, canonical_contract, channel_allows_author, entry_size, instant, money


class RecoveryEvaluator:
    # ponytail: assess browser-observed alerts once per revision; full history sync is separate work.

    def __init__(self, engine):
        self.engine = engine
        self.store = engine.store
        self.scan_cursor = 0
        self.scan_complete = False
        with self.store.db:
            self.store.db.execute("UPDATE events SET state='recovery_pending', reason='Recovery assessment interrupted; awaiting evaluation' WHERE state='recovery_evaluating'")

    def eligible(self, message):
        channel = self.engine.channels.get(message.get("channel_id"))
        if (not channel or channel.get("role") != "signals"
                or not channel_allows_author(channel, message.get("author_id", ""))
                or message.get("source") != "browser" or message.get("edited_timestamp")
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
        return self.store.record(message, "recovery_pending", "Missed alert queued for a current viability assessment; no order will be submitted")

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
                if ask > reference * (1 + money(engine.config["risk"]["max_chase_fraction"])):
                    blockers.append("Current ask exceeds the permitted chase from the original alert premium")
                if snapshot is not None:
                    quantity, _ = entry_size(engine.config["risk"], snapshot, contract, decision["confidence"], ask, message["source_group"])
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
            message = dict(message, source_group=engine.channels[message["channel_id"]]["source_group"])
            if self.store.db.execute("SELECT 1 FROM events WHERE message_id=? AND revision!=?", (message["id"], message["revision"])).fetchone():
                return self.store.record(message, "context", "A revised alert remains context only; no execution")
            self.store.record(message, "recovery_evaluating", "Evaluating missed alert against current conditions; no order will be submitted")
            older, _ = self.context(message, instant(message["timestamp"]))
            all_positions = self.store.positions()
            positions = [p for p in all_positions if p["source_group"] == message["source_group"]]
            decision = await engine.interpreter.interpret(message, older, positions)
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
                assessment = await engine.interpreter.assess_recovery(message, context, positions, decision, facts)
                current, _ = self.context(message, engine.clock())
                row = self.store.db.execute("SELECT revision FROM messages WHERE id=?", (message["id"],)).fetchone()
                facts["context_changed"] = self.signature(context) != self.signature(current) or row is None or row[0] != message["revision"]
                if facts["context_changed"]:
                    facts["blockers"].append("Source messages changed during assessment")
                    assessment["status"] = "uncertain"
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
            return self.store.record(message, "recovery_review", "Recovery assessment only; no order submitted", decision | {"recovery": recovery})
        except asyncio.CancelledError:
            self.store.record(message, "recovery_pending", "Recovery assessment interrupted; awaiting evaluation")
            raise
        except Exception as exc:
            return self.store.record(message, "recovery_error", type(exc).__name__ + ": recovery assessment failed; no order submitted", decision)

    async def consume(self, fresh_queue, emit):
        while True:
            # An in-flight read-only assessment can finish alongside fresh interpretation.
            # It never holds Engine.lock or makes the fresh consumer wait for the backlog.
            await asyncio.sleep(1)
            if not fresh_queue.empty() or self.engine.lock.locked() or Path(self.engine.config["kill_switch"]).exists():
                continue
            self.recover_saved()
            row = self.store.db.execute("""SELECT m.body FROM events e JOIN messages m ON m.id=e.message_id AND m.revision=e.revision
                WHERE e.state='recovery_pending' ORDER BY julianday(m.timestamp) DESC, e.id DESC LIMIT 1""").fetchone()
            if row:
                emit(await self.assess(json.loads(row[0])))
