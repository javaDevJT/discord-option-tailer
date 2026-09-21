"""Optional, read-only relay notifications delivered to a Discord webhook."""

from __future__ import annotations

import argparse
import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import inspect
import json
from pathlib import Path
import re
import socket
import sqlite3
import threading
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from .status import AUTH_REQUIRED_STATES
from .pacing import discord_delay


UTC = timezone.utc
WEBHOOK_PATH = re.compile(r"^/api/(?:v10/)?webhooks/(\d+)/([A-Za-z0-9._-]+)$")
MESSAGE_ID = re.compile(r"^\d{15,22}$")
SYMBOL = re.compile(r"^[A-Z][A-Z0-9.]{0,9}$")
EXPIRY = re.compile(r"^\d{4}-\d{2}-\d{2}$")
SAFE_STATES = {"starting", "running", "retrying", "error", "disabled", "configuration_required", "unavailable"}
STALE_SECONDS = 120
ACTION_STATES = {"shadow_order", "paper_order", "broker_order"}
ASSIST_STATES = {"held", "unknown", "error"}
RECOVERY_STATES = {"recovery_pending", "recovery_evaluating", "recovery_review", "recovery_error"}
ORDER_STATES = {
    "submitting", "unknown", "rejected", "filled", "canceled", "expired", "submitted", "open",
    "partially_filled", "pending",
}
ACTION_NAMES = {"OPEN", "REDUCE", "CLOSE"}
MODE_LABELS = {"paper": "Paper", "shadow": "Shadow", "live": "Live"}
PROVIDERS = (("discord", "Discord"), ("codex", "Codex"), ("broker", "Robinhood"), ("jev", "JEV / TypeSafe"))


class DeliveryError(RuntimeError):
    """A webhook delivery failure with a safe retry decision."""

    def __init__(self, message="notification delivery failed", *, retryable=True, delay=None, destination_cooldown=None):
        super().__init__(message)
        self.retryable = retryable
        self.delay = delay
        self.destination_cooldown = (
            "rate limit" in str(message).lower()
            if destination_cooldown is None
            else bool(destination_cooldown)
        )


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, new):  # pragma: no cover - urllib calls this
        return None


def validate_webhook_url(value):
    """Return a normalized Discord webhook URL or raise a generic ValueError."""

    if not isinstance(value, str):
        raise ValueError("invalid Discord webhook URL")
    if not value or any(ord(char) < 0x21 or ord(char) == 0x7f for char in value):
        raise ValueError("invalid Discord webhook URL")
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except (TypeError, ValueError):
        raise ValueError("invalid Discord webhook URL") from None
    if (
        parsed.scheme != "https"
        or parsed.hostname != "discord.com"
        or parsed.username is not None
        or parsed.password is not None
        or port is not None
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("invalid Discord webhook URL")
    match = WEBHOOK_PATH.fullmatch(parsed.path)
    if not match:
        raise ValueError("invalid Discord webhook URL")
    return f"https://discord.com{parsed.path}"


def _config(path):
    path = Path(path).expanduser().resolve()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError):
        return None
    if not isinstance(raw, dict):
        return None
    settings = raw.get("notifications")
    settings = settings if isinstance(settings, dict) else {}
    enabled = settings.get("enabled") is True
    url = ""
    configured = False
    candidate = settings.get("webhook_url")
    if isinstance(candidate, str) and candidate.strip():
        try:
            url = validate_webhook_url(candidate)
        except ValueError:
            url = ""
        configured = bool(url)
    root = path.parent
    database = raw.get("database")
    runtime = raw.get("runtime_status_file")
    database = _resolve_path(root, database, root / "state" / "relay.sqlite3")
    runtime = _resolve_path(root, runtime, root / "state" / "runtime-status.json")
    browser = raw.get("browser") if isinstance(raw.get("browser"), dict) else {}
    interval = browser.get("poll_seconds", 3)
    if type(interval) not in (int, float) or not 0.2 <= interval <= 60:
        interval = 3
    return {
        "path": path,
        "root": root,
        "raw": raw,
        "enabled": enabled,
        "configured": configured,
        "url": url,
        "database": database,
        "runtime": runtime,
        "mode": raw.get("mode") if raw.get("mode") in MODE_LABELS else "unknown",
        "interval": float(interval),
    }


