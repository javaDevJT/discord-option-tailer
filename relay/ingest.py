"""Normalize Discord exports and the public, rendered browser DOM."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re
import shutil
import subprocess
from .images import attachment_identity, ImageTransportError
from datetime import datetime, timezone


SNOWFLAKE = re.compile(r"[0-9]{15,22}\Z")


def _id(value, field: str, *, optional: bool = False) -> str:
    if optional and value in (None, ""):
        return ""
    result = str(value)
    if not SNOWFLAKE.fullmatch(result):
        raise ValueError(f"{field} must be a Discord snowflake ID")
    return result


def _timestamp(value, field: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{field} must be an ISO timestamp with a timezone")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{field} must be an ISO timestamp with a timezone") from exc
    if parsed.tzinfo is None:
        raise ValueError(f"{field} must include a timezone")
    return parsed.astimezone(timezone.utc).isoformat(timespec="microseconds")


def _semantic_media(value):
    if isinstance(value, dict):
        return {key: _semantic_media(item) for key, item in value.items() if key != "proxy_url"}
    if isinstance(value, list):
        return [_semantic_media(item) for item in value]
    if isinstance(value, str):
        try:
            return "https://cdn.discordapp.com" + attachment_identity(value)
        except ImageTransportError:
            pass
    return value


def normalize(raw: dict, channel_id: str | None = None) -> dict:
    """Keep trade-relevant message data; reactions do not create new revisions."""
    if not isinstance(raw, dict):
        raise ValueError("Each message must be an object")
    author = raw.get("author") or {}
    if not isinstance(author, dict):
        raise ValueError("author must be an object")
    content = raw.get("content", "")
    if not isinstance(content, str):
        raise ValueError("content must be text")
    attachments = raw.get("attachments") or []
    embeds = raw.get("embeds") or []
    if not isinstance(attachments, list) or not all(isinstance(a, dict) for a in attachments):
        raise ValueError("attachments must be a list of objects")
    if not isinstance(embeds, list) or not all(isinstance(e, dict) for e in embeds):
        raise ValueError("embeds must be a list of objects")
    reference = raw.get("message_reference") or raw.get("reference") or {}
    if not isinstance(reference, dict):
        raise ValueError("message_reference must be an object")
    reply_to = raw.get("reply_to") or reference.get("message_id") or reference.get("messageId")
    edited = raw.get("edited_timestamp") or raw.get("timestampEdited")
    source = raw.get("source", "export")
    if source not in ("export", "browser"):
        raise ValueError("source must be export or browser")
    message = {
        "id": _id(raw.get("id"), "id"),
        "channel_id": _id(raw.get("channel_id") or channel_id, "channel_id"),
        "author_id": _id(raw.get("author_id") or author.get("id"), "author_id", optional=True),
        "author_name": str(raw.get("author_name") or author.get("global_name")
                           or author.get("nickname") or author.get("name")
                           or author.get("username") or ""),
        "content": content,
        "timestamp": _timestamp(raw.get("timestamp"), "timestamp"),
        "edited_timestamp": _timestamp(edited, "edited_timestamp") if edited else None,
        "reply_to": _id(reply_to, "reply_to", optional=True) or None,
        "attachments": [
            {key: attachment[key] for key in
             ("id", "filename", "url", "proxy_url", "size", "content_type", "width", "height", "description")
             if key in attachment}
            for attachment in attachments
        ],
        "embeds": embeds,
        "source": source,
    }
    semantic = {key: value for key, value in message.items() if key != "source"}
    message["transport_revision"] = hashlib.sha256(
        json.dumps(semantic, sort_keys=True, ensure_ascii=False, separators=(",", ":"),
                   allow_nan=False).encode("utf-8")
    ).hexdigest()
    semantic["attachments"] = _semantic_media(semantic["attachments"])
    semantic["embeds"] = _semantic_media(semantic["embeds"])
    message["revision"] = hashlib.sha256(
        json.dumps(semantic, sort_keys=True, ensure_ascii=False, separators=(",", ":"),
                   allow_nan=False).encode("utf-8")
    ).hexdigest()
    return message


def load_export(path: str | Path) -> list[dict]:
    """Read JSON, JSONL, or JSON stored in RTF; never execute exported content."""
    path = Path(path)
    if path.stat().st_size > 64 * 1024 * 1024:
        raise ValueError("Split exports larger than 64 MiB before importing")
    if path.suffix.lower() == ".rtf":
        converter = shutil.which("textutil")
        if converter is None:
            raise RuntimeError("RTF import requires macOS textutil; export plain JSON on other systems")
        text = subprocess.run(
            [converter, "-convert", "txt", "-stdout", str(path.resolve())],
            check=True, capture_output=True, text=True, timeout=30,
        ).stdout
    else:
        text = path.read_text(encoding="utf-8-sig")
    text = text.lstrip("\ufeff").strip()
    if not text:
        return []
    fallback_channel = None
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        data = []
        for line_number, line in enumerate(text.splitlines(), 1):
            if not line.strip():
                continue
            try:
                data.append(json.loads(line))
            except json.JSONDecodeError as exc:
                raise ValueError(f"Invalid JSON/JSONL in {path.name}, line {line_number}") from exc
    if isinstance(data, dict) and "messages" in data:
        channel = data.get("channel") or {}
        fallback_channel = channel.get("id") if isinstance(channel, dict) else None
        fallback_channel = data.get("channel_id") or fallback_channel
        data = data["messages"]
    elif isinstance(data, dict):
        data = [data]
    if not isinstance(data, list):
        raise ValueError("Export must contain a JSON message list, message object, or JSONL")
    messages = [normalize(raw, fallback_channel) for raw in data]
    return sorted(messages, key=lambda m: (m["timestamp"], int(m["id"])))
