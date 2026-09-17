"""Durable interpretation, account-relative sizing, and isolated paper/shadow/live execution."""
from __future__ import annotations

import asyncio
import fcntl
import hashlib
import json
import re
import sqlite3
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_CEILING, ROUND_FLOOR
from pathlib import Path
from zoneinfo import ZoneInfo

from .broker import BrokerPreflightHold
from .interpreter import InterpretationError, safe_interpretation_reason

UTC = timezone.utc
EASTERN = ZoneInfo("America/New_York")
TERMINAL = {"filled", "canceled", "rejected", "expired"}


class Hold(ValueError):
    """A deterministic control prevented an order."""


class RetryHold(Hold):
    """A readiness check failed before any submission was attempted."""


def channel_allows_author(channel, author_id):
    """Return whether a message author is allowed by a configured channel."""
    if not isinstance(channel, dict):
        return False
    authors = channel.get("authors", [])
    if not isinstance(authors, list):
        return False
    if any(not re.fullmatch(r"\d{15,22}", str(author)) for author in authors):
        return False
    return not authors or str(author_id) in {str(author) for author in authors}


def instant(value):
    try:
        result = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if result.tzinfo is None:
            raise ValueError("timezone required")
        return result.astimezone(UTC)
    except (TypeError, ValueError) as exc:
        raise Hold("invalid or missing timestamp") from exc


def money(value, *, positive=False):
    try:
        if isinstance(value, bool):
            raise ValueError("boolean is not a price")
        result = Decimal(str(value))
        if not result.is_finite() or result < 0 or (positive and result <= 0):
            raise ValueError("price out of range")
        return result
    except (ValueError, InvalidOperation) as exc:
        raise Hold("invalid monetary value") from exc


def _decimal_text(value, *, signed=False):
    text = format(Decimal(value).normalize(), "f")
    if text in {"", "-0"}:
        text = "0"
    if signed and not text.startswith("-"):
        text = "+" + text
    return text


def entry_chase_evaluation(ask, reference_price, limit_price, max_chase_fraction):
    """Return finite decimal diagnostics for the bounded entry premium check."""
    ask = money(ask, positive=True)
    reference_price = money(reference_price, positive=True)
    limit_price = money(limit_price, positive=True)
    max_chase_fraction = money(max_chase_fraction)
    return {
        "ask": _decimal_text(ask),
        "reference_price": _decimal_text(reference_price),
        "ask_deviation_percent": _decimal_text((ask - reference_price) / reference_price * 100, signed=True),
        "limit_price": _decimal_text(limit_price),
        "limit_deviation_percent": _decimal_text((limit_price - reference_price) / reference_price * 100, signed=True),
        "max_chase_percent": _decimal_text(max_chase_fraction * 100),
    }


def entry_chase_within_cap(evaluation):
    ceiling = money(evaluation["reference_price"], positive=True) * (1 + money(evaluation["max_chase_percent"]) / 100)
    return money(evaluation["ask"], positive=True) <= ceiling and money(evaluation["limit_price"], positive=True) <= ceiling


def entry_chase_reason(prefix, evaluation):
    if not evaluation:
        return prefix
    return (
        f"{prefix}; evaluated ask=${evaluation['ask']}, reference=${evaluation['reference_price']}, "
        f"ask deviation={evaluation['ask_deviation_percent']}%, "
        f"limit={evaluation['limit_price']}, limit deviation={evaluation['limit_deviation_percent']}%, "
        f"cap={evaluation['max_chase_percent']}%"
    )



def canonical_contract(value):
    if not isinstance(value, dict) or set(value) != {"symbol", "expiry", "strike", "option_type"}:
        raise Hold("an exact option contract is required")
    symbol = value["symbol"]
    if not isinstance(symbol, str) or not re.fullmatch(r"[A-Z][A-Z0-9.]{0,9}", symbol):
        raise Hold("unsupported underlying symbol")
    if value["option_type"] not in {"call", "put"}:
        raise Hold("call or put must be explicit")
    try:
        expiry = datetime.strptime(value["expiry"], "%Y-%m-%d").date().isoformat()
    except (ValueError, TypeError) as exc:
        raise Hold("expiry must be an absolute date") from exc
    return dict(symbol=symbol, expiry=expiry, strike=str(money(value["strike"], positive=True).normalize()), option_type=value["option_type"])


def contract_key(contract):
    return json.dumps(canonical_contract(contract), sort_keys=True, separators=(",", ":"))


