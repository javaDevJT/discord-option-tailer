"""Passive, credential-safe diagnostics for Discord sign-in failures."""

from __future__ import annotations

import asyncio
import json
import re
from urllib.parse import urlsplit


_AUTH_PATH = re.compile(r"^/api/v[0-9]+/auth/(?:login|mfa(?:/[a-z0-9_-]+)?)$")
_MAX_BODY_BYTES = 64 * 1024
_MAX_TASKS = 8
_BODY_TIMEOUT = 1.5
_MAX_DETAIL = 240
_ENUMS = frozenset({"CAPTCHA_INVALID", "CAPTCHA_REQUIRED", "INVALID_CREDENTIALS", "INVALID_LOGIN", "MFA_REQUIRED"})
_CAPTCHA_REQUIRED = frozenset({"captcha-required", "captcha_required", "captcha required"})
_CAPTCHA_REJECTED = frozenset({"captcha-invalid", "captcha_invalid", "captcha-failed", "captcha_failed"})
_NETWORK_MARKERS = {
    "ERR_NAME_NOT_RESOLVED": "dns_failed",
    "ERR_INTERNET_DISCONNECTED": "offline",
    "ERR_NETWORK_CHANGED": "network_changed",
    "ERR_CONNECTION_REFUSED": "connection_refused",
    "ERR_CONNECTION_RESET": "connection_reset",
    "ERR_CONNECTION_CLOSED": "connection_closed",
    "ERR_TIMED_OUT": "timeout",
    "ETIMEDOUT": "timeout",
    "ENETUNREACH": "network_unreachable",
}


def _auth_endpoint(url: object) -> bool:
    if not isinstance(url, str):
        return False
    try:
        parsed = urlsplit(url)
        return parsed.scheme == "https" and parsed.hostname == "discord.com" and parsed.port in (None, 443) and bool(
            _AUTH_PATH.fullmatch(parsed.path)
        )
    except (TypeError, ValueError):
        return False


def _safe_fallback(value: object) -> str:
    if not isinstance(value, str):
        return "Discord sign-in requires attention."
    value = " ".join(value.split())
    if not value or len(value) > _MAX_DETAIL or re.search(
        r"https?://|(?:token|password|cookie|authorization|captcha_rq|rqdata)\s*[:=]", value, re.I
    ):
        return "Discord sign-in requires attention."
    return value