def _resolve_path(root, value, default):
    if isinstance(value, str) and value.strip():
        candidate = Path(value).expanduser()
        if not candidate.is_absolute():
            candidate = root / candidate
    else:
        candidate = default
    try:
        return candidate.resolve(strict=False)
    except OSError:
        return Path(default)


def _state_path(config_path):
    return Path(config_path).expanduser().resolve().parent / "state" / "notifications.sqlite3"


def _config_fingerprint(info):
    value = {"url": info["url"], "mode": info["mode"], "database": str(info["database"])}
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _webhook_fingerprint(info):
    return hashlib.sha256(info["url"].encode("utf-8")).hexdigest()


def _parse_time(value):
    if not isinstance(value, str) or len(value) > 128:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(UTC)


def _timestamp(value=None):
    if value is None:
        value = datetime.now(UTC)
    if isinstance(value, (int, float)):
        value = datetime.fromtimestamp(value, UTC)
    if not isinstance(value, datetime):
        value = datetime.now(UTC)
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _number(value, *, positive=False, signed=False):
    if isinstance(value, bool) or value is None:
        return None
    try:
        text = str(value)
        if len(text) > 64:
            return None
        from decimal import Decimal, InvalidOperation
        parsed = Decimal(text)
        if not parsed.is_finite() or (not signed and parsed < 0) or (positive and parsed <= 0):
            return None
        if parsed.adjusted() > 1000 or parsed.adjusted() < -1000:
            return None
        rendered = format(parsed, "f")
        if len(rendered) > 64:
            return None
        if "." in rendered:
            rendered = rendered.rstrip("0").rstrip(".")
        return rendered or "0"
    except (InvalidOperation, ValueError, TypeError):
        return None


def _quantity(value):
    if type(value) is not int or not 0 < value <= 1_000_000:
        return None
    return value


def _contract(value):
    if not isinstance(value, dict):
        return None
    symbol = value.get("symbol")
    expiry = value.get("expiry")
    option_type = value.get("option_type")
    strike = _number(value.get("strike"), positive=True)
    if (
        not isinstance(symbol, str)
        or not SYMBOL.fullmatch(symbol)
        or not isinstance(expiry, str)
        or not EXPIRY.fullmatch(expiry)
        or option_type not in {"call", "put"}
        or strike is None
    ):
        return None
    try:
        datetime.strptime(expiry, "%Y-%m-%d")
    except ValueError:
        return None
    return {"symbol": symbol, "expiry": expiry, "strike": strike, "option_type": option_type}


def _action(value):
    if not isinstance(value, str):
        return None
    value = value.upper()
    return value if value in ACTION_NAMES else None


def _projection(decision, *, fallback_action=None, fallback_contract=None, fallback_quantity=None, fallback_price=None):
    decision = decision if isinstance(decision, dict) else {}
    proposal = decision.get("order_proposal") if isinstance(decision.get("order_proposal"), dict) else {}
    action = _action(decision.get("action")) or _action(proposal.get("action"))
    position_effect = proposal.get("position_effect")
    if action is None and isinstance(position_effect, str):
        action = {"open": "OPEN", "reduce": "REDUCE", "close": "CLOSE"}.get(position_effect.lower())
    action = action or _action(fallback_action)
    contract = _contract(decision.get("contract")) or _contract(proposal.get("contract")) or _contract(fallback_contract)
    quantity = _quantity(decision.get("quantity")) or _quantity(proposal.get("quantity")) or _quantity(fallback_quantity)
    price = (
        _number(decision.get("limit_price"), positive=True)
        or _number(proposal.get("limit_price"), positive=True)
        or _number(decision.get("alert_price"), positive=True)
        or _number(proposal.get("price"), positive=True)
        or _number(fallback_price, positive=True)
    )
    result = {}
    if action:
        result["action"] = action
    if contract:
        result["contract"] = contract
    if quantity:
        result["quantity"] = quantity
    if price:
        result["price"] = price
    expiry = decision.get("expiry_exit", proposal.get("expiry_exit"))
    if isinstance(expiry, dict) and expiry.get("status") in {
        "watching", "closing", "blocked", "unfilled", "unavailable", "market_closed"
    }:
        result["expiry_status"] = expiry["status"]
    evaluation = decision.get("entry_evaluation", proposal.get("entry_evaluation"))
    if isinstance(evaluation, dict):
        clean = {key: value for key in (
            "ask", "reference_price", "ask_deviation_percent", "limit_price",
            "limit_deviation_percent", "max_chase_percent",
        ) if (value := _number(evaluation.get(key), signed=key.endswith("deviation_percent"))) is not None}
        if "ask" in clean and "ask_deviation_percent" in clean:
            result["entry_evaluation"] = clean
    return result


