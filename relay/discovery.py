"""Read Discord's rendered server and channel directory in a browser context.

The discovery worker is deliberately separate from :mod:`relay.browser`.  It
uses a page created in the caller's existing Playwright context, so a running
monitor can keep its own pages.  Requests contain only optional Discord
snowflakes and results contain only a bounded public projection.  No Discord
token, API client, network interception, hidden React state, message content,
or arbitrary URL is accepted or persisted here.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import json
import logging
import os
from pathlib import Path
import re
import tempfile
import threading
import time
import uuid
from urllib.parse import urlsplit


SNOWFLAKE = re.compile(r"\d{15,22}\Z")
REQUEST_TIMEOUT_SECONDS = 40.0
RESULT_MAX_AGE_SECONDS = 300.0
DISCOVERY_POLL_SECONDS = 0.15
DIRECTORY_READY_WAIT_SECONDS = 10.0
DISCOVERY_START_URL = "https://discord.com/channels/@me"

# Fixed names beside the configured runtime status path.  The status path is
# an anchor supplied by setup; it is never rewritten by this module.
REQUEST_FILENAME = "discovery-request.json"
RESULT_FILENAME = "discovery-result.json"

MAX_FILE_BYTES = 512 * 1024
MAX_AUTHORS = 100
MAX_NAME_LENGTH = 120
MAX_DETAIL_LENGTH = 512

_STATES = frozenset({"idle", "waiting", "ready", "failed", "login_required"})
_PATH_LOCK = threading.RLock()


class _LoginRequired(RuntimeError):
    pass


def _runtime_path(runtime_path: str | os.PathLike[str]) -> Path:
    if not isinstance(runtime_path, (str, os.PathLike)):
        raise TypeError("runtime_path must be a filesystem path")
    path = Path(runtime_path).expanduser().resolve()
    if not path.name or path.exists() and path.is_dir():
        raise ValueError("runtime_path must name a status file")
    return path


def _request_path(runtime_path: str | os.PathLike[str]) -> Path:
    return _runtime_path(runtime_path).parent / REQUEST_FILENAME


def _result_path(runtime_path: str | os.PathLike[str]) -> Path:
    return _runtime_path(runtime_path).parent / RESULT_FILENAME


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _timestamp(value: object) -> float | None:
    if not isinstance(value, str) or len(value) > 64:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(timezone.utc).timestamp()


def _is_uuid(value: object) -> bool:
    if not isinstance(value, str) or len(value) > 64:
        return False
    try:
        uuid.UUID(value)
    except (ValueError, AttributeError, TypeError):
        return False
    return True


def _snowflake(value: object, field: str, *, optional: bool = False) -> str | None:
    if value is None and optional:
        return None
    if not isinstance(value, str) or not SNOWFLAKE.fullmatch(value):
        raise ValueError(f"{field} must be a Discord snowflake")
    return value


def _clean_name(value: object, *, fallback: str = "") -> str:
    if not isinstance(value, str):
        return fallback
    # Preserve normal Unicode display names while removing controls and
    # collapsing whitespace that could make the setup projection misleading.
    value = "".join(char if ord(char) >= 32 and char not in "\x7f\x80\x9f" else " "
                    for char in value)
    value = " ".join(value.split())
    return value[:MAX_NAME_LENGTH].strip()


def _clean_detail(value: object, fallback: str = "") -> str:
    if not isinstance(value, str):
        return fallback
    return _clean_name(value, fallback=fallback)[:MAX_DETAIL_LENGTH]


def _empty_snapshot(*, state: str = "idle", request_id: str | None = None,
                    guild_id: str | None = None, channel_id: str | None = None,
                    detail: str = "") -> dict:
    if state not in _STATES:
        state = "failed"
    return {
        "state": state,
        "request_id": request_id if _is_uuid(request_id) else None,
        "guild_id": guild_id if isinstance(guild_id, str) and SNOWFLAKE.fullmatch(guild_id) else None,
        "channel_id": channel_id if isinstance(channel_id, str) and SNOWFLAKE.fullmatch(channel_id) else None,
        "guilds": [],
        "channels": [],
        "authors": [],
        "detail": _clean_detail(detail),
        "authors_limited": True,
    }


def _public_projection(value: object) -> dict:
    """Return a bounded, credential-free public result projection."""
    if not isinstance(value, dict):
        return _empty_snapshot(state="failed", detail="Discovery state is unavailable.")
    request_id = value.get("request_id") if _is_uuid(value.get("request_id")) else None
    state = value.get("state") if value.get("state") in _STATES else "failed"
    guild_id = value.get("guild_id") if SNOWFLAKE.fullmatch(str(value.get("guild_id", ""))) else None
    channel_id = value.get("channel_id") if SNOWFLAKE.fullmatch(str(value.get("channel_id", ""))) else None
    result = _empty_snapshot(state=state, request_id=request_id, guild_id=guild_id,
                             channel_id=channel_id, detail=value.get("detail", ""))

    seen_guilds: set[str] = set()
    for item in value.get("guilds", []) if isinstance(value.get("guilds"), list) else []:
        if not isinstance(item, dict):
            continue
        identifier = item.get("id")
        if not isinstance(identifier, str) or not SNOWFLAKE.fullmatch(identifier) or identifier in seen_guilds:
            continue
        seen_guilds.add(identifier)
        result["guilds"].append({"id": identifier, "name": _clean_name(item.get("name"))})

    seen_channels: set[str] = set()
    for item in value.get("channels", []) if isinstance(value.get("channels"), list) else []:
        if not isinstance(item, dict):
            continue
        identifier = item.get("id")
        item_guild = item.get("guild_id")
        url = item.get("url")
        if (not isinstance(identifier, str) or not SNOWFLAKE.fullmatch(identifier)
                or not isinstance(item_guild, str) or not SNOWFLAKE.fullmatch(item_guild)
                or not isinstance(url, str) or not _channel_url(url, item_guild, identifier)
                or identifier in seen_channels):
            continue
        if guild_id is not None and item_guild != guild_id:
            continue
        seen_channels.add(identifier)
        result["channels"].append({"id": identifier, "guild_id": item_guild,
                                    "name": _clean_name(item.get("name")),
                                    "url": _canonical_channel_url(item_guild, identifier)})

    seen_authors: set[str] = set()
    for item in value.get("authors", []) if isinstance(value.get("authors"), list) else []:
        if not isinstance(item, dict):
            continue
        identifier = item.get("id")
        if (not isinstance(identifier, str) or not SNOWFLAKE.fullmatch(identifier)
                or identifier in seen_authors):
            continue
        seen_authors.add(identifier)
        result["authors"].append({"id": identifier, "name": _clean_name(item.get("name"))})
        if len(result["authors"]) >= MAX_AUTHORS:
            break
    return result


def _persistable_result(value: object, *, completed_at: str | None = None) -> dict:
    """Strip worker-only navigation controls before the result reaches disk."""
    public = _public_projection(value)
    if completed_at:
        public["completed_at"] = completed_at
    return public


def _json_read(path: Path) -> dict | None:
    try:
        if path.is_symlink() or not path.is_file() or path.stat().st_size > MAX_FILE_BYTES:
            return None
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, TypeError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def _atomic_json_write(path: Path, value: dict) -> None:
    parent = path.parent
    parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        parent.chmod(0o700)
    except OSError:
        pass
    if path.is_symlink():
        raise RuntimeError("discovery state path must not be a symlink")
    encoded = json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    if len(encoded.encode("utf-8")) > MAX_FILE_BYTES:
        raise RuntimeError("discovery state is too large")
    fd, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(parent))
    temporary = Path(temporary_name)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(encoded)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        path.chmod(0o600)
        try:
            directory_fd = os.open(parent, os.O_DIRECTORY)
        except OSError:
            directory_fd = None
        if directory_fd is not None:
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
    except OSError as exc:
        raise RuntimeError("could not persist discovery state") from exc
    finally:
        temporary.unlink(missing_ok=True)


def _validated_request(value: object) -> dict | None:
    if not isinstance(value, dict) or not _is_uuid(value.get("request_id")):
        return None
    created_at = value.get("created_at")
    if _timestamp(created_at) is None:
        return None
    try:
        guild_id = _snowflake(value.get("guild_id"), "guild_id", optional=True)
        channel_id = _snowflake(value.get("channel_id"), "channel_id", optional=True)
    except ValueError:
        return None
    if channel_id is not None and guild_id is None:
        return None
    return {"request_id": value["request_id"], "created_at": created_at,
            "guild_id": guild_id, "channel_id": channel_id}


def _expired(request: dict, *, now: float | None = None) -> bool:
    created = _timestamp(request.get("created_at"))
    if created is None:
        return True
    return (time.time() if now is None else now) - created >= REQUEST_TIMEOUT_SECONDS


def _result_expired(result: dict, *, now: float | None = None) -> bool:
    completed = _timestamp(result.get("completed_at"))
    return completed is not None and (time.time() if now is None else now) - completed >= RESULT_MAX_AGE_SECONDS


def _channel_url(url: object, guild_id: str, channel_id: str) -> bool:
    if not isinstance(url, str) or len(url) > 300:
        return False
    try:
        parsed = urlsplit(url)
    except ValueError:
        return False
    return (parsed.scheme == "https" and parsed.netloc == "discord.com"
            and not parsed.query and not parsed.fragment
            and parsed.path.rstrip("/") == f"/channels/{guild_id}/{channel_id}")


def _canonical_channel_url(guild_id: str, channel_id: str) -> str:
    return f"https://discord.com/channels/{guild_id}/{channel_id}"


def _validated_payload(payload: object) -> tuple[str | None, str | None]:
    if not isinstance(payload, dict):
        raise ValueError("discovery payload must be an object")
    if set(payload) - {"guild_id", "channel_id"}:
        raise ValueError("unsupported discovery field")
    guild_id = _snowflake(payload.get("guild_id"), "guild_id", optional=True)
    channel_id = _snowflake(payload.get("channel_id"), "channel_id", optional=True)
    if channel_id is not None and guild_id is None:
        raise ValueError("channel_id requires guild_id")
    return guild_id, channel_id


def request_discovery(runtime_path: str | os.PathLike[str], payload: dict | None = None) -> dict:
    """Queue one bounded discovery request and return its waiting snapshot."""
    runtime = _runtime_path(runtime_path)
    guild_id, channel_id = _validated_payload({} if payload is None else payload)
    with _PATH_LOCK:
        request_path = _request_path(runtime)
        result_path = _result_path(runtime)
        current = _validated_request(_json_read(request_path))
        result = _json_read(result_path)
        result_id = result.get("request_id") if isinstance(result, dict) else None
        if current is not None and current["request_id"] != result_id and not _expired(current):
            raise RuntimeError("Discord discovery request is already running")
        request_id = str(uuid.uuid4())
        _atomic_json_write(request_path, {
            "request_id": request_id,
            "created_at": _utc_now().isoformat(),
            "guild_id": guild_id,
            "channel_id": channel_id,
        })
    return _empty_snapshot(
        state="waiting", request_id=request_id, guild_id=guild_id,
        channel_id=channel_id,
        detail="Discord discovery request is waiting for the browser worker.",
    )


def discovery_status(runtime_path: str | os.PathLike[str]) -> dict:
    """Read only the result correlated with the current discovery request."""
    runtime = _runtime_path(runtime_path)
    with _PATH_LOCK:
        request = _validated_request(_json_read(_request_path(runtime)))
        result = _json_read(_result_path(runtime))
    result_id = result.get("request_id") if isinstance(result, dict) else None
    if request is not None:
        if result_id == request["request_id"]:
            if isinstance(result, dict) and _result_expired(result):
                return _empty_snapshot(
                    state="failed", request_id=request["request_id"],
                    guild_id=request["guild_id"], channel_id=request["channel_id"],
                    detail="Discord discovery result is stale; retry discovery.",
                )
            return _public_projection(result)
        if _expired(request):
            return _empty_snapshot(
                state="failed", request_id=request["request_id"],
                guild_id=request["guild_id"], channel_id=request["channel_id"],
                detail="Discord discovery request timed out; retry discovery.",
            )
        return _empty_snapshot(
            state="waiting", request_id=request["request_id"],
            guild_id=request["guild_id"], channel_id=request["channel_id"],
            detail="Discord discovery request is waiting for the browser worker.",
        )
    if isinstance(result, dict) and _is_uuid(result.get("request_id")):
        if _result_expired(result):
            return _empty_snapshot(
                state="failed", request_id=result["request_id"],
                guild_id=result.get("guild_id") if isinstance(result.get("guild_id"), str) else None,
                channel_id=result.get("channel_id") if isinstance(result.get("channel_id"), str) else None,
                detail="Discord discovery result is stale; retry discovery.",
            )
        return _public_projection(result)
    return _empty_snapshot(state="idle", detail="No Discord discovery request has been submitted.")


# The JavaScript below intentionally reads only visible DOM nodes.  Returned
# action metadata is private to the in-memory worker and is never persisted.
EXTRACT_GUILDS_JS = r"""() => {
  const visible = el => !!el && !!el.getClientRects().length &&
    getComputedStyle(el).visibility !== 'hidden' && getComputedStyle(el).display !== 'none';
  const login = location.pathname.startsWith('/login') || location.pathname.startsWith('/verify') ||
    location.pathname.startsWith('/register') || location.pathname.startsWith('/challenge') ||
    [...document.querySelectorAll(`input[type="password"], input[autocomplete="one-time-code"],
      form[action*="/login"], form[action*="/verify"], iframe[src*="hcaptcha.com"],
      iframe[src*="recaptcha"]`)].some(visible);
  if (login) return {login_required: true, sidebar_present: false, guilds: []};
  const snowflake = /^\d{15,22}$/;
  const path = /^\/channels\/(\d{15,22})\/(?:@home|\d{15,22})\/?$/;
  const guilds = [];
  const byId = new Map();
  const inMessage = el => !!el.closest(`[data-list-id="chat-messages"], [id^="chat-messages-"],
    [id^="message-content-"], [id^="message-accessories-"], article, [class*="embed"],
    [class*="message"]`);
  const inNavigation = el => !inMessage(el) && !!el.closest(`
    nav, [role="tree"], [data-list-id*="guild" i], [data-list-id*="channel" i],
    [data-list-item-id^="guildsnav___"], [data-list-item-id^="channels___"],
    [aria-label*="server" i], [aria-label*="guild" i], [aria-label*="channel" i],
    [class*="guilds"], [class*="sidebar"], [class*="channels"]`);
 const text = el => {
   if (!el) return '';
   const labelled = node => (node.getAttribute('aria-labelledby') || '').split(/\s+/)
     .map(id => document.getElementById(id)?.textContent || '').join(' ');
   const nodes = [el, ...el.querySelectorAll('[aria-label], [aria-labelledby], [title]')];
   const labels = nodes.flatMap(node => [node.getAttribute('aria-label'), labelled(node), node.getAttribute('title')]);
   labels.push(el.querySelector('img[alt]')?.getAttribute('alt'), el.innerText, el.textContent);
   return labels.map(value => String(value || '').trim()).find(Boolean) || '';
 };
  const add = (id, name, href, selector, preferred = false) => {
    if (!snowflake.test(id)) return;
    const existing = byId.get(id);
    const item = {id, name: String(name || ''), href: href || '', selector: selector || '', preferred};
    if (!existing) { byId.set(id, item); guilds.push(item); return; }
    if ((item.preferred || !existing.name) && item.name) existing.name = item.name;
    if (!existing.href && item.href) existing.href = item.href;
    if ((item.preferred || !existing.selector) && item.selector) existing.selector = item.selector;
  };
  // Discord often renders guilds as click-only treeitems.  Read them first so
  // their server label wins over text from a channel link for the same guild.
  for (const node of [...document.querySelectorAll('[data-list-item-id^="guildsnav___"]')].filter(visible)) {
    const raw = node.getAttribute('data-list-item-id') || '';
    const id = raw.slice('guildsnav___'.length);
    if (!snowflake.test(id)) continue;
    add(id, text(node), '', `guildsnav___${id}`, true);
  }
  for (const anchor of [...document.querySelectorAll('a[href*="/channels/"]')].filter(el => visible(el) && inNavigation(el))) {
    let parsed;
    try { parsed = new URL(anchor.href, location.href); } catch (_) { continue; }
    if (parsed.origin !== 'https://discord.com') continue;
    const match = parsed.pathname.match(path);
    if (!match) continue;
    add(match[1], text(anchor), `https://discord.com${parsed.pathname}`, '');
  }
  const sidebar = guilds.length > 0 || [...document.querySelectorAll(`
    nav, [role="tree"], [aria-label*="server" i], [aria-label*="guild" i],
    [data-list-id*="guild" i], [class*="guilds"]`)].some(visible);
  return {login_required: false, sidebar_present: sidebar, guilds};
}"""


EXTRACT_CHANNELS_JS = r"""(selectedGuildId) => {
  const visible = el => !!el && !!el.getClientRects().length &&
    getComputedStyle(el).visibility !== 'hidden' && getComputedStyle(el).display !== 'none';
  const login = location.pathname.startsWith('/login') || location.pathname.startsWith('/verify') ||
    location.pathname.startsWith('/register') || location.pathname.startsWith('/challenge') ||
    [...document.querySelectorAll(`input[type="password"], input[autocomplete="one-time-code"],
      form[action*="/login"], form[action*="/verify"], iframe[src*="hcaptcha.com"],
      iframe[src*="recaptcha"]`)].some(visible);
  if (login) return {login_required: true, sidebar_present: false, channels: []};
  const snowflake = /^\d{15,22}$/;
  const path = /^\/channels\/(\d{15,22})\/(\d{15,22})\/?$/;
  const channels = [];
  const seen = new Set();
  const inMessage = el => !!el.closest(`[data-list-id="chat-messages"], [id^="chat-messages-"],
    [id^="message-content-"], [id^="message-accessories-"], article, [class*="embed"],
    [class*="message"]`);
  const inNavigation = el => !inMessage(el) && !!el.closest(`
    nav, [role="tree"], [data-list-id*="channel" i], [data-list-item-id^="channels___"],
    [aria-label*="channel" i], [class*="sidebar"], [class*="channels"]`);
  const text = el => {
    if (!el) return '';
    return el.getAttribute('aria-label') || el.getAttribute('title') ||
      el.innerText || el.textContent || '';
  };
  const nonMessage = value => /^(?:voice|stage|category|channel category|folder|forum)(?: channel)?(?: [(]limited[)])?$/i.test(value.trim()) ||
    /[(](?:voice|stage|forum) channel[)]/i.test(value);
  for (const anchor of [...document.querySelectorAll('a[href*="/channels/"]')].filter(el => visible(el) && inNavigation(el))) {
    let parsed;
    try { parsed = new URL(anchor.href, location.href); } catch (_) { continue; }
    if (parsed.origin !== 'https://discord.com') continue;
    const match = parsed.pathname.match(path);
    if (!match || match[1] !== String(selectedGuildId) || !snowflake.test(match[2])) continue;
    const row = anchor.closest('[data-list-item-id^="channels___"]') ||
      anchor.closest('[role="treeitem"]') || anchor.closest('li') || anchor;
    const typeEvidence = [
      anchor.getAttribute('data-channel-type') || '', anchor.getAttribute('data-type') || '',
      row.getAttribute('data-channel-type') || '', row.getAttribute('data-type') || '',
      anchor.getAttribute('aria-label') || '', row.getAttribute('aria-label') || '',
    ];
    if (typeEvidence.some(nonMessage)) continue;
    if (seen.has(match[2])) continue;
    seen.add(match[2]);
    channels.push({id: match[2], guild_id: match[1], name: text(anchor),
      url: `https://discord.com${parsed.pathname}`});
  }
  const sidebar = channels.length > 0 || [...document.querySelectorAll(`
    nav, [role="tree"], [aria-label*="channel" i], [data-list-id*="channel" i],
    [class*="channel"]`)].some(visible);
  return {login_required: false, sidebar_present: sidebar, channels};
}"""


SCROLL_CHANNELS_JS = r"""({action, guild_id}) => {
 const prefix = `/channels/${guild_id}/`;
 const list = [...document.querySelectorAll('[data-list-id="channels"], [data-list-item-id^="channels___"]')]
   .find(el => el.getClientRects().length && getComputedStyle(el).visibility !== 'hidden' &&
     !el.closest('[data-list-id="chat-messages"], [id^="message-content-"], [class*="embed"]') &&
     [...(el.matches('a[href]') ? [el] : el.querySelectorAll('a[href]'))].some(a => [prefix, `https://discord.com${prefix}`]
       .some(value => a.getAttribute('href').startsWith(value))));
 let scroller = list;
 while (scroller && !(scroller.clientHeight > 0 &&
   /auto|scroll/.test(getComputedStyle(scroller).overflowY))) scroller = scroller.parentElement;
 if (!scroller) return {};
 const before = scroller.scrollTop;
 if (action === 'next') scroller.scrollTop += Math.max(1, scroller.clientHeight * 0.8);
 if (typeof action === 'number') scroller.scrollTop = action;
 return {top: scroller.scrollTop, before, end: scroller.scrollTop + scroller.clientHeight >= scroller.scrollHeight - 2};
}"""


EXTRACT_AUTHORS_JS = r"""(expectedChannelId) => {
  const visible = el => !!el && !!el.getClientRects().length &&
    getComputedStyle(el).visibility !== 'hidden' && getComputedStyle(el).display !== 'none';
  const login = location.pathname.startsWith('/login') || location.pathname.startsWith('/verify') ||
    location.pathname.startsWith('/register') || location.pathname.startsWith('/challenge') ||
    [...document.querySelectorAll(`input[type="password"], input[autocomplete="one-time-code"],
      form[action*="/login"], form[action*="/verify"], iframe[src*="hcaptcha.com"],
      iframe[src*="recaptcha"]`)].some(visible);
  if (login) return {login_required: true, chat_present: false, authors: []};
  const list = document.querySelector('[data-list-id="chat-messages"]');
  if (!list) return {login_required: false, chat_present: false, authors: []};
  const rows = [...list.querySelectorAll('[id^="chat-messages-"]')]
    .filter(row => visible(row) && /^chat-messages-\d{15,22}-\d{15,22}$/.test(row.id))
    .filter(row => row.id.split('-')[2] === String(expectedChannelId));
  const authors = [];
  const seen = new Set();
  const authorHeader = (element, row) => {
    if (!element || element.closest(`[id^="message-content-"], [id^="message-accessories-"],
      [class*="repliedMessage"], article, [class*="embed"]`)) return null;
    const heading = element.closest('h3');
    const authorRow = heading && heading.closest('[id^="chat-messages-"]');
    return heading && authorRow === row ? {element, heading} : null;
  };
  for (const row of rows) {
    const own = selector => [...row.querySelectorAll(selector)].filter(el =>
      !el.closest('[class*="repliedMessage"]') && el.closest('[id^="chat-messages-"]') === row);
    let author = own('[id^="message-username-"]').map(el => authorHeader(el, row)).find(Boolean);
    if (!author) {
      const labels = row.getAttribute('aria-labelledby') ||
        (row.firstElementChild && row.firstElementChild.getAttribute('aria-labelledby')) || '';
      const label = labels.split(/\s+/).find(value => value.startsWith('message-username-'));
      author = authorHeader(label && document.getElementById(label), row);
    }
    if (!author || !author.heading.parentElement) continue;
    const avatar = [...author.heading.parentElement.children]
      .find(element => element.matches('img[class*="avatar"]'));
    const avatarUrl = avatar && (avatar.currentSrc || avatar.src) || '';
    const match = avatarUrl.match(/\/avatars\/(\d{15,22})\//) ||
      avatarUrl.match(/\/guilds\/\d+\/users\/(\d{15,22})\/avatars\//);
    if (!match || seen.has(match[1])) continue;
    seen.add(match[1]);
    authors.push({id: match[1], name: author.element.textContent || ''});
    if (authors.length >= 100) break;
  }
  return {login_required: false, chat_present: true, authors};
}"""


def _page_url(page: object) -> str:
    try:
        value = page.url
    except Exception:
        return ""
    return value if isinstance(value, str) else ""


def _login_url(url: str) -> bool:
    try:
        parsed = urlsplit(url)
    except ValueError:
        return False
    return (parsed.netloc == "discord.com"
            and parsed.path.startswith(("/login", "/verify", "/register", "/challenge")))


async def _wait_briefly(page: object, milliseconds: int = 250) -> None:
    waiter = getattr(page, "wait_for_timeout", None)
    if waiter is not None:
        try:
            await waiter(milliseconds)
            return
        except Exception:
            pass
    await asyncio.sleep(milliseconds / 1000)


async def _goto(page: object, url: str) -> None:
    # Discord can render its sidebar while ancillary scripts delay DOMContentLoaded.
    # The directory reader checks the rendered UI before returning any choices.
    await page.goto(url, wait_until="commit", timeout=15000)
    await _wait_briefly(page)


async def _evaluate(page: object, expression: str, argument: object = None) -> dict:
    if argument is None:
        value = await page.evaluate(expression)
    else:
        value = await page.evaluate(expression, argument)
    return value if isinstance(value, dict) else {}


async def _directory_snapshot(page: object, expression: str, argument: object = None,
                              *, require_items: bool = False) -> dict:
    """Allow the SPA a short, bounded mount window before calling it empty."""
    deadline = time.monotonic() + DIRECTORY_READY_WAIT_SECONDS
    latest: dict = {}
    while True:
        latest = await _evaluate(page, expression, argument)
        items = latest.get("guilds") or latest.get("channels") or latest.get("authors")
        ready = latest.get("sidebar_present") or latest.get("chat_present")
        if latest.get("login_required") or (ready and (items or not require_items)):
            return latest
        if time.monotonic() >= deadline:
            return latest
        await _wait_briefly(page, 200)


async def _new_page(context: object) -> object:
    return await context.new_page()


async def _ensure_page(context: object, page: object | None) -> object:
    dedicated = page
    try:
        if dedicated is None:
            dedicated = await _new_page(context)
        else:
            try:
                if dedicated.is_closed():
                    dedicated = await _new_page(context)
            except Exception:
                dedicated = await _new_page(context)
        url = _page_url(dedicated)
        if not url or url == "about:blank" or not url.startswith("https://discord.com/"):
            await _goto(dedicated, DISCOVERY_START_URL)
        elif _login_url(url):
            # A prior request may have left this dedicated tab on the login
            # page.  Revisit the fixed Discord entry point on each new request
            # so a manual sign-in in another existing tab can take effect.
            await _goto(dedicated, DISCOVERY_START_URL)
            if _login_url(_page_url(dedicated)):
                raise _LoginRequired()
        return dedicated
    except asyncio.CancelledError:
        # If cancellation happens before the await in the caller assigns the
        # returned page, this function still owns the newly-created tab.
        if dedicated is not None:
            try:
                if not dedicated.is_closed():
                    await dedicated.close()
            except Exception:
                pass
        raise
    except Exception:
        # Navigation failures can happen before the caller receives the page;
        # close it here so a timed out or rejected login tab is not leaked.
        if dedicated is not None:
            try:
                if not dedicated.is_closed():
                    await dedicated.close()
            except Exception:
                pass
        raise


def _observed_guilds(value: dict) -> list[dict]:
    result = []
    seen: set[str] = set()
    for item in value.get("guilds", []) if isinstance(value.get("guilds"), list) else []:
        if not isinstance(item, dict):
            continue
        identifier = item.get("id")
        if not isinstance(identifier, str) or not SNOWFLAKE.fullmatch(identifier) or identifier in seen:
            continue
        href = item.get("href") if isinstance(item.get("href"), str) else ""
        selector = item.get("selector") if isinstance(item.get("selector"), str) else ""
        if href:
            parsed = urlsplit(href)
            match = re.fullmatch(r"/channels/(\d{15,22})/(?:@home|\d{15,22})/?", parsed.path)
            if (parsed.scheme != "https" or parsed.netloc != "discord.com" or not match
                    or match.group(1) != identifier or parsed.query or parsed.fragment):
                href = ""
        if not href and selector != f"guildsnav___{identifier}":
            selector = ""
        seen.add(identifier)
        result.append({"id": identifier, "name": _clean_name(item.get("name")),
                       "href": href, "selector": selector})
    return result


def _observed_channels(value: dict, guild_id: str) -> list[dict]:
    result = []
    seen: set[str] = set()
    for item in value.get("channels", []) if isinstance(value.get("channels"), list) else []:
        if not isinstance(item, dict):
            continue
        identifier = item.get("id")
        item_guild = item.get("guild_id")
        url = item.get("url")
        if (not isinstance(identifier, str) or not SNOWFLAKE.fullmatch(identifier)
                or item_guild != guild_id or identifier in seen
                or not _channel_url(url, guild_id, identifier)):
            continue
        seen.add(identifier)
        result.append({"id": identifier, "guild_id": guild_id,
                       "name": _clean_name(item.get("name")),
                       "url": _canonical_channel_url(guild_id, identifier)})
    return result


def _observed_authors(value: dict) -> list[dict]:
    result = []
    seen: set[str] = set()
    for item in value.get("authors", []) if isinstance(value.get("authors"), list) else []:
        if not isinstance(item, dict):
            continue
        identifier = item.get("id")
        if not isinstance(identifier, str) or not SNOWFLAKE.fullmatch(identifier) or identifier in seen:
            continue
        seen.add(identifier)
        result.append({"id": identifier, "name": _clean_name(item.get("name"))})
        if len(result) >= MAX_AUTHORS:
            break
    return result


async def _select_guild(page: object, guild: dict) -> None:
    selector = guild.get("selector")
    if not selector and guild.get("href"):
        await _goto(page, guild["href"])
        return
    if not selector:
        raise RuntimeError("selected Discord server has no observed navigation control")
    locator = page.locator(f'[data-list-item-id="{selector}"]')
    await locator.click(timeout=15000)
    await _wait_briefly(page)


async def _collect_channels(page: object, guild_id: str, target_id: str | None = None) -> dict:
    snapshot = await _directory_snapshot(page, EXTRACT_CHANNELS_JS, guild_id, require_items=True)
    channels = {row["id"]: row for row in _observed_channels(snapshot, guild_id)}
    scroll = await _evaluate(page, SCROLL_CHANNELS_JS, {"guild_id": guild_id, "action": None})
    original_top = scroll.get("top")
    if original_top is None:
        raise RuntimeError("Cannot locate the channel scroller; the directory is incomplete.")
    target_top = original_top if target_id in channels else None
    try:
        await _evaluate(page, SCROLL_CHANNELS_JS, {"guild_id": guild_id, "action": 0})
        # Read every viewport until the rendered bottom settles. The worker's
        # request timeout reports failure instead of presenting a truncated list.
        while True:
            await _wait_briefly(page, 500)
            current = await _evaluate(page, EXTRACT_CHANNELS_JS, guild_id)
            if current.get("login_required"):
                raise _LoginRequired()
            rows = _observed_channels(current, guild_id)
            channels.update({row["id"]: row for row in rows})
            scroll = await _evaluate(page, SCROLL_CHANNELS_JS, {"guild_id": guild_id, "action": None})
            if target_id in {row["id"] for row in rows}:
                target_top = scroll.get("top")
            if scroll.get("end"):
                await _wait_briefly(page, 500)
                settled = await _evaluate(page, EXTRACT_CHANNELS_JS, guild_id)
                if settled.get("login_required"):
                    raise _LoginRequired()
                settled_rows = _observed_channels(settled, guild_id)
                channels.update({row["id"]: row for row in settled_rows})
                end_scroll = await _evaluate(page, SCROLL_CHANNELS_JS, {"guild_id": guild_id, "action": None})
                if end_scroll.get("end") and settled_rows == rows:
                    break
                continue
            moved = await _evaluate(page, SCROLL_CHANNELS_JS, {"guild_id": guild_id, "action": "next"})
            if moved.get("top") is None or moved.get("top") == moved.get("before"):
                raise RuntimeError("Channel scrolling stopped before the directory was complete.")
    finally:
        await _evaluate(page, SCROLL_CHANNELS_JS, {"guild_id": guild_id, "action": target_top if target_top is not None else original_top})
        await _wait_briefly(page, 500)
    return {**snapshot, "channels": list(channels.values()), "complete": True}

async def _discover_page(page: object, request: dict) -> dict:
    guild_id = request.get("guild_id")
    channel_id = request.get("channel_id")
    try:
        guild_data = await _directory_snapshot(page, EXTRACT_GUILDS_JS, require_items=True)
    except Exception as exc:
        del exc
        raise RuntimeError("Discord guild sidebar is unavailable or still loading.")
    if guild_data.get("login_required") or _login_url(_page_url(page)):
        raise _LoginRequired()
    guilds = _observed_guilds(guild_data)
    if not guild_data.get("sidebar_present"):
        raise RuntimeError("Discord guild sidebar is unavailable or still loading.")
    if not guild_id:
        return {
            "state": "ready", "request_id": request["request_id"],
            "guild_id": None, "channel_id": None, "guilds": guilds,
            "channels": [], "authors": [],
            "detail": ("The rendered sidebar lists only servers currently visible to this account."
                       if guilds else "No accessible Discord servers are visible in the rendered sidebar."),
        }
    selected_guild = next((item for item in guilds if item["id"] == guild_id), None)
    if selected_guild is None:
        raise RuntimeError("Selected Discord server is not in the discovered server list.")
    await _select_guild(page, selected_guild)
    try:
        channel_data = await _collect_channels(page, guild_id, request.get("channel_id"))
    except Exception as exc:
        del exc
        raise RuntimeError("Discord channel sidebar is unavailable or still loading.")
    if channel_data.get("login_required") or _login_url(_page_url(page)):
        raise _LoginRequired()
    channels = _observed_channels(channel_data, guild_id)
    if not channel_data.get("sidebar_present"):
        raise RuntimeError("Discord channel sidebar is unavailable or still loading.")
    if not channel_id:
        return {
            "state": "ready", "request_id": request["request_id"],
            "guild_id": guild_id, "channel_id": None, "guilds": guilds,
            "channels": channels, "authors": [],
            "detail": ("The rendered sidebar lists only text channels currently visible in the selected server."
                       if channels else "No accessible text channels are visible in the selected server."),
        }
    selected_channel = next((item for item in channels if item["id"] == channel_id), None)
    if selected_channel is None:
        raise RuntimeError("Selected Discord channel is not in the discovered channel list.")
    # The URL came from the rendered channel sidebar and was validated before
    # reaching this point.  Never navigate to a request-supplied URL.
    path = urlsplit(selected_channel["url"]).path
    links = ", ".join(f'{scope} a[href="{href}"]' for scope in
        ('[data-list-id="channels"]', '[data-list-item-id^="channels___"]', 'nav', '[role="tree"]')
        for href in (path, selected_channel["url"]))
    await page.locator(links).first.click(timeout=10000)
    await _wait_briefly(page)
    if _page_url(page).split("?", 1)[0].rstrip("/") != selected_channel["url"]:
        if _login_url(_page_url(page)):
            raise _LoginRequired()
        raise RuntimeError("Discord navigated away from the selected channel.")
    try:
        author_data = await _directory_snapshot(page, EXTRACT_AUTHORS_JS, channel_id)
    except Exception as exc:
        del exc
        raise RuntimeError("Discord messages are unavailable in the selected channel.")
    if author_data.get("login_required") or _login_url(_page_url(page)):
        raise _LoginRequired()
    authors = _observed_authors(author_data)
    if not author_data.get("chat_present"):
        detail = "No visible messages were available; authors are limited to observed messages."
    elif authors:
        detail = "Authors are limited to observed messages in the selected channel."
    else:
        detail = "No authors were observed; authors are limited to observed messages."
    return {
        "state": "ready", "request_id": request["request_id"],
        "guild_id": guild_id, "channel_id": channel_id, "guilds": guilds,
        "channels": channels, "authors": authors, "detail": detail,
    }


async def _process_request(page: object, request: dict) -> dict:
    try:
        return await _discover_page(page, request)
    except _LoginRequired:
        return {
            "state": "login_required", "request_id": request["request_id"],
            "guild_id": request.get("guild_id"), "channel_id": request.get("channel_id"),
            "guilds": [], "channels": [], "authors": [],
            "detail": "Complete Discord sign-in or verification in the browser before discovery.",
        }
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        # Keep exception text out of the result: Playwright errors can include
        # URLs, DOM text, or browser details that do not belong in setup state.
        logging.getLogger(__name__).warning("Discord discovery failed: %s", str(exc).splitlines()[0][:240])
        return {
            "state": "failed", "request_id": request["request_id"],
            "guild_id": request.get("guild_id"), "channel_id": request.get("channel_id"),
            "guilds": [], "channels": [], "authors": [],
            "detail": "Discord discovery could not read the rendered page; retry after the browser is ready.",
        }


async def _prepare_and_process(context: object, page: object | None, request: dict) -> tuple[object, dict]:
    """Create/reuse the page and process it under one caller-owned timeout."""
    dedicated = page
    try:
        dedicated = await _ensure_page(context, dedicated)
        return dedicated, await _process_request(dedicated, request)
    except asyncio.CancelledError:
        # wait_for cancels this coroutine on timeout.  Closing the page here
        # prevents a timed-out navigation from leaking a tab into the shared
        # persistent context.
        if dedicated is not None:
            try:
                if not dedicated.is_closed():
                    await dedicated.close()
            except Exception:
                pass
        raise


async def serve_discovery(context: object, runtime_path: str | os.PathLike[str], *, setup_page=None) -> None:
    """Use the open setup tab, or own a separate tab during active monitoring.

    Discovery never navigates or closes monitoring pages. Only its dedicated
    tab is closed when the worker stops; the borrowed setup tab stays open.
    """
    runtime = _runtime_path(runtime_path)
    request_path = _request_path(runtime)
    result_path = _result_path(runtime)
    page = None
    processed_id: str | None = None
    try:
        while True:
            request = _validated_request(_json_read(request_path))
            if request is not None and request["request_id"] != processed_id:
                result = _json_read(result_path)
                if result and result.get("request_id") == request["request_id"]:
                    processed_id = request["request_id"]
                elif _expired(request):
                    processed_id = request["request_id"]
                    timeout_result = {
                        "state": "failed", "request_id": request["request_id"],
                        "guild_id": request.get("guild_id"), "channel_id": request.get("channel_id"),
                        "guilds": [], "channels": [], "authors": [],
                        "detail": "Discord discovery request timed out; retry discovery.",
                        "completed_at": _utc_now().isoformat(),
                    }
                    current = _validated_request(_json_read(request_path))
                    if current is not None and current["request_id"] == request["request_id"]:
                        _atomic_json_write(result_path, timeout_result)
                else:
                    processed_id = request["request_id"]
                    try:
                        if setup_page is not None:
                            result = await asyncio.wait_for(_process_request(setup_page, request), timeout=REQUEST_TIMEOUT_SECONDS)
                        else:
                            page, result = await asyncio.wait_for(
                                _prepare_and_process(context, page, request),
                                timeout=REQUEST_TIMEOUT_SECONDS,
                            )
                    except asyncio.TimeoutError:
                        result = {
                            "state": "failed", "request_id": request["request_id"],
                            "guild_id": request.get("guild_id"), "channel_id": request.get("channel_id"),
                            "guilds": [], "channels": [], "authors": [],
                            "detail": "Discord discovery request timed out; retry discovery.",
                        }
                    except asyncio.CancelledError:
                        raise
                    except _LoginRequired:
                        result = {
                            "state": "login_required", "request_id": request["request_id"],
                            "guild_id": request.get("guild_id"), "channel_id": request.get("channel_id"),
                            "guilds": [], "channels": [], "authors": [],
                            "detail": "Complete Discord sign-in or verification in the browser before discovery.",
                        }
                    except Exception as exc:
                        logging.getLogger(__name__).warning("Discord discovery failed: %s", str(exc).splitlines()[0][:240])
                        result = {
                            "state": "failed", "request_id": request["request_id"],
                            "guild_id": request.get("guild_id"), "channel_id": request.get("channel_id"),
                            "guilds": [], "channels": [], "authors": [],
                            "detail": "Discord discovery could not read the rendered page; retry after the browser is ready.",
                        }
                    result = _persistable_result(result, completed_at=_utc_now().isoformat())
                    # A request can be replaced after timeout.  Never let a
                    # stale worker overwrite the result for the newer request.
                    current = _validated_request(_json_read(request_path))
                    if current is not None and current["request_id"] == request["request_id"]:
                        _atomic_json_write(result_path, result)
            await asyncio.sleep(DISCOVERY_POLL_SECONDS)
    except asyncio.CancelledError:
        raise
    finally:
        if page is not None:
            try:
                if not page.is_closed():
                    await page.close()
            except Exception:
                pass


__all__ = [
    "DISCOVERY_POLL_SECONDS", "DISCOVERY_START_URL", "EXTRACT_AUTHORS_JS",
    "EXTRACT_CHANNELS_JS", "EXTRACT_GUILDS_JS", "REQUEST_FILENAME",
    "REQUEST_TIMEOUT_SECONDS", "RESULT_FILENAME", "discovery_status",
    "request_discovery", "serve_discovery",
]
