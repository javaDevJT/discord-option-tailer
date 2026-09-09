"""Local setup, offline rehearsal, and supervised personal-browser monitoring."""
from __future__ import annotations

import argparse
import asyncio
import copy
import importlib.util
import json
import os
import platform
import sys
import tempfile
from collections import Counter
from contextlib import AsyncExitStack
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

from .core import Engine, Hold, Store, channel_allows_author, load_config, money
from .ingest import load_export, normalize
from .status import publish_status


def emit(value):
    print(json.dumps(value, indent=2, default=str))


def private_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    # Open with a restrictive mode before writing content, including on overwrite.
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as handle:
        os.fchmod(handle.fileno(), 0o600)
        json.dump(value, handle, indent=2, default=str)
        handle.write("\n")


def paper_broker(config, store):
    from .broker import PaperBroker
    broker = PaperBroker(config)
    broker.restore_positions(store.positions())
    # Restore simulated cash from cumulative fills; restarting cannot reset the bankroll.
    cash = money(config["paper"]["buying_power"])
    fee = money(config["risk"]["fee_reserve_per_contract"])
    for row in store.db.execute("SELECT body,filled_notional,filled_quantity FROM orders"):
        side = json.loads(row["body"])["side"]
        cash += money(row["filled_notional"]) * 100 * (1 if side == "sell" else -1)
        cash -= fee * row["filled_quantity"]
    if cash < 0:
        raise Hold("configured paper bankroll is below recorded spending")
    broker.buying_power = cash
    return broker


async def replay(args, config):
    from .interpreter import CodexInterpreter
    path = Path(args.database).resolve()
    if path == Path(config["database"]):
        raise Hold("use a separate replay database to preserve monitoring state")
    store = Store(path)
    try:
        messages = sorted((m for p in args.exports for m in load_export(p)), key=lambda m: (m["timestamp"], int(m["id"])))
        engine = Engine(config, store, CodexInterpreter(config) if args.interpret else None, None)
        counts, reasons = Counter(), Counter()
        for message in messages:
            result = await engine.handle(message, analyze_history=args.interpret)
            counts[result["state"]] += 1
            reasons[result["reason"]] += 1
        result = {"operation": "historical_analysis_only" if args.interpret else "offline_import_only", "input_messages": len(messages), "results": dict(counts), "reasons": dict(reasons), "state": store.report(), "database": str(path), "live_orders": 0}
        if args.output:
            private_json(args.output, result)
        emit(result)
    finally:
        store.close()


async def demo(args, config):
    """A scripted, synthetic scenario exercises the actual engine, without calling a model."""
    from .broker import PaperBroker
    from .interpreter import validate_decision
    config = copy.deepcopy(config)
    config["mode"] = "paper"
    now = datetime(2026, 9, 8, 14, 0, tzinfo=timezone.utc)
    contract = {"symbol": "SPY", "expiry": "2026-09-18", "strike": "600", "option_type": "call"}
    channel = config["channels"][0]
    decisions = {}

    class ScriptedInterpreter:
        async def interpret(self, message, context, positions):
            value = decisions[message["id"]]
            return validate_decision(value, message, context)

    with tempfile.TemporaryDirectory(prefix="options-relay-demo-") as temporary:
        base = Path(temporary)
        config["kill_switch"] = str(base / "STOP")
        config["paper"] = {"buying_power": "1000.00", "market_open": True, "timestamp": now.isoformat(), "quotes_file": str(base / "quotes.json")}
        quote = dict(contract=contract, bid="0.76", ask="0.80", tick_size="0.01", timestamp=now.isoformat(), tradable=True, multiplier=100, currency="USD", asset_type="equity_option")
        private_json(config["paper"]["quotes_file"], {"quotes": [quote]})
        store = Store(base / "demo.sqlite3")
        try:
            engine = Engine(config, store, ScriptedInterpreter(), PaperBroker(config), clock=lambda: now)

            def message(number, content, action="OPEN", **changes):
                author_id = channel["authors"][0] if channel.get("authors") else "2000000000000000001"
                raw = dict(id=str(1545000000000000000 + number), channel_id=channel["id"], author={"id": author_id, "username": "synthetic-test"}, content=content, timestamp=now.isoformat())
                result = normalize(raw)
                result.update(source="browser", ingestion="live", source_group=channel["source_group"])
                decisions[result["id"]] = dict(action=action, origin_message_id=result["id"], contract=contract, quantity=None, fraction=None, alert_price="0.80", stop_price=None, confidence=0.99, ambiguous=False, reason="Scripted demonstration; no model inference", evidence=[{"message_id": result["id"], "quote": content}], **changes)
                return result

            entry = message(1, "BUY SPY 600 call 2026-09-18 at 0.80")
            results = [await engine.handle(entry), await engine.handle(entry)]
            results.append(await engine.handle(message(2, entry["content"])))
            trim = message(3, "Sell half of SPY 600 calls expiring 2026-09-18", "REDUCE")
            decisions[trim["id"]]["fraction"] = 0.5
            results.append(await engine.handle(trim))
            results.append(await engine.handle(message(4, "Close remaining SPY 600 calls expiring 2026-09-18", "CLOSE")))
            historical = message(5, "BUY SPY 600 call 2026-09-18 at 0.80")
            historical["timestamp"] = (now - timedelta(minutes=5)).isoformat()
            results.append(await engine.handle(historical))
            edited = dict(entry, content="CANCEL that entry", edited_timestamp=now.isoformat(), revision="changed")
            results.append(await engine.handle(edited))
            states = [r["state"] for r in results]
            if states != ["paper_order", "duplicate", "held", "held", "paper_order", "held", "context"] or store.positions():
                raise Hold("synthetic rehearsal failed its expected safety outcomes")
            report = {"scenario": "synthetic scripted decisions and fixture quotes; no LLM, live market, Discord, or Robinhood calls", "passed": True, "results": results, "state": store.report(), "live_orders": 0}
            private_json(args.output, report)
            emit({"passed": True, "scenarios": len(results), "simulated_fills": 2, "remaining_positions": 0, "report": str(Path(args.output).resolve())})
        finally:
            store.close()


