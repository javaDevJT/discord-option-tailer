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
from .discord_auth import DiscordLoginDiagnostics
from .ingest import SNOWFLAKE, normalize
from .pacing import discord_delay
from .status import failure_detail, publish_status


LOG = logging.getLogger(__name__)

# Selectors deliberately stay together so a Discord UI change can be reviewed.
# Only mounted, rendered channel rows are read; no hidden React state is accessed.
AUTH_REQUIRED_JS = r"""() => {
  const visible = el => !!el && !!el.getClientRects().length && getComputedStyle(el).visibility !== 'hidden';
  return [...document.querySelectorAll(`
    input[type="password"], input[autocomplete="one-time-code"],
    [role="dialog"] input[name*="code" i], [role="dialog"] input[id*="code" i],
    form[action*="/login"], form[action*="/verify"], form[action*="/challenge"],
    iframe[src*="hcaptcha.com"], iframe[src*="challenges.cloudflare.com"], iframe[src*="recaptcha"]
  `)].some(el => visible(el) && !el.closest('[data-list-id="chat-messages"], [id^="chat-messages-"], [class*="embed"]'));
}"""

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
  const authControls = (""" + AUTH_REQUIRED_JS + r""")();
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


NAVIGATION_TIMEOUT_MS = 60000
NAVIGATION_PROGRESS_SECONDS = 3


async def requires_manual_auth(page) -> bool | None:
    """None means the document could not be inspected, not a proven challenge."""
    if page.is_closed():
        return False
    parsed = urlsplit(page.url)
    if parsed.scheme != "https" or parsed.netloc != "discord.com":
        return False
    if session_state(page.url) == "login_required":
        page._relay_manual_auth_detected = True
        return True
    try:
        detected = await page.evaluate(AUTH_REQUIRED_JS) is True
        page._relay_manual_auth_detected = detected
        return detected
    except Exception:
        # Preserve a known challenge across a changing document.
        return True if getattr(page, "_relay_manual_auth_detected", False) else None


async def manual_auth_page(context):
    """Wait for sign-in and the rendered account UI before navigating any tab."""
    for page in context.pages:
        if page.is_closed():
            continue
        if await requires_manual_auth(page) is not False:
            return page
        if session_state(page.url) == "connected":
            from .discovery import EXTRACT_GUILDS_JS
            try:
                sidebar = await page.evaluate(EXTRACT_GUILDS_JS)
                if (not isinstance(sidebar, dict) or sidebar.get("login_required")
                        or sidebar.get("sidebar_present") is not True or not sidebar.get("guilds")):
                    return page
            except Exception:
                return page
    return None


async def _navigate_with_progress(page, url: str, *, on_progress=None) -> None:
    """Navigate with bounded waits while keeping cancellation cleanup explicit."""
    navigation = asyncio.create_task(page.goto(url, wait_until="domcontentloaded", timeout=NAVIGATION_TIMEOUT_MS))
    try:
        while not navigation.done():
            await asyncio.wait({navigation}, timeout=NAVIGATION_PROGRESS_SECONDS)
            if not navigation.done() and on_progress is not None:
                try:
                    on_progress()
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    LOG.warning("Discord status publication unavailable (%s)", type(exc).__name__)
        await navigation
    except asyncio.CancelledError:
        navigation.cancel()
        await asyncio.gather(navigation, return_exceptions=True)
        raise


