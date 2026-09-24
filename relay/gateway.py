"""Discord gateway transport using the user-requested ``discord.py-self`` client.

The module deliberately imports the Discord library only when a gateway is
started.  The rest of the relay can therefore be tested with the standard
library alone, and discovery uses only objects already held in the gateway
cache.
"""

from __future__ import annotations

import asyncio
from collections import OrderedDict
from datetime import datetime, timezone
import inspect
import json
import logging
import math
import re
import time
import uuid
from typing import Any

from .core import channel_allows_author
from .ingest import normalize
from .pacing import discord_delay


LOG = logging.getLogger(__name__)
SNOWFLAKE = re.compile(r"\d{15,22}\Z")

_DEFAULT_QUEUE_SIZE = 64
_DEFAULT_CACHE_SIZE = 2048
_MAX_HISTORY = 100
_DEFAULT_HISTORY_SECONDS = 1.0
_DISCOVERY_MAX_AUTHORS = 100
_STATUS_SECONDS = 20.0
_ACK_MAX_AGE_SECONDS = 90.0


class GatewayLoginRequired(RuntimeError):
    """The saved personal-token session needs manual authentication."""


class GatewayFatalError(RuntimeError):
    """A gateway observation or delivery failure that must stop the worker."""


def _value(obj: object, *names: str, default: Any = None) -> Any:
    """Read a field from either a Discord object or a test dictionary."""

    for name in names:
        if isinstance(obj, dict) and name in obj:
            value = obj[name]
        else:
            value = getattr(obj, name, None)
        if value is not None:
            return value
    return default


def _identifier(value: object) -> str:
    if value is None:
        return ""
    return str(value)


def _snowflake(value: object) -> str | None:
    result = _identifier(value)
    return result if SNOWFLAKE.fullmatch(result) else None


def _utc_iso(value: object, *, fallback: str | None = None) -> str:
    if isinstance(value, datetime):
        parsed = value
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc).isoformat(timespec="microseconds")
    if isinstance(value, str) and value:
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            parsed = None
        if parsed is not None:
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return parsed.astimezone(timezone.utc).isoformat(timespec="microseconds")
    if fallback is None:
        fallback = datetime.now(timezone.utc).isoformat(timespec="microseconds")
    return fallback


def _to_dict(value: object) -> dict[str, Any]:
    if isinstance(value, dict):
        return dict(value)
    method = getattr(value, "to_dict", None)
    if callable(method):
        try:
            result = method()
        except Exception:
            result = None
        if isinstance(result, dict):
            return dict(result)
    if value is None:
        return {}
    return {
        key: getattr(value, key)
        for key in (
            "id",
            "filename",
            "url",
            "proxy_url",
            "size",
            "content_type",
            "width",
            "height",
            "description",
        )
        if getattr(value, key, None) is not None
    }


def _attachment_payload(value: object) -> dict[str, Any]:
    data = _to_dict(value)
    return {
        key: data[key]
        for key in (
            "id",
            "filename",
            "url",
            "proxy_url",
            "size",
            "content_type",
            "width",
            "height",
            "description",
        )
        if key in data
    }


def _author_payload(message: object) -> tuple[str, str]:
    author = _value(message, "author", default={}) or {}
    author_id = _value(message, "author_id") or _value(author, "id")
    author_name = (
        _value(message, "author_name")
        or _value(author, "global_name", "display_name", "nickname", "name", "username")
        or ""
    )
    return _identifier(author_id), str(author_name)


def _message_payload(message: object, *, edited: bool = False) -> dict[str, Any]:
    """Convert a Discord message object into the public normalizer shape."""

    raw_data = _value(message, "data")
    if isinstance(raw_data, dict) and _value(message, "id") is None:
        message = raw_data
    channel = _value(message, "channel", default={}) or {}
    channel_id = _value(message, "channel_id") or _value(channel, "id")
    author_id, author_name = _author_payload(message)
    reference = _value(message, "reference", "message_reference", default={}) or {}
    reply_to = (
        _value(message, "reply_to")
        or _value(reference, "message_id", "messageId", "id")
    )
    attachments = _value(message, "attachments", default=[]) or []
    embeds = _value(message, "embeds", default=[]) or []
    edited_timestamp = _value(message, "edited_timestamp", "timestampEdited")
    if edited_timestamp is None:
        edited_timestamp = _value(message, "edited_at")
    if edited and edited_timestamp is None:
        edited_timestamp = datetime.now(timezone.utc)
    timestamp = _value(message, "timestamp", "created_at")
    return {
        "id": _identifier(_value(message, "id")),
        "channel_id": _identifier(channel_id),
        "author_id": author_id,
        "author_name": author_name,
        "content": str(_value(message, "content", default="") or ""),
        "timestamp": _utc_iso(timestamp),
        "edited_timestamp": _utc_iso(edited_timestamp) if edited_timestamp else None,
        "reply_to": _identifier(reply_to) if reply_to else None,
        "attachments": [_attachment_payload(item) for item in attachments],
        "embeds": [_to_dict(item) for item in embeds],
        "source": "gateway",
    }