async def run(config, *, observe_only=False, on_status=None):
    from .browser import monitor
    from .interpreter import CodexInterpreter
    if any(not str(c["guild_id"]).isdigit() for c in config["channels"]):
        raise Hold("set both guild_id values from the Discord channel URLs")
    config["require_browser_verification"] = True
    store = Store(config["database"])
    connections = AsyncExitStack()
    queue = None
    try:
        if config["mode"] == "paper":
            broker = paper_broker(config, store)
        else:
            from .broker import RobinhoodBroker
            broker = await connections.enter_async_context(RobinhoodBroker(config, on_status=on_status))
        publish_status(on_status, "broker", "paper" if config["mode"] == "paper" else "connected")
        engine = Engine(config, store, None if observe_only else CodexInterpreter(config, on_status=on_status), broker)
        from .recovery import RecoveryEvaluator
        recovery = RecoveryEvaluator(engine)
        queue = asyncio.Queue(maxsize=config["risk"]["max_pending_messages"])

        async def on_message(message):
            engine.note_observation(message)
            channel = engine.channels.get(str(message.get("channel_id")))
            if not channel or not channel_allows_author(channel, message.get("author_id", "")):
                return
            message = dict(message, source_group=channel["source_group"])
            observed = store.observe(message)
            try:
                queue.put_nowait((message, observed))
            except asyncio.QueueFull as exc:
                stop = Path(config["kill_switch"])
                stop.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                stop.touch(mode=0o600)
                raise Hold("message queue filled; kill switch set and reader stopped") from exc

        async def consume():
            while True:
                message, observed = await queue.get()
                try:
                    result = await engine.handle(message, _observed=observed)
                    emit(recovery.enqueue(message, observed) or result)
                finally:
                    queue.task_done()

        def register_verifier(callback):
            engine.verify_current = callback

        async with asyncio.TaskGroup() as tasks:
            consumer = tasks.create_task(consume())
            recovery_consumer = tasks.create_task(recovery.consume(queue, emit))

            async def read():
                try:
                    await monitor(config, on_message, register_verifier=register_verifier, on_status=on_status)
                finally:
                    consumer.cancel()
                    recovery_consumer.cancel()

            tasks.create_task(read())
    finally:
        try:
            await connections.aclose()
        finally:
            if queue is not None:
                with store.db:
                    store.db.execute("""UPDATE events SET state='held',
                        reason='Worker stopped before interpretation completed; retained for context, never automatically replayed'
                        WHERE state='observed' AND NOT EXISTS
                        (SELECT 1 FROM orders WHERE orders.message_id=events.message_id)""")
            store.close()