async def login(profile_dir: str, *, keep_open=False, on_status=None, discovery_runtime_path=None) -> None:
    """Open Discord for manual login and persist the dedicated browser session."""
    publish_status(on_status, "discord", "starting", detail="Opening the saved Discord browser. Manual sign-in has no time limit.")
    async with _playwright()() as playwright:
        context = await playwright.chromium.launch_persistent_context(
            _profile(profile_dir), headless=False, accept_downloads=False,
        )
        discovery_task = None
        diagnostics = DiscordLoginDiagnostics(context)
        try:
            page = await manual_auth_page(context)
            if page is None:
                page = context.pages[0] if context.pages else await context.new_page()
            navigation_problem = ""
            try:
                if session_state(page.url) != "connected" and await manual_auth_page(context) is None:
                    await _navigate_with_progress(
                        page,
                        "https://discord.com/login",
                        on_progress=lambda: publish_status(
                            on_status,
                            "discord",
                            "starting",
                            detail="Discord is still loading. The browser will stay open; manual sign-in has no time limit.",
                        ),
                    )
            except Exception as exc:
                if page.is_closed():
                    raise
                navigation_problem = failure_detail(exc, provider="Discord", phase="page navigation")
                if "[timeout]" in navigation_problem:
                    navigation_problem = "Discord's initial page navigation exceeded 60 seconds [timeout]. The browser is still open. Finish sign-in there, or reload the page if it remains blank; manual sign-in has no time limit."
                publish_status(on_status, "discord", "reconnecting", detail=navigation_problem)
            if discovery_runtime_path:
                from .discovery import serve_discovery
                discovery_task = asyncio.create_task(serve_discovery(context, discovery_runtime_path, setup_page=page))
            LOG.warning("Sign in manually in the browser, including any MFA. Waiting for Discord's channel view.")
            while not page.is_closed():
                state = session_state(page.url)
                pending = await manual_auth_page(context)
                if pending is not None:
                    state = "login_required" if await requires_manual_auth(pending) is True else "reconnecting"
                if state == "connected":
                    diagnostics.clear()
                    detail = "Discord sign-in detected. Return to Setup and save both channel URLs to start monitoring."
                elif state == "login_required":
                    detail = "Complete Discord sign-in and any verification in the browser; close unused sign-in tabs. Manual sign-in has no time limit."
                else:
                    state = "reconnecting"
                    detail = navigation_problem or "Discord is still loading or redirecting. The browser will stay open; finish sign-in when it appears."
                publish_status(on_status, "discord", state, detail=diagnostics.detail(detail))
                if state == "connected" and not keep_open:
                    LOG.info("Discord channel view reached; session saved to the dedicated profile")
                    break
                await asyncio.sleep(discord_delay(3))
            if page.is_closed():
                publish_status(on_status, "discord", "needs_attention", detail="The Discord browser window was closed. Use Reconnect to reopen the saved session.")
        finally:
            await diagnostics.close()
            if discovery_task is not None:
                discovery_task.cancel()
                await asyncio.gather(discovery_task, return_exceptions=True)
            try:
                await context.close()
            except Exception as exc:
                LOG.warning("Discord browser context cleanup failed (%s)", type(exc).__name__)


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

    def status(channel, state, detail):
        publish_status(on_status, "discord", state, detail=detail, channel_id=str(channel["id"]))

    async with _playwright()() as playwright:
        context = await playwright.chromium.launch_persistent_context(
            profile, headless=False, accept_downloads=False,
        )
        discovery_task = None
        diagnostics = DiscordLoginDiagnostics(context)
        try:
            pages = list(context.pages)
            while len(pages) < 2:
                pages.append(await context.new_page())
            # Restored extra tabs may contain an in-progress manual challenge.
            pages = pages[:2]
            urls = [f"https://discord.com/channels/{c['guild_id']}/{c['id']}" for c in channels]
            for channel, page, url in zip(channels, pages, urls):
                if await manual_auth_page(context) is not None:
                    break
                if _channel_url_matches(page.url, channel):
                    continue
                try:
                    await _navigate_with_progress(
                        page,
                        url,
                        on_progress=lambda channel=channel: status(
                            channel,
                            "starting",
                            "Discord is still loading the configured channel. Monitoring will resume when it appears.",
                        ),
                    )
                except Exception as exc:
                    LOG.warning("Discord navigation unavailable; the browser will reconnect")
                    status(channel, "reconnecting", failure_detail(exc, provider="Discord", phase="channel navigation"))

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
            next_recovery: dict[str, float] = {}

            while True:
                pending = await manual_auth_page(context)
                if pending is not None:
                    auth_pending = await requires_manual_auth(pending) is True
                    detail = ("Discord sign-in or CAPTCHA is pending. Automatic navigation is paused in all tabs; finish verification in Browser login and close unused sign-in tabs."
                              if auth_pending else "Discord's signed-in interface is still loading. Automatic navigation is paused until it appears; the browser will stay open.")
                    for channel in channels:
                        trackers[str(channel["id"])].reset()
                        unavailable.add(str(channel["id"]))
                        status(channel, "login_required" if auth_pending else "reconnecting", diagnostics.detail(detail))
                    await asyncio.sleep(discord_delay(poll_seconds))
                    continue
                all_channels_ready = True
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
                            all_channels_ready = False
                            tracker.reset()
                            unavailable.add(channel_id)
                            status(channel, state, diagnostics.detail("Discord requires login or verification in Browser login."))
                            continue
                        snapshot = await page.evaluate(EXTRACT_MESSAGES_JS, channel_id)
                        if snapshot.get("auth_required"):
                            raise ValueError("Manual Discord verification is pending")
                        if not _channel_url_matches(page.url, channel):
                            if await manual_auth_page(context) is not None:
                                all_channels_ready = False
                                break
                            if now >= next_recovery.get(channel_id, -1):
                                next_recovery[channel_id] = now + discord_delay(30)
                                tracker.reset()
                                await _navigate_with_progress(
                                    page,
                                    urls[index],
                                    on_progress=lambda channel=channel: status(
                                        channel,
                                        "reconnecting",
                                        "Discord is still loading the configured channel. Monitoring will resume when it appears.",
                                    ),
                                )
                            raise ValueError("Restoring the configured channel after navigation or login")
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
                            all_channels_ready = False
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
                        all_channels_ready = False
                        tracker.reset()
                        if channel_id not in unavailable:
                            LOG.warning("Channel %s unavailable (%s); messages may be missed", channel_id, exc)
                        unavailable.add(channel_id)
                        state = "login_required" if snapshot.get("auth_required") else session_state(page.url, channel)
                        if state == "login_required":
                            detail = "Discord requires sign-in or verification. Complete it in Browser login; manual sign-in has no time limit."
                        elif isinstance(exc, ValueError):
                            detail = "Discord's channel message list is not ready. Waiting without reloading the page. Finish any verification; if the page stays blank, reload it manually or use Reconnect."
                        else:
                            detail = failure_detail(exc, provider="Discord", phase="channel observation")
                        status(channel, "login_required" if state == "login_required" else "reconnecting", diagnostics.detail(detail))
                        if state == "login_required":
                            break
                        continue
                    # Callback failure propagates and stops the monitor; never blindly replay a money action.
                    for message in events:
                        message["browser_connection_epoch"] = snapshot.get("connection_epoch", "")
                        await on_message(message)
                if all_channels_ready and await manual_auth_page(context) is None:
                    diagnostics.clear()
                await asyncio.sleep(discord_delay(poll_seconds))
        finally:
            await diagnostics.close()
            if discovery_task is not None:
                discovery_task.cancel()
                await asyncio.gather(discovery_task, return_exceptions=True)
            try:
                await context.close()
            except Exception as exc:
                LOG.warning("Discord browser context cleanup failed (%s)", type(exc).__name__)