def entry_size(risk, snapshot, contract, confidence, limit_price, source_group):
    """Allocate the user's premium risk; parsing confidence is never a win probability."""
    equity = money(snapshot.get("equity"), positive=True)
    buying_power = money(snapshot.get("buying_power"))
    exposures = snapshot.get("option_exposure_by_symbol")
    if not isinstance(exposures, dict):
        raise Hold("complete account option exposure is required for sizing")
    if any(not isinstance(symbol, str) or not re.fullmatch(r"[A-Z][A-Z0-9.]{0,9}", symbol) for symbol in exposures):
        raise Hold("account option exposure has an invalid underlying symbol")
    exposures = {symbol: money(value) for symbol, value in exposures.items()}
    confidence = money(confidence, positive=True)
    minimum_confidence = money(risk["min_confidence"], positive=True)
    if not minimum_confidence <= confidence <= 1:
        raise Hold("parsing confidence is outside the permitted range")
    low_cap = money(risk["entry_risk_min_fraction"], positive=True)
    high_cap = money(risk["entry_risk_max_fraction"], positive=True)
    if not low_cap <= high_cap <= 1:
        raise Hold("entry risk fractions must be ordered and at most one")
    confidence_scale = (confidence - minimum_confidence) / (1 - minimum_confidence) if minimum_confidence < 1 else Decimal(1)
    confidence_cap = low_cap + (high_cap - low_cap) * confidence_scale
    stats = risk.get("strategy_stats", {}).get(source_group)
    if stats is None:
        fraction = confidence_cap
        method = "confidence_allocation_cap"
    else:
        if not isinstance(stats, dict) or stats.get("calibrated") is not True:
            raise Hold("Kelly sizing requires explicitly calibrated strategy statistics")
        probability = money(stats.get("win_probability"), positive=True)
        payoff = money(stats.get("payoff_ratio"), positive=True)
        if probability >= 1:
            raise Hold("calibrated win probability must be strictly between zero and one")
        edge = probability - (1 - probability) / payoff
        if edge <= 0:
            raise Hold("calibrated strategy has no positive Kelly edge")
        fraction = min(confidence_cap, edge * money(risk["fractional_kelly"], positive=True) * confidence)
        method = "fractional_kelly"
    # Percentage allocations size additional contracts. They do not prevent
    # one affordable whole contract when the reference budget is smaller.
    limits = {
        "strategy_risk": equity * fraction,
        "symbol_concentration": equity * money(risk["max_position_fraction"], positive=True) - exposures.get(contract["symbol"], Decimal(0)),
        "total_option_exposure": equity * money(risk["max_total_exposure_fraction"], positive=True) - sum(exposures.values()),
        "buying_power_reserve": buying_power - equity * money(risk["buying_power_reserve_fraction"]),
    }
    binding = min(limits, key=limits.get)
    budget = limits[binding]
    per_contract = money(limit_price, positive=True) * 100 + money(risk["fee_reserve_per_contract"])
    available = max(Decimal(0), limits["buying_power_reserve"])
    if available < per_contract:
        raise Hold(f"one whole contract including fee reserve ({per_contract}) exceeds buying_power_reserve maximum ({available})")
    quantity = int((max(Decimal(0), budget) / per_contract).to_integral_value(rounding=ROUND_FLOOR))
    minimum_contract_fallback = quantity == 0
    quantity = max(1, quantity)
    return quantity, dict(method=method, source_group=source_group, equity=str(equity),
                          parse_confidence=str(confidence), risk_fraction=str(fraction), confidence_cap_fraction=str(confidence_cap),
                          budget=str(max(Decimal(0), budget)), binding_limit=binding,
                          minimum_contract_fallback=minimum_contract_fallback, available_buying_power=str(available),
                          premium_risk_per_contract=str(per_contract), allocated_premium_risk=str(per_contract * quantity))


def load_config(path, *, allow_unbound=False):
    path = Path(path).resolve()
    config = json.loads(path.read_text())
    mode = config.get("mode")
    if mode not in {"paper", "shadow", "live"}:
        raise Hold("mode must be paper, shadow, or live")
    account = config.get("robinhood", {}).get("account_number")
    if mode != "paper" and not (allow_unbound and mode == "shadow") and (not isinstance(account, str) or not re.fullmatch(r"\d{5,20}", account)):
        raise Hold("shadow and live execution require a bound Robinhood account number")
    if mode == "live" and config.get("robinhood", {}).get("enable_live_orders") is not True:
        raise Hold("live order submission must be explicitly enabled")
    channels = config.get("channels", [])
    if len(channels) != 2 or len({c.get("id") for c in channels}) != 2:
        raise Hold("configure exactly two distinct Discord channels")
    for channel in channels:
        channel["id"] = str(channel.get("id", ""))
        channel["guild_id"] = str(channel.get("guild_id", ""))
        authors = channel.get("authors", [])
        if not isinstance(authors, list):
            raise Hold("channel authors must be a list")
        channel["authors"] = [str(author) for author in authors]
        if not re.fullmatch(r"\d{15,22}", str(channel.get("id", ""))):
            raise Hold("channel IDs must be Discord snowflakes")
        if any(not re.fullmatch(r"\d{15,22}", author) for author in channel["authors"]):
            raise Hold("channel author IDs must be Discord snowflakes")
        if channel.get("role") not in {"signals", "context"} or not channel.get("source_group"):
            raise Hold("each channel needs a role and source_group")
    risk = config["risk"]
    for name in ("max_signal_age_seconds", "max_quote_age_seconds", "duplicate_window_seconds", "max_pending_messages"):
        if type(risk.get(name)) is not int or risk[name] <= 0:
            raise Hold(f"{name} must be a positive integer")
    for name in ("entry_risk_min_fraction", "entry_risk_max_fraction", "fractional_kelly", "max_position_fraction", "max_total_exposure_fraction"):
        if money(risk.get(name), positive=True) > 1:
            raise Hold(f"{name} must be greater than zero and at most one")
    if money(risk["entry_risk_min_fraction"]) > money(risk["entry_risk_max_fraction"]):
        raise Hold("entry risk fractions must be ordered and at most one")
    if money(risk.get("buying_power_reserve_fraction")) >= 1:
        raise Hold("buying_power_reserve_fraction must be between zero inclusive and one exclusive")
    stats = risk.get("strategy_stats", {})
    if not isinstance(stats, dict):
        raise Hold("strategy_stats must map source groups to calibrated statistics")
    for source_group, values in stats.items():
        if not isinstance(source_group, str) or not source_group or not isinstance(values, dict) or values.get("calibrated") is not True:
            raise Hold("each Kelly strategy needs an explicit source group and calibrated=true")
        if money(values.get("win_probability"), positive=True) >= 1:
            raise Hold("calibrated win probability must be strictly between zero and one")
        money(values.get("payoff_ratio"), positive=True)
    for name in ("max_spread_fraction", "max_chase_fraction"):
        if money(risk[name]) > 1:
            raise Hold(f"{name} must be between zero and one")
    try:
        if isinstance(risk["min_confidence"], bool):
            raise ValueError("boolean confidence")
        risk["min_confidence"] = float(risk["min_confidence"])
    except (ValueError, TypeError) as exc:
        raise Hold("min_confidence must be numeric") from exc
    if not 0 < risk["min_confidence"] <= 1:
        raise Hold("min_confidence must be greater than zero and at most one")
    money(risk["fee_reserve_per_contract"])
    if type(risk["allow_same_day_expiry"]) is not bool:
        raise Hold("allow_same_day_expiry must be boolean")
    discord = config.setdefault("discord", {})
    if not isinstance(discord, dict) or discord.get("transport", "browser") not in {"browser", "gateway"}:
        raise Hold("Discord transport must be browser or gateway")
    if "token" in discord:
        raise Hold("Store the Discord credential through Setup, not in configuration")
    discord.setdefault("token_store", "state/discord-user.json")
    discord.setdefault("token_env", "DISCORD_USER_TOKEN")
    if (not isinstance(discord["token_store"], str) or not discord["token_store"]
            or not isinstance(discord["token_env"], str)
            or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,127}", discord["token_env"])):
        raise Hold("Discord credential storage or environment setting is invalid")
    # Keep the final credential path intact so its reader can reject symlinks.
    discord["token_store"] = str((path.parent / discord["token_store"]).absolute())
    for section, key in ((config, "database"), (config, "kill_switch"), (config["browser"], "profile_dir"), (config["robinhood"], "token_store"), (config["paper"], "quotes_file")):
        if section.get(key):
            section[key] = str((path.parent / section[key]).resolve())
    poll = config["browser"].get("poll_seconds", 3)
    if type(poll) not in (int, float) or not 2 <= poll <= 60:
        raise Hold("browser polling must be between 2 and 60 seconds")
    return config