def _enum(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    value = value.strip().upper().replace("-", "_")
    return value if value in _ENUMS else None


def _has_marker(value: object, markers: frozenset[str]) -> bool:
    if isinstance(value, str):
        return value.strip().lower() in markers
    return isinstance(value, list) and any(_has_marker(item, markers) for item in value[:16])


def _metadata(payload: object) -> tuple[int | None, str | None, str | None]:
    code = enum = captcha = None
    seen = 0

    def visit(value: object, depth: int = 0) -> None:
        nonlocal code, enum, captcha, seen
        if depth > 4 or seen >= 64:
            return
        seen += 1
        if isinstance(value, dict):
            for key, child in list(value.items())[:32]:
                if key == "code":
                    if isinstance(child, int) and not isinstance(child, bool) and 0 <= child <= 999_999_999:
                        code = child
                    else:
                        enum = enum or _enum(child)
                elif key in {"captcha_key", "captcha_keys"}:
                    if _has_marker(child, _CAPTCHA_REQUIRED):
                        captcha = "requested"
                    elif captcha != "requested" and _has_marker(child, _CAPTCHA_REJECTED):
                        captcha = "rejected"
                elif key == "captcha_sitekey" and child:
                    captcha = captcha or "requested"
                elif key == "captcha_service" and isinstance(child, str) and child.lower() == "hcaptcha":
                    captcha = captcha or "requested"
                visit(child, depth + 1)
        elif isinstance(value, list):
            for child in value[:32]:
                visit(child, depth + 1)

    visit(payload)
    return code, enum, captcha


def _response_detail(status: int, *, code: int | None = None, enum: str | None = None, captcha: str | None = None) -> str:
    if captcha == "requested" or enum == "CAPTCHA_REQUIRED":
        category, lead = "captcha_requested", "Discord CAPTCHA challenge requested"
    elif captcha == "rejected" or enum == "CAPTCHA_INVALID":
        category, lead = "captcha_rejected", "Discord CAPTCHA rejected"
    elif status == 429:
        category, lead = "rate_limited", "Discord login rate limited"
    elif enum in {"INVALID_CREDENTIALS", "INVALID_LOGIN"} or status in {401, 403}:
        category, lead = "auth_rejected", "Discord login rejected"
    elif 400 <= status < 500:
        category, lead = f"auth_http_{status}", f"Discord login returned HTTP {status}"
    elif status >= 500:
        category, lead = f"server_http_{status}", f"Discord login server error HTTP {status}"
    else:
        return ""
    parts = [f"{lead} [{category}]", f"HTTP {status}"]
    if code is not None:
        parts.append(f"Discord code {code}")
    if enum:
        parts.append(f"enum {enum}")
    return "; ".join(parts) + "."


class DiscordLoginDiagnostics:
    """Listen only to BrowserContext response/requestfailed events."""

    def __init__(self, context: object):
        self._context = context
        self._failure: str | None = None
        self._failure_sequence = -1
        self._sequence = 0
        self._generation = 0
        self._closed = False
        self._tasks: set[asyncio.Task[None]] = set()
        self._bindings: list[tuple[str, object]] = []
        try:
            on = context.on
            for event, callback in (("response", self._on_response), ("requestfailed", self._on_request_failed)):
                try:
                    on(event, callback)
                    self._bindings.append((event, callback))
                except Exception:
                    pass
        except Exception:
            pass

    def _next(self) -> int:
        self._sequence += 1
        return self._sequence

    def _latch(self, detail: str, sequence: int, generation: int) -> None:
        if not self._closed and generation == self._generation and sequence >= self._failure_sequence and detail:
            self._failure = detail[:_MAX_DETAIL]
            self._failure_sequence = sequence

    def _on_response(self, response: object) -> None:
        try:
            if not _auth_endpoint(response.url):
                return
            status = response.status
            if not isinstance(status, int) or isinstance(status, bool) or status < 400 or status > 599:
                return
            sequence, generation = self._next(), self._generation
            self._latch(_response_detail(status), sequence, generation)
            if status != 429:
                self._schedule(self._inspect_response(response, status, sequence, generation))
        except Exception:
            pass

    def _on_request_failed(self, request: object) -> None:
        try:
            if not _auth_endpoint(request.url):
                return
            sequence, generation = self._next(), self._generation
            failure = str(request.failure or "").upper()
            kind = next((name for marker, name in _NETWORK_MARKERS.items() if marker in failure), "network_failed")
            self._latch(f"Discord login network failure [network_failed]; {kind}.", sequence, generation)
        except Exception:
            pass

    def _schedule(self, coroutine: object) -> None:
        if self._closed or len(self._tasks) >= _MAX_TASKS:
            coroutine.close()
            return
        try:
            task = asyncio.create_task(coroutine)
        except Exception:
            coroutine.close()
            return
        self._tasks.add(task)
        task.add_done_callback(self._task_done)

    def _task_done(self, task: asyncio.Task[None]) -> None:
        self._tasks.discard(task)
        try:
            task.result()
        except BaseException:
            pass

    async def _inspect_response(self, response: object, status: int, sequence: int, generation: int) -> None:
        try:
            body = await asyncio.wait_for(response.body(), timeout=_BODY_TIMEOUT)
            if not isinstance(body, (bytes, bytearray)) or len(body) > _MAX_BODY_BYTES:
                return
            code, enum, captcha = _metadata(json.loads(body))
            self._latch(_response_detail(status, code=code, enum=enum, captcha=captcha), sequence, generation)
        except asyncio.CancelledError:
            raise
        except Exception:
            pass

    def detail(self, fallback: str) -> str:
        return self._failure or _safe_fallback(fallback)

    def clear(self) -> None:
        self._generation += 1
        self._failure = None
        self._failure_sequence = -1

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._generation += 1
        for event, callback in self._bindings:
            try:
                remove = getattr(self._context, "off", None) or getattr(self._context, "remove_listener", None)
                if callable(remove):
                    remove(event, callback)
            except Exception:
                pass
        self._bindings.clear()
        tasks = tuple(self._tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