def _format_projection(projection):
    parts = []
    expiry = projection.get("expiry_status")
    if expiry:
        parts.append("Expiry exercise protection")
        if expiry in {"unfilled", "blocked", "unavailable", "market_closed"}:
            parts.append("— remaining inventory may exercise; check broker immediately —")
    if projection.get("action"):
        parts.append(projection["action"])
    contract = projection.get("contract")
    if contract:
        parts.append(
            f"{contract['symbol']} {contract['expiry']} {contract['strike']} {contract['option_type']}"
        )
    if projection.get("quantity"):
        parts.append(f"x{projection['quantity']}")
    if projection.get("price"):
        parts.append(f"at ${projection['price']}")
    evaluation = projection.get("entry_evaluation")
    if evaluation:
        deviation = evaluation["ask_deviation_percent"]
        deviation = deviation if deviation.startswith("-") else "+" + deviation
        parts.append(f"— evaluated ask ${evaluation['ask']} ({deviation}% vs alert)")
        if "reference_price" in evaluation:
            parts.append(f"alert ${evaluation['reference_price']}")
        if "max_chase_percent" in evaluation:
            parts.append(f"chase cap {evaluation['max_chase_percent']}%")
        if "limit_price" in evaluation and "limit_deviation_percent" in evaluation:
            limit_deviation = evaluation["limit_deviation_percent"]
            limit_deviation = limit_deviation if limit_deviation.startswith("-") else "+" + limit_deviation
            parts.append(f"limit ${evaluation['limit_price']} ({limit_deviation}%)")
    return " ".join(parts) or "an option action"


def _payload(content):
    return {"content": content[:2000], "allowed_mentions": {"parse": []}}


def _fingerprint(value):
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True)
class _Candidate:
    kind: str
    identity: str
    fingerprint: str
    payload: dict


def _event_candidate(row, *, mode, order_ids, order_message_ids):
    state = str(row["state"] or "")
    if state in RECOVERY_STATES or state not in ACTION_STATES | ASSIST_STATES:
        return None
    decision = {}
    if row["decision"]:
        try:
            decision = json.loads(row["decision"])
        except (TypeError, ValueError, json.JSONDecodeError):
            decision = {}
    proposal = decision.get("order_proposal") if isinstance(decision, dict) else {}
    client_order_id = proposal.get("client_order_id") if isinstance(proposal, dict) else None
    if state in {"unknown", "paper_order", "broker_order"} and (
        (isinstance(client_order_id, str) and client_order_id in order_ids)
        or str(row["message_id"]) in order_message_ids
    ):
        return None
    label = {"shadow_order": "Shadow", "paper_order": "Paper", "broker_order": "Live"}.get(
        state, MODE_LABELS.get(mode, "Relay")
    )
    projection = _projection(decision)
    if state == "shadow_order":
        content = f"{label} proposal: {_format_projection(projection)}."
    elif state == "held":
        content = f"{label} action held for review: {_format_projection(projection)}."
    elif state == "unknown":
        content = f"{label} action needs reconciliation: {_format_projection(projection)}."
    elif state == "error":
        content = f"{label} action failed and needs assistance: {_format_projection(projection)}."
    else:
        content = f"{label} action taken: {_format_projection(projection)}."
    safe = {"state": state, "mode": label, "decision": projection}
    return _Candidate("event", str(row["id"]), _fingerprint(safe), _payload(content))


def _order_candidate(row, *, mode):
    status = str(row["status"] or "")
    if status not in ORDER_STATES:
        return None
    body = {}
    try:
        body = json.loads(row["body"]) if row["body"] else {}
    except (TypeError, ValueError, json.JSONDecodeError):
        body = {}
    contract = body.get("contract") if isinstance(body, dict) else None
    if not _contract(contract) and "contract" in row.keys():
        try:
            contract = json.loads(row["contract"]) if row["contract"] else None
        except (TypeError, ValueError, json.JSONDecodeError):
            contract = None
    projection = _projection(
        body,
        fallback_action=row["action"],
        fallback_contract=contract,
        fallback_quantity=body.get("quantity") if isinstance(body, dict) else None,
        fallback_price=body.get("limit_price") if isinstance(body, dict) else None,
    )
    label = MODE_LABELS.get(mode, "Relay")
    if status == "unknown":
        prefix = f"{label} order needs reconciliation"
    elif status in {"filled", "submitted", "open", "partially_filled", "pending", "submitting"}:
        prefix = f"{label} action taken"
    else:
        prefix = f"{label} order update"
    content = f"{prefix}: {_format_projection(projection)} ({status})."
    safe = {
        "id": str(row["id"]), "status": status, "mode": label, "decision": projection,
        "filled_quantity": row["filled_quantity"] if type(row["filled_quantity"]) is int else 0,
    }
    return _Candidate("order", str(row["id"]), _fingerprint(safe), _payload(content))