class Store:
    def __init__(self, path, *, read_only=False):
        self.path = Path(path)
        self.process_lock = None
        if read_only:
            self.db = sqlite3.connect(self.path.as_uri() + "?mode=ro", uri=True)
            self.db.row_factory = sqlite3.Row
            return
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.process_lock = self.path.with_suffix(self.path.suffix + ".lock").open("a+")
        self.path.with_suffix(self.path.suffix + ".lock").chmod(0o600)
        try:
            fcntl.flock(self.process_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            self.process_lock.close()
            raise Hold("another relay process owns this database") from exc
        self.db = sqlite3.connect(self.path)
        self.path.chmod(0o600)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS messages (
                id TEXT PRIMARY KEY, channel_id TEXT NOT NULL, source_group TEXT NOT NULL,
                timestamp TEXT NOT NULL, revision TEXT NOT NULL, body TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS events (
                id INTEGER PRIMARY KEY, message_id TEXT NOT NULL, revision TEXT NOT NULL,
                state TEXT NOT NULL, reason TEXT NOT NULL, decision TEXT, created_at TEXT NOT NULL,
                UNIQUE(message_id, revision));
            CREATE TABLE IF NOT EXISTS orders (
                id TEXT PRIMARY KEY, message_id TEXT NOT NULL, source_group TEXT NOT NULL,
                contract TEXT NOT NULL, action TEXT NOT NULL, body TEXT NOT NULL,
                status TEXT NOT NULL, broker_id TEXT, created_at TEXT NOT NULL,
                filled_quantity INTEGER NOT NULL DEFAULT 0, filled_notional TEXT NOT NULL DEFAULT '0');
            CREATE TABLE IF NOT EXISTS positions (
                source_group TEXT NOT NULL, contract TEXT NOT NULL, quantity INTEGER NOT NULL,
                average_price TEXT NOT NULL, PRIMARY KEY(source_group, contract));
        """)
        # A crash after dispatch cannot establish whether the broker accepted an order.
        self.db.execute("UPDATE orders SET status='unknown' WHERE status='submitting'")
        self.db.commit()

    def close(self):
        self.db.close()
        if self.process_lock:
            self.process_lock.close()

    def bind_execution(self, mode, account):
        binding = json.dumps({"mode": mode, "account": account}, sort_keys=True)
        row = self.db.execute("SELECT value FROM metadata WHERE key='execution_binding'").fetchone()
        if row and row[0] != binding:
            raise Hold("this ledger belongs to another execution mode or account; use a separate database")
        if not row:
            if mode != "paper" and self.db.execute("SELECT COUNT(*) FROM orders").fetchone()[0]:
                raise Hold("an unbound ledger with prior orders cannot be used for a real account")
            with self.db:
                self.db.execute("INSERT INTO metadata VALUES ('execution_binding', ?)", (binding,))

    def observe(self, message):
        old = self.db.execute("SELECT revision, body FROM messages WHERE id=?", (message["id"],)).fetchone()
        if old and old[0] == message["revision"]:
            if message.get("transport_revision"):
                stored = json.loads(old[1])
                if stored.get("transport_revision") != message["transport_revision"]:
                    for key in ("attachments", "embeds", "transport_revision"):
                        if key in message:
                            stored[key] = message[key]
                    with self.db:
                        self.db.execute("UPDATE messages SET body=? WHERE id=? AND revision=?",
                                        (json.dumps(stored), message["id"], message["revision"]))
            return "same"
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO messages VALUES (?,?,?,?,?,?)", (
                message["id"], message["channel_id"], message["source_group"], message["timestamp"],
                message["revision"], json.dumps(message)))
            self.db.execute("INSERT OR IGNORE INTO events(message_id,revision,state,reason,created_at) VALUES (?,?,?,?,?)", (
                message["id"], message["revision"], "observed", "awaiting interpretation", datetime.now(UTC).isoformat()))
        return "edit" if old else "new"

    def record(self, message, state, reason, decision=None):
        if decision and state not in {"recovery_pending", "recovery_evaluating"}:
            # Observation time predates evaluation. Finish timing at the durable
            # outcome, including policy checks and any broker response.
            finished = datetime.now(UTC)
            for stage in (decision, decision.get("recovery", {})):
                if not isinstance(stage, dict):
                    continue
                timing = stage.get("evaluation_timing")
                if not isinstance(timing, dict):
                    continue
                timing["decision_at"] = finished.isoformat()
                timing.pop("posted_to_decision_seconds", None)
                timing.pop("delayed", None)
                try:
                    elapsed = (finished - instant(message["timestamp"])).total_seconds()
                except Hold:
                    continue
                if elapsed >= 0:
                    timing["posted_to_decision_seconds"] = round(elapsed, 6)
                    duration = timing.get("model_duration_seconds")
                    if isinstance(duration, (int, float)):
                        timing["delayed"] = elapsed > duration + 1
        with self.db:
            self.db.execute("UPDATE events SET state=?,reason=?,decision=? WHERE message_id=? AND revision=?", (
                state, reason, json.dumps(decision) if decision else None, message["id"], message["revision"]))
        return {"message_id": message["id"], "state": state, "reason": reason}

    def context(self, message, limit=60):
        # Sort in Python by parsed time: ISO strings with different offsets do not sort reliably.
        rows = self.db.execute("SELECT body FROM messages WHERE id!=? ORDER BY rowid DESC LIMIT ?", (message["id"], max(120, limit * 4))).fetchall()
        items = [json.loads(r[0]) for r in rows]
        cutoff = instant(message["timestamp"])
        items = [m for m in items if instant(m["timestamp"]) <= cutoff and (not m.get("edited_timestamp") or instant(m["edited_timestamp"]) <= cutoff)]
        return sorted(items, key=lambda m: (instant(m["timestamp"]), int(m["id"]))) [-limit:]

    def positions(self):
        return [dict(source_group=r["source_group"], contract=json.loads(r["contract"]), quantity=r["quantity"], average_price=r["average_price"], bot_owned=True)
                for r in self.db.execute("SELECT * FROM positions WHERE quantity>0")]

    def unresolved(self):
        return self.db.execute("SELECT COUNT(*) FROM orders WHERE status NOT IN ('filled','canceled','rejected','expired')").fetchone()[0]

    def reserve(self, message, decision, order, now):
        with self.db:
            self.db.execute("INSERT INTO orders(id,message_id,source_group,contract,action,body,status,created_at) VALUES (?,?,?,?,?,?,?,?)", (
                order["client_order_id"], message["id"], message["source_group"], contract_key(order["contract"]), decision["action"], json.dumps(order), "submitting", now.isoformat()))

    def mark_unknown(self, order_id):
        with self.db:
            self.db.execute("UPDATE orders SET status='unknown' WHERE id=?", (order_id,))

    def reject_before_submission(self, order_id):
        with self.db:
            changed = self.db.execute("UPDATE orders SET status='rejected' WHERE id=? AND status='submitting' AND broker_id IS NULL AND filled_quantity=0", (order_id,))
            if changed.rowcount != 1:
                raise Hold("cannot establish that this order was never submitted")

    def apply_result(self, order_id, result):
        """Apply cumulative fills once, including partial fills and repeat reconciliations."""
        row = self.db.execute("SELECT * FROM orders WHERE id=?", (order_id,)).fetchone()
        order = json.loads(row["body"])
        broker_id = result.get("id")
        if not isinstance(broker_id, str) or not broker_id:
            raise Hold("broker order ID is missing")
        if row["broker_id"] and row["broker_id"] != broker_id:
            raise Hold("broker order identity changed")
        # Keep an acknowledged broker ID even if its fill payload is unusable, for reconciliation.
        if not row["broker_id"]:
            with self.db:
                self.db.execute("UPDATE orders SET broker_id=? WHERE id=?", (broker_id, order_id))
        status = result.get("status")
        count = result.get("filled_quantity")
        if status not in TERMINAL | {"submitted", "open", "partially_filled", "pending"}:
            raise Hold("broker order status is unknown")
        if type(count) is not int or not row["filled_quantity"] <= count <= order["quantity"]:
            raise Hold("broker fill quantity is invalid")
        if status == "filled" and count != order["quantity"]:
            raise Hold("filled status does not match the requested quantity")
        if row["status"] in TERMINAL and (status != row["status"] or count != row["filled_quantity"]):
            raise Hold("terminal order result changed")
        price = money(result.get("fill_price"), positive=True) if count else Decimal(0)
        limit = money(order["limit_price"], positive=True)
        if count and ((order["side"] == "buy" and price > limit) or (order["side"] == "sell" and price < limit)):
            raise Hold("fill violates the order limit")
        total = price * count
        delta = count - row["filled_quantity"]
        delta_notional = total - money(row["filled_notional"])
        if delta < 0 or delta_notional < 0 or (delta == 0 and delta_notional != 0) or (delta > 0 and delta_notional <= 0):
            raise Hold("inconsistent cumulative fill accounting")
        if delta and ((order["side"] == "buy" and delta_notional / delta > limit) or (order["side"] == "sell" and delta_notional / delta < limit)):
            raise Hold("incremental fill violates the order limit")
        with self.db:
            if delta:
                position = self.db.execute("SELECT quantity,average_price FROM positions WHERE source_group=? AND contract=?", (row["source_group"], row["contract"])).fetchone()
                qty = position[0] if position else 0
                average = money(position[1]) if position else Decimal(0)
                if order["side"] == "buy":
                    average = (qty * average + delta_notional) / (qty + delta)
                    qty += delta
                else:
                    if delta > qty:
                        raise Hold("sell exceeds the position owned by this relay")
                    qty -= delta
                self.db.execute("INSERT OR REPLACE INTO positions VALUES (?,?,?,?)", (row["source_group"], row["contract"], qty, str(average)))
            self.db.execute("UPDATE orders SET status=?,broker_id=?,filled_quantity=?,filled_notional=? WHERE id=?", (status, result["id"], count, str(total), order_id))

    def report(self):
        return {
            "messages": self.db.execute("SELECT COUNT(*) FROM messages").fetchone()[0],
            "events": dict(self.db.execute("SELECT state,COUNT(*) FROM events GROUP BY state").fetchall()),
            "orders": dict(self.db.execute("SELECT status,COUNT(*) FROM orders GROUP BY status").fetchall()),
            "positions": self.positions(),
            "unresolved_orders": self.unresolved(),
        }


class Engine:
    def __init__(self, config, store, interpreter, broker, clock=None):
        self.config, self.store, self.interpreter, self.broker = config, store, interpreter, broker
        self.mode = config.get("mode")
        self.account = config.get("robinhood", {}).get("account_number") if self.mode != "paper" else None
        self.check_execution_mode()
        self.store.bind_execution(self.mode, self.account)
        self.clock = clock or (lambda: datetime.now(UTC))
        self.lock = asyncio.Lock()
        self.channels = {c["id"]: c for c in config["channels"]}
        self.observations = {}
        self.source_latest = {}
        self.verify_current = None

    def check_execution_mode(self):
        if self.mode not in {"paper", "shadow", "live"} or self.config.get("mode") != self.mode:
            raise Hold("execution mode changed; restart with its separate ledger")
        if self.mode != "paper":
            if not isinstance(self.account, str) or not re.fullmatch(r"\d{5,20}", self.account):
                raise Hold("a bound Robinhood account number is required")
            if self.config.get("robinhood", {}).get("account_number") != self.account:
                raise Hold("bound account changed; restart with its separate ledger")
        if self.mode == "live" and self.config.get("robinhood", {}).get("enable_live_orders") is not True:
            raise Hold("live order submission is not explicitly enabled")

    def note_observation(self, message):
        """Called by the reader immediately, even while an older decision awaits the model."""
        channel = self.channels.get(str(message.get("channel_id")))
        if channel and channel_allows_author(channel, message.get("author_id", "")):
            identity = str(message.get("id", ""))
            if identity.isdigit():
                self.observations[identity] = message.get("revision")
                group = channel["source_group"]
                self.source_latest[group] = max(int(identity), self.source_latest.get(group, 0))

    def unchanged(self, message):
        if self.observations.get(message["id"], message["revision"]) != message["revision"]:
            raise Hold("message was revised during interpretation")
        if self.source_latest.get(message["source_group"], int(message["id"])) > int(message["id"]):
            raise Hold("a newer source message requires interpretation before this action")

    def fresh(self, message, now):
        age = (now - instant(message["timestamp"])).total_seconds()
        if age < -5 or age > self.config["risk"]["max_signal_age_seconds"]:
            raise Hold("signal is stale or its timestamp is in the future")

    def origin(self, message, decision):
        origin_id = decision.get("origin_message_id")
        if not isinstance(origin_id, str) or not any(e.get("message_id") == origin_id for e in decision.get("evidence", [])):
            raise Hold("the original action message must be identified with evidence")
        if origin_id == message["id"]:
            origin = message
        else:
            row = self.store.db.execute("SELECT body FROM messages WHERE id=?", (origin_id,)).fetchone()
            if row is None:
                raise Hold("the original action message is unavailable")
            origin = json.loads(row[0])
        if origin["source_group"] != message["source_group"] or origin.get("edited_timestamp"):
            raise Hold("the original action source is different or edited")
        if self.observations.get(origin["id"], origin["revision"]) != origin["revision"]:
            raise Hold("the original action message was revised")
        return origin

    async def verify_dispatch(self, message, decision, order, snapshot=None, quote=None, *, recovery_guard=None, expiry_guard=None):
        """Recheck intent after broker review and immediately before its placement boundary."""
        async def current(*, refresh=False):
            now = self.clock()
            if expiry_guard is not None:
                if decision["action"] != "CLOSE" or order["side"] != "sell":
                    raise Hold("expiry policy may only close owned contracts")
                await expiry_guard(refresh=refresh)
                if refresh:
                    with self.store.db:
                        self.store.db.execute("UPDATE orders SET body=? WHERE id=? AND status='submitting'",
                                              (json.dumps(order), order["client_order_id"]))
            elif recovery_guard is None:
                self.fresh(message, now)
                self.fresh(self.origin(message, decision), now)
                self.unchanged(message)
            else:
                if decision["action"] not in {"REDUCE", "CLOSE"} or order["side"] != "sell":
                    raise Hold("recovery execution is restricted to closing owned contracts")
                recovery_guard()
            self.check_execution_mode()
            if Path(self.config["kill_switch"]).exists():
                raise Hold("kill switch is present")
            self.check_quote_age(quote if quote is not None else {"timestamp": order["quote_timestamp"]}, now)
            self.check_quote_age(snapshot if snapshot is not None else {"timestamp": order["account_timestamp"]}, now, "account snapshot")
            if snapshot is not None:
                if snapshot.get("account_id") != self.account or snapshot.get("market_open") is not True:
                    raise Hold("bound account or options session changed during broker review")
                restrictions = snapshot.get("restrictions", [])
                if not isinstance(restrictions, list) or restrictions:
                    raise Hold("broker account restrictions prevent this action")
                if quote is None or canonical_contract(quote.get("contract")) != order["contract"] or quote.get("tradable") is not True:
                    raise Hold("reviewed quote no longer identifies this tradable contract")
                bid, ask = money(quote.get("bid"), positive=True), money(quote.get("ask"), positive=True)
                if ask < bid or (expiry_guard is None and (ask - bid) / ask > money(self.config["risk"]["max_spread_fraction"])):
                    raise Hold("reviewed quote spread exceeds the configured limit")
                if order["side"] == "buy":
                    evaluation = entry_chase_evaluation(ask, decision["alert_price"], order["limit_price"], self.config["risk"]["max_chase_fraction"])
                    decision["entry_evaluation"] = order["entry_evaluation"] = evaluation
                    # Notifications read the durable order reserved before broker review.
                    with self.store.db:
                        self.store.db.execute("UPDATE orders SET body=? WHERE id=? AND status='submitting'",
                                              (json.dumps(order), order["client_order_id"]))
                    if not entry_chase_within_cap(evaluation):
                        raise Hold(entry_chase_reason("current ask exceeds permitted chase during broker review", evaluation))
                    quantity, _ = entry_size(self.config["risk"], snapshot, order["contract"], decision["confidence"], order["limit_price"], message["source_group"])
                    if quantity < order["quantity"]:
                        raise Hold("available account risk capacity fell during broker review")
                else:
                    tick = money(quote.get("tick_size"), positive=True)
                    executable_price = (bid / tick).to_integral_value(rounding=ROUND_FLOOR) * tick
                    self.check_exit_profit(message, decision, order, executable_price)

        await current(refresh=snapshot is not None)
        if expiry_guard is None and (recovery_guard is not None or self.config.get("require_source_verification") or self.config.get("require_browser_verification")):
            options = {} if recovery_guard is None else {"recovery": True, "latest_id": recovery_guard()}
            if self.verify_current is None or not await self.verify_current(message, **options):
                raise RetryHold("current Discord message could not be verified before dispatch")
            origin = self.origin(message, decision)
            if recovery_guard is not None and origin["id"] != message["id"] and not await self.verify_current(origin, **options):
                raise RetryHold("original Discord exit could not be verified before dispatch")
        await current()

    async def resolve_expiry(self, message, decision):
        contract = decision.get("contract")
        if not isinstance(contract, dict) or contract.get("expiry") != "nearest":
            return decision
        if decision.get("action") != "OPEN":
            raise Hold("only a new entry can default its expiry")
        origin = self.origin(message, decision)
        source_day = instant(origin["timestamp"]).astimezone(EASTERN).date()
        if source_day != self.clock().astimezone(EASTERN).date():
            raise Hold("an older entry without an explicit expiry cannot roll into a new contract")
        requested = canonical_contract(dict(contract, expiry=source_day.isoformat()))
        resolved = canonical_contract(await self.broker.nearest_expiry(requested))
        if any(resolved[key] != requested[key] for key in ("symbol", "strike", "option_type")) or resolved["expiry"] < requested["expiry"]:
            raise Hold("broker expiry resolution changed the intended option")
        return decision | {"contract": resolved, "reason": (decision["reason"][:3800] +
            f" Default expiry: {resolved['expiry']}, nearest listed to the original {source_day} New York message date.")}

    async def handle(self, message, *, analyze_history=False, _observed=None):
        # ponytail: one serialized decision stream; per-account workers only if throughput requires them.
        async with self.lock:
            message = dict(message)
            channel = self.channels.get(str(message.get("channel_id")))
            if not channel or not channel_allows_author(channel, message.get("author_id", "")):
                return {"message_id": message.get("id"), "state": "untrusted", "reason": "channel or author is not allowlisted"}
            message["source_group"] = channel["source_group"]
            try:
                instant(message.get("timestamp"))
                if not re.fullmatch(r"\d{15,22}", str(message.get("id", ""))):
                    raise Hold("message ID is invalid")
                if not isinstance(message.get("revision"), str) or not message["revision"]:
                    raise Hold("message revision is missing")
                if message.get("edited_timestamp"):
                    instant(message["edited_timestamp"])
            except Hold as exc:
                return {"message_id": message.get("id"), "state": "invalid", "reason": str(exc)}
            # The live reader commits observation before enqueueing; its in-memory
            # receipt is never persisted/reused to execute a message after restart.
            observed = self.store.observe(message) if _observed is None else _observed
            if observed == "same":
                return {"message_id": message["id"], "state": "duplicate", "reason": "already observed"}
            context_only = observed == "edit" or message.get("edited_timestamp") or message.get("ingestion") != "live" or message.get("source") not in {"browser", "gateway"} or channel["role"] != "signals"
            if context_only and not analyze_history:
                return self.store.record(message, "context", "baseline, import, edit, or context channel; no execution")
            if self.interpreter is None:
                return self.store.record(message, "context", "no interpreter configured")
            decision = None
            try:
                if not context_only:
                    self.fresh(message, self.clock())
                    self.unchanged(message)
                if Path(self.config["kill_switch"]).exists():
                    raise Hold("kill switch is present")
                context = self.store.context(message, self.config.get("llm", {}).get("context_messages", 60))
                decision = await self.interpreter.interpret(message, context, self.store.positions())
                decision = await self.resolve_expiry(message, decision)
                # Interpreter validates the complete schema and evidence; controls remain deterministic below.
                if decision["action"] in {"IGNORE", "WAIT"}:
                    return self.store.record(message, decision["action"].lower(), decision["reason"], decision)
                if context_only:
                    raise Hold("historical, baseline, or revised message; analysis only")
                if decision["action"] == "UPDATE_STOP":
                    raise Hold("stop changes require broker-native order support and review; no software stop was installed")
                return await self.execute_decision(message, decision)
            except Hold as exc:
                return self.store.record(message, "held", str(exc), decision)
            except InterpretationError as exc:
                # The interpreter owns provider details and retry accounting.
                # Persist only its allowlisted diagnostic contract; no broker
                # call occurs until a validated decision reaches plan().
                timing = getattr(exc, "evaluation_timing", None)
                if isinstance(timing, dict):
                    decision = (decision or {}) | {"evaluation_timing": timing}
                return self.store.record(message, "error", safe_interpretation_reason(exc), decision)
            except Exception as exc:
                # Do not leak response bodies, tokens, or Discord message content.
                if decision is None:
                    return self.store.record(message, "error", safe_interpretation_reason(
                        InterpretationError("interpreter failed before producing a decision", code="internal_error", retryable=False),
                    ), decision)
                return self.store.record(message, "error", "internal execution failure; no retry submitted", decision)

    async def execute_decision(self, message, decision, *, recovery_guard=None, expiry_guard=None):
        def reason(label):
            if expiry_guard is not None:
                facts = decision["expiry_exit"]
                label = f"Expiry exercise protection: {label}; underlying ${facts['underlying_price']}; close {facts['close_at']}"
            evaluation = decision.get("exit_evaluation")
            if evaluation:
                return (f"{label}; sell {evaluation['sell_quantity']} of {evaluation['owned_quantity']} contracts; "
                        f"evaluated sell ${evaluation['evaluated_sell_price']}; "
                        f"estimated net profit ${evaluation['estimated_net_profit']} after fee reserves")
            return entry_chase_reason(label, decision.get("entry_evaluation"))
        origin = self.origin(message, decision) if expiry_guard is None else message
        if expiry_guard is not None:
            await expiry_guard()
        elif recovery_guard is None:
            self.fresh(origin, self.clock())
        else:
            recovery_guard()
        order = await self.plan(message, decision, recovery_guard=recovery_guard, expiry_guard=expiry_guard)
        await self.verify_dispatch(message, decision, order, recovery_guard=recovery_guard, expiry_guard=expiry_guard)
        if self.mode == "shadow":
            return self.store.record(message, "shadow_order", reason("account-sized proposal; no order submitted"),
                                     decision | {"order_proposal": order})
        self.store.reserve(message, decision, order, self.clock())
        try:
            if self.mode == "live":
                result = await self.broker.submit(order, before_submit=lambda snapshot, quote: self.verify_dispatch(message, decision, order, snapshot, quote, recovery_guard=recovery_guard, expiry_guard=expiry_guard))
            else:
                result = await self.broker.submit(order)
            self.store.apply_result(order["client_order_id"], result)
        except BrokerPreflightHold as exc:
            self.store.reject_before_submission(order["client_order_id"])
            evaluation = decision.get("entry_evaluation")
            detail = "no order submitted: " + str(exc)
            if evaluation and not entry_chase_within_cap(evaluation):
                detail = "no order submitted: current ask or rounded limit exceeds permitted chase during broker review"
            return self.store.record(message, "held", reason(detail), decision | {"order_proposal": order})
        except Exception:
            self.store.mark_unknown(order["client_order_id"])
            return self.store.record(message, "unknown", "submission or fill result is uncertain; all new orders are blocked pending reconciliation", decision)
        state = "paper_order" if self.mode == "paper" else "broker_order"
        label = "simulated order: " if self.mode == "paper" else "broker order: "
        if recovery_guard is not None:
            label = "missed exit catch-up; " + label
        return self.store.record(message, state, reason(label + result["status"]), decision | {"order_proposal": order})

    async def plan(self, message, decision, *, recovery_guard=None, expiry_guard=None):
        risk = self.config["risk"]
        self.check_execution_mode()
        if type(decision.get("ambiguous")) is not bool or decision["ambiguous"]:
            raise Hold("the intended action or contract is ambiguous")
        if type(decision.get("confidence")) not in (float, int) or not risk["min_confidence"] <= decision["confidence"] <= 1:
            raise Hold("interpreter confidence is below the configured threshold")
        action = decision["action"]
        if action not in {"OPEN", "REDUCE", "CLOSE"}:
            raise Hold("unsupported action")
        contract = canonical_contract(decision.get("contract"))
        key = contract_key(contract)
        now = self.clock()
        if expiry_guard is not None:
            if action != "CLOSE":
                raise Hold("expiry policy may only close owned contracts")
            await expiry_guard()
        elif recovery_guard is None:
            self.fresh(message, now)
        elif action in {"REDUCE", "CLOSE"}:
            recovery_guard()
        else:
            raise Hold("recovery execution is restricted to closing owned contracts")
        expiry = datetime.strptime(contract["expiry"], "%Y-%m-%d").date()
        today = now.astimezone(EASTERN).date()
        if expiry < today:
            raise Hold("contract is expired")
        if action == "OPEN" and expiry == today and not risk["allow_same_day_expiry"]:
            raise Hold("same-day expiry entries are disabled")
        if self.store.unresolved():
            raise RetryHold("an unresolved order blocks further orders")
        signal_fingerprint = hashlib.sha256(json.dumps(dict(
            action=action, contract=contract, content=message.get("content"), embeds=message.get("embeds"),
            quantity=decision.get("quantity"), fraction=decision.get("fraction"), alert_price=decision.get("alert_price")),
            sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        last = self.store.db.execute("SELECT body,created_at FROM orders WHERE source_group=? AND contract=? ORDER BY rowid DESC LIMIT 1", (message["source_group"], key)).fetchone()
        if expiry_guard is None and last and json.loads(last["body"]).get("signal_fingerprint") == signal_fingerprint and (now - instant(last["created_at"])).total_seconds() < risk["duplicate_window_seconds"]:
            raise Hold("probable duplicate message content and action for this source and contract")
        owned = [p for p in self.store.positions() if p["source_group"] == message["source_group"] and contract_key(p["contract"]) == key]
        qty_owned = owned[0]["quantity"] if owned else 0
        if action == "OPEN":
            if qty_owned:
                raise Hold("adding to an existing position is disabled")
            qty = None  # Publisher lot counts describe their account; allocate this account below.
        else:
            if not qty_owned:
                raise Hold("no position from this source is owned by the relay")
            if action == "CLOSE":
                qty = qty_owned
            elif decision.get("quantity") is not None:
                qty = decision["quantity"]
            elif decision.get("fraction") is not None and 0 < money(decision["fraction"]) <= 1:
                qty = int((Decimal(qty_owned) * money(decision["fraction"])).to_integral_value(rounding=ROUND_CEILING))
            elif decision.get("profit_only") is True:
                qty = int((Decimal(qty_owned) / 2).to_integral_value(rounding=ROUND_CEILING))
            else:
                raise Hold("a trim needs an explicit quantity or fraction")
            if qty > qty_owned:
                raise Hold("sell quantity exceeds the owned position")
        if action != "OPEN" and (type(qty) is not int or qty <= 0):
            raise Hold("quantity must be a positive whole contract")
        snapshot = await self.broker.snapshot()
        if self.mode != "paper" and snapshot.get("account_id") != self.account:
            raise Hold("broker snapshot does not match the bound account")
        restrictions = snapshot.get("restrictions", [])
        if not isinstance(restrictions, list) or restrictions:
            raise Hold("broker account restrictions prevent this action; inspect the account snapshot")
        if snapshot.get("market_open") is not True:
            raise RetryHold("broker has not confirmed an open options session")
        self.check_quote_age(snapshot, self.clock(), "account snapshot")
        quote = await self.broker.quote(contract)
        if canonical_contract(quote.get("contract")) != contract or quote.get("tradable") is not True:
            raise Hold("quote does not identify the requested tradable contract")
        if quote.get("multiplier") != 100 or quote.get("currency") != "USD" or quote.get("asset_type") != "equity_option":
            raise Hold("only standard USD equity or ETF option contracts are supported")
        self.check_quote_age(quote, self.clock())
        self.check_quote_age(snapshot, self.clock(), "account snapshot")
        bid, ask = money(quote.get("bid"), positive=True), money(quote.get("ask"), positive=True)
        tick = money(quote.get("tick_size"), positive=True)
        if tick > ask or ask < bid or (expiry_guard is None and (ask - bid) / ask > money(risk["max_spread_fraction"])):
            raise Hold("quote spread or tick size is unacceptable")
        side = "buy" if action == "OPEN" else "sell"
        price = ((ask if side == "buy" else bid) / tick).to_integral_value(rounding=ROUND_CEILING if side == "buy" else ROUND_FLOOR) * tick
        if price <= 0:
            raise Hold("limit price rounds to zero")
        sizing = None
        if action == "OPEN":
            reference = money(decision.get("alert_price"), positive=True)
            evaluation = entry_chase_evaluation(ask, reference, price, risk["max_chase_fraction"])
            decision["entry_evaluation"] = evaluation
            if not entry_chase_within_cap(evaluation):
                raise Hold(entry_chase_reason("current ask or rounded limit exceeds permitted chase from alert premium", evaluation))
            qty, sizing = entry_size(risk, snapshot, contract, decision["confidence"], price, message["source_group"])
        self.check_quote_age(quote, self.clock())
        identity = message["id"] + ":" + message["revision"]
        order = dict(contract=contract, side=side, quantity=qty, limit_price=str(price), position_effect="open" if side == "buy" else "close",
                     client_order_id=hashlib.sha256(identity.encode()).hexdigest(),
                     origin_message_id=decision.get("origin_message_id"),
                     quote_timestamp=quote["timestamp"], account_timestamp=snapshot["timestamp"],
                     signal_fingerprint=signal_fingerprint)
        if sizing:
            order["sizing"] = sizing
            order["entry_evaluation"] = decision["entry_evaluation"]
        if side == "sell":
            self.check_exit_profit(message, decision, order, price)
        if expiry_guard is not None:
            order["expiry_exit"] = decision["expiry_exit"]
        return order

    def check_exit_profit(self, message, decision, order, executable_price):
        owned = next((p for p in self.store.positions()
                      if p["source_group"] == message["source_group"]
                      and canonical_contract(p["contract"]) == order["contract"]), None)
        if owned is None or owned["quantity"] < order["quantity"]:
            raise Hold("sell quantity exceeds the remaining relay-owned position")
        if type(decision.get("profit_only", False)) is not bool:
            raise Hold("profit-only exit policy must be boolean")
        cost = money(owned["average_price"], positive=True)
        price = min(money(executable_price, positive=True), money(order["limit_price"], positive=True))
        fees = money(self.config["risk"]["fee_reserve_per_contract"]) * 2
        net = (price - cost) * 100 - fees
        decision["exit_evaluation"] = {
            "owned_quantity": owned["quantity"], "sell_quantity": order["quantity"],
            "requested_fraction": decision.get("fraction"),
            "default_half": decision["action"] == "REDUCE" and decision.get("fraction") is None and decision.get("quantity") is None,
            "profit_only": decision.get("profit_only", False),
            "evaluated_sell_price": str(price), "average_entry_price": str(cost),
            "round_trip_fee_reserve_per_contract": str(fees),
            "estimated_net_profit": str(net * order["quantity"]),
        }
        if decision.get("profit_only") is True and net <= 0:
            raise Hold("optional profit-taking requires a current sell price above entry cost plus the round-trip fee reserve")

    def check_quote_age(self, quote, now, label="option quote"):
        age = (now - instant(quote.get("timestamp"))).total_seconds()
        if age < -5 or age > self.config["risk"]["max_quote_age_seconds"]:
            raise RetryHold(label + " is stale or future-dated")
