"""Close relay-owned, expiring ITM options independently of Discord signals."""
from __future__ import annotations

import asyncio
import hashlib
import json
from datetime import timedelta

from .broker import regular_session
from .core import EASTERN, Hold, contract_key, money


EXIT_WINDOW = timedelta(minutes=60)
CHECK_SECONDS = 30


class ExpiryExits:
    def __init__(self, engine, reconcile):
        self.engine, self.store, self.reconcile = engine, engine.store, reconcile

    def entry(self, position):
        return self.store.db.execute("""SELECT id FROM orders
            WHERE source_group=? AND contract=? AND action='OPEN' AND filled_quantity>0
            ORDER BY julianday(created_at) DESC, rowid DESC LIMIT 1""",
            (position["source_group"], contract_key(position["contract"]))).fetchone()

    async def underlying(self, contract):
        quote = await self.engine.broker.underlying_quote(contract["symbol"])
        self.engine.check_quote_age(quote, self.engine.clock(), "underlying quote")
        if quote.get("symbol") != contract["symbol"]:
            raise Hold("Expiry protection blocked: underlying symbol mismatch")
        price, strike = money(quote.get("price"), positive=True), money(contract["strike"], positive=True)
        itm = price > strike if contract["option_type"] == "call" else price < strike
        return quote, price, itm

    def event(self, position, entry):
        identity = json.dumps([position["source_group"], position["contract"], entry], sort_keys=True)
        message = {"id": "expiry:" + hashlib.sha256(identity.encode()).hexdigest(),
                   "revision": "expiry-itm-v1", "source_group": position["source_group"],
                   "timestamp": self.engine.clock().isoformat(), "content": "Expiry exercise protection"}
        attempts = self.store.db.execute("""SELECT COUNT(*) FROM orders WHERE message_id=?
            AND status='rejected' AND broker_id IS NULL AND filled_quantity=0""", (message["id"],)).fetchone()[0]
        message["revision"] += ":" + str(attempts)
        # Internal policy events are deliberately absent from Discord message history.
        with self.store.db:
            self.store.db.execute("""INSERT OR IGNORE INTO events
                (message_id,revision,state,reason,created_at) VALUES (?,?,?,?,?)""",
                (message["id"], message["revision"], "held", "Checking expiry exercise protection", message["timestamp"]))
        return message

    async def check(self):
        results = []
        engine = self.engine
        if engine.interpreter is None:  # --observe-only disables every execution path.
            return results
        today = engine.clock().astimezone(EASTERN).date().isoformat()
        positions = [p for p in self.store.positions() if p["contract"]["expiry"] == today]
        if not positions:
            return results
        try:
            session = regular_session(engine.clock())
        except Exception:
            session = None
        if session is not None and engine.clock() < session[1] - EXIT_WINDOW:
            return results
        await self.reconcile()
        for position in positions:
            # Reconciliation may have filled an existing Discord or expiry exit.
            current = next((p for p in self.store.positions() if p["source_group"] == position["source_group"]
                            and p["contract"] == position["contract"]), None)
            if current is None:
                continue
            position = current
            entry = self.entry(position)
            message = self.event(position, entry["id"] if entry else None)
            decision = {"action": "CLOSE", "contract": position["contract"], "quantity": None,
                        "fraction": None, "alert_price": None, "stop_price": None,
                        "ambiguous": False, "confidence": 1.0, "profit_only": False,
                        "origin_message_id": None, "evidence": [],
                        "reason": "Close expiring in-the-money inventory to avoid exercise",
                        "expiry_exit": {"status": "blocked"}}
            try:
                if entry is None:
                    raise Hold("Expiry protection blocked: current relay-owned entry cannot be identified")
                if session is None:
                    raise Hold("Expiry protection blocked: exchange session is unavailable")
                if engine.clock() >= session[1]:
                    decision["expiry_exit"]["status"] = "market_closed"
                    raise Hold("Expiry protection: market closed with remaining inventory; exercise risk needs immediate broker assistance")
                underlying, price, itm = await self.underlying(position["contract"])
                decision["expiry_exit"].update(underlying_price=str(price), close_at=session[1].isoformat())
                if not itm:
                    decision["expiry_exit"]["status"] = "watching"
                    results.append(self.store.record(message, "context", "Expiry protection watching: underlying is not in the money; rechecking every 30 seconds", decision))
                    continue
                prior = self.store.db.execute("SELECT * FROM orders WHERE message_id=? ORDER BY rowid DESC LIMIT 1", (message["id"],)).fetchone()
                if prior is not None:
                    # An accepted/uncertain order is never replaced blindly, including across restarts.
                    # A final dispatch hold is the sole safe retry: broker transport never began.
                    if not (prior["status"] == "rejected" and prior["broker_id"] is None and not prior["filled_quantity"]):
                        decision["expiry_exit"]["status"] = "unfilled"
                        raise Hold("Expiry protection: prior exit has remaining inventory; check the broker for pending, partial, canceled or uncertain fills")

                async def guard(*, refresh=False):
                    nonlocal underlying
                    if not max(session[0], session[1] - EXIT_WINDOW) <= engine.clock() < session[1]:
                        raise Hold("Expiry protection: exchange session ended before submission")
                    if refresh:
                        underlying, refreshed_price, still_itm = await self.underlying(position["contract"])
                        decision["expiry_exit"]["underlying_price"] = str(refreshed_price)
                        if not still_itm:
                            raise Hold("Expiry protection: underlying is no longer in the money at final review")
                    engine.check_quote_age(underlying, engine.clock(), "underlying quote")
                    if engine.clock().astimezone(EASTERN).date().isoformat() != today:
                        raise Hold("Expiry protection: expiry date changed")
                    latest = self.entry(position)
                    if latest is None or latest["id"] != entry["id"] or position not in self.store.positions():
                        raise Hold("Expiry protection: owned position changed before submission")

                decision["expiry_exit"]["status"] = "closing"
                result = await engine.execute_decision(message, decision, expiry_guard=guard)
                if result["state"] == "held":
                    decision["expiry_exit"]["status"] = "blocked"
                    result = self.store.record(message, "held", result["reason"], decision)
                results.append(result)
            except Hold as exc:
                if decision["expiry_exit"]["status"] == "closing":
                    decision["expiry_exit"]["status"] = "blocked"
                facts = decision["expiry_exit"]
                detail = str(exc)
                if "underlying_price" in facts:
                    detail += f"; underlying ${facts['underlying_price']}; strike ${position['contract']['strike']}; close {facts['close_at']}"
                results.append(self.store.record(message, "held", detail, decision))
            except Exception as exc:
                decision["expiry_exit"]["status"] = "unavailable"
                detail = engine.diagnose_failure(decision, exc, stage="expiry")
                results.append(self.store.record(message, "error", f"Expiry protection could not verify broker quotes or account state; {detail}", decision))
        return results

    async def run(self, emit):
        while True:
            async with self.engine.lock:
                for result in await self.check():
                    emit(result)
            await asyncio.sleep(CHECK_SECONDS)
