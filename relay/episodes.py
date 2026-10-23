"""Durable source-entry attribution for bounded, contextual management rules.

Anchors survive a flat position and are never selected from inventory alone.
Ownership and fills are read from the existing order ledger on each decision.
"""
from __future__ import annotations

from datetime import datetime
import hashlib
import json

from .entry_rules import _declaration_text
from .management_rules import deterministic_management


def _time(value):
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("episode timestamps require a timezone")
    return parsed


def record_episode(db, message, *, force=False):
    identity = message.get("id")
    if not isinstance(identity, str) or not identity.isdigit():
        return
    values = (identity, message["source_group"], message["channel_id"],
              message.get("author_id", ""), message["timestamp"], message["revision"])
    existing = db.execute("SELECT * FROM signal_episodes WHERE message_id=?", (identity,)).fetchone()
    if existing and tuple(existing) == values:
        return
    if not force and not existing and not _declaration_text(message):
        return
    # Even an incomplete or edited ENTRY remains a barrier to an older holding.
    db.execute("""INSERT INTO signal_episodes VALUES (?,?,?,?,?,?)
        ON CONFLICT(message_id) DO UPDATE SET source_group=excluded.source_group,
        channel_id=excluded.channel_id,author_id=excluded.author_id,
        timestamp=excluded.timestamp,revision=excluded.revision""",
        values)


def initialize_episodes(db):
    db.executescript("""
        CREATE TABLE IF NOT EXISTS signal_episodes (
            message_id TEXT PRIMARY KEY, source_group TEXT NOT NULL,
            channel_id TEXT NOT NULL, author_id TEXT NOT NULL,
            timestamp TEXT NOT NULL, revision TEXT NOT NULL);
        CREATE INDEX IF NOT EXISTS signal_episode_source ON signal_episodes
            (source_group,channel_id,author_id,length(message_id),message_id);
    """)
    if db.execute("SELECT 1 FROM metadata WHERE key='signal_episodes_v1'").fetchone():
        return
    with db:
        for row in db.execute("SELECT body FROM messages"):
            record_episode(db, json.loads(row[0]))
        # Include model-resolved entries and held proposals from existing history.
        for row in db.execute("""SELECT DISTINCT m.body FROM messages m
            JOIN orders o ON o.message_id=m.id WHERE o.action='OPEN'"""):
            record_episode(db, json.loads(row[0]), force=True)
        for row in db.execute("""SELECT DISTINCT m.body FROM messages m
            JOIN events e ON e.message_id=m.id WHERE json_valid(e.decision)
            AND json_extract(e.decision,'$.action')='OPEN'"""):
            record_episode(db, json.loads(row[0]), force=True)
        db.execute("INSERT INTO metadata VALUES ('signal_episodes_v1','1')")


def _unclassified_after(db, entry, message):
    """An unknown intervening post needs semantic review, not a direct guess."""
    rows = db.execute("""SELECT m.body,e.decision FROM messages m
        LEFT JOIN events e ON e.message_id=m.id AND e.revision=m.revision
        WHERE m.source_group=? AND m.channel_id=? AND json_extract(m.body,'$.author_id')=?
        AND (length(m.id)>? OR (length(m.id)=? AND m.id>?))
        AND (length(m.id)<? OR (length(m.id)=? AND m.id<?))
        ORDER BY length(m.id),m.id""",
        (message["source_group"], message["channel_id"], message.get("author_id", ""),
         len(entry["id"]), len(entry["id"]), entry["id"],
         len(message["id"]), len(message["id"]), message["id"]))
    for row in rows:
        prior = json.loads(row[0])
        decision = json.loads(row[1]) if row[1] else {}
        if (decision.get("action") in {"IGNORE", "WAIT", "REDUCE", "CLOSE", "UPDATE_STOP"}
                and decision.get("ambiguous") is False):
            continue
        # Known status/watch/management templates cannot introduce a new entry.
        if deterministic_management(prior, episode={"state": "flat"}) is not None:
            continue
        return prior
    return None


