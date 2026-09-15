"""Persistent browser worker; setup and connection recovery never replay orders."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import signal
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from .browser import SNOWFLAKE, login, monitor
from .broker import broker_credentials_state
from .core import Store, channel_allows_author, load_config
from .interpreter import CodexInterpreter, InterpretationError, safe_interpretation_reason
from .status import AUTH_REQUIRED_STATES, HEALTHY_STATES, failure_detail, previous_auth_state

LOG = logging.getLogger(__name__)


def timestamp():
    return datetime.now(timezone.utc).isoformat()


class RuntimeStatus:
    def __init__(self, path, channels=()):
        self.path = Path(path)
        self.channel_ids = [str(channel["id"]) for channel in channels]
        self.ready = False
        self.detail = "Waiting for configuration and authentication."
        self.value = {"state": "starting", "detail": self.detail,
                      "discord": {"state": "starting", "channels": []},
                      "codex": {"state": "unknown"}, "broker": {"state": "unknown"}}
        for component in ("codex", "broker"):
            state = previous_auth_state(self.path, component)
            if state is not None:
                self.value[component] = {"state": state}

    def event(self, event):
        component = event.get("component")
        if component not in {"discord", "codex", "broker"}:
            return
        state = str(event.get("state", "unknown"))
        if component == "discord" and event.get("channel_id"):
            rows = {row["id"]: row for row in self.value[component]["channels"]}
            channel_id = str(event["channel_id"])
            row = rows.setdefault(channel_id, {"id": channel_id})
            row.update(state=state, detail=event.get("detail", ""), updated_at=timestamp())
            if state == "connected":
                row["last_seen_at"] = timestamp()
            states = [rows.get(key, {}).get("state", "starting") for key in self.channel_ids]
            aggregate = next((candidate for candidate in ("login_required", "needs_attention", "reconnecting", "starting")
                              if candidate in states), "connected")
            affected = next((row for row in rows.values() if row.get("state") == aggregate), {})
            self.value[component].update(state=aggregate, detail=affected.get("detail", ""), channels=list(rows.values()))
        else:
            if (component in {"codex", "broker"}
                    and str(self.value[component].get("state", "")).lower() in AUTH_REQUIRED_STATES
                    and state not in HEALTHY_STATES and state not in AUTH_REQUIRED_STATES):
                # A credential file, saved login, or nonhealthy probe is only a
                # setup signal. Preserve a provider failure until real health.
                self.write(heartbeat=True)
                return
            self.value[component].update(state=state, detail=event.get("detail", ""))
        self.write()

    def write(self, *, state=None, detail=None, heartbeat=False):
        self.value["heartbeat_at"] = timestamp()
        if not heartbeat:
            self.value["updated_at"] = timestamp()
        self.value.update(state=state or ("running" if self.ready and self.value["discord"]["state"] == "connected"
                                          else "degraded" if self.ready else "setup_required"),
                          detail=detail or self.detail)
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(mode="w", dir=self.path.parent, prefix=".runtime-", delete=False) as stream:
                temporary = Path(stream.name)
                json.dump(self.value, stream)
            os.replace(temporary, self.path)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)


def signature(paths):
    result = []
    for path in paths:
        try:
            info = Path(path).stat()
            result.append((info.st_ino, info.st_size, info.st_mtime_ns))
        except OSError:
            result.append(None)
    return tuple(result)


async def watch_changes(paths, status, *, interval=3, initial_signature=None):
    original = signature(paths) if initial_signature is None else initial_signature
    while signature(paths) == original:
        status.write(heartbeat=True)
        await asyncio.sleep(interval)


async def observe(config, status):
    """Record trusted messages while setup is incomplete; no broker or model call."""
    store = Store(config["database"])
    channels = {str(channel["id"]): channel for channel in config["channels"]}
    try:
        async def record(message):
            channel = channels.get(str(message.get("channel_id")))
            if not channel or not channel_allows_author(channel, message.get("author_id")):
                return
            message["source_group"] = channel["source_group"]
            if store.observe(message) != "same":
                store.record(message, "context", "Observation only: complete broker and Codex setup before interpreting new messages.")
        await monitor(config, record, on_status=status.event)
    finally:
        store.close()


async def run_until_change(operation, paths, status, *, initial_signature=None):
    worker = asyncio.create_task(operation)
    watcher = asyncio.create_task(watch_changes(paths, status, **(
        {"initial_signature": initial_signature} if initial_signature is not None else {})))
    try:
        done, _ = await asyncio.wait({worker, watcher}, return_when=asyncio.FIRST_COMPLETED)
        if worker in done:
            await worker
            raise RuntimeError("browser worker stopped")
    finally:
        worker.cancel()
        watcher.cancel()
        await asyncio.gather(worker, watcher, return_exceptions=True)


async def serve(config_path):
    config_path = Path(config_path).resolve()
    status = RuntimeStatus(config_path.parent / "state/runtime-status.json")
    delay = 5
    while True:
        try:
            paths = [config_path, config_path.parent / "state/RECONNECT"]
            loaded_signature = signature(paths)
            config = load_config(config_path, allow_unbound=True)
            status = RuntimeStatus(config_path.parent / config.get("runtime_status_file", "state/runtime-status.json"), config["channels"])
            # The previous worker has finished cleanup before this acknowledgement.
            status.value.update(mode=config["mode"], mode_change_id=config.get("mode_change_id"),
                                live_enabled=config.get("robinhood", {}).get("enable_live_orders") is True)
            config["runtime_status_file"] = str(status.path)
            configured_channels = all(SNOWFLAKE.fullmatch(str(channel["guild_id"])) for channel in config["channels"])
            broker_state = broker_credentials_state(config)
            broker_ready = broker_state in {"paper", "configured"}
            status.event({"component": "broker", "state": broker_state})
            try:
                subscription = await CodexInterpreter(config).subscription_status()
                codex_ready = subscription.get("authenticated") is True and subscription.get("isolated_execution_available") is True
                codex_state = "ready" if codex_ready else "auth_required"
                if (codex_ready
                        and str(status.value["codex"].get("state", "")).lower() in AUTH_REQUIRED_STATES):
                    # A saved Codex login is not proof that a real evaluation can
                    # authenticate; interpreter success publishes the clearing ready event.
                    codex_state = "configured"
                status.event({"component": "codex", "state": codex_state})
            except (InterpretationError, OSError) as exc:
                codex_ready = False
                detail = (f"Codex startup check: {safe_interpretation_reason(exc)}. Use Codex sign-in if authorization expired; otherwise check the configured model and container runtime."
                          if isinstance(exc, InterpretationError) else failure_detail(exc, provider="Codex", phase="startup check"))
                status.event({"component": "codex", "state": "auth_required" if isinstance(exc, InterpretationError) and exc.code == "auth_required" else "unavailable", "detail": detail})
            status.ready = configured_channels and broker_ready and codex_ready
            status.detail = ("Monitoring configured channels; execution follows the configured mode." if status.ready else
                             "Observation only until channel IDs, Robinhood authorization and Codex subscription login are configured.")
            status.write()
            if not status.ready:
                paths += [Path(config["robinhood"]["token_store"]),
                          Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex"))) / "auth.json"]
            if not configured_channels:
                operation = login(config["browser"]["profile_dir"], keep_open=True, on_status=status.event,
                                  discovery_runtime_path=status.path)
            elif not status.ready:
                operation = observe(config, status)
            else:
                from .cli import run
                operation = run(config, on_status=status.event)
            await run_until_change(operation, paths, status,
                                   initial_signature=loaded_signature + signature(paths[2:]))
            delay = 5
        except asyncio.CancelledError:
            status.write(state="stopped", detail="Worker stopped; no new messages are being processed.")
            raise
        except Exception as exc:
            # Do not expose OAuth responses, tokens or browser exception URLs in the dashboard.
            LOG.warning("Worker needs attention (%s); retrying in %s seconds", type(exc).__name__, delay)
            status.ready = False
            pending, failures = [exc], []
            while pending and len(failures) < 4:
                failure = pending.pop()
                if isinstance(failure, BaseExceptionGroup):
                    pending.extend(failure.exceptions[:4])
                    continue
                failure_frame = failure.__traceback__
                while failure_frame and failure_frame.tb_next:
                    failure_frame = failure_frame.tb_next
                location = f" at {Path(failure_frame.tb_frame.f_code.co_filename).name}:{failure_frame.tb_lineno}" if failure_frame else ""
                failures.append(f"{type(failure).__name__}{location}")
            detail = failure_detail(exc, provider="Worker", phase="startup or processing")
            status.write(state="error", detail=f"{detail} ({'; '.join(failures)}). Automatic retry in {delay} seconds.")
            await asyncio.sleep(delay)
            delay = min(delay * 2, 60)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default="config.local.json")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    async def start():
        loop = asyncio.get_running_loop()
        loop.add_signal_handler(signal.SIGTERM, asyncio.current_task().cancel)
        await serve(args.config)
    try:
        asyncio.run(start())
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass


if __name__ == "__main__":
    main()