def _message_key(message: dict[str, Any]) -> tuple[str, str]:
    return str(message.get("channel_id", "")), str(message.get("id", ""))


def _numeric_id(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value >= 0 else None
    if isinstance(value, str) and value.isascii() and value.isdigit():
        return int(value)
    return None


def _message_order(message: dict[str, Any]) -> tuple[int, str, str]:
    identifier = str(message.get("id", ""))
    try:
        return (int(identifier), str(message.get("timestamp", "")), identifier)
    except (TypeError, ValueError):
        return (0, str(message.get("timestamp", "")), identifier)


def _newer(left: dict[str, Any], right: dict[str, Any]) -> bool:
    return _message_order(left) > _message_order(right)


def _bounded_int(value: object, default: int, *, lower: int, upper: int) -> int:
    try:
        result = int(value)
    except (TypeError, ValueError):
        return default
    return max(lower, min(upper, result))


def _is_message_channel(channel: Any) -> bool:
    """Keep readable text channels and threads out of directory results."""

    kind = str(_value(channel, "type", "channel_type", default="")).lower()
    class_name = type(channel).__name__.lower()
    combined = f"{kind} {class_name}"
    if any(name in combined for name in ("category", "voice", "stage", "directory")):
        return False
    if any(name in combined for name in ("text", "news", "forum", "thread", "dm", "private")):
        return True
    return callable(getattr(channel, "history", None))


def _auth_failure(error: BaseException) -> bool:
    name = type(error).__name__.lower()
    if any(
        token in name
        for token in (
            "loginfailure",
            "loginrequired",
            "authentication",
            "authfailure",
            "unauthorized",
            "captcha",
            "verification",
            "challenge",
        )
    ):
        return True
    text = str(error).lower()
    return any(
        marker in text
        for marker in (
            "401",
            "403",
            "invalid token",
            "login required",
            "not authorized",
            "unauthorized",
            "captcha",
            "verification required",
            "challenge required",
        )
    )


def _safe_exception_detail(error: BaseException) -> str:
    """Return a stable, credential-free detail for status/logging."""

    if _auth_failure(error):
        return "Discord personal-token authentication was rejected; complete sign-in or verification."
    if _rate_limited(error):
        return "Discord gateway rate limit persisted; observation stopped safely."
    return f"Discord gateway failure ({type(error).__name__})."


def _rate_limited(error: BaseException) -> bool:
    status = getattr(error, "status", None)
    if status == 429:
        return True
    retry_after = getattr(error, "retry_after", None)
    try:
        retry_after = float(retry_after)
    except (TypeError, ValueError):
        return False
    return math.isfinite(retry_after) and retry_after >= 0


def _load_discord() -> Any:
    try:
        import discord  # type: ignore
    except ImportError as exc:  # pragma: no cover - exercised by packaging checks
        raise RuntimeError("Install discord.py-self to use the Discord gateway transport.") from exc
    return discord


def _make_client(discord_module: Any) -> Any:
    # discord.py-self has user subscriptions, not bot Intents.
    return discord_module.Client(
        chunk_guilds_at_startup=False, guild_subscriptions=True,
        max_messages=1000, member_cache_flags=discord_module.MemberCacheFlags.none(),
        enable_debug_events=True,
    )


async def _maybe_await(callback: Any, *args: Any, **kwargs: Any) -> Any:
    result = callback(*args, **kwargs)
    if inspect.isawaitable(result):
        return await result
    return result


class _GatewayRuntime:
    def __init__(
        self,
        config: dict[str, Any],
        on_message: Any | None,
        register_verifier: Any | None,
        on_status: Any | None,
    ) -> None:
        self.config = config
        self.discord_config = config.get("discord", {}) if isinstance(config.get("discord", {}), dict) else {}
        self.channels = [channel for channel in config.get("channels", []) if isinstance(channel, dict)]
        self.channels_by_id = {str(channel.get("id")): channel for channel in self.channels}
        self.on_message = on_message
        self.register_verifier = register_verifier
        self.on_status = on_status
        risk = config.get("risk", {}) if isinstance(config.get("risk", {}), dict) else {}
        self.queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(
            maxsize=_bounded_int(
                risk.get("max_pending_messages"),
                _DEFAULT_QUEUE_SIZE,
                lower=1,
                upper=4096,
            )
        )
        context = config.get("llm", {}) if isinstance(config.get("llm", {}), dict) else {}
        self.history_limit = _bounded_int(
            context.get("context_messages"),
            60,
            lower=0,
            upper=_MAX_HISTORY,
        )
        self.history_spacing = self.discord_config.get("history_spacing_seconds", _DEFAULT_HISTORY_SECONDS)
        try:
            self.history_spacing = max(0.0, float(self.history_spacing))
        except (TypeError, ValueError):
            self.history_spacing = _DEFAULT_HISTORY_SECONDS
        self.cache_limit = _bounded_int(
            self.discord_config.get("cache_messages"),
            _DEFAULT_CACHE_SIZE,
            lower=16,
            upper=10000,
        )
        self.client: Any | None = None
        self.delivery_task: asyncio.Task[Any] | None = None
        self.health_task: asyncio.Task[Any] | None = None
        self.discovery_task: asyncio.Task[Any] | None = None
        self.fatal: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        self.session_lock = asyncio.Lock()
        self.epoch = ""
        self.generation = 0
        self.healthy = False
        self.initializing = False
        self.closed = False
        self.auth_rejected = False
        self.auth_detail = "Discord gateway credential needs attention; update it in Setup."
        self.last_gateway_ack: float | None = None
        self.channel_status: dict[str, tuple[str, str]] = {}
        self.connection_status = ("starting", "Discord gateway is connecting.")
        self.load_history = True
        self.latest: OrderedDict[tuple[str, str], dict[str, Any]] = OrderedDict()
        self.invalidated: OrderedDict[tuple[str, str], None] = OrderedDict()
        self.latest_by_group: dict[str, dict[str, Any]] = {}
        self.history_floor: dict[str, dict[str, Any]] = {}

    async def status(self, state: str, detail: str, *, channel_id: str | None = None) -> None:
        if state == "login_required":
            self.auth_detail = detail
        elif self.auth_rejected:
            state, detail = "login_required", self.auth_detail
        if channel_id:
            self.channel_status[str(channel_id)] = (state, detail)
        else:
            self.connection_status = (state, detail)
        if self.on_status is None:
            return
        event: dict[str, Any] = {
            "component": "discord",
            "state": state,
            "detail": detail,
        }
        if channel_id:
            event["channel_id"] = str(channel_id)
        try:
            await _maybe_await(self.on_status, event)
        except asyncio.CancelledError:
            raise
        except BaseException as exc:
            self._set_fatal(exc)

    def _set_fatal(self, error: BaseException) -> None:
        if isinstance(error, asyncio.CancelledError):
            return
        if not self.fatal.done():
            self.fatal.set_exception(error)

    def _bind(self, name: str, callback: Any) -> None:
        event = getattr(self.client, "event", None)
        if callable(event):
            event(callback)
        else:
            setattr(self.client, name, callback)

    def bind_events(self) -> None:
        async def on_ready() -> None:
            await self._guard(self.start_session)

        async def on_resumed() -> None:
            await self._guard(self.start_session)

        async def on_disconnect() -> None:
            self.generation += 1
            self.last_gateway_ack = None
            self.healthy = False
            self.initializing = False
            detail = "Discord gateway disconnected; waiting for library reconnect."
            await self.status("reconnecting", detail)
            for channel in self.channels[:2]:
                await self.status("reconnecting", detail, channel_id=str(channel.get("id", "")))

        async def on_message(message: Any) -> None:
            await self._guard(lambda: self.observe(message, kind="live", reason="live"))

        async def on_socket_raw_receive(payload: Any) -> None:
            # Observe only the protocol acknowledgement. Never retain or log
            # Gateway payloads, and leave heartbeat scheduling to the library.
            # discord.py-self dispatches this before decoding the JSON frame.
            if isinstance(payload, (str, bytes)):
                if len(payload) > 1024:
                    return
                try:
                    payload = json.loads(payload)
                except (ValueError, UnicodeError):
                    return
            if isinstance(payload, dict) and payload.get("op") == 11:
                self.last_gateway_ack = time.monotonic()

        async def on_message_edit(before: Any, after: Any) -> None:
            del before
            await self._guard(lambda: self.observe(after, kind="edit", reason="edit", edited=True))

        async def on_message_delete(message: Any) -> None:
            await self._guard(lambda: self.delete(message))

        async def on_raw_message_edit(payload: Any) -> None:
            await self._guard(lambda: self.edit_raw(payload))

        async def on_raw_message_delete(payload: Any) -> None:
            await self._guard(lambda: self.delete_raw(payload))

        async def on_raw_bulk_message_delete(payload: Any) -> None:
            await self._guard(lambda: self.delete_raw_bulk(payload))

        async def on_error(*_args: Any, **_kwargs: Any) -> None:
            # discord.py swallows exceptions raised by event handlers.  Make
            # the worker-visible failure generic so token-bearing arguments or
            # gateway payloads never reach logs or runtime status.
            error = GatewayFatalError("Discord gateway event failed; observation stopped.")
            await self.status("error", "Discord gateway event failed; observation stopped.")
            self._set_fatal(error)

        for name, callback in (
            ("on_ready", on_ready),
            ("on_resumed", on_resumed),
            ("on_disconnect", on_disconnect),
            ("on_message", on_message),
            ("on_socket_raw_receive", on_socket_raw_receive),
            ("on_message_edit", on_message_edit),
            ("on_message_delete", on_message_delete),
            ("on_raw_message_edit", on_raw_message_edit),
            ("on_raw_message_delete", on_raw_message_delete),
            ("on_raw_bulk_message_delete", on_raw_bulk_message_delete),
            ("on_error", on_error),
        ):
            callback.__name__ = name
            self._bind(name, callback)

    async def _guard(self, operation: Any) -> None:
        try:
            await _maybe_await(operation)
        except asyncio.CancelledError:
            raise
        except BaseException as exc:
            if _auth_failure(exc):
                self.auth_rejected = True
                self.healthy = False
                await self.status("login_required", _safe_exception_detail(exc))
                await self._wait_for_restart()
            elif _rate_limited(exc):
                self._set_fatal(GatewayFatalError(_safe_exception_detail(exc)))
            else:
                self._set_fatal(exc)

    async def delivery_loop(self) -> None:
        while True:
            message = await self.queue.get()
            try:
                if self.on_message is not None:
                    await _maybe_await(self.on_message, message)
            except asyncio.CancelledError:
                raise
            except BaseException as exc:
                self._set_fatal(exc)
                return
            finally:
                self.queue.task_done()

    async def enqueue(self, message: dict[str, Any]) -> None:
        if message.get("ingestion") == "baseline":
            await self.queue.put(message)
            return
        try:
            self.queue.put_nowait(message)
        except asyncio.QueueFull as exc:
            error = GatewayFatalError("Discord gateway delivery queue filled; observation stopped.")
            self._set_fatal(error)
            raise error from exc

    def _channel_config(self, channel_id: object) -> dict[str, Any] | None:
        return self.channels_by_id.get(str(channel_id))

    def _allowed(self, message: dict[str, Any], channel: dict[str, Any] | None) -> bool:
        if channel is None:
            return False
        if str(message.get("channel_id")) != str(channel.get("id")):
            return False
        return channel_allows_author(channel, message.get("author_id", ""))

    def _remember(self, message: dict[str, Any], channel: dict[str, Any], *, invalid: bool = False) -> None:
        key = _message_key(message)
        self.latest[key] = message
        self.latest.move_to_end(key)
        while len(self.latest) > self.cache_limit:
            old_key, _old = self.latest.popitem(last=False)
            self.invalidated.pop(old_key, None)
        if invalid:
            self._mark_invalid(key)
        else:
            self.invalidated.pop(key, None)

        if message.get("ingestion") in {"live", "baseline"}:
            group = str(channel.get("source_group", ""))
            previous = self.latest_by_group.get(group)
            if previous is None or _newer(message, previous) or _message_key(previous) == key:
                self.latest_by_group[group] = message

    def _mark_invalid(self, key: tuple[str, str]) -> None:
        self.invalidated[key] = None
        self.invalidated.move_to_end(key)
        while len(self.invalidated) > self.cache_limit * 2:
            self.invalidated.popitem(last=False)

    async def observe(
        self,
        message: Any,
        *,
        kind: str = "live",
        reason: str = "live",
        edited: bool = False,
    ) -> None:
        payload = _message_payload(message)
        channel = self._channel_config(payload.get("channel_id"))
        allowed = self._allowed(payload, channel)
        if not allowed and kind == "edit" and channel is not None:
            previous = self.latest.get(_message_key(payload))
            allowed = bool(previous and channel_allows_author(channel, previous.get("author_id", "")))
        if not allowed:
            return
        normalized = normalize(payload, str(payload["channel_id"]))
        key = _message_key(normalized)
        previous = self.latest.get(key)
        if kind == "edit" and previous is not None and normalized["revision"] == previous["revision"]:
            # Discord also emits MESSAGE_UPDATE for unchanged embeds and renewed
            # media URLs. Keep the original live eligibility and pending decision.
            refreshed = dict(previous)
            for field in ("attachments", "embeds", "transport_revision"):
                refreshed[field] = normalized[field]
            self._remember(refreshed, channel, invalid=key in self.invalidated)
            if refreshed["transport_revision"] != previous["transport_revision"]:
                await self.enqueue(refreshed)
            return
        if edited and not payload.get("edited_timestamp"):
            normalized = normalize(_message_payload(message, edited=True), str(payload["channel_id"]))
        normalized["browser_connection_epoch"] = self.epoch

        floor = self.history_floor.get(str(payload["channel_id"]))
        if kind == "live" and (not self.healthy or self.initializing or not self.connection_fresh()
                               or (floor is not None and not _newer(normalized, floor))):
            kind = "baseline"
            reason = "baseline"
        if kind == "edit":
            kind = "baseline"
            reason = "edit"
        normalized["ingestion"] = kind
        normalized["ingestion_reason"] = reason
        if kind == "baseline" and (floor is None or _newer(normalized, floor)):
            self.history_floor[str(payload["channel_id"])] = normalized
        self._remember(normalized, channel, invalid=reason == "edit")
        await self.enqueue(normalized)

    async def delete(self, message: Any) -> None:
        payload = _message_payload(message)
        channel = self._channel_config(payload.get("channel_id"))
        # A cached-message delete may not contain an author.  Channel binding
        # is sufficient to invalidate a previously verified revision.
        if channel is None or str(payload.get("channel_id")) != str(channel.get("id")):
            return
        key = _message_key(payload)
        self._mark_invalid(key)
        self.latest.pop(key, None)

    async def edit_raw(self, payload: Any) -> None:
        key = (_identifier(_value(payload, "channel_id")), _identifier(_value(payload, "message_id")))
        previous = self.latest.get(key)
        if self._channel_config(key[0]) is None or previous is None:
            return
        data = _value(payload, "data", default={})
        if not isinstance(data, dict):
            self._mark_invalid(key)
            return
        updated = {**previous, **data, "id": key[1], "channel_id": key[0]}
        if "author" in data:
            updated.pop("author_id", None)
            updated.pop("author_name", None)
        # Cached updates also produce on_message_edit. Fence actual changes
        # immediately, but an unchanged embed update must not interrupt an entry.
        if _value(payload, "cached_message") is not None:
            current = normalize(_message_payload(updated), key[0])
            if current["revision"] != previous["revision"]:
                self._mark_invalid(key)
            return
        await self.observe(updated, kind="edit", reason="edit", edited=True)

    async def delete_raw(self, payload: Any) -> None:
        channel_id = _identifier(_value(payload, "channel_id"))
        message_id = _identifier(_value(payload, "message_id", "id"))
        if self._channel_config(channel_id) is None or not message_id:
            return
        key = (channel_id, message_id)
        self._mark_invalid(key)
        self.latest.pop(key, None)

    async def delete_raw_bulk(self, payload: Any) -> None:
        channel_id = _identifier(_value(payload, "channel_id"))
        if self._channel_config(channel_id) is None:
            return
        message_ids = _value(payload, "message_ids", "ids", default=[]) or []
        for message_id in message_ids:
            identifier = _identifier(message_id)
            if not identifier:
                continue
            key = (channel_id, identifier)
            self._mark_invalid(key)
            self.latest.pop(key, None)

    def _find_channel(self, channel_id: str) -> Any | None:
        getter = getattr(self.client, "get_channel", None)
        if callable(getter):
            try:
                value = getter(int(channel_id))
            except (TypeError, ValueError):
                value = getter(channel_id)
            if value is not None:
                return value
        for guild in getattr(self.client, "guilds", ()) or ():
            for channel in getattr(guild, "channels", ()) or ():
                if str(_value(channel, "id")) == str(channel_id):
                    return channel
            for channel in getattr(guild, "threads", ()) or ():
                if str(_value(channel, "id")) == str(channel_id):
                    return channel
        return None

    def _readable(self, channel: Any) -> bool:
        if not _is_message_channel(channel):
            return False
        permissions_for = getattr(channel, "permissions_for", None)
        user = getattr(self.client, "user", None)
        if callable(permissions_for) and user is not None:
            try:
                member = getattr(getattr(channel, "guild", None), "me", None)
                permissions = permissions_for(member) if member is not None else None
            except Exception:
                permissions = None
            if permissions is not None:
                for field in ("view_channel", "read_messages"):
                    if hasattr(permissions, field) and getattr(permissions, field) is False:
                        return False
        return True

    async def _history(self, channel: Any) -> list[Any]:
        if self.history_limit <= 0:
            return []
        history = getattr(channel, "history", None)
        if not callable(history):
            return []
        values: list[Any] = []
        for attempt in range(2):
            try:
                # Ask the library for one bounded newest-message page.  The
                # adapter sorts that page before delivery so durable context
                # sees it chronologically without walking older history.
                iterator = history(limit=self.history_limit, oldest_first=False)
                if inspect.isawaitable(iterator):
                    iterator = await iterator
                values = []
                if hasattr(iterator, "__aiter__"):
                    async for item in iterator:
                        values.append(item)
                        if len(values) >= self.history_limit:
                            break
                else:
                    for item in iterator:
                        values.append(item)
                        if len(values) >= self.history_limit:
                            break
                break
            except asyncio.CancelledError:
                raise
            except BaseException as exc:
                retry_after = getattr(exc, "retry_after", None)
                try:
                    retry_after = float(retry_after)
                except (TypeError, ValueError):
                    retry_after = None
                if (
                    attempt
                    or _auth_failure(exc)
                    or retry_after is None
                    or not math.isfinite(retry_after)
                    or retry_after < 0
                ):
                    if attempt and _rate_limited(exc):
                        raise GatewayFatalError(
                            "Discord gateway rate limit persisted; observation stopped safely."
                        ) from None
                    raise
                # Let the provider's Retry-After control rate-limit waits;
                # never turn an auth error into a credential hot-retry loop.
                await asyncio.sleep(retry_after)
        def sort_key(item: Any) -> tuple[int, str]:
            identifier = _identifier(_value(item, "id"))
            try:
                return int(identifier), identifier
            except ValueError:
                return 0, identifier

        return sorted(values, key=sort_key)

    async def start_session(self) -> None:
        async with self.session_lock:
            if self.fatal.done() or self.auth_rejected:
                return
            self.healthy = False
            self.initializing = True
            self.generation += 1
            generation = self.generation
            self.epoch = f"gateway:{uuid.uuid4()}"
            detail = "Discord gateway connected; loading bounded channel context."
            await self.status("starting", detail)
            for channel in self.channels[:2]:
                await self.status("starting", detail, channel_id=str(channel.get("id", "")))
            try:
                all_channels_ready = True
                for index, channel_config in enumerate(self.channels[:2] if self.load_history else []):
                    if generation != self.generation:
                        self.initializing = False
                        self.healthy = False
                        return
                    channel_id = str(channel_config.get("id", ""))
                    channel = self._find_channel(channel_id)
                    if channel is None or not self._readable(channel):
                        all_channels_ready = False
                        await self.status(
                            "reconnecting",
                            "Configured Discord channel is not in the gateway cache.",
                            channel_id=channel_id,
                        )
                        continue
                    if self.load_history:
                        for item in await self._history(channel):
                            if generation != self.generation:
                                self.initializing = False
                                self.healthy = False
                                return
                            await self.observe(item, kind="baseline", reason="baseline")
                    if self.load_history and index + 1 < min(2, len(self.channels)) and self.history_spacing:
                        await asyncio.sleep(discord_delay(self.history_spacing))
                await self.queue.join()
                if generation != self.generation:
                    self.initializing = False
                    self.healthy = False
                    return
                self.initializing = False
                self.healthy = all_channels_ready
                current = all_channels_ready and self.connection_fresh()
                state = "connected" if current else "reconnecting"
                detail = (
                    "Discord gateway observing configured channels."
                    if current
                    else "Waiting for a current Discord gateway heartbeat acknowledgement."
                    if all_channels_ready
                    else "Discord gateway is waiting for configured channels in its cache."
                )
                await self.status(state, detail)
                for channel in self.channels[:2]:
                    await self.status(state, detail, channel_id=str(channel.get("id", "")))
            except asyncio.CancelledError:
                raise
            except BaseException:
                self.initializing = False
                self.healthy = False
                raise

    def _author_rows(self, channel: Any, guild: Any) -> list[dict[str, str]]:
        members = getattr(channel, "members", None)
        if members is None:
            members = getattr(guild, "members", None)
        result: list[dict[str, str]] = []
        seen: set[str] = set()
        for member in members or ():
            identifier = _snowflake(_value(member, "id"))
            if identifier is None or identifier in seen:
                continue
            seen.add(identifier)
            name = _value(member, "global_name", "display_name", "nick", "name", "username", default="")
            result.append({"id": identifier, "name": str(name or "")})
            if len(result) >= _DISCOVERY_MAX_AUTHORS:
                break
        return result

    async def discover(self, request: dict[str, Any] | None = None) -> dict[str, Any]:
        request = request if isinstance(request, dict) else {}
        requested_guild = _snowflake(request.get("guild_id"))
        requested_channel = _snowflake(request.get("channel_id"))
        guilds: list[dict[str, str]] = []
        channels: list[dict[str, str]] = []
        authors: list[dict[str, str]] = []
        selected_channel_obj: Any | None = None
        selected_guild_obj: Any | None = None
        for guild in getattr(self.client, "guilds", ()) or ():
            guild_id = _snowflake(_value(guild, "id"))
            if guild_id is None or (requested_guild and guild_id != requested_guild):
                continue
            guild_channels = list(getattr(guild, "channels", ()) or ())
            guild_channels.extend(getattr(guild, "threads", ()) or ())
            guild_channels = [channel for channel in guild_channels if self._readable(channel)]
            if requested_channel and requested_guild is None and not any(
                _snowflake(_value(channel, "id")) == requested_channel
                for channel in guild_channels
            ):
                continue
            guilds.append({"id": guild_id, "name": str(_value(guild, "name", default="") or "")})
            # The picker requests channels after a server is selected. Returning
            # every server's channels here can overflow the discovery IPC file.
            if requested_guild is None and requested_channel is None:
                continue
            selected_guild_obj = guild
            for channel in guild_channels:
                channel_id = _snowflake(_value(channel, "id"))
                if channel_id is None or (requested_channel and channel_id != requested_channel):
                    continue
                channel_guild = _snowflake(_value(channel, "guild_id")) or guild_id
                channels.append(
                    {
                        "id": channel_id,
                        "guild_id": channel_guild,
                        "name": str(_value(channel, "name", default="") or ""),
                        "url": f"https://discord.com/channels/{channel_guild}/{channel_id}",
                    }
                )
                if requested_channel and requested_channel == channel_id:
                    selected_channel_obj = channel
        if selected_channel_obj is not None:
            authors = self._author_rows(selected_channel_obj, selected_guild_obj)
        if self.auth_rejected:
            state = "login_required"
            detail = "Complete Discord sign-in or verification before discovery."
        elif self.healthy:
            state = "ready"
            detail = "Discord gateway cache is ready."
        else:
            state = "waiting"
            detail = "Discord gateway cache is waiting for connection."
        return {
            "state": state,
            "request_id": request.get("request_id"),
            "guild_id": requested_guild,
            "channel_id": requested_channel,
            "guilds": guilds,
            "channels": channels,
            "authors": authors,
            "detail": detail,
        }

    async def verify(
        self,
        message: dict[str, Any],
        *,
        recovery: bool = False,
        latest_id: object | None = None,
    ) -> bool:
        if not self.healthy or not self.epoch or not isinstance(message, dict):
            return False
        if not self.connection_fresh():
            return False
        if message.get("source") != "gateway":
            return False
        watermark = _numeric_id(latest_id) if recovery else None
        if recovery:
            if watermark is None or message.get("ingestion") not in {"baseline", "live"}:
                return False
            if not message.get("browser_connection_epoch"):
                return False
        elif message.get("ingestion") != "live":
            return False
        if not recovery and str(message.get("browser_connection_epoch", "")) != self.epoch:
            return False
        channel = self._channel_config(message.get("channel_id"))
        if not self._allowed(message, channel):
            return False
        key = _message_key(message)
        if key in self.invalidated:
            return False
        current = self.latest.get(key)
        if current is None or current.get("revision") != message.get("revision"):
            return False
        if recovery:
            if (
                current.get("source") != "gateway"
                or current.get("ingestion") not in {"baseline", "live"}
                or str(current.get("channel_id")) != str(message.get("channel_id"))
                or current.get("id") != message.get("id")
                or current.get("author_id") != message.get("author_id")
                or current.get("browser_connection_epoch") != self.epoch
                or not channel_allows_author(channel, current.get("author_id", ""))
            ):
                return False
            current_id = _numeric_id(current.get("id"))
            if current_id is None or current_id > watermark:
                return False
            latest = self.latest_by_group.get(str(channel.get("source_group", "")))
            latest_id_value = _numeric_id(latest.get("id")) if latest is not None else None
            if latest_id_value is None or latest_id_value > watermark:
                return False
            return True
        latest = self.latest_by_group.get(str(channel.get("source_group", "")))
        if latest is not None and _newer(latest, message):
            return False
        return True

    def connection_fresh(self) -> bool:
        return bool(not self.auth_rejected and self.client is not None and self.client.is_ready() and not self.client.is_closed()
                    and self.last_gateway_ack is not None
                    and 0 <= time.monotonic() - self.last_gateway_ack <= _ACK_MAX_AGE_SECONDS)

    async def report_health(self) -> None:
        if self.auth_rejected:
            state, detail = self.connection_status
            await self.status(state, detail)
            return
        if self.healthy and not self.initializing:
            if self.connection_fresh():
                age = int(time.monotonic() - self.last_gateway_ack)
                state = "connected"
                detail = f"Discord gateway connected; heartbeat acknowledged {age}s ago."
            else:
                state = "reconnecting"
                detail = "Waiting for a current Discord gateway heartbeat acknowledgement."
            await self.status(state, detail)
            for channel in self.channels[:2] if self.load_history else []:
                await self.status(state, detail, channel_id=str(channel["id"]))
        else:
            state, detail = self.connection_status
            await self.status(state, detail)
            for channel_id, (state, detail) in list(self.channel_status.items()):
                await self.status(state, detail, channel_id=channel_id)

    async def health_loop(self) -> None:
        while True:
            await asyncio.sleep(discord_delay(_STATUS_SECONDS))
            await self._guard(self.report_health)

    async def _serve_discovery(self) -> None:
        runtime_path = self.config.get("runtime_status_file")
        if not runtime_path:
            return
        from .discovery import serve_discovery

        await serve_discovery(None, runtime_path, resolver=self.discover)

    async def _wait_for_restart(self) -> None:
        """Keep an auth-blocked worker pending until service cancellation."""

        self.healthy = False
        self.initializing = False
        while True:
            await asyncio.sleep(discord_delay(_STATUS_SECONDS))
            state, detail = self.connection_status
            await self.status(state, detail)

    async def _start_client(self, token: str) -> None:
        await self.client.start(token, reconnect=True)

    async def run(self, *, observe: bool) -> None:
        self.load_history = observe
        if observe and len(self.channels) != 2:
            raise ValueError("Configure exactly two distinct Discord channels")
        if observe and len({str(channel.get("id")) for channel in self.channels}) != 2:
            raise ValueError("Configure exactly two distinct Discord channels")

        try:
            from .discord_session import read_token
        except ImportError as exc:
            raise RuntimeError("relay.discord_session.read_token is required for gateway transport") from exc
        try:
            token = read_token(self.config)
        except asyncio.CancelledError:
            raise
        except BaseException as exc:
            if _auth_failure(exc):
                self.auth_rejected = True
                await self.status("login_required", _safe_exception_detail(exc))
                await self._wait_for_restart()
            if _rate_limited(exc):
                raise GatewayFatalError(_safe_exception_detail(exc)) from None
            raise
        if inspect.isawaitable(token):
            token = await token
        if not isinstance(token, str) or not token.strip():
            self.auth_rejected = True
            await self.status("login_required", "Complete Discord sign-in or verification before gateway observation.")
            await self._wait_for_restart()

        discord_module = _load_discord()
        self.client = _make_client(discord_module)
        self.bind_events()
        if self.register_verifier is not None and observe:
            await _maybe_await(self.register_verifier, self.verify)

        self.delivery_task = asyncio.create_task(self.delivery_loop())
        self.health_task = asyncio.create_task(self.health_loop())
        if self.config.get("runtime_status_file"):
            self.discovery_task = asyncio.create_task(self._guard(self._serve_discovery))
        client_task = asyncio.create_task(self._start_client(token))
        try:
            done, _pending = await asyncio.wait(
                {client_task, self.fatal},
                return_when=asyncio.FIRST_COMPLETED,
            )
            if self.fatal in done:
                await self.fatal
            if client_task in done:
                await client_task
                await self.queue.join()
                if self.fatal.done():
                    await self.fatal
        except asyncio.CancelledError:
            raise
        except BaseException as exc:
            if _auth_failure(exc):
                self.auth_rejected = True
                self.healthy = False
                await self.status("login_required", _safe_exception_detail(exc))
                await self._wait_for_restart()
            if _rate_limited(exc):
                safe = GatewayFatalError(_safe_exception_detail(exc))
                await self.status("error", str(safe))
                self._set_fatal(safe)
                raise safe
            self._set_fatal(exc)
            raise
        finally:
            self.healthy = False
            self.initializing = False
            if self.fatal.done() and not self.fatal.cancelled():
                self.fatal.exception()
            if not client_task.done():
                client_task.cancel()
                await asyncio.gather(client_task, return_exceptions=True)
            if self.discovery_task is not None:
                self.discovery_task.cancel()
                await asyncio.gather(self.discovery_task, return_exceptions=True)
            if self.health_task is not None:
                self.health_task.cancel()
                await asyncio.gather(self.health_task, return_exceptions=True)
            if self.delivery_task is not None:
                self.delivery_task.cancel()
                await asyncio.gather(self.delivery_task, return_exceptions=True)
            close = getattr(self.client, "close", None) if self.client is not None else None
            if callable(close) and not self.closed:
                self.closed = True
                try:
                    await _maybe_await(close)
                except BaseException:
                    pass


async def monitor(
    config: dict[str, Any],
    on_message: Any,
    *,
    register_verifier: Any | None = None,
    on_status: Any | None = None,
) -> None:
    """Observe two configured channels through the Discord gateway."""

    runtime = _GatewayRuntime(config, on_message, register_verifier, on_status)
    await runtime.run(observe=True)


async def setup(config: dict[str, Any], on_status: Any | None = None) -> None:
    """Run the gateway cache worker used while Discord channels are unconfigured."""

    runtime = _GatewayRuntime(config, None, None, on_status)
    await runtime.run(observe=False)


__all__ = ["GatewayFatalError", "GatewayLoginRequired", "monitor", "setup"]