def resolve_episode(db, message):
    """Resolve the latest source episode, or an explicit consistent reply target."""
    result = {"state": "missing", "reason": "no preceding entry from this channel and author"}
    identity, group = str(message.get("id", "")), message.get("source_group")
    if not identity.isdigit() or not group or not message.get("author_id"):
        return result
    if _declaration_text(message) and not message.get("reply_to"):
        return result | {"reason": "current message declares an entry rather than managing a prior episode"}
    reference = message
    reply = message.get("reply_to")
    if reply:
        row = db.execute("SELECT body FROM messages WHERE id=?", (reply,)).fetchone()
        if row is None:
            return {"state": "ambiguous", "reason": "referenced source message is unavailable"}
        reference = json.loads(row[0])
        try:
            consistent = (reference.get("source_group") == group
                          and _time(reference["timestamp"]) <= _time(message["timestamp"])
                          and int(reference["id"]) < int(identity)
                          and not reference.get("edited_timestamp"))
        except (KeyError, TypeError, ValueError):
            consistent = False
        if not consistent:
            return {"state": "ambiguous", "reason": "referenced source message is inconsistent"}
    cutoff = str(reference["id"])
    row = db.execute("""SELECT m.body FROM signal_episodes s JOIN messages m ON m.id=s.message_id
        WHERE s.source_group=? AND s.channel_id=? AND s.author_id=?
        AND (length(s.message_id)<? OR (length(s.message_id)=? AND s.message_id<=?))
        ORDER BY length(s.message_id) DESC,s.message_id DESC LIMIT 1""",
        (group, reference.get("channel_id"), reference.get("author_id", ""), len(cutoff), len(cutoff), cutoff)).fetchone()
    if row is None:
        return result
    entry = json.loads(row[0])
    result.update(entry_message=entry, entry_message_id=entry["id"],
                  source_group=group, state="unresolved", quantity=0)
    try:
        if (entry.get("edited_timestamp") or _time(entry["timestamp"]) > _time(message["timestamp"])
                or int(entry["id"]) >= int(identity)):
            return result | {"state": "ambiguous", "reason": "entry is edited or not earlier than this message"}
    except (KeyError, TypeError, ValueError):
        return result | {"state": "ambiguous", "reason": "entry chronology is invalid"}
    orders = db.execute("SELECT rowid,* FROM orders WHERE message_id=? AND source_group=? AND action='OPEN'",
                        (entry["id"], group)).fetchall()
    if not orders:
        return result | {"reason": "latest source entry has no confirmed relay order"}
    if len(orders) != 1:
        return result | {"state": "ambiguous", "reason": "entry has multiple order attempts"}
    order = orders[0]
    expected = hashlib.sha256((entry["id"] + ":" + entry["revision"]).encode()).hexdigest()
    if order["id"] != expected:
        return result | {"state": "ambiguous", "reason": "entry revision differs from the submitted order"}
    contract = json.loads(order["contract"])
    result.update(contract=contract, entry_order_id=order["id"])
    if order["status"] not in {"filled", "canceled", "rejected", "expired"}:
        return result | {"reason": "entry fill is not reconciled"}
    if order["filled_quantity"] <= 0:
        return result | {"state": "flat", "reason": "this source entry never acquired contracts"}
    latest = db.execute("""SELECT id FROM orders WHERE source_group=? AND contract=?
        AND action='OPEN' AND filled_quantity>0 ORDER BY rowid DESC LIMIT 1""", (group, order["contract"])).fetchone()
    if latest is None or latest[0] != order["id"]:
        return result | {"state": "flat", "reason": "this episode was superseded by a reopened position"}
    pending = db.execute("""SELECT 1 FROM orders WHERE source_group=? AND contract=?
        AND status NOT IN ('filled','canceled','rejected','expired')
        AND NOT (action='UPDATE_STOP' AND json_extract(body,'$.order_type')='stop_market'
                 AND status IN ('open','partially_filled')) LIMIT 1""", (group, order["contract"])).fetchone()
    if pending:
        return result | {"reason": "position has an unreconciled order"}
    position = db.execute("SELECT quantity,average_price FROM positions WHERE source_group=? AND contract=?",
                          (group, order["contract"])).fetchone()
    if position is None or position[0] <= 0:
        return result | {"state": "flat", "reason": "the current source episode is fully closed"}
    result = result | {"state": "owned", "quantity": position[0], "average_price": position[1],
                       "reason": "latest source entry matches the current reconciled relay position"}
    if not reply:
        blocking = _unclassified_after(db, entry, message)
        if blocking is not None:
            try:
                if (_time(blocking["timestamp"]) > _time(message["timestamp"])
                        or (blocking.get("edited_timestamp") and
                            _time(blocking["edited_timestamp"]) > _time(message["timestamp"]))):
                    return result | {"state": "ambiguous", "reason": "intervening source chronology is invalid"}
            except (KeyError, TypeError, ValueError):
                return result | {"state": "ambiguous", "reason": "intervening source chronology is invalid"}
            return result | {"state": "ambiguous", "context_ambiguity": True,
                             "blocking_message": blocking, "blocking_message_id": blocking["id"],
                             "reason": "an intervening source post requires contextual classification"}
    return result


def episode_diagnostic(episode):
    return {key: episode[key] for key in ("state", "reason", "entry_message_id", "entry_order_id", "quantity", "contract", "blocking_message_id")
            if key in episode}
