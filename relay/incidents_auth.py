"""Publish credential problems discovered by the incident reporter."""

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from tempfile import NamedTemporaryFile


def read_timestamp(value):
    if not isinstance(value, str) or len(value) > 64:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed.timestamp() if parsed.tzinfo else None
    except (ValueError, OverflowError, OSError):
        return None


def _marker(runtime_path, provider):
    return Path(runtime_path).parent / f"incident-{provider}-auth.json"


def _mark(runtime_path, provider):
    path = _marker(runtime_path, provider)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    temporary = None
    try:
        with NamedTemporaryFile(mode="w", dir=path.parent, delete=False) as stream:
            temporary = Path(stream.name)
            json.dump({"auth_required_at": datetime.now(timezone.utc).isoformat()}, stream)
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _read_attention(runtime_path, provider, now):
    try:
        with _marker(runtime_path, provider).open(encoding="utf-8") as stream:
            marker = json.loads(stream.read(512))
        at = read_timestamp(marker.get("auth_required_at"))
    except (OSError, ValueError, AttributeError):
        return None
    now = now.timestamp() if isinstance(now, datetime) else (
        datetime.now(timezone.utc).timestamp() if now is None else float(now)
    )
    return at if at is not None and -5 <= now - at <= 3600 else None


def mark_codex_auth_attention(runtime_path):
    _mark(runtime_path, "codex")


def codex_auth_attention(runtime_path, runtime, now=None):
    at = _read_attention(runtime_path, "codex", now)
    if at is None:
        return False
    codex = runtime.get("codex") if isinstance(runtime, dict) else None
    refreshed = read_timestamp(codex.get("updated_at")) if isinstance(codex, dict) else None
    return not refreshed or refreshed <= at or codex.get("state") not in {"ready", "connected"}


def mark_t3_auth_attention(runtime_path):
    _mark(runtime_path, "t3")


def clear_t3_auth_attention(runtime_path):
    _marker(runtime_path, "t3").unlink(missing_ok=True)


def t3_auth_attention(runtime_path, now=None):
    return _read_attention(runtime_path, "t3", now) is not None
