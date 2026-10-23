"""Bounded deterministic handling for a small set of literal management posts."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import date
from decimal import Decimal, InvalidOperation
import re
import unicodedata
from typing import Any

from .signal_text import is_gain_status


_MAX_CONTENT = 6000
_MAX_ID = 128
_CONTRACT_KEYS = ("symbol", "expiry", "strike", "option_type")
_ROLE_MENTION = re.compile(r"<@&\d+>")
_WATCH_CONTRACT = re.compile(
    r"\$?([A-Z][A-Z0-9.]{0,14})\s+\$?((?:\d{1,5}(?:\.\d+)?|\.\d+))\s*(call|put|c|p)",
    re.IGNORECASE,
)


def _clean_role_mentions(text: str) -> str:
    return _ROLE_MENTION.sub(" ", text).strip()


def _role_only_content(value: object) -> bool:
    if value is None or value == "":
        return True
    if not isinstance(value, str) or len(value) > _MAX_CONTENT:
        return False
    return not _clean_role_mentions(value).strip()


def _has_media(message: Mapping[str, Any]) -> bool:
    attachments = message.get("attachments")
    if attachments not in (None, []):
        # Even a text attachment can contain a conflicting instruction that is
        # not represented in the bounded text evidence used here.
        return True
    stickers = message.get("sticker_items")
    if stickers not in (None, []):
        return True
    embeds = message.get("embeds")
    if embeds is None:
        return False
    if not isinstance(embeds, list) or len(embeds) > 1:
        return True
    for embed in embeds:
        if not isinstance(embed, Mapping):
            return True
        for key in ("image", "thumbnail", "video"):
            if embed.get(key):
                return True
    return False


def _strict_embed_text(embed: Mapping[str, Any]) -> tuple[str, str] | None:
    # These are the only source-template fields consumed here. Reject other
    # display-bearing fields rather than dropping a possible contradiction.
    allowed = {"type", "title", "description", "color", "content_scan_version"}
    if set(embed) - allowed:
        return None
    title = embed.get("title")
    description = embed.get("description")
    if not isinstance(title, str) or len(title) > 256:
        return None
    if not isinstance(description, str) or not description.strip() or len(description) > _MAX_CONTENT:
        return None
    return title.strip(), description.strip()


def _current_body(message: Mapping[str, Any]) -> tuple[str, str, str] | None:
    """Return (body, kind, exact evidence quote) with no truncation."""
    if message.get("edited_timestamp") or message.get("edited_at") or message.get("edited"):
        return None
    if _has_media(message):
        return None
    raw_content = message.get("content")
    if raw_content is None:
        content = ""
    elif isinstance(raw_content, str) and len(raw_content) <= _MAX_CONTENT:
        content = raw_content
    else:
        return None

    embeds = message.get("embeds") or []
    if embeds:
        parsed = _strict_embed_text(embeds[0])
        if parsed is None or not _role_only_content(content):
            return None
        title, description = parsed
        if title.casefold() == "comment":
            return description, "comment", description
        if title.casefold() == "on watch":
            return description, "watch", description
        return None

    body = _clean_role_mentions(content)
    if not body:
        return None
    return body, "plain", body


def _without_emoji(text: str) -> str:
    # Unicode symbol characters include the emoji used by the source template.
    return "".join(
        " " if unicodedata.category(char) in {"So", "Sk", "Cf"} or char == "\ufe0f" else char
        for char in text
    )


def _exact_management_kind(body: str) -> str | None:
    text = unicodedata.normalize("NFKC", _without_emoji(body)).strip()
    if re.fullmatch(r"took\s+half\s+here\s+[-–—]\s+s/l\s+be", text, re.IGNORECASE):
        return "half_be"
    if re.fullmatch(r"closed", text, re.IGNORECASE):
        return "closed"
    if re.fullmatch(r"stopped\s+out", text, re.IGNORECASE):
        return "stopped_out"
    return None


def _watch_status(body: str, kind: str) -> bool:
    text = unicodedata.normalize("NFKC", _without_emoji(body)).strip()
    if re.fullmatch(r"watching\s+not\s+entry", text, re.IGNORECASE):
        return kind in {"watch", "plain"}
    if kind == "watch":
        match = _WATCH_CONTRACT.fullmatch(text)
        return match is not None
    if kind == "plain":
        match = re.fullmatch(r"on\s+watch\s+(.+)", text, re.IGNORECASE)
        return bool(match and _WATCH_CONTRACT.fullmatch(match.group(1).strip()))
    return False


def _field(value: object, key: str, default: Any = None) -> Any:
    if isinstance(value, Mapping):
        return value.get(key, default)
    return getattr(value, key, default)


def _valid_contract(value: object) -> dict[str, str] | None:
    if not isinstance(value, Mapping) or set(value) != set(_CONTRACT_KEYS):
        return None
    contract = {key: value.get(key) for key in _CONTRACT_KEYS}
    symbol, expiry, strike, option_type = (contract[key] for key in _CONTRACT_KEYS)
    if not isinstance(symbol, str) or not re.fullmatch(r"[A-Z][A-Z0-9.]{0,14}", symbol):
        return None
    if not isinstance(expiry, str) or expiry == "nearest":
        return None
    try:
        date.fromisoformat(expiry)
    except ValueError:
        return None
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", expiry):
        return None
    if not isinstance(strike, str) or len(strike) > 40 or not re.fullmatch(
        r"(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?", strike,
    ):
        return None
    try:
        decimal_strike = Decimal(strike)
        if not decimal_strike.is_finite() or decimal_strike <= 0:
            return None
    except InvalidOperation:
        return None
    if option_type not in {"call", "put"}:
        return None
    if abs(decimal_strike.as_tuple().exponent) > 40:
        return None
    fixed_strike = format(decimal_strike, "f")
    if len(fixed_strike) > 40:
        return None
    contract["strike"] = fixed_strike
    return contract  # type: ignore[return-value]


def _record_id(record: object) -> str | None:
    if not isinstance(record, Mapping):
        return None
    value = record.get("id", record.get("message_id"))
    return value if isinstance(value, str) and 0 < len(value) <= _MAX_ID else None


def _evidence_quote(record: object) -> tuple[str, str] | None:
    if not isinstance(record, Mapping):
        return None
    content = record.get("content")
    if (
        isinstance(content, str)
        and content.strip()
        and len(content) <= _MAX_CONTENT
        and _clean_role_mentions(content).strip()
    ):
        return str(record.get("id", record.get("message_id", ""))), content.strip()
    embeds = record.get("embeds")
    if not isinstance(embeds, list) or len(embeds) > 10:
        return None
    for embed in embeds:
        if isinstance(embed, str) and embed.strip() and len(embed) <= _MAX_CONTENT:
            return str(record.get("id", record.get("message_id", ""))), embed.strip()
        if isinstance(embed, Mapping):
            parsed = _strict_embed_text(embed)
            if parsed:
                return str(record.get("id", record.get("message_id", ""))), "\n".join(parsed)
    return None


def _decision(
    message: Mapping[str, Any],
    *,
    action: str,
    quote: str,
    reason: str,
    contract: dict[str, str] | None = None,
    origin: str | None = None,
    fraction: float | None = None,
    stop_price: str | None = None,
    evidence: list[dict[str, str]] | None = None,
) -> dict[str, Any] | None:
    message_id = _record_id(message)
    if message_id is None or not quote.strip() or len(quote) > _MAX_CONTENT:
        return None
    citations = evidence or [{"message_id": message_id, "quote": quote}]
    return {
        "action": action,
        "origin_message_id": origin,
        "contract": contract,
        "quantity": None,
        "fraction": fraction,
        "alert_price": None,
        "stop_price": stop_price,
        "profit_only": False,
        "confidence": 1.0,
        "ambiguous": False,
        "reason": reason,
        "evidence": citations,
        "monitor": None,
    }


def _contextual_wait(message: Mapping[str, Any], quote: str, reason: str) -> dict[str, Any] | None:
    return _decision(message, action="WAIT", quote=quote, reason=reason)


def _owned_episode(
    episode: object, context: Sequence[object], message: Mapping[str, Any],
) -> tuple[dict[str, str], int, dict[str, str]] | None:
    if _field(episode, "state") != "owned":
        return None
    contract = _valid_contract(_field(episode, "contract"))
    quantity = _field(episode, "quantity")
    entry_message = _field(episode, "entry_message")
    entry_id = _record_id(entry_message)
    if contract is None or type(quantity) is not int or quantity < 1 or entry_id is None:
        return None
    matched_entry = next((record for record in context if _record_id(record) == entry_id), None)
    if matched_entry is None:
        return None
    current_group = message.get("source_group")
    entry_group = matched_entry.get("source_group") if isinstance(matched_entry, Mapping) else None
    if current_group != entry_group:
        return None
    entry_quote = _evidence_quote(matched_entry)
    if entry_quote is None or entry_quote[0] != entry_id:
        return None
    return contract, quantity, {"message_id": entry_id, "quote": entry_quote[1]}


def deterministic_management(
    message: Mapping[str, Any],
    context: Sequence[Mapping[str, Any]] = (),
    positions: Sequence[Mapping[str, Any]] = (),
    *,
    episode: object = None,
) -> dict[str, Any] | None:
    """Return a valid literal management decision, or defer with None.

    positions is deliberately unused: only the caller's trusted episode
    state can establish that a contract and quantity belong to this relay.
    """
    if not isinstance(message, Mapping):
        return None
    message_id = _record_id(message)
    if message_id is None:
        return None
    parsed = _current_body(message)
    if parsed is None:
        return None
    body, kind, quote = parsed

    if _watch_status(body, kind):
        return _contextual_wait(message, quote, "Watch status is contextual; the engine handles watch prewarming.")

    if is_gain_status(body):
        return _contextual_wait(message, quote, "Gain/status update has no explicit sale instruction.")

    management_kind = _exact_management_kind(body)
    if management_kind is None:
        return None

    state = _field(episode, "state")
    if state == "flat":
        return _contextual_wait(message, quote, "No owned position remains for this management comment.")
    safe_context = context if isinstance(context, Sequence) and not isinstance(context, (str, bytes)) else ()
    owned = _owned_episode(episode, safe_context, message)
    if owned is None:
        return None
    contract, _quantity, entry_evidence = owned
    evidence = [entry_evidence, {"message_id": message_id, "quote": quote}]
    if management_kind == "half_be":
        return _decision(
            message,
            action="REDUCE",
            quote=quote,
            reason="Exact half-exit report with breakeven stop; the engine resolves the owned quantity and average.",
            contract=contract,
            origin=message_id,
            fraction=0.5,
            stop_price="breakeven",
            evidence=evidence,
        )
    return _decision(
        message,
        action="CLOSE",
        quote=quote,
        reason="Exact full-close report for the verified owned episode.",
        contract=contract,
        origin=message_id,
        evidence=evidence,
    )


__all__ = ["deterministic_management"]
