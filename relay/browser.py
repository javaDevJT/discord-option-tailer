"""Read only Discord's rendered UI in a manually authenticated user profile.

No user token, Discord API client, network interception, or message sending.
DOM observation is best effort: history gaps and selector changes fail closed.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import logging
from pathlib import Path
import re
import time
from urllib.parse import urlsplit

from .core import channel_allows_author, instant
from .ingest import SNOWFLAKE, normalize


LOG = logging.getLogger(__name__)

# Selectors deliberately stay together so a Discord UI change can be reviewed.
# Only mounted, rendered channel rows are read; no hidden React state is accessed.
EXTRACT_MESSAGES_JS = r"""(expectedChannelId = null) => {
  if (!window.__discordRelayConnection) {
    window.__discordRelayConnection = {epoch: 0};
    for (const event of ['online', 'offline']) {
      window.addEventListener(event, () => window.__discordRelayConnection.epoch++);
    }
  }
  const connectionEpoch = String(performance.timeOrigin) + ':' + window.__discordRelayConnection.epoch;
  const connecting = [...document.querySelectorAll('[role="alert"], [role="status"]')]
    .some(el => el.getClientRects().length && /reconnecting|connecting to discord|connection lost|disconnected from discord|trying to reconnect/i.test(el.innerText));
  const observation = {url: location.href, connection_epoch: connectionEpoch};
  const visible = el => !!el && !!el.getClientRects().length && getComputedStyle(el).visibility !== 'hidden';
  const authControls = [...document.querySelectorAll(`
    input[type="password"], input[autocomplete="one-time-code"],
    [role="dialog"] input[name*="code" i], [role="dialog"] input[id*="code" i],
    form[action*="/login"], form[action*="/verify"], form[action*="/challenge"],
    iframe[src*="hcaptcha.com"], iframe[src*="challenges.cloudflare.com"], iframe[src*="recaptcha"]
  `)].some(el => visible(el) && !el.closest('[data-list-id="chat-messages"], [id^="chat-messages-"], [class*="embed"]'));
  if (authControls) return {...observation, ready: false, auth_required: true, at_bottom: false, messages: []};
  if (!navigator.onLine || connecting) return {...observation, ready: false, auth_required: false, at_bottom: false, messages: []};
  const list = document.querySelector('[data-list-id="chat-messages"]');
  if (!list) return {...observation, ready: false, auth_required: false, at_bottom: false, messages: []};
  const loading = !!list.closest('[aria-busy="true"]') || [...list.querySelectorAll('[aria-busy="true"], [role="progressbar"]')].some(visible);
  const rawRows = [...list.querySelectorAll('[id^="chat-messages-"]')].filter(visible);
  const mountedRows = [...list.querySelectorAll('[id^="chat-messages-"]')]
    .filter(row => row.getClientRects().length && /^chat-messages-\d+-\d+$/.test(row.id));
  const unparseableRows = rawRows.some(row => !/^chat-messages-\d+-\d+$/.test(row.id));
  const foreignRows = expectedChannelId === null ? [] : mountedRows.filter(row => row.id.split('-')[2] !== String(expectedChannelId));
  const rows = mountedRows.filter(row => !foreignRows.includes(row));
  let scroller = list;
  while (scroller && !(scroller.clientHeight > 0 &&
         ['auto', 'scroll'].includes(getComputedStyle(scroller).overflowY))) {
    scroller = scroller.parentElement;
  }
  const atBottom = !!scroller && scroller.scrollHeight - scroller.scrollTop - scroller.clientHeight < 80;
  const own = (row, selector) => [...row.querySelectorAll(selector)].filter(el =>
    !el.closest('[class*="repliedMessage"]') && el.closest('[id^="chat-messages-"]') === row);
  const authorHeader = element => {
    if (!element || element.closest('[id^="message-content-"], [id^="message-accessories-"], [class*="repliedMessage"], article, [class*="embed"]')) return null;
    const heading = element.closest('h3');
    const authorRow = heading && heading.closest('[id^="chat-messages-"]');
    if (!heading || !authorRow || (expectedChannelId !== null && authorRow.id.split('-')[2] !== String(expectedChannelId))) return null;
    return {element, heading};
  };
  const messages = [];
  for (const row of rows) {
    const id = row.id.split('-').pop();
    const content = document.getElementById('message-content-' + id);
    const stamp = document.getElementById('message-timestamp-' + id);
    const time = (stamp && (stamp.matches('time') ? stamp : stamp.querySelector('time'))) || own(row, 'time')[0];
    if (!time || !time.getAttribute('datetime')) continue;
    let author = own(row, '[id^="message-username-"]').map(authorHeader).find(Boolean);
    if (!author) {
      const labels = row.getAttribute('aria-labelledby') || (row.firstElementChild && row.firstElementChild.getAttribute('aria-labelledby')) || '';
      const label = labels.split(/\s+/)
        .find(value => value.startsWith('message-username-'));
      author = authorHeader(label && document.getElementById(label));
    }
    const username = author && author.element;
    // Actual Discord avatar is a direct sibling of the message's heading.
    // Never use an arbitrary descendant: embeds may contain forged avatar URLs.
    const avatar = author && [...author.heading.parentElement.children]
      .find(element => element.matches('img[class*="avatar"]'));
    const avatarUrl = avatar && (avatar.currentSrc || avatar.src) || '';
    const authorMatch = avatarUrl.match(/\/avatars\/(\d{15,22})\//) ||
      avatarUrl.match(/\/guilds\/\d+\/users\/(\d{15,22})\/avatars\//);
    const authorId = authorMatch ? authorMatch[1] : '';
    const reply = row.querySelector('[class*="repliedMessage"] a[href*="/channels/"]');
    const replyMatch = reply && reply.href.match(/\/channels\/\d+\/\d+\/(\d{15,22})(?:$|[?#])/);
    const attachments = [];
    const urls = new Map();
    for (const element of own(row, 'a[href*="/attachments/"], img[src*="/attachments/"]')) {
      const url = element.href || element.currentSrc || element.src;
      if (!url) continue;
      const image = element.matches('img') ? element : element.querySelector('img');
      let filename = '';
      let identity = url;
      try {
        const parsed = new URL(url);
        filename = decodeURIComponent(parsed.pathname.split('/').pop());
        if (['cdn.discordapp.com', 'media.discordapp.net'].includes(parsed.hostname)) identity = parsed.pathname;
      } catch (_) {}
      const proxy = image && (image.currentSrc || image.src);
      if (urls.has(identity)) {
        if (proxy && proxy !== urls.get(identity).url) urls.get(identity).proxy_url = proxy;
        continue;
      }
      const attachment = {url, filename, description: (image && image.alt) || element.textContent || ''};
      if (proxy && proxy !== url) attachment.proxy_url = proxy;
      attachments.push(attachment);
      urls.set(identity, attachment);
    }
    const embeds = own(row, 'article[class*="embed"], [class*="embedFull"]')
      .filter(el => !el.parentElement.closest('article[class*="embed"], [class*="embedFull"]'))
      .map(el => {
        const image = el.querySelector('[class*="embedImage"] img, [class*="embedThumbnail"] img');
        return {description: el.innerText || '', ...(image ? {image: {url: image.currentSrc || image.src}} : {})};
      });
    messages.push({id, channel_id: row.id.split('-')[2], author_id: authorId, author_name: username ? username.textContent : '',
      content: content ? content.innerText : '', timestamp: time.getAttribute('datetime'),
      reply_to: replyMatch ? replyMatch[1] : null, attachments, embeds, source: 'browser'});
  }
  const emptyReady = rawRows.length === 0 && !loading;
  const parseReady = emptyReady || (!loading && !unparseableRows && messages.length === rows.length);
  return {...observation, ready: foreignRows.length === 0 && parseReady, empty_ready: emptyReady,
    auth_required: false, loading, unparseable_rows: unparseableRows, at_bottom: atBottom,
    messages, foreign_rows: foreignRows.length};
}"""


def _channel_url_matches(url: str, channel: dict) -> bool:
    parsed = urlsplit(url)
    return (parsed.scheme == "https" and parsed.netloc == "discord.com"
            and parsed.path.rstrip("/") == f"/channels/{channel['guild_id']}/{channel['id']}")


def snapshot_matches(snapshot: dict, message: dict, channel: dict) -> bool:
    """Require the exact still-visible, unsuperseded alert in the current channel."""
    if (not snapshot.get("ready") or not snapshot.get("at_bottom") or snapshot.get("foreign_rows")
            or not _channel_url_matches(snapshot.get("url", ""), channel)
            or message.get("source") != "browser" or message.get("ingestion") != "live"
            or str(message.get("channel_id")) != str(channel["id"])
            or not message.get("browser_connection_epoch")
            or snapshot.get("connection_epoch") != message.get("browser_connection_epoch")):
        return False
    if not channel_allows_author(channel, message.get("author_id")):
        return False
    try:
        rows = [normalize(row) for row in snapshot.get("messages", [])]
        if any(row["channel_id"] != str(channel["id"]) for row in rows):
            return False
        target = next((row for row in rows if row["id"] == message["id"]), None)
        if (target is None or target["revision"] != message["revision"]
                or target["author_id"] != message["author_id"]):
            return False
        return not any(channel_allows_author(channel, row["author_id"]) and int(row["id"]) > int(message["id"])
                       for row in rows)
    except (TypeError, ValueError, KeyError):
        return False


class SnapshotTracker:
    """Emit new IDs once, keeping startup, edits, gaps and older history contextual."""

    def __init__(self, channel_id: str, *, clock=None):
        self.channel_id = channel_id
        self.high_water = 0
        self.seen: dict[str, str] = {}
        self.transport_seen: dict[str, str] = {}
        self.needs_baseline = True
        self.started = False
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.empty_cutoff = None

    def reset(self) -> None:
        self.needs_baseline = True
        self.empty_cutoff = None

    def observe(self, rows: list[dict], *, force_baseline: bool = False, empty_ready: bool = False) -> list[dict]:
        messages = sorted((normalize(row, self.channel_id) for row in rows),
                          key=lambda m: int(m["id"]))
        if any(message["channel_id"] != self.channel_id for message in messages):
            raise ValueError("Snapshot contains a message from a different channel")
        if not messages:
            if empty_ready and not self.started and not force_baseline:
                self.needs_baseline = False
            if empty_ready:
                self.empty_cutoff = self.clock()
            self.started = True
            return []
        overlap = any(message["id"] in self.seen for message in messages)
        gap = bool(self.seen) and not overlap
        baseline = self.needs_baseline or force_baseline or gap
        if gap:
            LOG.warning("Channel %s has no overlap with the previous snapshot; possible gap, context only",
                        self.channel_id)
        events = []
        previous_high_water = self.high_water
        for message in messages:
            old_revision = self.seen.get(message["id"])
            if old_revision == message["revision"]:
                if self.transport_seen.get(message["id"]) != message["transport_revision"]:
                    message.update(ingestion="refresh", ingestion_reason="image_url_refresh")
                    events.append(message)
                    self.transport_seen[message["id"]] = message["transport_revision"]
                continue
            before_empty = self.empty_cutoff is not None and instant(message["timestamp"]) <= self.empty_cutoff
            contextual = baseline or before_empty or old_revision is not None or int(message["id"]) <= previous_high_water
            message["ingestion"] = "baseline" if contextual else "live"
            message["ingestion_reason"] = ("history" if force_baseline else "edit" if old_revision is not None
                                           else "baseline" if baseline or before_empty
                                           else "backscroll" if int(message["id"]) <= previous_high_water else "live")
            events.append(message)
            self.seen[message["id"]] = message["revision"]
            self.transport_seen[message["id"]] = message["transport_revision"]
        self.high_water = max(self.high_water, *(int(m["id"]) for m in messages))
        self.needs_baseline = False
        self.started = True
        if self.empty_cutoff is not None and any(
            instant(message["timestamp"]) > self.empty_cutoff for message in messages
        ):
            self.empty_cutoff = None
        # ponytail: bounded DOM cache; SQLite downstream owns durable deduplication.
        if len(self.seen) > 2000:
            self.seen = dict(sorted(self.seen.items(), key=lambda item: int(item[0]))[-1000:])
            self.transport_seen = {key: value for key, value in self.transport_seen.items() if key in self.seen}
        return events


def _playwright():
    try:
        from playwright.async_api import async_playwright
    except ImportError as exc:
        raise RuntimeError("Install playwright, then run: python -m playwright install chromium") from exc
    return async_playwright


def _profile(profile_dir: str) -> str:
    path = Path(profile_dir).expanduser().resolve()
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    path.chmod(0o700)
    return str(path)


def session_state(url: str, channel: dict | None = None) -> str:
    """Classify navigation without reading Discord credentials or account internals."""
    parsed = urlsplit(url)
    if parsed.scheme != "https" or parsed.netloc != "discord.com":
        return "reconnecting"
    if parsed.path.startswith(("/login", "/verify", "/register", "/challenge")):
        return "login_required"
    if channel is not None and _channel_url_matches(url, channel):
        return "connected"
    if parsed.path.startswith("/channels/"):
        return "connected" if channel is None else "restore_channel"
    return "needs_attention"


async def login(profile_dir: str, *, keep_open=False, on_status=None, discovery_runtime_path=None) -> None:
    """Open Discord for manual login and persist the dedicated browser session."""
    async with _playwright()() as playwright:
        context = await playwright.chromium.launch_persistent_context(
            _profile(profile_dir), headless=False, accept_downloads=False,
        )
        discovery_task = None
        try:
            page = context.pages[0] if context.pages else await context.new_page()
            await page.goto("https://discord.com/login", wait_until="domcontentloaded")
            if discovery_runtime_path:
                from .discovery import serve_discovery
                discovery_task = asyncio.create_task(serve_discovery(context, discovery_runtime_path, setup_page=page))
            LOG.warning("Sign in manually in the browser, including any MFA. Waiting for Discord's channel view.")
            if keep_open:
                while not page.is_closed():
                    if on_status:
                        state = session_state(page.url)
                        detail = ("Discord sign-in detected. Return to Setup and save both channel URLs to start monitoring."
                                  if state == "connected" else "Complete Discord sign-in and any verification in the browser.")
                        on_status({"component": "discord", "state": state, "detail": detail})
                    await asyncio.sleep(3)
            else:
                await page.wait_for_url(re.compile(r"https://discord\.com/channels/"), timeout=0)
                LOG.info("Discord channel view reached; session saved to the dedicated profile")
        finally:
            if discovery_task is not None:
                discovery_task.cancel()
                await asyncio.gather(discovery_task, return_exceptions=True)
            await context.close()


async def monitor(config: dict, on_message, register_verifier=None, on_status=None) -> None:
    """Poll two configured tabs; callbacks are awaited serially without retrying orders."""
    channels = config["channels"]
    if len(channels) != 2 or len({str(c["id"]) for c in channels}) != 2:
        raise ValueError("Configure exactly two distinct Discord channels")
    for channel in channels:
        if not all(SNOWFLAKE.fullmatch(str(channel[key])) for key in ("id", "guild_id")):
            raise ValueError("Channel and guild IDs must be Discord snowflakes")
    browser_config = config["browser"]
    poll_seconds = float(browser_config.get("poll_seconds", 3))
    if not 2 <= poll_seconds <= 60:
        raise ValueError("browser.poll_seconds must be between 2 and 60")
    trackers = {str(c["id"]): SnapshotTracker(str(c["id"])) for c in channels}
    profile = _profile(browser_config["profile_dir"])
    async with _playwright()() as playwright:
        context = await playwright.chromium.launch_persistent_context(
            profile, headless=False, accept_downloads=False,
        )
        discovery_task = None
        try:
            pages = list(context.pages)
            while len(pages) < 2:
                pages.append(await context.new_page())
            for page in pages[2:]:
                await page.close()
            pages = pages[:2]
            urls = [f"https://discord.com/channels/{c['guild_id']}/{c['id']}" for c in channels]
            for page, url in zip(pages, urls):
                try:
                    await page.goto(url, wait_until="domcontentloaded", timeout=30000)
                except Exception:
                    LOG.warning("Discord navigation unavailable; the browser will reconnect")

            async def verify(message: dict) -> bool:
                try:
                    index = next(i for i, channel in enumerate(channels)
                                 if str(channel["id"]) == str(message.get("channel_id")))
                    channel, page = channels[index], pages[index]
                    if page.is_closed() or not _channel_url_matches(page.url, channel):
                        return False
                    snapshot = await page.evaluate(EXTRACT_MESSAGES_JS, str(channel["id"]))
                    return (_channel_url_matches(page.url, channel)
                            and snapshot_matches(snapshot, message, channel))
                except Exception:
                    return False

            if register_verifier is not None:
                register_verifier(verify)
            if config.get("runtime_status_file"):
                from .discovery import serve_discovery
                discovery_task = asyncio.create_task(serve_discovery(context, config["runtime_status_file"]))
            last_success: dict[str, float] = {}
            connection_epochs: dict[str, str] = {}
            unavailable: set[str] = set()
            last_recovery: dict[str, float] = {}

            def status(channel, state, detail):
                if on_status:
                    on_status({"component": "discord", "channel_id": str(channel["id"]),
                               "state": state, "detail": detail})

            while True:
                for index, channel in enumerate(channels):
                    channel_id = str(channel["id"])
                    tracker = trackers[channel_id]
                    page = pages[index]
                    if page.is_closed():
                        tracker.reset()
                        pages[index] = page = await context.new_page()
                        status(channel, "reconnecting", "Restoring the channel tab; existing messages remain context only.")
                    now = time.monotonic()
                    if channel_id in last_success and now - last_success[channel_id] > max(30, poll_seconds * 5):
                        tracker.reset()
                        LOG.warning("Channel %s observation paused; next snapshot is context only", channel_id)
                    snapshot = {}
                    try:
                        state = session_state(page.url, channel)
                        if state == "login_required":
                            tracker.reset()
                            unavailable.add(channel_id)
                            status(channel, state, "Discord requires login or verification in Browser login.")
                            continue
                        if not _channel_url_matches(page.url, channel):
                            if now - last_recovery.get(channel_id, -60) >= 30:
                                last_recovery[channel_id] = now
                                tracker.reset()
                                await page.goto(urls[index], wait_until="domcontentloaded", timeout=30000)
                            raise ValueError("Restoring the configured channel after navigation or login")
                        snapshot = await page.evaluate(EXTRACT_MESSAGES_JS, channel_id)
                        if not _channel_url_matches(snapshot.get("url", ""), channel) or not _channel_url_matches(page.url, channel):
                            raise ValueError("Channel changed during observation")
                        if not snapshot.get("ready") or (not snapshot["messages"] and not snapshot.get("empty_ready")):
                            raise ValueError("No message rows; login, channel access or DOM selectors need attention")
                        epoch = snapshot.get("connection_epoch", "")
                        if channel_id in connection_epochs and connection_epochs[channel_id] != epoch:
                            tracker.reset()
                            LOG.warning("Channel %s browser connection changed; next snapshot is context only", channel_id)
                        connection_epochs[channel_id] = epoch
                        if not snapshot["at_bottom"]:
                            tracker.reset()
                            if channel_id not in unavailable:
                                LOG.warning("Channel %s is scrolled into history; context only until the channel bottom is restored",
                                            channel_id)
                            unavailable.add(channel_id)
                            status(channel, "needs_attention", "Scroll the channel to its newest messages to resume.")
                        elif channel_id in unavailable:
                            LOG.warning("Channel %s resumed; establishing a context-only baseline", channel_id)
                            tracker.reset()
                            unavailable.discard(channel_id)
                        events = tracker.observe(
                            snapshot["messages"],
                            force_baseline=not snapshot["at_bottom"],
                            empty_ready=snapshot.get("empty_ready", False),
                        )
                        last_success[channel_id] = now
                        if snapshot["at_bottom"]:
                            status(channel, "connected", "Reading current messages; Discord manages this browser session.")
                    except Exception as exc:
                        tracker.reset()
                        if channel_id not in unavailable:
                            LOG.warning("Channel %s unavailable (%s); messages may be missed", channel_id, exc)
                        unavailable.add(channel_id)
                        state = "login_required" if snapshot.get("auth_required") else session_state(page.url, channel)
                        status(channel, "login_required" if state == "login_required" else "reconnecting",
                               "Discord requires reauthentication." if state == "login_required" else "Waiting for the channel connection or visible messages.")
                        if state != "login_required" and now - last_recovery.get(channel_id, -60) >= 30:
                            last_recovery[channel_id] = now
                            try:
                                await page.goto(urls[index], wait_until="domcontentloaded", timeout=30000)
                            except Exception:
                                pass
                        continue
                    # Callback failure propagates and stops the monitor; never blindly replay a money action.
                    for message in events:
                        message["browser_connection_epoch"] = snapshot.get("connection_epoch", "")
                        await on_message(message)
                await asyncio.sleep(poll_seconds)
        finally:
            if discovery_task is not None:
                discovery_task.cancel()
                await asyncio.gather(discovery_task, return_exceptions=True)
            await context.close()
