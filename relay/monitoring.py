"""Durable, bounded position observations that wake Codex without a new alert."""
from __future__ import annotations

import asyncio
import hashlib
import json
from contextlib import nullcontext
from datetime import datetime, time as day_time, timedelta
from decimal import Decimal
from pathlib import Path

from .core import EASTERN, Hold, canonical_contract, channel_allows_author, contract_key, instant, money
from .monitor_contract import project_monitor_facts, validate_monitor_plan
from .status import execution_failure, log_execution


ACTIVE = ("active", "evaluating", "error")


class PositionMonitors:
    def __init__(self, engine):
        self.engine, self.store = engine, engine.store
        self.tasks = {}
        self.emit = None
        self.queue = None

    def get(self, identity):
        row = self.store.db.execute("SELECT * FROM position_monitors WHERE id=?", (identity,)).fetchone()
        return dict(row) | {"body": json.loads(row["body"]), "_saved_body": row["body"], "_saved_state": row["state"]} if row else None

    def save(self, row, *, state=None, reason=None, next_poll=None):
        if state is not None:
            row["state"] = state
        if reason is not None:
            row["body"]["reason"] = reason
        if next_poll is not None:
            row["next_poll_at"] = next_poll.isoformat()
        body = json.dumps(row["body"])
        with self.store.db:
            changed = self.store.db.execute(
                "UPDATE position_monitors SET state=?,expires_at=?,next_poll_at=?,body=? "
                "WHERE id=? AND state=? AND body=?",
                (row["state"], row["expires_at"], row["next_poll_at"], body, row["id"],
                 row["_saved_state"], row["_saved_body"])).rowcount
        if changed:
            row.update(_saved_state=row["state"], _saved_body=body)
            if row["state"] not in ACTIVE:
                log_execution("monitor_finished", context={"monitor_id": row["id"], "state": row["state"],
                    "message_id": row["body"]["source_message_id"], "monitor_generation": row["body"]["generation"]})
        return bool(changed)

    def emit_result(self, result):
        if self.emit is not None:
            self.emit(result)

    def source(self, row):
        body = row["body"]
        source = self.store.db.execute("SELECT body FROM messages WHERE id=?", (body["source_message_id"],)).fetchone()
        if source is None:
            raise Hold("Monitor source was removed")
        message = json.loads(source[0])
        channel = self.engine.channels.get(message.get("channel_id"))
        if (message.get("revision") != body["source_revision"] or message.get("edited_timestamp")
                or not channel or channel.get("role") != "signals"
                or channel.get("source_group") != row["source_group"]
                or not channel_allows_author(channel, message.get("author_id", ""))
                or message.get("source") not in {"gateway", "browser"}
                or message.get("ingestion") not in {"live", "baseline"}
                or message.get("ingestion_reason") in {"history", "backscroll", "edit"}):
            raise Hold("Monitor source revision or permission changed")
        if self.engine.observations.get(message["id"], message["revision"]) != message["revision"]:
            raise Hold("Monitor source changed before its database update")
        return message

    def owned(self, row):
        contract = json.loads(row["contract"])
        position = self.engine.stops.owned(row["source_group"], contract)
        if (position is None or position["quantity"] <= 0
                or self.engine.stops.entry_id(row["source_group"], row["contract"]) != row["entry_order_id"]):
            raise Hold("Monitored position was closed or belongs to a different entry")
        return position

    def arm(self, message, decision):
        if decision.get("action") != "WAIT" or self.engine.observe_only:
            raise Hold("Only a WAIT decision can request a position monitor")
        try:
            plan = validate_monitor_plan(decision.get("monitor"))
        except ValueError as exc:
            raise Hold("Invalid position monitoring plan") from exc
        contract = canonical_contract(decision.get("contract"))
        key, group = contract_key(contract), message["source_group"]
        entry = self.engine.stops.entry_id(group, key)
        if entry is None:
            raise Hold("Monitoring requires a filled relay-owned entry")
        self.engine.check_entry_lifetime(message, decision | {"origin_message_id": message["id"]})
        identity = hashlib.sha256(json.dumps([message["id"], message["revision"], entry, plan], sort_keys=True).encode()).hexdigest()
        existing = self.get(identity)
        if existing is not None:
            return existing["body"] | {"id": identity, "state": existing["state"]}
        now = self.engine.clock()
        expiry_close = datetime.combine(datetime.fromisoformat(contract["expiry"]).date(), day_time(16), EASTERN)
        deadline = min(now + timedelta(seconds=plan["duration_seconds"]), expiry_close)
        if deadline <= now:
            raise Hold("Monitoring window ends after the option's trading expiry")
        body = {"source_message_id": message["id"], "source_revision": message["revision"],
                "source_market_date": instant(message["timestamp"]).astimezone(EASTERN).date().isoformat(),
                "plan": plan, "generation": 0, "conditions_resolved": {},
                "review_at": (now + timedelta(seconds=plan["reassess_after_seconds"])).isoformat()
                    if plan["reassess_after_seconds"] is not None else None,
                "reason": f"Monitoring owned position until {deadline.isoformat()}; polling every {plan['poll_interval_seconds']} seconds. " + decision["reason"]}
        row = {"id": identity, "source_group": group, "contract": key, "entry_order_id": entry,
               "state": "active", "created_at": now.isoformat(), "expires_at": deadline.isoformat(),
               "next_poll_at": now.isoformat(), "body": body}
        self.source(row)
        self.owned(row)
        with self.store.db:
            self.store.db.execute("UPDATE position_monitors SET state='canceled' WHERE source_group=? AND contract=? AND state IN ('active','evaluating','error')",
                                  (group, key))
            self.store.db.execute("INSERT INTO position_monitors VALUES (?,?,?,?,?,?,?,?,?)",
                                  (identity, group, key, entry, "active", now.isoformat(), deadline.isoformat(), now.isoformat(), json.dumps(body)))
        log_execution("monitor_armed", context={"message_id": message["id"], "monitor_id": identity})
        return body | {"id": identity, "state": "active"}

    def context(self, source):
        rows = self.store.db.execute(
            "SELECT body FROM messages WHERE source_group=? AND id!=? "
            "AND julianday(timestamp)>=julianday(?) AND julianday(timestamp)<=julianday(?) "
            "ORDER BY julianday(timestamp) DESC,id DESC LIMIT 61",
            (source["source_group"], source["id"], source["timestamp"], self.engine.clock().isoformat())).fetchall()
        records = []
        for row in reversed(rows):
            message = json.loads(row[0])
            channel = self.engine.channels.get(message.get("channel_id"))
            if (channel and channel.get("source_group") == source["source_group"]
                    and channel_allows_author(channel, message.get("author_id", ""))):
                records.append(message)
        return records[-60:], len(rows) > 60

    async def observe(self, row):
        engine, contract = self.engine, json.loads(row["contract"])
        position = self.owned(row)
        with getattr(engine.broker, "execution_reads", nullcontext)():
            snapshot, quote = await asyncio.gather(engine.broker.snapshot(), engine.broker.quote(contract))
            engine.check_quote_age(snapshot, engine.clock(), "account snapshot")
            engine.check_quote_age(quote, engine.clock())
            if engine.mode != "paper" and snapshot.get("account_id") != engine.account:
                raise Hold("Monitor account does not match the bound account")
            if canonical_contract(quote.get("contract")) != contract or quote.get("tradable") is not True:
                raise Hold("Monitor quote does not match a tradable owned contract")
            if snapshot.get("currency", "USD") != "USD" or quote.get("currency") != "USD" or quote.get("multiplier") != 100:
                raise Hold("Monitor requires standard USD option data")
            matching = [p for p in snapshot.get("positions", []) if p.get("contract") == contract]
            broker_quantity = sum(p["quantity"] for p in matching)
            average, bid = money(position["average_price"], positive=True), money(quote["bid"])
            facts = {"observed_at": engine.clock().isoformat(), "source_market_date": row["body"]["source_market_date"],
                     "market_open": snapshot.get("market_open") is True, "owned_quantity": position["quantity"],
                     "broker_quantity": broker_quantity, "average_entry_price": str(average),
                     "option_bid": str(bid), "option_ask": quote["ask"], "option_quote_at": quote["timestamp"],
                     "unrealized_return_fraction": str(bid / average - 1), "blockers": []}
            relay_quantity = sum(p["quantity"] for p in self.store.positions() if p["contract"] == contract)
            if broker_quantity < relay_quantity:
                facts["blockers"].append("Broker inventory is below the relay-owned quantity; reconcile holdings")
            if not facts["market_open"]:
                facts["blockers"].append("Options market is closed")
            if snapshot.get("restrictions", []):
                facts["blockers"].append("Broker account restrictions are present")
            needs_underlying = any(c["metric"] == "underlying_price" for c in row["body"]["plan"]["conditions"])
            if needs_underlying and hasattr(engine.broker, "underlying_quote"):
                try:
                    underlying = await engine.broker.underlying_quote(contract["symbol"])
                    engine.check_quote_age(underlying, engine.clock(), "underlying quote")
                    if underlying.get("symbol") != contract["symbol"]:
                        raise Hold("Monitor underlying symbol mismatch")
                    facts.update(underlying_price=str(money(underlying["price"], positive=True)), underlying_quote_at=underlying["timestamp"])
                    if underlying.get("market_date") == row["body"]["source_market_date"]:
                        for name in ("day_low", "day_high"):
                            if underlying.get(name) is not None:
                                facts["source_" + name] = str(money(underlying[name], positive=True))
                        facts["reference_market_date"] = underlying["market_date"]
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    self.failure(row, exc, "monitor_poll")
                    facts["blockers"].append("Underlying quote is unavailable")
        if needs_underlying and "underlying_price" not in facts:
            facts["blockers"].append("Underlying quote unavailable")
        for index, condition in enumerate(row["body"]["plan"]["conditions"]):
            reference = condition["threshold"]
            resolved = row["body"]["conditions_resolved"].get(str(index))
            if reference in {"source_day_low", "source_day_high"} and resolved is not None:
                facts[reference] = resolved
                facts["reference_market_date"] = row["body"]["source_market_date"]
        return project_monitor_facts(facts)

    def triggers(self, row, facts):
        now, matched = self.engine.clock(), []
        body = row["body"]
        for index, condition in enumerate(body["plan"]["conditions"]):
            value = facts.get("owned_quantity" if condition["metric"] == "held_quantity" else condition["metric"])
            threshold = condition["threshold"]
            if threshold in {"source_day_low", "source_day_high", "entry_premium"}:
                field = "average_entry_price" if threshold == "entry_premium" else threshold
                resolved = body["conditions_resolved"].get(str(index))
                if resolved is None and facts.get(field) is not None:
                    resolved = body["conditions_resolved"][str(index)] = facts[field]
                if resolved is None:
                    facts.setdefault("blockers", []).append(f"Reference {threshold} is unavailable for the original source date")
                    continue
                threshold = resolved
            if value is not None and ((Decimal(str(value)) <= Decimal(threshold)) if condition["comparison"] == "lte" else (Decimal(str(value)) >= Decimal(threshold))):
                matched.append(index)
        timer = body.get("review_at") is not None and now >= instant(body["review_at"])
        facts["triggered_conditions"] = matched
        facts["trigger_reason"] = "condition" if matched else "timer" if timer else None
        return bool(matched or timer)

    def failure(self, row, exc, stage):
        detail, diagnostic = execution_failure(exc, stage=stage, context={
            "message_id": row["body"]["source_message_id"], "monitor_id": row["id"]})
        row["body"]["diagnostic"] = diagnostic
        return detail

    async def poll_once(self, *, background=True):
        engine, now = self.engine, self.engine.clock()
        if engine.observe_only or engine.interpreter is None or engine.evaluations_active or engine.lock.locked():
            return 0
        rows = self.store.db.execute("SELECT id FROM position_monitors WHERE state IN ('active','evaluating','error') ORDER BY next_poll_at LIMIT 100").fetchall()
        checked = 0
        for item in rows:
            if engine.evaluations_active or engine.lock.locked() or (self.queue is not None and not self.queue.empty()):
                break
            row = self.get(item["id"])
            if row["id"] in self.tasks:
                continue
            if now >= instant(row["expires_at"]):
                self.save(row, state="expired", reason="Codex monitoring duration ended")
                continue
            try:
                self.source(row)
                self.owned(row)
            except Hold as exc:
                self.save(row, state="canceled", reason=str(exc))
                continue
            event_id = row["body"].get("event_id")
            if event_id and self.store.db.execute("SELECT 1 FROM orders WHERE message_id=?", (event_id,)).fetchone():
                self.save(row, state="completed", reason="Monitor action is in the order ledger; normal reconciliation owns its outcome")
                continue
            if now < instant(row["next_poll_at"]) or Path(engine.config["kill_switch"]).exists():
                continue
            try:
                facts = await self.observe(row)
                self.source(row)
                self.owned(row)
                triggered = self.triggers(row, facts)
                if event_id:
                    facts.update(trigger_reason="resume", triggered_at=row["body"].get("triggered_at"))
                row["body"]["observation"] = facts
                if not self.save(row, state="active", next_poll=engine.clock() + timedelta(seconds=row["body"]["plan"]["poll_interval_seconds"])):
                    continue
                checked += 1
                log_execution("monitor_polled", context={"message_id": row["body"]["source_message_id"], "monitor_id": row["id"],
                    "owned_quantity": facts.get("owned_quantity"), "broker_quantity": facts.get("broker_quantity"),
                    "bid": facts.get("option_bid"), "ask": facts.get("option_ask"),
                    "underlying_price": facts.get("underlying_price"), "market_open": facts.get("market_open")})
                if not (event_id or triggered) or not facts.get("market_open"):
                    continue
                if self.queue is not None and not self.queue.empty():
                    continue
                if not event_id:
                    row["body"].update(triggered_at=engine.clock().isoformat(), event_id=f"monitor:{row['id']}:{row['body']['generation']}")
                    facts["triggered_at"] = row["body"]["triggered_at"]
                if not self.save(row, state="evaluating"):
                    continue
                log_execution("monitor_triggered", context={"message_id": row["body"]["source_message_id"], "monitor_id": row["id"],
                                                            "monitor_generation": row["body"]["generation"]})
                if background:
                    task = asyncio.create_task(self.evaluate(row["id"]))
                    self.tasks[row["id"]] = task
                    task.add_done_callback(lambda done, identity=row["id"]: self.finished(identity, done))
                else:
                    await self.evaluate(row["id"])
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                detail = self.failure(row, exc, "monitor_poll")
                self.save(row, state="error", reason="Monitoring observation failed; " + detail,
                          next_poll=engine.clock() + timedelta(seconds=max(15, row["body"]["plan"]["poll_interval_seconds"])))
        return checked

    def finished(self, identity, task):
        self.tasks.pop(identity, None)
        if not task.cancelled() and task.exception() is not None:
            execution_failure(task.exception(), stage="monitor_evaluation", context={"monitor_id": identity})

    async def evaluate(self, identity):
        row = self.get(identity)
        if row is None or row["state"] != "evaluating":
            return
        engine, body = self.engine, row["body"]
        try:
            source = self.source(row)
            self.owned(row)
            context, truncated = self.context(source)
            positions = [p for p in self.store.positions() if p["source_group"] == row["source_group"]]
            generation = self.store.source_generation(row["source_group"])
            position_generation = self.store.position_generation(row["source_group"])
            facts = dict(body["observation"], evaluated_at=engine.clock().isoformat())
            if truncated:
                facts.setdefault("blockers", []).append("Source context is truncated")
            monitored_positions = [p for p in positions if p["contract"] == json.loads(row["contract"])]
            decision = await engine.interpreter.assess_monitor(source, context, monitored_positions, body["plan"], facts)
            current = self.get(identity)
            if current is None or current["state"] != "evaluating" or current["body"]["generation"] != body["generation"] or current["body"].get("event_id") != body.get("event_id"):
                return
            if decision.get("action") not in {"WAIT", "IGNORE", "REDUCE", "CLOSE"}:
                raise Hold("Monitor evaluation cannot open positions or place native stops")
            if self.queue is not None and not self.queue.empty():
                return
            body.update(decision=decision, evaluated_at=engine.clock().isoformat())

            def guard():
                latest = self.get(identity)
                if latest["state"] != "evaluating" or latest["body"]["generation"] != body["generation"]:
                    raise Hold("Monitor was superseded during evaluation")
                if engine.clock() >= instant(row["expires_at"]) or Path(engine.config["kill_switch"]).exists():
                    raise Hold("Monitor deadline or kill switch prevents this action")
                self.source(row)
                self.owned(row)
                if self.store.source_generation(row["source_group"]) != generation:
                    raise Hold("Discord context changed during monitor evaluation")
                if [p for p in self.store.positions() if p["source_group"] == row["source_group"]] != positions:
                    raise Hold("Owned inventory changed during monitor evaluation")
                latest_id = max([int(source["id"])] + [int(m["id"]) for m in context])
                if engine.source_latest.get(row["source_group"], latest_id) > latest_id:
                    raise Hold("A newer Discord message is awaiting processing")
                return str(latest_id)

            async with engine.lock:
                guard()
                if decision.get("contract") != json.loads(row["contract"]):
                    if decision.get("action") != "IGNORE":
                        raise Hold("Monitor decision must retain the exact owned contract")
                if decision["action"] in {"REDUCE", "CLOSE"}:
                    if facts.get("blockers") or truncated:
                        raise Hold("Monitor observations contain unresolved blockers")
                    if self.store.position_generation(row["source_group"]) != position_generation:
                        raise Hold("Orders changed during monitor evaluation")
                    event = {"id": body["event_id"], "revision": "monitor-v1", "source_group": row["source_group"],
                             "channel_id": source["channel_id"], "timestamp": engine.clock().isoformat(),
                             "content": f"Position monitor {identity} generation {body['generation']}"}
                    with self.store.db:
                        self.store.db.execute("INSERT OR IGNORE INTO events(message_id,revision,state,reason,created_at) VALUES(?,?,?,?,?)",
                                              (event["id"], event["revision"], "evaluated", "Codex position-monitor decision", event["timestamp"]))
                    result = await engine.execute_decision(event, decision, recovery_guard=guard, source_message=source)
                    body["result"] = result
                    self.save(row, state="completed", reason=result["reason"])
                    self.emit_result(result)
                elif decision["action"] == "WAIT" and decision.get("monitor") is not None:
                    plan = validate_monitor_plan(decision["monitor"])
                    row["expires_at"] = min(instant(row["expires_at"]), engine.clock() + timedelta(seconds=plan["duration_seconds"])).isoformat()
                    resolved = body["conditions_resolved"] if plan["conditions"] == body["plan"]["conditions"] else {}
                    body.pop("triggered_at", None)
                    body.update(plan=plan, generation=body["generation"] + 1, event_id=None, conditions_resolved=resolved,
                                review_at=(engine.clock() + timedelta(seconds=plan["reassess_after_seconds"])).isoformat()
                                if plan["reassess_after_seconds"] is not None else None)
                    self.save(row, state="active", reason=decision["reason"],
                              next_poll=engine.clock() + timedelta(seconds=plan["poll_interval_seconds"]))
                elif decision["action"] in {"WAIT", "IGNORE"}:
                    self.save(row, state="completed", reason=decision["reason"])
                else:
                    raise Hold("Monitor evaluation cannot open positions or place native stops")
            log_execution("monitor_evaluated", context={"message_id": source["id"], "monitor_id": identity, "action": decision["action"]})
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            detail = self.failure(row, exc, "monitor_evaluation")
            current = self.get(identity)
            if current and current["state"] == "evaluating" and current["body"]["generation"] == body["generation"] and current["body"].get("event_id") == body.get("event_id"):
                self.save(row, state="error", reason="Monitor reassessment held; " + detail,
                          next_poll=engine.clock() + timedelta(seconds=max(30, body["plan"]["poll_interval_seconds"])))

    async def run(self, queue=None, emit=None):
        self.queue, self.emit = queue, emit
        try:
            while True:
                try:
                    if not self.engine.lock.locked():
                        for identity, task in list(self.tasks.items()):
                            row = self.get(identity)
                            if row is None or row["state"] not in ACTIVE:
                                task.cancel()
                            elif self.engine.clock() >= instant(row["expires_at"]):
                                self.save(row, state="expired", reason="Codex monitoring duration ended")
                                task.cancel()
                            elif Path(self.engine.config["kill_switch"]).exists():
                                task.cancel()
                    foreground = self.engine.evaluations_active or (queue is not None and not queue.empty())
                    if foreground:
                        # Claims are durable: canceled model work resumes with fresh facts.
                        if not self.engine.lock.locked():
                            for task in list(self.tasks.values()):
                                task.cancel()
                    else:
                        await self.poll_once()
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    execution_failure(exc, stage="monitor_poll")
                await asyncio.sleep(1)
        finally:
            tasks = list(self.tasks.values())
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
