"""Offline gateway transport checks with a small discord.py-self-shaped fake."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from types import ModuleType, SimpleNamespace
import sys
import time
import unittest
from unittest.mock import patch

from relay import gateway
from relay.ingest import normalize as ingest_normalize


GUILD = "100000000000000001"
CHANNEL_SIGNALS = "200000000000000001"
CHANNEL_CONTEXT = "200000000000000002"
AUTHOR = "300000000000000001"
OTHER_AUTHOR = "300000000000000002"


def _when(second: int) -> datetime:
    return datetime(2026, 9, 16, 12, 0, second, tzinfo=timezone.utc)


class FakeMessage:
    def __init__(
        self,
        identifier: str,
        channel: "FakeChannel",
        *,
        author_id: str = AUTHOR,
        content: str = "OPEN TSLA",
        second: int = 0,
        edited_at: datetime | None = None,
    ) -> None:
        self.id = identifier
        self.channel = channel
        self.author = SimpleNamespace(id=author_id, display_name="Analyst")
        self.content = content
        self.created_at = _when(second)
        self.edited_at = edited_at
        self.reference = None
        self.attachments = [
            SimpleNamespace(
                id="400000000000000001",
                filename="chart.png",
                url="https://cdn.discord.com/chart.png",
                proxy_url="https://media.discord.com/chart.png",
                size=10,
                content_type="image/png",
                width=10,
                height=10,
                description="chart",
            )
        ]
        self.embeds = [SimpleNamespace(to_dict=lambda: {"title": "TSLA"})]


class FakeChannel:
    def __init__(self, identifier: str, history_items: list[FakeMessage] | None = None) -> None:
        self.id = identifier
        self.name = "signals" if identifier == CHANNEL_SIGNALS else "context"
        self.guild_id = GUILD
        self.members = [SimpleNamespace(id=AUTHOR, display_name="Analyst")]
        self._history_items = list(history_items or [])
        self.history_calls: list[dict[str, object]] = []

    def history(self, *, limit: int, oldest_first: bool):
        self.history_calls.append({"limit": limit, "oldest_first": oldest_first})

        async def rows():
            for item in self._history_items:
                yield item

        return rows()


class FakeGuild:
    def __init__(self, channels: list[FakeChannel]) -> None:
        self.id = GUILD
        self.name = "Relay Guild"
        self.channels = channels
        self.members = [SimpleNamespace(id=AUTHOR, display_name="Analyst")]


class FakeClient:
    instances: list["FakeClient"] = []

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.callbacks: dict[str, object] = {}
        self.channels: dict[str, FakeChannel] = {}
        self.guilds: list[FakeGuild] = []
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.closed = False
        type(self).instances.append(self)

    def event(self, callback):
        self.callbacks[callback.__name__] = callback
        return callback

    def get_channel(self, identifier):
        return self.channels.get(str(identifier))

    async def start(self, token, reconnect=True):
        self.token = token
        self.reconnect = reconnect
        await self.callbacks["on_socket_raw_receive"]('{"op":11,"d":null}')
        await self.callbacks["on_ready"]()
        self.started.set()
        await self.release.wait()

    async def close(self):
        self.closed = True
        self.release.set()

    def is_ready(self):
        return self.started.is_set()

    def is_closed(self):
        return self.closed


class FakeIntents:
    @classmethod
    def none(cls):
        return cls()

    guilds = False
    messages = False
    message_content = False
    members = False
    presences = False
    typing = False


class FakeMemberCacheFlags:
    @classmethod
    def none(cls):
        return cls()


def _discord_module() -> ModuleType:
    module = ModuleType("discord")
    module.Client = FakeClient
    module.MemberCacheFlags = FakeMemberCacheFlags
    return module


def _config() -> dict:
    return {
        "discord": {
            "transport": "gateway",
            "history_spacing_seconds": 0,
            "cache_messages": 64,
        },
        "channels": [
            {
                "id": CHANNEL_SIGNALS,
                "guild_id": GUILD,
                "authors": [AUTHOR],
                "source_group": "primary",
                "role": "signals",
            },
            {
                "id": CHANNEL_CONTEXT,
                "guild_id": GUILD,
                "authors": [AUTHOR],
                "source_group": "primary",
                "role": "context",
            },
        ],
        "llm": {"context_messages": 150},
        "risk": {"max_pending_messages": 8},
    }


def _normalizer(raw: dict, channel_id: str | None = None) -> dict:
    """A local stand-in until the root adapter adds source=gateway support."""

    result = dict(raw)
    result["channel_id"] = str(channel_id or result["channel_id"])
    result["source"] = "gateway"
    result["revision"] = f"revision:{result['id']}:{result['content']}:{result.get('edited_timestamp')}"
    result["transport_revision"] = result["revision"]
    return result


class GatewayTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        FakeClient.instances.clear()
        self.discord_patch = patch.dict(sys.modules, {"discord": _discord_module()})
        self.discord_patch.start()
        self.session_module = ModuleType("relay.discord_session")
        self.session_module.read_token = lambda config: "personal-token"
        self.session_module.credential_configured = lambda config: True
        self.session_patch = patch.dict(sys.modules, {"relay.discord_session": self.session_module})
        self.session_patch.start()

    async def asyncTearDown(self):
        self.session_patch.stop()
        self.discord_patch.stop()

    def _ready_runtime(self):
        normalizer_patch = patch.object(gateway, "normalize", ingest_normalize)
        normalizer_patch.start()
        self.addCleanup(normalizer_patch.stop)
        runtime = gateway._GatewayRuntime(_config(), None, None, None)
        runtime.healthy = True
        runtime.epoch = "gateway:fixture"
        runtime.client = FakeClient()
        runtime.client.started.set()
        runtime.last_gateway_ack = time.monotonic()
        return runtime

    def _signal_message(self, identifier, description, *, edited_at=None, attachments=None):
        message = FakeMessage(
            identifier,
            FakeChannel(CHANNEL_SIGNALS),
            content="OPEN QQQ",
            edited_at=edited_at,
        )
        message.embeds = [SimpleNamespace(to_dict=lambda: {"description": description})]
        if attachments is not None:
            message.attachments = attachments
        return message

    async def _live_signal(self, runtime, message):
        await runtime.observe(message)
        original = await runtime.queue.get()
        runtime.queue.task_done()
        self.assertEqual(original["ingestion"], "live")
        self.assertTrue(await runtime.verify(original))
        return original

    def _attachment(self, token):
        return SimpleNamespace(
            id="400000000000000001",
            filename="chart.png",
            url=f"https://cdn.discordapp.com/attachments/123/456/chart.png?ex={token}&hm={token}",
            proxy_url=f"https://media.discordapp.net/attachments/123/456/chart.png?ex={token}&hm={token}",
            size=10,
            content_type="image/png",
            width=10,
            height=10,
            description="chart",
        )

    async def test_cached_raw_and_full_noop_embed_update_preserve_live_signal(self):
        runtime = self._ready_runtime()
        description = "ENTRY QQQ 738C @ 1.00"
        message = self._signal_message("500000000000000051", description)
        original = await self._live_signal(runtime, message)
        key = (CHANNEL_SIGNALS, original["id"])

        await runtime.edit_raw(SimpleNamespace(
            channel_id=CHANNEL_SIGNALS,
            message_id=original["id"],
            cached_message=message,
            data={"embeds": [{"description": description}]},
        ))
        await runtime.observe(message, kind="edit", reason="edit", edited=True)

        self.assertTrue(await runtime.verify(original))
        self.assertNotIn(key, runtime.invalidated)
        self.assertEqual(runtime.latest[key]["revision"], original["revision"])
        self.assertIsNone(runtime.latest[key]["edited_timestamp"])
        self.assertTrue(runtime.queue.empty())

    async def test_uncached_raw_noop_embed_update_does_not_revise_live_signal(self):
        runtime = self._ready_runtime()
        description = "ENTRY QQQ 738C @ 1.00"
        message = self._signal_message("500000000000000052", description)
        original = await self._live_signal(runtime, message)
        key = (CHANNEL_SIGNALS, original["id"])

        await runtime.edit_raw(SimpleNamespace(
            channel_id=CHANNEL_SIGNALS,
            message_id=original["id"],
            cached_message=None,
            data={"embeds": [{"description": description}]},
        ))

        self.assertTrue(await runtime.verify(original))
        self.assertNotIn(key, runtime.invalidated)
        self.assertEqual(runtime.latest[key]["revision"], original["revision"])
        self.assertIsNone(runtime.latest[key]["edited_timestamp"])
        self.assertTrue(runtime.queue.empty())

    async def test_signed_cdn_url_renewal_refreshes_media_without_revising_signal(self):
        runtime = self._ready_runtime()
        message = self._signal_message(
            "500000000000000053",
            "ENTRY QQQ 738C @ 1.00",
            attachments=[self._attachment("old")],
        )
        original = await self._live_signal(runtime, message)
        previous_transport_revision = original["transport_revision"]
        renewed = dict(original["attachments"][0])
        renewed["url"] = renewed["url"].replace("ex=old&hm=old", "ex=new&hm=new")
        renewed["proxy_url"] = renewed["proxy_url"].replace("ex=old&hm=old", "ex=new&hm=new")

        await runtime.edit_raw(SimpleNamespace(
            channel_id=CHANNEL_SIGNALS,
            message_id=original["id"],
            cached_message=None,
            data={"attachments": [renewed]},
        ))
        refreshed = await runtime.queue.get()
        runtime.queue.task_done()

        self.assertEqual(refreshed["revision"], original["revision"])
        self.assertNotEqual(refreshed["transport_revision"], previous_transport_revision)
        self.assertEqual(refreshed["attachments"][0]["url"], renewed["url"])
        self.assertEqual(refreshed["ingestion"], "live")
        self.assertTrue(await runtime.verify(original))

    async def test_changed_embed_contract_or_price_still_invalidates_live_signal(self):
        runtime = self._ready_runtime()
        original = await self._live_signal(
            runtime,
            self._signal_message("500000000000000054", "ENTRY QQQ 738C @ 1.00"),
        )
        key = (CHANNEL_SIGNALS, original["id"])

        await runtime.edit_raw(SimpleNamespace(
            channel_id=CHANNEL_SIGNALS,
            message_id=original["id"],
            cached_message=None,
            data={"embeds": [{"description": "ENTRY QQQ 739C @ 1.25"}]},
        ))
        changed = await runtime.queue.get()
        runtime.queue.task_done()

        self.assertFalse(await runtime.verify(original))
        self.assertIn(key, runtime.invalidated)
        self.assertNotEqual(changed["revision"], original["revision"])
        self.assertEqual(changed["ingestion_reason"], "edit")
        self.assertIsNotNone(changed["edited_timestamp"])

    async def test_explicit_edit_timestamp_blocks_and_duplicate_cannot_revive_signal(self):
        runtime = self._ready_runtime()
        description = "ENTRY QQQ 738C @ 1.00"
        original = await self._live_signal(
            runtime, self._signal_message("500000000000000055", description)
        )
        key = (CHANNEL_SIGNALS, original["id"])
        edited_at = _when(5)
        edited = self._signal_message(original["id"], description, edited_at=edited_at)

        await runtime.observe(edited, kind="edit", reason="edit", edited=True)
        revised = await runtime.queue.get()
        runtime.queue.task_done()
        self.assertEqual(datetime.fromisoformat(revised["edited_timestamp"]), edited_at)
        self.assertFalse(await runtime.verify(original))
        self.assertIn(key, runtime.invalidated)

        await runtime.edit_raw(SimpleNamespace(
            channel_id=CHANNEL_SIGNALS,
            message_id=original["id"],
            cached_message=edited,
            data={
                "embeds": [{"description": description}],
                "edited_timestamp": edited_at.isoformat(),
            },
        ))
        await runtime.observe(edited, kind="edit", reason="edit", edited=True)

        self.assertFalse(await runtime.verify(original))
        self.assertIn(key, runtime.invalidated)
        self.assertEqual(runtime.latest[key]["revision"], revised["revision"])
        self.assertTrue(runtime.queue.empty())

    async def test_partial_raw_edit_and_bulk_delete_invalidate_retained_alerts(self):
        runtime = gateway._GatewayRuntime(_config(), None, None, None)
        runtime.healthy, runtime.epoch = True, "gateway:fixture"
        runtime.client = FakeClient()
        runtime.client.started.set()
        runtime.last_gateway_ack = time.monotonic()
        channel = FakeChannel(CHANNEL_SIGNALS)
        await runtime.observe(FakeMessage("500000000000000041", channel))
        original = await runtime.queue.get()
        runtime.queue.task_done()
        self.assertTrue(await runtime.verify(original))
        await runtime.edit_raw(SimpleNamespace(channel_id=CHANNEL_SIGNALS,
            message_id=original["id"], cached_message=None,
            data={"embeds": [{"description": "Stopped out"}]}))
        edited = await runtime.queue.get()
        runtime.queue.task_done()
        self.assertFalse(await runtime.verify(original))
        self.assertEqual(edited["content"], original["content"])
        self.assertEqual(edited["author_id"], AUTHOR)
        self.assertEqual(edited["ingestion_reason"], "edit")
        self.assertIn("Stopped out", str(edited["embeds"]))
        await runtime.observe(FakeMessage("500000000000000042", channel))
        latest = await runtime.queue.get()
        runtime.queue.task_done()
        self.assertTrue(await runtime.verify(latest))
        await runtime.delete_raw_bulk(SimpleNamespace(channel_id=CHANNEL_SIGNALS, message_ids=[latest["id"]]))
        self.assertFalse(await runtime.verify(latest))

    async def test_disconnect_during_history_never_restores_ready_state(self):
        runtime = gateway._GatewayRuntime(_config(), None, None, None)
        runtime.client = _make_client_for_discovery()
        runtime.bind_events()
        async def disconnected_history(channel):
            await runtime.client.callbacks["on_disconnect"]()
            return []
        with patch.object(runtime, "_history", side_effect=disconnected_history):
            await runtime.start_session()
        self.assertFalse(runtime.healthy)

    async def test_full_history_larger_than_queue_is_delivered_without_replay(self):
        delivered = []
        runtime = gateway._GatewayRuntime(_config(), delivered.append, None, None)
        runtime.client = _make_client_for_discovery()
        channel = runtime.client.get_channel(int(CHANNEL_SIGNALS))
        channel._history_items = [FakeMessage(str(500000000000000100 + i), channel) for i in range(100)]
        delivery = asyncio.create_task(runtime.delivery_loop())
        try:
            with patch("relay.gateway.discord_delay", return_value=.001):
                await asyncio.wait_for(runtime.start_session(), timeout=2)
            self.assertEqual(len(delivered), 100)
            self.assertTrue(all(m["ingestion"] == "baseline" for m in delivered))
            self.assertTrue(runtime.healthy)
        finally:
            delivery.cancel()
            await asyncio.gather(delivery, return_exceptions=True)

    async def test_setup_directory_is_ready_before_channels_are_configured(self):
        runtime = gateway._GatewayRuntime(_config(), None, None, None)
        runtime.client = _make_client_for_discovery()
        runtime.channels = [{"id": "999999999999999999"}]
        runtime.load_history = False
        await runtime.start_session()
        self.assertTrue(runtime.healthy)
        result = await runtime.discover({})
        self.assertEqual(result["state"], "ready")
        self.assertEqual(result["guilds"][0]["id"], GUILD)

    async def test_initial_ready_without_ack_never_reports_connected_or_emits_live(self):
        events = []
        runtime = gateway._GatewayRuntime(_config(), None, None, events.append)
        runtime.client = _make_client_for_discovery()
        runtime.client.started.set()
        await runtime.start_session()
        self.assertFalse(any(e["state"] == "connected" for e in events))
        await runtime.observe(FakeMessage("500000000000000060", FakeChannel(CHANNEL_SIGNALS)))
        message = await runtime.queue.get()
        runtime.queue.task_done()
        self.assertEqual(message["ingestion"], "baseline")
        self.assertFalse(await runtime.verify(message))

    async def test_quiet_connection_requires_recent_ack_and_preserves_auth_failures(self):
        import tempfile
        from pathlib import Path
        from relay.service import RuntimeStatus
        from relay.dashboard import _safe_runtime

        with tempfile.TemporaryDirectory() as directory:
            config = _config()
            status = RuntimeStatus(Path(directory) / "runtime.json", config["channels"])
            status.ready = True
            runtime = gateway._GatewayRuntime(config, None, None, status.event)
            runtime.client = FakeClient()
            runtime.client.started.set()
            runtime.healthy, runtime.epoch = True, "gateway:fixture"
            runtime.bind_events()
            with patch("relay.service.timestamp", return_value="2020-01-01T00:00:00+00:00"):
                status.write()
            status.write(heartbeat=True)
            self.assertTrue(_safe_runtime(config, status.path)["stale"])
            with patch("relay.gateway.time.monotonic", return_value=1000):
                await runtime.client.callbacks["on_socket_raw_receive"]('{"op":11,"private":"never retain"}')
                await runtime.observe(FakeMessage("500000000000000050", FakeChannel(CHANNEL_SIGNALS)))
                message = await runtime.queue.get()
                runtime.queue.task_done()
            with patch("relay.gateway.time.monotonic", return_value=1020):
                await runtime.report_health()
                view = _safe_runtime(config, status.path)
                self.assertFalse(view["stale"])
                self.assertEqual(view["discord"]["state"], "connected")
                self.assertTrue(await runtime.verify(message))
                self.assertTrue(all("20s ago" in c["detail"] for c in view["discord"]["channels"]))
                self.assertNotIn("never retain", status.path.read_text())
            with patch("relay.gateway.time.monotonic", return_value=1100):
                await runtime.report_health()
                self.assertEqual(_safe_runtime(config, status.path)["discord"]["state"], "reconnecting")
                self.assertFalse(await runtime.verify(message))
                for payload in ('{"op":0}', 'invalid JSON', b'\xff', '[]',
                                '{"op":11,"padding":"' + 'x' * 1024 + '"}'):
                    await runtime.client.callbacks["on_socket_raw_receive"](payload)
                self.assertEqual(runtime.last_gateway_ack, 1000)
                await runtime.client.callbacks["on_socket_raw_receive"](b'{"op":11,"d":null}')
                await runtime.report_health()
                self.assertTrue(await runtime.verify(message))
                await runtime.client.callbacks["on_disconnect"]()
                await runtime.report_health()
                self.assertEqual(_safe_runtime(config, status.path)["discord"]["state"], "reconnecting")
                self.assertFalse(await runtime.verify(message))
            runtime.auth_rejected = True
            await runtime.status("login_required", "Credential rejected; update Discord setup.")
            await runtime.client.callbacks["on_disconnect"]()
            await runtime.client.callbacks["on_resumed"]()
            await runtime.report_health()
            self.assertEqual(_safe_runtime(config, status.path)["discord"]["state"], "login_required")
            self.assertFalse(runtime.healthy)
            self.assertFalse(await runtime.verify(message))

    async def test_history_floor_rejects_delayed_equal_and_older_gateway_events(self):
        runtime = gateway._GatewayRuntime(_config(), None, None, None)
        runtime.epoch = "gateway:fixture"
        runtime.client = FakeClient()
        runtime.client.started.set()
        runtime.last_gateway_ack = time.monotonic()
        channel = FakeChannel(CHANNEL_SIGNALS)
        await runtime.observe(FakeMessage("500000000000000002", channel), kind="baseline", reason="baseline")
        runtime.healthy = True
        for identifier in ("500000000000000001", "500000000000000002"):
            await runtime.observe(FakeMessage(identifier, channel))
            replay = runtime.latest[(CHANNEL_SIGNALS, identifier)]
            self.assertEqual(replay["ingestion"], "baseline")
            self.assertFalse(await runtime.verify(replay))
        await runtime.observe(FakeMessage("500000000000000003", channel))
        self.assertTrue(await runtime.verify(runtime.latest[(CHANNEL_SIGNALS, "500000000000000003")]))
        # A newer trusted baseline in the paired context channel also fences
        # an earlier live candidate in the same source group.
        await runtime.observe(FakeMessage("500000000000000004", FakeChannel(CHANNEL_CONTEXT)),
                              kind="baseline", reason="baseline")
        self.assertFalse(await runtime.verify(runtime.latest[(CHANNEL_SIGNALS, "500000000000000003")]))

    async def test_recovery_verification_is_opt_in_and_bounded_to_current_cache(self):
        async def prepared(identifier="500000000000000002"):
            runtime = gateway._GatewayRuntime(_config(), None, None, None)
            runtime.epoch = "gateway:current"
            runtime.client = FakeClient()
            runtime.client.started.set()
            runtime.last_gateway_ack = time.monotonic()
            channel = FakeChannel(CHANNEL_SIGNALS)
            await runtime.observe(FakeMessage(identifier, channel), kind="baseline", reason="baseline")
            row = await runtime.queue.get()
            runtime.queue.task_done()
            runtime.healthy = True
            return runtime, channel, row

        runtime, _channel, baseline = await prepared()
        persisted = dict(baseline, browser_connection_epoch="gateway:old")
        self.assertFalse(await runtime.verify(persisted))
        self.assertFalse(await runtime.verify(persisted, recovery=True))
        self.assertFalse(await runtime.verify(persisted, recovery=True, latest_id="not-numeric"))
        self.assertTrue(await runtime.verify(persisted, recovery=True, latest_id=baseline["id"]))

        stale = runtime.latest[(CHANNEL_SIGNALS, baseline["id"])]
        stale["browser_connection_epoch"] = "gateway:stale"
        self.assertFalse(await runtime.verify(persisted, recovery=True, latest_id=baseline["id"]))

        runtime, channel, baseline = await prepared()
        await runtime.observe(FakeMessage(baseline["id"], channel, content="edited"), kind="edit", reason="edit", edited=True)
        edited = await runtime.queue.get()
        runtime.queue.task_done()
        self.assertFalse(await runtime.verify(baseline, recovery=True, latest_id=baseline["id"]))
        self.assertNotEqual(edited["revision"], baseline["revision"])

        runtime, channel, baseline = await prepared()
        await runtime.delete(FakeMessage(baseline["id"], channel))
        self.assertFalse(await runtime.verify(baseline, recovery=True, latest_id=baseline["id"]))

        runtime, _channel, baseline = await prepared()
        unknown = dict(baseline, id="5000000000000000999")
        self.assertFalse(await runtime.verify(unknown, recovery=True, latest_id=unknown["id"]))

        runtime, channel, baseline = await prepared()
        await runtime.observe(FakeMessage("500000000000000003", channel), kind="live", reason="live")
        newer = await runtime.queue.get()
        runtime.queue.task_done()
        self.assertFalse(await runtime.verify(baseline, recovery=True, latest_id=baseline["id"]))
        self.assertEqual(newer["id"], "500000000000000003")

    async def test_initial_history_is_bounded_baseline_and_live_delivery_is_serialized(self):
        config = _config()
        signal_channel = FakeChannel(CHANNEL_SIGNALS)
        context_channel = FakeChannel(CHANNEL_CONTEXT)
        history = FakeMessage("500000000000000001", signal_channel, content="old", second=1)
        signal_channel._history_items = [history]
        client_holder = {}
        delivered: list[dict] = []
        verifier_holder = {}

        async def on_message(message):
            delivered.append(message)

        async def register(verifier):
            verifier_holder["verify"] = verifier

        original_start = FakeClient.start

        async def start(client, token, reconnect=True):
            client.channels = {
                CHANNEL_SIGNALS: signal_channel,
                CHANNEL_CONTEXT: context_channel,
            }
            client.guilds = [FakeGuild([signal_channel, context_channel])]
            client_holder["client"] = client
            await original_start(client, token, reconnect=reconnect)

        with patch.object(FakeClient, "start", new=start):
            task = asyncio.create_task(
                gateway.monitor(config, on_message, register_verifier=register)
            )
            for _ in range(20):
                if client_holder:
                    break
                await asyncio.sleep(0)
            await asyncio.wait_for(client_holder["client"].started.wait(), timeout=1)
            client = client_holder["client"]
            self.assertFalse(client.kwargs["chunk_guilds_at_startup"])
            self.assertTrue(client.kwargs["guild_subscriptions"])
            self.assertEqual(signal_channel.history_calls[0]["limit"], 100)
            self.assertEqual(signal_channel.history_calls[0]["oldest_first"], False)
            self.assertEqual(delivered[0]["ingestion"], "baseline")
            self.assertEqual(delivered[0]["ingestion_reason"], "baseline")

            live = FakeMessage("500000000000000002", signal_channel, content="fresh", second=2)
            await client.callbacks["on_message"](live)
            await asyncio.sleep(0)
            self.assertEqual(delivered[-1]["ingestion"], "live")
            self.assertEqual(delivered[-1]["source"], "gateway")
            self.assertTrue(delivered[-1]["browser_connection_epoch"].startswith("gateway:"))
            self.assertTrue(await verifier_holder["verify"](delivered[-1]))

            newer = FakeMessage("500000000000000003", signal_channel, content="newer", second=3)
            await client.callbacks["on_message"](newer)
            self.assertFalse(await verifier_holder["verify"](delivered[-1]))
            client.release.set()
            await asyncio.wait_for(task, timeout=1)

    async def test_edit_delete_disconnect_and_reconnect_fail_closed(self):
        config = _config()
        signal_channel = FakeChannel(CHANNEL_SIGNALS)
        context_channel = FakeChannel(CHANNEL_CONTEXT)
        delivered: list[dict] = []
        client_holder = {}
        verifier_holder = {}
        original_start = FakeClient.start

        async def register(verifier):
            verifier_holder["verify"] = verifier

        async def start(client, token, reconnect=True):
            client.channels = {CHANNEL_SIGNALS: signal_channel, CHANNEL_CONTEXT: context_channel}
            client.guilds = [FakeGuild([signal_channel, context_channel])]
            client_holder["client"] = client
            await original_start(client, token, reconnect=reconnect)

        with patch.object(FakeClient, "start", new=start):
            task = asyncio.create_task(
                gateway.monitor(config, delivered.append, register_verifier=register)
            )
            for _ in range(20):
                if client_holder:
                    break
                await asyncio.sleep(0)
            client = client_holder["client"]
            await asyncio.wait_for(client.started.wait(), timeout=1)
            live = FakeMessage("500000000000000011", signal_channel, second=1)
            await client.callbacks["on_message"](live)
            await asyncio.sleep(0)
            current = delivered[-1]
            self.assertEqual(current["ingestion"], "live")

            edited = FakeMessage(
                live.id,
                signal_channel,
                content="edited",
                second=1,
                edited_at=_when(2),
            )
            await client.callbacks["on_message_edit"](live, edited)
            await asyncio.sleep(0)
            self.assertEqual(delivered[-1]["ingestion_reason"], "edit")

            self.assertFalse(await verifier_holder["verify"](current))
            await client.callbacks["on_message_delete"](edited)
            self.assertFalse(await verifier_holder["verify"](current))
            await client.callbacks["on_disconnect"]()
            self.assertFalse(await verifier_holder["verify"](current))
            client.release.set()
            await asyncio.wait_for(task, timeout=1)

    async def test_fatal_delivery_callback_propagates(self):
        config = _config()
        signal_channel = FakeChannel(CHANNEL_SIGNALS)
        context_channel = FakeChannel(CHANNEL_CONTEXT)
        client_holder = {}
        original_start = FakeClient.start

        async def start(client, token, reconnect=True):
            client.channels = {CHANNEL_SIGNALS: signal_channel, CHANNEL_CONTEXT: context_channel}
            client.guilds = [FakeGuild([signal_channel, context_channel])]
            client_holder["client"] = client
            await original_start(client, token, reconnect=reconnect)

        async def fail(_message):
            raise RuntimeError("callback failure")

        with patch.object(FakeClient, "start", new=start):
            task = asyncio.create_task(gateway.monitor(config, fail))
            for _ in range(20):
                if client_holder:
                    break
                await asyncio.sleep(0)
            await asyncio.wait_for(client_holder["client"].started.wait(), timeout=1)
            live = FakeMessage("500000000000000021", signal_channel)
            await client_holder["client"].callbacks["on_message"](live)
            with self.assertRaisesRegex(RuntimeError, "callback failure"):
                await asyncio.wait_for(task, timeout=1)

    async def test_captcha_rejection_is_safe_login_required_status(self):
        config = _config()
        statuses = []

        class CaptchaRequired(Exception):
            pass

        async def rejected_start(self, token, reconnect=True):
            del token, reconnect
            raise CaptchaRequired("secret token and challenge details")

        with patch.object(FakeClient, "start", new=rejected_start):
            task = asyncio.create_task(
                gateway.monitor(config, lambda _message: None, on_status=statuses.append)
            )
            for _ in range(20):
                if statuses:
                    break
                await asyncio.sleep(0)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertTrue(any(event["state"] == "login_required" for event in statuses))
        self.assertNotIn("secret", repr(statuses))

    async def test_cached_discovery_uses_guild_channel_and_author_projection(self):
        runtime = gateway._GatewayRuntime(_config(), None, None, None)
        runtime.client = _make_client_for_discovery()
        runtime.healthy = True
        result = await runtime.discover(
            {"request_id": "discovery-1", "guild_id": GUILD, "channel_id": CHANNEL_SIGNALS}
        )
        self.assertEqual(result["state"], "ready")
        self.assertEqual(result["guilds"], [{"id": GUILD, "name": "Relay Guild"}])
        self.assertEqual(result["channels"][0]["url"], f"https://discord.com/channels/{GUILD}/{CHANNEL_SIGNALS}")
        self.assertEqual(result["authors"], [{"id": AUTHOR, "name": "Analyst"}])

    async def test_directory_lists_servers_then_all_selected_server_channels(self):
        import tempfile
        from pathlib import Path
        from relay.discovery import discovery_status, request_discovery

        with tempfile.TemporaryDirectory() as directory:
            config = _config()
            config["runtime_status_file"] = str(Path(directory) / "runtime.json")
            runtime = gateway._GatewayRuntime(config, None, None, None)
            runtime.client = _make_client_for_discovery()
            runtime.healthy = True
            runtime.client.guilds = []
            for index in range(10):
                guild = FakeGuild([FakeChannel(str(200000000000000000 + index * 500 + i))
                                   for i in range(500)])
                guild.id = str(100000000000000000 + index)
                for channel in guild.channels:
                    channel.guild_id = guild.id
                runtime.client.guilds.append(guild)
            worker = asyncio.create_task(runtime._serve_discovery())
            try:
                for payload, expected_channels in (({}, 0), ({"guild_id": GUILD}, 500)):
                    request_discovery(config["runtime_status_file"], payload)
                    for _ in range(100):
                        await asyncio.sleep(.01)
                        if worker.done():
                            await worker
                        result = discovery_status(config["runtime_status_file"])
                        if result["state"] != "waiting":
                            break
                    self.assertEqual(result["state"], "ready")
                    self.assertEqual(len(result["guilds"]), 1 if payload else 10)
                    self.assertEqual(len(result["channels"]), expected_channels)
            finally:
                worker.cancel()
                await asyncio.gather(worker, return_exceptions=True)

    async def test_discovery_failure_propagates_instead_of_silently_stalling(self):
        config = _config()
        config["runtime_status_file"] = "unused-by-mocked-discovery.json"
        with patch.object(gateway._GatewayRuntime, "_serve_discovery",
                          side_effect=RuntimeError("discovery fixture failure")):
            with self.assertRaisesRegex(RuntimeError, "discovery fixture failure"):
                await asyncio.wait_for(gateway.monitor(config, lambda _message: None), timeout=1)

def _make_client_for_discovery():
    client = FakeClient()
    signal_channel = FakeChannel(CHANNEL_SIGNALS)
    context_channel = FakeChannel(CHANNEL_CONTEXT)
    client.channels = {CHANNEL_SIGNALS: signal_channel, CHANNEL_CONTEXT: context_channel}
    client.guilds = [FakeGuild([signal_channel, context_channel])]
    return client


class InstalledGatewayAPITests(unittest.IsolatedAsyncioTestCase):
    async def test_pinned_library_constructs_without_bot_intents_or_network(self):
        try:
            import discord
        except ImportError:
            self.skipTest("Install the discord extra for the upstream API check")
        self.assertEqual(discord.__version__, "2.1.0")
        client = gateway._make_client(discord)
        try:
            self.assertFalse(client._connection._chunk_guilds)
            self.assertTrue(client._enable_debug_events)
            from discord.gateway import DiscordWebSocket
            received = []
            socket = SimpleNamespace(log_receive=received.append, _keep_alive=None,
                                     DISPATCH=0, RECONNECT=7, HEARTBEAT_ACK=11)
            await DiscordWebSocket.received_message(socket, '{"op":11,"d":null}')
            self.assertEqual(received, ['{"op":11,"d":null}'])
            runtime = gateway._GatewayRuntime(_config(), None, None, None)
            runtime.client = FakeClient()
            runtime.bind_events()
            await runtime.client.callbacks["on_socket_raw_receive"](received[0])
            self.assertIsNotNone(runtime.last_gateway_ack)
        finally:
            await client.close()


if __name__ == "__main__":
    unittest.main()
