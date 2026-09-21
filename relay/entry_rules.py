"""Bounded, text-only rules for literal options entry alerts."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal, InvalidOperation
import re
from typing import Any, Mapping
from zoneinfo import ZoneInfo

from .interpreter import InterpretationError, validate_decision


EASTERN = ZoneInfo("America/New_York")
_SYMBOL = r"\$?[A-Z]{1,6}(?:\.[A-Z])?"
_STRIKE = r"\$?(?:\d{1,5}(?:\.\d+)?|\.\d+)"
_SIDE = {"call": "call", "c": "call", "put": "put", "p": "put"}
_STOP_WORDS = {
    "buy", "sell", "open", "close", "reduce", "trim", "call", "put",
    "entry", "price", "premium", "contract", "comments", "watching", "at",
    "exp", "expiry", "expires", "expiring",
}
_IMAGE_EXTENSIONS = {
    ".avif", ".bmp", ".gif", ".heic", ".jpeg", ".jpg", ".png", ".svg",
    ".tif", ".tiff", ".webp",
}
_TEXT_EXTENSIONS = {".txt", ".md", ".markdown", ".csv", ".tsv", ".json", ".xml"}
_TEXT_TYPES = {
    "text/plain", "text/markdown", "text/csv", "text/tab-separated-values",
    "application/json", "application/xml",
}


def _text(value: object, limit: int = 6000) -> str:
    return value[:limit] if isinstance(value, str) else ""

def _strict_text(value: object, limit: int) -> str | None:
    """Return bounded text without silently discarding a contradiction."""
    if not isinstance(value, str) or len(value) > limit:
        return None
    return value


def _embed_text(embed: object) -> str:
    if isinstance(embed, str):
        return _text(embed)
    if not isinstance(embed, Mapping):
        return ""
    parts = [_text(embed.get(key), 2048) for key in ("title", "description", "text")]
    fields = embed.get("fields") or []
    if isinstance(fields, list):
        for field in fields[:20]:
            if isinstance(field, Mapping):
                parts.extend((_text(field.get("name"), 256), _text(field.get("value"), 1024)))
    footer = embed.get("footer")
    if isinstance(footer, Mapping):
        parts.append(_text(footer.get("text"), 2048))
    return "\n".join(part for part in parts if part)[:6000]


def visible_parts(message: Mapping[str, Any]) -> list[str]:
    """Return displayed message/embed prose, excluding attachment metadata and URLs."""
    parts: list[str] = []
    content = _text(message.get("content"))
    if content:
        parts.append(content)
    embeds = message.get("embeds") or []
    if isinstance(embeds, list):
        parts.extend(text for embed in embeds[:10] if (text := _embed_text(embed)))
    return parts


def visible_text(message: Mapping[str, Any]) -> str:
    return "\n".join(visible_parts(message))[:12_000]


def has_image_evidence(message: Mapping[str, Any]) -> bool:
    attachments = message.get("attachments") or []
    if isinstance(attachments, list):
        for attachment in attachments:
            if not isinstance(attachment, Mapping):
                return True
            content_type = str(attachment.get("content_type") or "").strip().lower()
            filename = str(attachment.get("filename") or attachment.get("url") or "").lower()
            match = re.search(r"(\.[a-z0-9]{1,12})(?:$|[?#])", filename)
            extension = match.group(1) if match else ""
            if content_type.startswith("image/") or extension in _IMAGE_EXTENSIONS:
                return True
            if content_type.startswith("text/") or content_type in _TEXT_TYPES:
                continue
            if not content_type and extension in _TEXT_EXTENSIONS:
                continue
            if content_type == "application/octet-stream" and extension in _TEXT_EXTENSIONS:
                continue
            # Unknown attachments are not safe to treat as decorative text.
            return True
    embeds = message.get("embeds") or []
    if isinstance(embeds, list):
        for embed in embeds[:10]:
            if not isinstance(embed, Mapping):
                continue
            for key in ("image", "thumbnail", "video"):
                value = embed.get(key)
                if isinstance(value, Mapping) and isinstance(value.get("url"), str) and value["url"]:
                    return True
    return False


def _source_date(message: Mapping[str, Any]) -> str | None:
    value = message.get("timestamp") or message.get("created_at")
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(EASTERN).date().isoformat()


def _expiry_values(text: str, message: Mapping[str, Any]) -> tuple[list[str], bool]:
    values: list[str] = []
    invalid = False

    for value in re.findall(r"\b(20\d{2}-\d{2}-\d{2})\b", text):
        try:
            datetime.strptime(value, "%Y-%m-%d")
        except ValueError:
            invalid = True
        else:
            values.append(value)

    marked = re.findall(
        r"\b(?:exp(?:iry)?|expires?|date)\s*[:@-]?\s*"
        r"(\d{1,2})[/-](\d{1,2})(?:[/-](\d{2,4}))?\b",
        text,
        re.I,
    )
    generic = re.findall(r"\b(\d{1,2})[/-](\d{1,2})(?:[/-](\d{2,4}))?\b", text)
    if not marked and len(generic) > 1:
        invalid = True
    date_parts = marked or (generic if len(generic) == 1 else [])
    source = _source_date(message)
    for month, day, year in date_parts:
        if year:
            year = year if len(year) == 4 else f"20{year}"
        elif source:
            year = source[:4]
        else:
            invalid = True
            continue
        try:
            values.append(datetime.strptime(f"{year}-{int(month):02d}-{int(day):02d}", "%Y-%m-%d").date().isoformat())
        except ValueError:
            invalid = True

    if re.search(r"\b(?:0\s*dte|dte|today|same[- ]day)\b", text, re.I):
        values.append("nearest")
    unique = list(dict.fromkeys(values))
    return unique, invalid


def _normalize_expiry(text: str, message: Mapping[str, Any]) -> tuple[str | None, bool]:
    values, invalid = _expiry_values(text, message)
    if invalid or len(set(values)) > 1:
        return None, True
    return (values[0] if values else "nearest"), False


def option_matches(text: str) -> list[dict[str, str]]:
    patterns = (
        re.compile(rf"\b(?P<symbol>{_SYMBOL})\s+(?P<strike>{_STRIKE})\s*(?P<option>call|put|c|p)\b", re.I),
        re.compile(rf"\b(?P<symbol>{_SYMBOL})\s+(?P<strike>{_STRIKE})(?P<option>[CP])\b", re.I),
        re.compile(rf"\b(?P<option>call|put)\s+(?P<symbol>{_SYMBOL})\s+(?P<strike>{_STRIKE})\b", re.I),
    )
    found: list[dict[str, str]] = []
    for pattern in patterns:
        for match in pattern.finditer(text):
            symbol = match.group("symbol").lstrip("$").upper()
            if symbol.lower() in _STOP_WORDS:
                continue
            strike = match.group("strike").lstrip("$")
            try:
                if Decimal(strike) <= 0:
                    continue
            except InvalidOperation:
                continue
            candidate = {
                "symbol": symbol,
                "strike": strike,
                "option_type": _SIDE[match.group("option").lower()],
            }
            if candidate not in found:
                found.append(candidate)
    return found


def _action(text: str) -> str | None:
    lower = text.lower()
    if re.search(r"^\s*(?:open|buy(?:\s+to\s+open)?|entry)\b", lower, re.I | re.M):
        return "OPEN"
    if re.search(r"\b(?:watch|watchlist|watching\s+this\s+too|wait|hold|no\s+trade|not\s+a\s+signal)\b", lower):
        return "WAIT"
    if re.search(r"\b(?:all\s+out|sell\s+(?:the\s+)?rest|close\s+(?:all|remaining))\b", lower):
        return "CLOSE"
    if (
        re.search(r"\b(?:trim|reduce|half\s+out|take\s+profits?|scale\s+out)\b", lower)
        or (_fraction(text) is not None and re.search(r"\b(?:sell|sold|took|take|close|exit)\b", lower))
        or (_quantity(text) is not None and re.search(r"\b(?:sell|sold)\b", lower))
    ):
        return "REDUCE"
    if re.search(r"\b(?:close|closed|sell|sold|exit)\b", lower):
        return "CLOSE"
    if re.search(r"\b(?:buy|bought|open|enter|long|entry)\b", lower):
        return "OPEN"
    return None


def _unsupported_effect(text: str) -> bool:
    return bool(re.search(
        r"\b(?:sell[-\s]+to[-\s]+open|buy[-\s]+to[-\s]+close|write|writing|written|"
        r"short|shorting|spread|vertical|iron\s+condor|straddle|strangle)\b",
        text,
        re.I,
    ))


def _contextual_text(text: str) -> bool:
    return bool(re.search(
        r"\b(?:if|unless|when|would|could|maybe|might|planning|watching|hypothetical|"
        r"example|test(?:ing)?|sample|demo|yesterday|recap|earlier|hindsight|"
        r"last\s+(?:week|session|trade))\b|^\s*>",
        text,
        re.I | re.M,
    ) or re.search(
        r"\b(?:don't|do\s+not|never|not|no\s+longer|shouldn't)\s+"
        r"(?:buy|sell|enter|exit|open|close|trim|long)\w*\b",
        text,
        re.I,
    ))


def _fraction(text: str) -> float | None:
    match = re.search(r"\b(\d{1,3}(?:\.\d+)?)\s*%", text)
    if match:
        value = float(match.group(1)) / 100
        return value if 0 < value <= 1 else None
    return 0.5 if re.search(r"\bhalf\b", text, re.I) else None


def _quantity(text: str) -> int | None:
    match = re.search(r"\b(?:x|qty(?:uantity)?\s*[:=]?|sell\s+|buy\s+)(\d{1,3})\s*(?:contracts?|x)?\b", text, re.I)
    if match and not re.search(rf"{re.escape(match.group(1))}\s*%", text[match.start():match.end() + 1]) and int(match.group(1)) > 0:
        return int(match.group(1))
    match = re.search(r"\b(\d{1,3})\s+contracts?\b", text, re.I)
    return int(match.group(1)) if match and int(match.group(1)) > 0 else None


def _prices(text: str) -> list[str]:
    values: list[str] = []
    for match in re.finditer(
        r"(?<!\w)(?:@|at|entry|premium|price)\s*(?:at|@|=|:)?\s*\$?((?:\d{1,5}(?:\.\d+)?|\.\d+))\b",
        text,
        re.I,
    ):
        value = match.group(1)
        try:
            if Decimal(value) <= 0:
                continue
        except InvalidOperation:
            continue
        if value not in values:
            values.append(value)
    return values


def _image_dependency(text: str) -> bool:
    return bool(re.search(
        r"(?:see|check|from|in|per|pictured|shown)\s+(?:the\s+)?"
        r"(?:image|picture|screenshot|chart)|(?:image|picture|screenshot|chart).*(?:expiry|expiration|contract|strike|price)",
        text,
        re.I | re.S,
    ))


def _declaration_text(message: Mapping[str, Any]) -> str | None:
    embeds = message.get("embeds") or []
    if isinstance(embeds, list) and len(embeds) == 1 and isinstance(embeds[0], Mapping):
        if _text(embeds[0].get("title"), 256).strip().upper() == "ENTRY":
            return _embed_text(embeds[0])
    content = message.get("content")
    if not isinstance(content, str):
        return None
    for line in content.splitlines() or [content]:
        if re.search(r"^\s*(?:OPEN|BUY(?:\s+TO\s+OPEN)?|ENTRY)\b", line, re.I):
            return line.strip()
    return None


def _entry_candidate(message: Mapping[str, Any], *, require_no_image: bool) -> dict[str, Any] | None:
    if not isinstance(message, Mapping) or not isinstance(message.get("id"), str) or not message["id"]:
        return None
    if message.get("edited_timestamp") or message.get("edited"):
        return None
    raw_embeds = message.get("embeds")
    if raw_embeds is not None:
        if not isinstance(raw_embeds, list) or any(not isinstance(embed, Mapping) for embed in raw_embeds):
            return None
        if (
            len(raw_embeds) == 1
            and _text(raw_embeds[0].get("title"), 256).strip().upper() == "ENTRY"
            and _structured_entry_candidate(message) is None
        ):
            return None
    raw_attachments = message.get("attachments")
    if raw_attachments is not None:
        if not isinstance(raw_attachments, list) or any(not isinstance(attachment, Mapping) for attachment in raw_attachments):
            return None
    text = visible_text(message).strip()
    declaration = _declaration_text(message)
    if not text or not declaration or _unsupported_effect(declaration):
        return None
    if re.search(r"\b(?:watchlist|watching\s+this\s+too|watching)\b", text, re.I):
        return None
    action = _action(text)
    if action != "OPEN":
        return None
    matches = option_matches(declaration)
    if len(matches) != 1:
        return None
    expiry, expiry_conflict = _normalize_expiry(declaration, message)
    if expiry_conflict or expiry is None:
        return None
    prices = _prices(declaration)
    if len(prices) != 1:
        return None
    image = has_image_evidence(message)
    if require_no_image and image and expiry == "nearest":
        return None
    if image and (expiry == "nearest" or _image_dependency(text)):
        return None
    if re.search(r"\bstop\b", declaration, re.I):
        return None
    contract = dict(matches[0], expiry=expiry)
    part = next((part for part in visible_parts(message) if declaration.strip() in part), declaration)
    return {
        "action": "OPEN",
        "contract": contract,
        "quantity": None,
        "fraction": None,
        "alert_price": prices[0],
        "stop_price": None,
        "profit_only": False,
        "origin_message_id": message["id"],
        "evidence": [{"message_id": message["id"], "quote": part}],
        "_image_evidence": image,
    }


def _role_only_content(value: object) -> bool:
    if not isinstance(value, str):
        return value in (None, "")
    remaining = re.sub(r"<@&[^>\s]+>", "", value)
    return not remaining.strip()


def _structured_entry_candidate(message: Mapping[str, Any]) -> dict[str, Any] | None:
    embeds = message.get("embeds") or []
    if not isinstance(embeds, list) or len(embeds) != 1 or not _role_only_content(message.get("content")):
        return None
    embed = embeds[0]
    if not isinstance(embed, Mapping):
        return None
    title = _strict_text(embed.get("title"), 256)
    if title is None or title.strip().upper() != "ENTRY":
        return None
    embed_text = embed.get("text")
    if embed_text is not None and (not isinstance(embed_text, str) or embed_text.strip()):
        return None
    values: dict[str, list[str]] = {"contract": [], "price": [], "comments": []}
    description = embed.get("description")
    if description is not None:
        description = _strict_text(description, 6000)
        if description is None:
            return None
    if description:
        if not isinstance(description, str):
            return None
        for raw_line in description.splitlines():
            line = re.sub(r"^[^A-Za-z$]+", "", raw_line).strip()
            if not line:
                continue
            match = re.fullmatch(r"(contract|price|comments)\s*:\s*(.*?)\s*", line, re.I)
            if not match:
                return None
            values[match.group(1).lower()].append(match.group(2))
    raw_fields = embed.get("fields")
    if raw_fields is None:
        fields = []
    elif not isinstance(raw_fields, list):
        return None
    else:
        fields = raw_fields
    for field in fields:
        if not isinstance(field, Mapping):
            return None
        name = _strict_text(field.get("name"), 256)
        if name is None:
            return None
        name = name.strip().lower()
        if name not in values:
            return None
        value = _strict_text(field.get("value"), 1024)
        if value is None:
            return None
        values[name].append(value.strip())
    footer = embed.get("footer")
    if footer is not None:
        if not isinstance(footer, Mapping):
            return None
        footer_text = _strict_text(footer.get("text"), 256)
        if footer.get("text") is not None and footer_text is None:
            return None
        footer_text = (footer_text or "").strip()
        if footer_text and footer_text.lower() != "@zendotrades":
            return None
    if len(values["contract"]) != 1 or len(values["price"]) != 1 or len(values["comments"]) > 1:
        return None
    if values["comments"] and values["comments"][0].strip().lower() != "none":
        return None
    contract_text = values["contract"][0].strip()
    matches = option_matches(contract_text)
    if len(matches) != 1 or not re.fullmatch(
        r"\$?[A-Z]{1,6}(?:\.[A-Z])?\s+\$?(?:\d{1,5}(?:\.\d+)?|\.\d+)\s*(?:call|put|c|p)",
        contract_text,
        re.I,
    ):
        return None
    price_match = re.fullmatch(r"\$?((?:\d{1,5}(?:\.\d+)?|\.\d+))", values["price"][0].strip())
    if not price_match:
        return None
    try:
        if Decimal(price_match.group(1)) <= 0 or Decimal(matches[0]["strike"]) <= 0:
            return None
    except InvalidOperation:
        return None
    image = has_image_evidence(message)
    if image:
        return None
    quote = _embed_text(embed)
    return {
        "action": "OPEN",
        "contract": dict(matches[0], expiry="nearest"),
        "quantity": None,
        "fraction": None,
        "alert_price": price_match.group(1),
        "stop_price": None,
        "profit_only": False,
        "origin_message_id": message["id"],
        "evidence": [{"message_id": message["id"], "quote": quote}],
    }


def _plain_entry_candidate(message: Mapping[str, Any]) -> dict[str, Any] | None:
    content = message.get("content")
    if not isinstance(content, str) or not content.strip():
        return None
    raw_embeds = message.get("embeds")
    if raw_embeds is not None and not isinstance(raw_embeds, list):
        return None
    embeds = raw_embeds or []
    if any(not isinstance(embed, Mapping) for embed in embeds):
        return None
    if any(_embed_text(embed) for embed in embeds):
        return None
    text = re.sub(r"<@&[^>\s]+>", "", content).replace("**", "")
    text = re.sub(r"\s+", " ", text).strip()
    pattern = re.compile(
        r"^(?:OPEN|BUY(?:\s+TO\s+OPEN)?)\s+"
        r"(?P<symbol>\$?[A-Z]{1,6}(?:\.[A-Z])?)\s+"
        r"(?P<strike>\$?(?:\d{1,5}(?:\.\d+)?|\.\d+))\s*"
        r"(?P<option>call|put|c|p)\s+"
        r"(?P<expiry>20\d{2}-\d{1,2}-\d{1,2}|\d{1,2}[/-]\d{1,2}(?:[/-]\d{2,4})?)\s*"
        r"(?:@|at)\s*\$?(?P<price>(?:\d{1,5}(?:\.\d+)?|\.\d+))"
        r"\s*(?:\(\s*swing\s*\))?\s*$",
        re.I,
    )
    match = pattern.fullmatch(text)
    if not match:
        return None
    expiry, conflict = _normalize_expiry(match.group("expiry"), message)
    if conflict or expiry == "nearest" or expiry is None:
        return None
    try:
        if Decimal(match.group("strike").lstrip("$")) <= 0 or Decimal(match.group("price")) <= 0:
            return None
    except InvalidOperation:
        return None
    image = has_image_evidence(message)
    if image and _image_dependency(text):
        return None
    option_type = _SIDE[match.group("option").lower()]
    return {
        "action": "OPEN",
        "contract": {"symbol": match.group("symbol").lstrip("$").upper(), "strike": match.group("strike").lstrip("$"), "option_type": option_type, "expiry": expiry},
        "quantity": None,
        "fraction": None,
        "alert_price": match.group("price"),
        "stop_price": None,
        "profit_only": False,
        "origin_message_id": message["id"],
        "evidence": [{"message_id": message["id"], "quote": content.strip()}],
    }


def _direct_entry_candidate(message: Mapping[str, Any]) -> dict[str, Any] | None:
    if not isinstance(message, Mapping) or not isinstance(message.get("id"), str) or not message["id"]:
        return None
    if message.get("edited_timestamp") or message.get("edited"):
        return None
    structured = _structured_entry_candidate(message)
    if structured is not None:
        return structured
    return _plain_entry_candidate(message)


def bounded_entry_candidate(message: Mapping[str, Any]) -> dict[str, Any] | None:
    """Return a complete bounded OPEN candidate for JEV, if text is sufficient."""
    candidate = _entry_candidate(message, require_no_image=False)
    if candidate is None:
        return None
    # Keep JEV from selecting a date that only a picture could provide.
    if candidate["contract"]["expiry"] == "nearest" and candidate.get("_image_evidence"):
        return None
    candidate.pop("_image_evidence", None)
    candidate["label"] = "candidate_0"
    return candidate


def deterministic_entry(message: Mapping[str, Any]) -> dict[str, Any] | None:
    """Return a locally validated, literal OPEN decision or ``None``."""
    candidate = _direct_entry_candidate(message)
    if candidate is None:
        return None
    decision = {key: value for key, value in candidate.items() if not key.startswith("_") and key != "label"}
    decision["confidence"] = 1.0
    decision["ambiguous"] = False
    decision["reason"] = "Deterministic literal entry."
    try:
        return validate_decision(decision, message, [])
    except InterpretationError:
        return None


__all__ = [
    "bounded_entry_candidate",
    "deterministic_entry",
    "has_image_evidence",
    "option_matches",
    "visible_parts",
    "visible_text",
]