def _runtime_issues(path, now=None):
    try:
        if not path.is_file() or path.stat().st_size > 1024 * 1024:
            return {"worker": "unavailable"}
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError):
        return {"worker": "unavailable"}
    if not isinstance(value, dict):
        return {"worker": "unavailable"}
    worker_state = str(value.get("state", "")).lower()
    heartbeat = _parse_time(value.get("heartbeat_at")) or _parse_time(value.get("updated_at"))
    if heartbeat is not None:
        reference = _timestamp(now) if now is not None else datetime.now(UTC)
        age = (reference - heartbeat).total_seconds()
        if age > STALE_SECONDS or age < -5:
            return {"worker": "unavailable"}
    issues = {}
    for key, _ in PROVIDERS:
        section = value.get(key)
        if not isinstance(section, dict):
            continue
        state = str(section.get("state", "")).lower()
        if (section.get("auth_required") is True
                or section.get("reauth_required") is True
                or state in AUTH_REQUIRED_STATES):
            issues[key] = "reauth"
        elif state in {"unavailable", "unknown"}:
            issues[key] = "unavailable"
        elif state in {"error", "needs_attention"}:
            issues[key] = "error"
    if worker_state in {"error", "stopped", "unavailable"} and not issues:
        issues["worker"] = "error" if worker_state == "error" else "unavailable"
    return issues


def _runtime_payload(provider, issue):
    labels = dict(PROVIDERS) | {"worker": "relay worker"}
    label = labels.get(provider, "relay worker")
    if issue == "reauth":
        content = f"Assistance needed: {label} reauthentication is required."
    elif issue == "error":
        content = f"Assistance needed: {label} connection failed; check the relay connection."
    else:
        content = f"Assistance needed: {label} connection is unavailable; check the relay connection."
    return _payload(content)


def _meta(db, key, default=""):
    row = db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return default if row is None else row[0]


def _set_meta(db, key, value):
    db.execute("INSERT OR REPLACE INTO meta(key,value) VALUES (?,?)", (key, str(value)))


def _open_state(path):
    path = Path(path)
    if path.is_symlink() or path.parent.is_symlink():
        raise OSError("notification state path must not be a symlink")
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    db = sqlite3.connect(path, timeout=1)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA journal_mode=DELETE")
    db.execute("PRAGMA synchronous=FULL")
    db.executescript(
        """
        CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS dedup (
            kind TEXT NOT NULL, identity TEXT NOT NULL, fingerprint TEXT NOT NULL,
            created_at TEXT NOT NULL, PRIMARY KEY(kind, identity, fingerprint)
        );
        CREATE TABLE IF NOT EXISTS outbox (
            id INTEGER PRIMARY KEY AUTOINCREMENT, dedup_key TEXT NOT NULL UNIQUE,
            payload TEXT NOT NULL, created_at TEXT NOT NULL,
            attempts INTEGER NOT NULL DEFAULT 0, next_attempt REAL NOT NULL DEFAULT 0,
            state TEXT NOT NULL DEFAULT 'pending', last_error TEXT NOT NULL DEFAULT '',
            sent_at TEXT
        );
        CREATE INDEX IF NOT EXISTS outbox_due ON outbox(state, next_attempt, id);
        """
    )
    try:
        path.chmod(0o600)
    except OSError:
        pass
    return db


def _open_reader(path):
    path = Path(path)
    if not path.is_file():
        return None
    connection = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=0.5)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only=ON")
    return connection


