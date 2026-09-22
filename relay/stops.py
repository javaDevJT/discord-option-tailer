"""Durable agent-directed protection for exact relay-owned long options."""
from __future__ import annotations

import asyncio
import hashlib
import json
from decimal import ROUND_CEILING
from pathlib import Path

from .broker import BrokerPreflightHold
from .core import EASTERN, Hold, RetryHold, TERMINAL, canonical_contract, contract_key, money


class StopLoss:
    def __init__(self, engine):
        self.engine, self.store = engine, engine.store

    def owned(self, group, contract):
        return next((p for p in self.store.positions()
                     if p["source_group"] == group and p["contract"] == contract), None)

    def entry_id(self, group, key):
        row = self.store.db.execute("""SELECT id FROM orders WHERE source_group=? AND contract=?
            AND action='OPEN' AND filled_quantity>0 ORDER BY rowid DESC LIMIT 1""", (group, key)).fetchone()
        if row is None:
            raise Hold("stop requires an identifiable relay-owned entry fill")
        return row["id"]

    def request(self, message, decision):
        with self.store.db:
            return self.store.save_stop_request(message, decision)

    def get(self, group, key):
        return self.store.db.execute("SELECT * FROM stop_requests WHERE source_group=? AND contract=?", (group, key)).fetchone()

    def state(self, row, state, reason, order_id=None):
        with self.store.db:
            self.store.db.execute("""UPDATE stop_requests SET status=?,reason=?,order_id=COALESCE(?,order_id)
                WHERE source_group=? AND contract=?""", (state, reason, order_id, row["source_group"], row["contract"]))
        return {"status": state, "reason": reason, "stop_price": row["stop_price"], "order_id": order_id or row["order_id"]}

    def active_orders(self, group, key):
        return self.store.db.execute("""SELECT * FROM orders WHERE source_group=? AND contract=?
            AND action='UPDATE_STOP' AND status NOT IN ('filled','canceled','rejected','expired')""", (group, key)).fetchall()

    async def cancel(self, group, key):
        """Only our persisted protection orders can be canceled; ACK is not completion."""
        for order in self.active_orders(group, key):
            if not order["broker_id"]:
                raise RetryHold("stop submission is uncertain; reconcile before changing protection")
            body = json.loads(order["body"])
            options = {"broker_order_id": order["broker_id"], "expected_order": body}
            try:
                result = await self.engine.broker.cancel_order(order["id"], **options)
                self.store.apply_result(order["id"], result)
                # ponytail: bounded polling; durable reconciliation finishes slow cancels.
                for delay in (0.2, 0.5, 1.0):
                    if result["status"] in TERMINAL:
                        break
                    await asyncio.sleep(delay)
                    result = await self.engine.broker.order_status(order["id"], **options)
                    self.store.apply_result(order["id"], result)
            except Exception as exc:
                raise RetryHold("native stop cancellation could not be confirmed; no competing sale submitted") from exc
            if result["status"] not in TERMINAL:
                raise RetryHold("native stop cancellation is pending; no competing sale submitted")

    async def before_exit(self, message, decision):
        contract = canonical_contract(decision["contract"])
        group, key = message["source_group"], contract_key(contract)
        before = self.owned(group, contract)
        if not self.active_orders(group, key):
            return
        self.engine.check_execution_mode()
        if Path(self.engine.config["kill_switch"]).exists():
            raise Hold("kill switch is present")
        await self.cancel(group, key)
        row = self.get(group, key)
        if row:
            self.state(row, "pending", "Rearming protection after the exit completes")
        after = self.owned(group, contract)
        if before and (after or {}).get("quantity", 0) != before["quantity"]:
            # The standing stop can win the race. Never sell the original quantity again.
            raise Hold("native stop filled during cancellation; remaining position will be reconciled before another sale")

    async def update(self, message, decision):
        engine = self.engine
        engine.check_execution_mode()
        if Path(engine.config["kill_switch"]).exists():
            raise Hold("kill switch is present")
        if decision.get("ambiguous") is not False or type(decision.get("confidence")) not in (float, int) or not engine.config["risk"]["min_confidence"] <= decision["confidence"] <= 1:
            raise Hold("stop instruction is ambiguous or below configured confidence")
        if decision.get("quantity") is not None or decision.get("fraction") is not None:
            raise Hold("standalone stop changes protect the entire remaining owned position")
        engine.fresh(message, engine.clock())
        engine.fresh(engine.origin(message, decision), engine.clock())
        engine.unchanged(message)
        contract = canonical_contract(decision["contract"])
        if self.owned(message["source_group"], contract) is None:
            raise Hold("no position from this source is owned by the relay")
        engine.check_entry_lifetime(message, decision)
        async def source_guard(*, own_mutation=False):
            if own_mutation and message.get("_evaluation_claim") is not None:
                message["_evaluation_claim"]["position_generation"] = self.store.position_generation(message["source_group"])
            engine.fresh(message, engine.clock())
            engine.unchanged(message)
            if engine.config.get("require_source_verification") or engine.config.get("require_browser_verification"):
                if engine.verify_current is None or not await engine.verify_current(message):
                    raise Hold("current Discord stop instruction could not be verified")
            engine.fresh(message, engine.clock())
            engine.unchanged(message)
        await source_guard()
        if engine.mode == "shadow":
            value = decision.get("stop_price")
            price = money(self.owned(message["source_group"], contract)["average_price"] if value == "breakeven" else value, positive=True)
            return self.store.record(message, "shadow_order", "Native stop proposal; no order submitted", decision | {"stop_evaluation": {"stop_price": str(price), "status": "shadow"}})
        previous = self.get(message["source_group"], contract_key(contract))
        row = self.request(message, decision)
        result = await self.arm(row, source_guard=source_guard)
        if result["status"] not in {"active", "complete"} and previous and not self.store.unresolved():
            with self.store.db:
                self.store.db.execute("""UPDATE stop_requests SET message=?,decision=?,stop_price=?,entry_id=?,
                    status='pending',reason='Restoring previous protection after a held stop change'
                    WHERE source_group=? AND contract=?""", (previous["message"], previous["decision"], previous["stop_price"],
                    previous["entry_id"], previous["source_group"], previous["contract"]))
            await self.arm(self.get(previous["source_group"], previous["contract"]))
        decision["stop_evaluation"] = result
        state = "paper_order" if engine.mode == "paper" else "broker_order"
        if result["status"] not in {"active", "complete"}:
            state = "held"
        return self.store.record(message, state, result["reason"], decision)

    async def after_exit(self, message, decision):
        group, key = message["source_group"], contract_key(decision["contract"])
        row = self.get(group, key)
        if not row:
            return {"status": "complete", "reason": "No remaining contracts to protect"} if decision.get("stop_price") else None
        return await self.arm(row)

    async def restore(self, message, decision):
        """A rejected competing exit must not wait for the next background tick."""
        try:
            row = self.get(message["source_group"], contract_key(decision["contract"]))
        except (KeyError, Hold):
            return
        if row and row["status"] == "pending":
            await self.arm(row)

    async def arm(self, row, *, source_guard=None):
        if row is None:
            return {"status": "complete", "reason": "No remaining contracts to protect"}
        try:
            return await self._arm(row, source_guard=source_guard)
        except Exception as exc:
            # Provider response bodies and credentials never enter events or webhooks.
            reason = str(exc) if isinstance(exc, Hold) else "Native stop operation failed; inspect broker connection and reconcile orders"
            return self.state(row, "pending" if isinstance(exc, RetryHold) else "blocked", reason)

    async def _arm(self, row, *, source_guard=None):
        engine, store = self.engine, self.store
        group, key = row["source_group"], row["contract"]
        contract = json.loads(key)
        owned = self.owned(group, contract)
        if owned is None:
            entry = store.db.execute("SELECT status FROM orders WHERE id=?", (row["entry_id"],)).fetchone()
            if entry and entry["status"] not in TERMINAL:
                raise RetryHold("Stop pending until the entry fill is reconciled")
            return self.state(row, "complete", "No remaining contracts from the protected entry")
        if self.entry_id(group, key) != row["entry_id"]:
            return self.state(row, "complete", "No remaining contracts from the protected entry")
        engine.check_execution_mode()
        if engine.mode == "shadow" or Path(engine.config["kill_switch"]).exists():
            raise RetryHold("stop placement paused; existing native stops remain with the broker")
        if contract["expiry"] < engine.clock().astimezone(EASTERN).date().isoformat():
            return self.state(row, "complete", "Protected contract has expired")
        current = self.active_orders(group, key)
        price = money(owned["average_price"] if row["stop_price"] == "breakeven" else row["stop_price"], positive=True)
        if len(current) == 1:
            order = current[0]
            body = json.loads(order["body"])
            if (order["status"] in {"open", "partially_filled"} and body.get("order_type") == "stop_market"
                    and body["quantity"] - order["filled_quantity"] == owned["quantity"]
                    and money(body["requested_stop_price"]) == price):
                return self.state(row, "active", f"Native stop active at ${body['stop_price']} for {owned['quantity']} remaining contracts", order["id"])
        # Never place competing sells against an in-flight ordinary exit or uncertain order.
        if store.unresolved():
            raise RetryHold("Stop pending while another order is reconciled")
        if row["stop_price"] == "breakeven":
            with store.db:
                store.db.execute("UPDATE stop_requests SET stop_price=? WHERE source_group=? AND contract=?", (str(price), group, key))
            row = self.get(group, key)
        quote, snapshot = await asyncio.gather(engine.broker.quote(contract), engine.broker.snapshot())
        engine.check_quote_age(quote, engine.clock())
        engine.check_quote_age(snapshot, engine.clock(), "account snapshot")
        if snapshot.get("market_open") is not True:
            raise RetryHold("Stop pending until the options session is open")
        if snapshot.get("restrictions") or (engine.mode != "paper" and snapshot.get("account_id") != engine.account):
            raise Hold("broker account restrictions or account identity prevent stop placement")
        if (canonical_contract(quote.get("contract")) != contract or quote.get("tradable") is not True
                or quote.get("multiplier") != 100 or quote.get("currency") != "USD" or quote.get("asset_type") != "equity_option"):
            raise Hold("stop quote does not identify a tradable standard option")
        bid, ask = money(quote.get("bid"), positive=True), money(quote.get("ask"), positive=True)
        if bid > ask:
            raise Hold("invalid stop quote spread")
        ticks = quote.get("min_ticks")
        tick = money(ticks["above_tick" if price >= money(ticks["cutoff_price"]) else "below_tick"], positive=True) if ticks else money(quote.get("tick_size"), positive=True)
        price = (price / tick).to_integral_value(rounding=ROUND_CEILING) * tick
        before_quantity = owned["quantity"]
        if source_guard is not None:
            await source_guard()
        await self.cancel(group, key)
        owned = self.owned(group, contract)
        if owned is None:
            return self.state(row, "complete", "Native stop closed the remaining position during replacement")
        if owned["quantity"] != before_quantity:
            raise RetryHold("Native stop filled during replacement; rechecking remaining quantity")
        message, decision = json.loads(row["message"]), json.loads(row["decision"])
        generation = row["generation"] + 1
        identity = f"{message['id']}:{message['revision']}:stop:{generation}"
        order = {"client_order_id": hashlib.sha256(identity.encode()).hexdigest(), "contract": contract,
                 "option_id": quote.get("option_id"),
                 "side": "sell", "position_effect": "close", "quantity": owned["quantity"],
                 "order_type": "market" if bid <= price else "stop_market", "stop_price": str(price),
                 "time_in_force": "gfd" if bid <= price else "gtc", "requested_stop_price": row["stop_price"],
                 "quote_timestamp": quote["timestamp"], "account_timestamp": snapshot["timestamp"],
                 "origin_message_id": decision.get("origin_message_id")}
        if order["order_type"] == "market":
            order.pop("stop_price")
        with store.db:
            store.db.execute("UPDATE stop_requests SET generation=? WHERE source_group=? AND contract=?", (generation, group, key))
            store.reserve(message, decision | {"action": "UPDATE_STOP"}, order, engine.clock())
            self.state(row, "pending", "Native stop submission pending", order["client_order_id"])

        source_failed = False
        async def before_submit(current_snapshot, current_quote):
            nonlocal source_failed
            engine.check_execution_mode()
            if Path(engine.config["kill_switch"]).exists():
                raise Hold("kill switch is present")
            current = self.owned(group, contract)
            if not current or current["quantity"] != order["quantity"] or self.entry_id(group, key) != row["entry_id"]:
                raise Hold("protected inventory changed before dispatch")
            engine.check_quote_age(current_quote, engine.clock())
            engine.check_quote_age(current_snapshot, engine.clock(), "account snapshot")
            if (current_snapshot.get("account_id") != engine.account or current_snapshot.get("market_open") is not True
                    or current_snapshot.get("restrictions") or canonical_contract(current_quote["contract"]) != contract):
                raise Hold("stop account or quote changed during broker review")
            if source_guard is not None:
                try:
                    await source_guard(own_mutation=True)
                except Hold:
                    source_failed = True
                    raise

        try:
            result = await engine.broker.submit(order, **({"before_submit": before_submit} if engine.mode == "live" else {}))
            store.apply_result(order["client_order_id"], result)
        except BrokerPreflightHold as exc:
            store.reject_before_submission(order["client_order_id"])
            if source_failed:
                raise Hold("Stop instruction changed or could not be verified before submission; reassessment required") from exc
            raise RetryHold("Native stop held before submission; no stop installed") from exc
        except Exception as exc:
            store.mark_unknown(order["client_order_id"])
            raise Hold("Native stop submission is uncertain; reconcile before submitting another order") from exc
        if result["status"] == "filled":
            return self.state(row, "complete", f"Stop level ${price} already reached; remaining contracts sold", order["client_order_id"])
        if result["status"] in {"open", "partially_filled"} and order["order_type"] == "stop_market":
            return self.state(row, "active", f"Native stop active at ${price} for {order['quantity'] - result['filled_quantity']} remaining contracts", order["client_order_id"])
        return self.state(row, "pending" if result["status"] not in TERMINAL else "blocked",
                          f"Native stop order {result['status']}; protection is not confirmed active", order["client_order_id"])

    async def maintain(self):
        """Called under the execution lock after normal order reconciliation."""
        for row in self.store.db.execute("SELECT * FROM stop_requests WHERE status IN ('pending','active')").fetchall():
            order = self.store.db.execute("SELECT status FROM orders WHERE id=?", (row["order_id"],)).fetchone()
            if order and order["status"] == "filled":
                self.state(row, "complete", "Native protection order filled")
                continue
            result = await self.arm(row)
            if result["status"] != row["status"] or result["reason"] != row["reason"]:
                message, decision = json.loads(row["message"]), json.loads(row["decision"])
                decision["stop_evaluation"] = result
                self.store.record(message, "broker_order" if result["status"] in {"active", "complete"} else "held", result["reason"], decision)
