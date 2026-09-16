"""Offline gateway transport checks with a small discord.py-self-shaped fake."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from types import ModuleType, SimpleNamespace
import sys
import unittest
from unittest.mock import patch

from relay import gateway


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
        await self.callbacks["on_ready"]()
        self.started.set()
        await self.release.wait()

    async def close(self):
        self.closed = True
        self.release.set()


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
        self.session_patch = patch.dict(sys.modules, {"relay.discord_session": self.session_module})
        self.session_patch.start()

    async def asyncTearDown(self):
        self.session_patch.stop()
        self.discord_patch.stop()

    async def test_partial_raw_edit_and_bulk_delete_invalidate_retained_alerts(self):
        runtime = gateway._GatewayRuntime(_config(), None, None, None)
        runtime.healthy, runtime.epoch = True, "gateway:fixture"
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

    async def test_history_floor_rejects_delayed_equal_and_older_gateway_events(self):
        runtime = gateway._GatewayRuntime(_config(), None, None, None)
        runtime.epoch = "gateway:fixture"
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
        finally:
            await client.close()


if __name__ == "__main__":
    unittest.main()