def _table_names(connection):
    return {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def _paged(connection, table, columns, *, page_size=200):
    last = 0
    while True:
        rows = connection.execute(
            f"SELECT {columns} FROM {table} WHERE id>? ORDER BY id LIMIT ?", (last, page_size)
        ).fetchall()
        if not rows:
            return
        for row in rows:
            yield row
        last = int(rows[-1]["id"])
        if len(rows) < page_size:
            return


def _paged_rowid(connection, table, columns, *, page_size=200):
    last = 0
    while True:
        rows = connection.execute(
            f"SELECT rowid AS _rowid, {columns} FROM {table} WHERE rowid>? ORDER BY rowid LIMIT ?",
            (last, page_size),
        ).fetchall()
        if not rows:
            return
        for row in rows:
            yield row
        last = int(rows[-1]["_rowid"])
        if len(rows) < page_size:
            return


def _read_ledger(path):
    if not Path(path).is_file():
        return [], [], True
    connection = None
    try:
        connection = _open_reader(path)
        if connection is None:
            return [], [], True
        tables = _table_names(connection)
        orders = []
        if "orders" in tables:
            orders = list(_paged_rowid(
                connection,
                "orders",
                "id, message_id, action, contract, body, status, filled_quantity, filled_notional",
            ))
        events = []
        if "events" in tables:
            events = list(_paged(connection, "events", "id, message_id, state, decision"))
        return events, orders, True
    except (OSError, sqlite3.Error, ValueError, TypeError):
        return [], [], False
    finally:
        if connection is not None:
            connection.close()


def notification_status(config_path):
    """Return a credential-free status projection for dashboard setup."""

    info = _config(config_path)
    if info is None:
        return {
            "enabled": False, "configured": False, "state": "unavailable",
            "detail": "Notifications configuration is unavailable.",
        }
    result = {"enabled": info["enabled"], "configured": info["configured"]}
    if not info["enabled"]:
        result.update(state="disabled", detail="Notifications are disabled.")
    elif not info["configured"]:
        result.update(
            state="configuration_required",
            detail="Add a valid Discord webhook URL before enabling notifications.",
        )
    else:
        result.update(state="starting", detail="Notification worker has not reported a heartbeat.")
        path = _state_path(info["path"])
        db = None
        try:
            if path.is_file() and not path.is_symlink() and not path.parent.is_symlink():
                db = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=0.2)
                db.row_factory = sqlite3.Row
                db.execute("PRAGMA query_only=ON")
                expected = _config_fingerprint(info)
                if _meta(db, "config_hash") == expected and _meta(db, "active") == "1":
                    state = _meta(db, "state", "starting")
                    result["state"] = state if state in SAFE_STATES else "unavailable"
                    heartbeat = _parse_time(_meta(db, "heartbeat_at", ""))
                    if heartbeat is None:
                        result["state"] = "starting"
                    else:
                        age = (datetime.now(UTC) - heartbeat).total_seconds()
                        if age > STALE_SECONDS or age < -5:
                            result["state"] = "unavailable"
                    details = {
                        "starting": "Notification worker has not reported a heartbeat.",
                        "running": "Notification worker is running.",
                        "retrying": "Notification delivery is retrying.",
                        "error": "Notification worker needs attention.",
                        "unavailable": "Notification worker is unavailable.",
                    }
                    result["detail"] = details.get(result["state"], "Notification worker needs attention.")
                last_sent = _meta(db, "last_sent_at", "")
                if _parse_time(last_sent) is not None:
                    result["last_sent_at"] = last_sent
        except (OSError, sqlite3.Error):
            result.update(state="unavailable", detail="Notification worker state is unavailable.")
        finally:
            if db is not None:
                db.close()
    return result