async def inspect_broker(args, config):
    """Read and bind the uniquely identified Agentic account; never review or submit an order."""
    from .broker import RobinhoodBroker, RobinhoodMCP
    async with RobinhoodMCP(config) as discovery:
        response = await discovery.call("get_accounts", {})
        accounts = response["structuredContent"]["data"]["accounts"]
    eligible = [a for a in accounts if a.get("agentic_allowed") is True and a.get("state") == "active"]
    configured = config["robinhood"].get("account_number")
    matches = [a for a in eligible if a["account_number"] == configured] if configured else eligible
    if len(matches) != 1:
        raise Hold("configure the intended Agentic account number; accessible account selection is not unique")
    account = matches[0]
    config["robinhood"]["account_number"] = account["account_number"]
    config["robinhood"]["enable_live_orders"] = False
    config["mode"] = "shadow"
    async with RobinhoodBroker(config) as broker:
        snapshot = await broker.snapshot()
    sanitized = {key: value for key, value in snapshot.items() if key not in {"account_id", "account_number", "positions"}}
    report = {
        "account": {"nickname": account.get("nickname"), "last_four": account["account_number"][-4:], "type": account["type"], "state": account["state"], "option_level": account.get("option_level"), "agentic_allowed": account["agentic_allowed"]},
        "snapshot": sanitized,
        "option_position_count": len(snapshot["positions"]),
        "actions_performed": ["account discovery", "balance and position reads", "market schedule lookup"],
        "orders_submitted": 0,
    }
    if args.bind:
        destination = Path(args.bind).resolve()
        if destination.name == "config.example.json":
            raise Hold("account bindings belong in a private local config, not the example")
        config["database"] = str(destination.parent / "state" / "relay-shadow.sqlite3")
        private_json(destination, config)
        report["bound_config"] = str(destination)
    private_json(args.output, report)
    emit(report)


async def dispatch(args, config):
    if args.command == "doctor":
        from .interpreter import CodexInterpreter
        subscription = await CodexInterpreter(config).subscription_status()
        emit({"python": platform.python_version(), "mode": config["mode"], "dependencies": {name: importlib.util.find_spec(name) is not None for name in ("playwright", "mcp", "jsonschema", "exchange_calendars")}, "guild_ids_configured": all(str(c["guild_id"]).isdigit() for c in config["channels"]), "codex_subscription": subscription, "robinhood_token_store_exists": Path(config["robinhood"]["token_store"]).exists(), "robinhood_account_bound": bool(config["robinhood"].get("account_number")), "kill_switch_present": Path(config["kill_switch"]).exists(), "live_execution_enabled": config["mode"] == "live" and config["robinhood"].get("enable_live_orders") is True})
    elif args.command == "replay":
        await replay(args, config)
    elif args.command == "demo":
        await demo(args, config)
    elif args.command == "discord-login":
        from .browser import login
        await login(config["browser"]["profile_dir"])
    elif args.command == "run":
        await run(config, observe_only=args.observe_only)
    elif args.command == "status":
        store = Store(Path(config["database"]).resolve(), read_only=True)
        try:
            emit(store.report())
        finally:
            store.close()
    elif args.command == "broker-login":
        from .broker import login
        result = await login(config)
        emit({"connected": True, "tool_count": len(result), "token_store": config["robinhood"]["token_store"], "live_execution": "disabled"})
    elif args.command == "broker-discover":
        from .broker import RobinhoodMCP
        async with RobinhoodMCP(config) as broker:
            tools = await broker.discover()
            private_json(args.output, tools)
            emit({"tool_count": len(tools), "schema_catalog": str(Path(args.output).resolve()), "orders_submitted": 0})
    elif args.command == "broker-inspect":
        await inspect_broker(args, config)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default="config.local.json" if Path("config.local.json").exists() else "config.example.json")
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("doctor", "discord-login", "status", "broker-login"):
        commands.add_parser(name)
    commands.add_parser("run").add_argument("--observe-only", action="store_true", help="observe and persist context without sending it to a model")
    replay_parser = commands.add_parser("replay", help="import historical messages; never submit orders")
    replay_parser.add_argument("exports", nargs="+")
    replay_parser.add_argument("--database", default="artifacts/private/replay.sqlite3")
    replay_parser.add_argument("--interpret", action="store_true", help="interpret message text through the signed-in Codex subscription")
    replay_parser.add_argument("--output")
    commands.add_parser("demo", help="run a synthetic offline engine rehearsal").add_argument("--output", default="artifacts/demo-report.json")
    commands.add_parser("broker-discover", help="save authenticated MCP tool schemas; no orders").add_argument("--output", default="artifacts/private/robinhood-tools.json")
    inspect_parser = commands.add_parser("broker-inspect", help="inspect actual Agentic account capabilities; no orders")
    inspect_parser.add_argument("--bind", nargs="?", const="config.local.json", help="save the selected account and shadow mode in a private config")
    inspect_parser.add_argument("--output", default="artifacts/private/account-capabilities.json")
    args = parser.parse_args()
    try:
        setup_command = args.command in {"doctor", "discord-login", "status", "broker-login", "broker-discover", "broker-inspect"}
        asyncio.run(dispatch(args, load_config(args.config, allow_unbound=setup_command)))
    except KeyboardInterrupt:
        raise SystemExit(130)
    except (Hold, FileNotFoundError, json.JSONDecodeError) as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(2)
    except Exception as exc:
        print(type(exc).__name__ + ": operation failed; no sensitive response details logged", file=sys.stderr)
        raise SystemExit(2)