class NotificationWorker:
    """Poll relay state and deliver sanitized, durable output notifications."""

    def __init__(self, config_path, *, sender=None, send=None, clock=None, poll_seconds=None, http_timeout=5):
        if sender is not None and send is not None:
            raise ValueError("provide one notification sender")
        self.config_path = Path(config_path).expanduser().resolve()
        self._sender = sender if sender is not None else send
        self.clock = clock or (lambda: datetime.now(UTC))
        self.poll_seconds = poll_seconds
        try:
            self.http_timeout = max(1.0, min(30.0, float(http_timeout)))
        except (TypeError, ValueError):
            self.http_timeout = 5.0
        self._active_url = ""
        self.db_path = _state_path(self.config_path)

    def _now(self):
        try:
            return _timestamp(self.clock())
        except Exception:
            return _timestamp()

    def send(self, payload):
        """Send one already-sanitized payload without following redirects."""

        if not self._active_url:
            raise DeliveryError("notification destination unavailable", retryable=False, delay=3600)
        body = json.dumps(payload, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
        request = Request(
            f"{self._active_url}?wait=true",
            data=body,
            headers={"Content-Type": "application/json", "User-Agent": "relay-notifications/1"},
            method="POST",
        )
        opener = build_opener(_NoRedirect())
        try:
            with opener.open(request, timeout=self.http_timeout) as response:
                status = int(response.getcode())
                if 200 <= status < 300:
                    try:
                        raw = response.read(1024 * 1024 + 1)
                        if len(raw) > 1024 * 1024:
                            raise ValueError("notification acknowledgement is too large")
                        acknowledged = json.loads(raw.decode("utf-8"))
                    except (OSError, UnicodeError, ValueError, json.JSONDecodeError):
                        raise DeliveryError(
                            "notification acknowledgement unavailable",
                            retryable=False,
                            delay=3600,
                        ) from None
                    if (
                        not isinstance(acknowledged, dict)
                        or not isinstance(acknowledged.get("id"), str)
                        or not MESSAGE_ID.fullmatch(acknowledged["id"])
                    ):
                        raise DeliveryError(
                            "notification acknowledgement invalid",
                            retryable=False,
                            delay=3600,
                        )
                    return None
                raise self._http_failure(status, getattr(response, "headers", None), None)
        except HTTPError as exc:
            raise self._http_failure(exc.code, exc.headers, exc) from None
        except (TimeoutError, socket.timeout, URLError, OSError):
            raise DeliveryError("notification connection failed", delay=None) from None

    @staticmethod
    def _http_failure(status, headers=None, error=None):
        if status == 429:
            retry_after = None
            if headers is not None:
                try:
                    retry_after = float(headers.get("Retry-After"))
                except (TypeError, ValueError):
                    retry_after = None
            if retry_after is None and error is not None:
                try:
                    data = error.read(4096)
                    value = json.loads(data.decode("utf-8", "replace"))
                    if isinstance(value, dict):
                        retry_after = float(value.get("retry_after"))
                except (OSError, TypeError, ValueError, json.JSONDecodeError):
                    retry_after = None
            if retry_after is None:
                retry_after = 5.0
            return DeliveryError(
                "notification rate limited",
                delay=max(1.0, min(retry_after, 3600.0)),
                destination_cooldown=True,
            )
        if status in {401, 404}:
            return DeliveryError("notification webhook rejected", retryable=False, delay=3600)
        if 500 <= status < 600:
            return DeliveryError("notification service unavailable")
        if 300 <= status < 400:
            return DeliveryError("notification redirect rejected", retryable=False, delay=3600)
        return DeliveryError("notification request rejected", retryable=False, delay=3600)

    @staticmethod
    def _invoke(sender, payload):
        result = sender(payload)
        if not inspect.isawaitable(result):
            return result
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.run(result)
        completed = []
        failure = []
        def run_awaitable():
            try:
                completed.append(asyncio.run(result))
            except BaseException as exc:  # propagate in caller thread
                failure.append(exc)
        thread = threading.Thread(target=run_awaitable, daemon=True)
        thread.start()
        thread.join()
        if failure:
            raise failure[0]
        return completed[0] if completed else None

    def _deliver(self, payload):
        sender = self._sender or self.send
        return self._invoke(sender, payload)

    def _baseline_or_scan(self, db, info, *, baseline, events, orders, now):
        order_ids = {str(row["id"]) for row in orders}
        order_message_ids = {str(row["message_id"]) for row in orders}
        candidates = []
        for row in events:
            candidate = _event_candidate(row, mode=info["mode"], order_ids=order_ids, order_message_ids=order_message_ids)
            if candidate is not None:
                candidates.append(candidate)
        for row in orders:
            candidate = _order_candidate(row, mode=info["mode"])
            if candidate is not None:
                candidates.append(candidate)
        for candidate in candidates:
            existing = db.execute(
                "SELECT 1 FROM dedup WHERE kind=? AND identity=? AND fingerprint=?",
                (candidate.kind, candidate.identity, candidate.fingerprint),
            ).fetchone()
            if existing:
                continue
            db.execute(
                "INSERT INTO dedup(kind,identity,fingerprint,created_at) VALUES (?,?,?,?)",
                (candidate.kind, candidate.identity, candidate.fingerprint, now.isoformat()),
            )
            if not baseline:
                key = f"{candidate.kind}:{candidate.identity}:{candidate.fingerprint}"
                db.execute(
                    "INSERT OR IGNORE INTO outbox(dedup_key,payload,created_at) VALUES (?,?,?)",
                    (key, json.dumps(candidate.payload, separators=(",", ":")), now.isoformat()),
                )
        if events:
            _set_meta(db, "event_cursor", max(int(row["id"]) for row in events))
        if baseline:
            _set_meta(db, "initialized", "1")
        return len(candidates) if not baseline else 0

    def _sync_runtime(self, db, info, now):
        issues = _runtime_issues(info["runtime"], now=now)
        for provider, issue in issues.items():
            key = f"runtime:{provider}"
            previous = _meta(db, key, "")
            if previous == issue:
                continue
            generation_key = f"runtime_generation:{provider}"
            try:
                generation = int(_meta(db, generation_key, "0")) + 1
            except (TypeError, ValueError):
                generation = 1
            _set_meta(db, key, issue)
            _set_meta(db, generation_key, generation)
            fingerprint = _fingerprint({"provider": provider, "issue": issue, "generation": generation})
            dedup_key = f"runtime:{provider}:{generation}"
            db.execute(
                "INSERT OR IGNORE INTO dedup(kind,identity,fingerprint,created_at) VALUES (?,?,?,?)",
                ("runtime", provider, fingerprint, now.isoformat()),
            )
            db.execute(
                "INSERT OR IGNORE INTO outbox(dedup_key,payload,created_at) VALUES (?,?,?)",
                (dedup_key, json.dumps(_runtime_payload(provider, issue), separators=(",", ":")), now.isoformat()),
            )
        known = {key for key, _ in PROVIDERS} | {"worker"}
        for provider in known - set(issues):
            key = f"runtime:{provider}"
            if _meta(db, key, ""):
                _set_meta(db, key, "")

    def _flush(self, db, now):
        sent = 0
        failure = None
        try:
            cooldown_until = float(_meta(db, "cooldown_until", "0"))
        except (TypeError, ValueError):
            cooldown_until = 0.0
        if cooldown_until > now.timestamp():
            pending = db.execute("SELECT COUNT(*) FROM outbox WHERE state='pending'").fetchone()[0]
            blocked = db.execute("SELECT COUNT(*) FROM outbox WHERE state='blocked'").fetchone()[0]
            return (
                sent,
                pending,
                blocked,
                DeliveryError(
                    "notification rate limited",
                    delay=cooldown_until - now.timestamp(),
                    destination_cooldown=True,
                ),
            )
        while True:
            row = db.execute(
                "SELECT * FROM outbox WHERE state IN ('pending','blocked') AND next_attempt<=? ORDER BY id LIMIT 1",
                (now.timestamp(),),
            ).fetchone()
            if row is None:
                break
            try:
                payload = json.loads(row["payload"])
                self._deliver(payload)
            except HTTPError as exc:
                failure = self._http_failure(exc.code, exc.headers, exc)
            except DeliveryError as exc:
                failure = exc
            except (OSError, RuntimeError, TypeError, ValueError):
                failure = DeliveryError("notification delivery failed")
            if failure is not None:
                attempts = int(row["attempts"]) + 1
                if failure.retryable:
                    delay = failure.delay if failure.delay is not None else discord_delay(min(5.0 * (2 ** min(attempts - 1, 6)), 300.0))
                    state = "pending"
                else:
                    delay = failure.delay if failure.delay is not None else 3600.0
                    state = "blocked"
                db.execute(
                    "UPDATE outbox SET attempts=?,next_attempt=?,state=?,last_error=? WHERE id=?",
                    (attempts, now.timestamp() + max(1.0, min(float(delay), 86400.0)), state, "delivery_failed", row["id"]),
                )
                if failure.destination_cooldown:
                    try:
                        current_cooldown = float(_meta(db, "cooldown_until", "0"))
                    except (TypeError, ValueError):
                        current_cooldown = 0.0
                    _set_meta(db, "cooldown_until", max(current_cooldown, now.timestamp() + float(delay)))
                db.commit()
                break
            sent_at = now.isoformat()
            db.execute("UPDATE outbox SET state='sent',sent_at=? WHERE id=?", (sent_at, row["id"]))
            _set_meta(db, "last_sent_at", sent_at)
            db.commit()
            sent += 1
        pending = db.execute("SELECT COUNT(*) FROM outbox WHERE state='pending'").fetchone()[0]
        blocked = db.execute("SELECT COUNT(*) FROM outbox WHERE state='blocked'").fetchone()[0]
        return sent, pending, blocked, failure

    def poll(self):
        """Run one synchronous poll; useful for deterministic tests and supervisors."""

        info = _config(self.config_path)
        if info is None:
            return {"state": "unavailable", "queued": 0, "sent": 0, "pending": 0}
        self._active_url = info["url"]
        db = None
        try:
            now = self._now()
            if not info["enabled"] or not info["configured"]:
                if self.db_path.is_file():
                    db = _open_state(self.db_path)
                    with db:
                        _set_meta(db, "active", "0")
                        _set_meta(db, "state", "disabled" if not info["enabled"] else "configuration_required")
                        _set_meta(db, "detail", "Notifications are disabled." if not info["enabled"] else "Add a valid Discord webhook URL before enabling notifications.")
                state = "disabled" if not info["enabled"] else "configuration_required"
                return {"state": state, "queued": 0, "sent": 0, "pending": 0}
            db = _open_state(self.db_path)
            config_hash = _config_fingerprint(info)
            old_config_hash = _meta(db, "config_hash")
            old_webhook_hash = _meta(db, "webhook_hash")
            webhook_hash = _webhook_fingerprint(info)
            context_changed = old_config_hash != config_hash
            webhook_changed = old_webhook_hash != webhook_hash
            baseline = context_changed or _meta(db, "active") != "1" or _meta(db, "initialized") != "1"
            if baseline:
                with db:
                    db.execute("DELETE FROM dedup")
                    if webhook_changed:
                        db.execute("DELETE FROM outbox")
                        db.execute("DELETE FROM meta WHERE key LIKE 'runtime:%' OR key LIKE 'runtime_generation:%'")
                        db.execute("DELETE FROM meta WHERE key IN ('last_sent_at', 'cooldown_until')")
                    _set_meta(db, "config_hash", config_hash)
                    _set_meta(db, "webhook_hash", webhook_hash)
                    _set_meta(db, "active", "1")
                    _set_meta(db, "initialized", "0")
                    _set_meta(db, "state", "starting")
            events, orders, ledger_ok = _read_ledger(info["database"])
            if not ledger_ok:
                with db:
                    self._sync_runtime(db, info, now)
                    _set_meta(db, "state", "retrying")
                    _set_meta(db, "detail", "Trading ledger is temporarily unavailable.")
                sent, pending, blocked, failure = self._flush(db, now)
                return {"state": "retrying", "queued": 0, "sent": sent, "pending": pending + blocked, "error": bool(failure)}
            with db:
                _set_meta(db, "heartbeat_at", now.isoformat())
                queued = self._baseline_or_scan(db, info, baseline=baseline, events=events, orders=orders, now=now)
                self._sync_runtime(db, info, now)
                _set_meta(db, "state", "running")
                _set_meta(db, "detail", "Notification worker is running.")
            sent, pending, blocked, failure = self._flush(db, now)
            state = "error" if (failure and not failure.retryable) or blocked else "retrying" if failure else "running"
            with db:
                _set_meta(db, "state", state)
                _set_meta(db, "detail", {
                    "running": "Notification worker is running.",
                    "retrying": "Notification delivery is retrying.",
                    "error": "Notification worker needs attention.",
                }[state])
            return {"state": state, "queued": queued, "sent": sent, "pending": pending + blocked, "error": bool(failure)}
        except (OSError, sqlite3.Error, ValueError, TypeError):
            if db is not None:
                try:
                    with db:
                        _set_meta(db, "state", "error")
                        _set_meta(db, "detail", "Notification worker needs attention.")
                except sqlite3.Error:
                    pass
            return {"state": "error", "queued": 0, "sent": 0, "pending": 0}
        finally:
            if db is not None:
                db.close()

    async def step(self):
        return await asyncio.to_thread(self.poll)

    async def run(self, *, interval=None, stop_event=None):
        delay = interval if interval is not None else self.poll_seconds
        while True:
            await self.step()
            if stop_event is not None and stop_event.is_set():
                return
            wait = delay
            if wait is None:
                info = _config(self.config_path)
                wait = info["interval"] if info is not None else 3.0
            wait = discord_delay(max(0.2, min(float(wait), 60.0)))
            if stop_event is None:
                await asyncio.sleep(wait)
            else:
                try:
                    await asyncio.wait_for(stop_event.wait(), timeout=wait)
                except asyncio.TimeoutError:
                    pass


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default="config.local.json")
    args = parser.parse_args()
    try:
        asyncio.run(NotificationWorker(args.config).run())
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass


if __name__ == "__main__":
    main()
