"""Offline money-path checks: python -m unittest discover -s tests -p test_core.py."""

import copy
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

from relay.core import Engine, Hold, Store, canonical_contract, channel_allows_author, contract_key, entry_size, load_config
from relay.broker import BrokerPreflightHold
from relay.ingest import normalize


NOW = datetime(2026, 9, 8, 14, tzinfo=timezone.utc)
CONTRACT = {"symbol": "BAC", "expiry": "2026-09-18", "strike": "63", "option_type": "call"}
SYNTHETIC_AUTHOR_ID = "1545000000000000000"


class Interpreter:
    def __init__(self):
        self.decision = dict(action="OPEN", contract=CONTRACT, quantity=None, fraction=None,
                             alert_price=".80", stop_price=None, confidence=.99,
                             ambiguous=False, reason="Fixture explicit entry", origin_message_id=None, evidence=[])
        self.calls = []

    async def interpret(self, message, context, positions):
        self.calls.append((message, context, positions))
        result = copy.deepcopy(self.decision)
        if result["origin_message_id"] is None:
            result["origin_message_id"] = message["id"]
            result["evidence"] = [{"message_id": message["id"], "quote": message["content"]}]
        return result


class Broker:
    def __init__(self):
        self.submissions = []
        self.quotes = {}
        self.account = {"market_open": True, "equity": "10000", "buying_power": "10000",
                        "timestamp": NOW.isoformat(), "option_exposure_by_symbol": {}}
        self.result = None
        self.error = None
        self.review_hook = None

    async def snapshot(self):
        return self.account

    async def quote(self, contract):
        return dict(contract=contract, timestamp=NOW.isoformat(), tradable=True,
                    multiplier=100, currency="USD", asset_type="equity_option",
                    bid=".77", ask=".80", tick_size=".01") | self.quotes

    async def submit(self, order, before_submit=None):
        if self.review_hook:
            await self.review_hook()
        if before_submit:
            try:
                await before_submit(self.account, await self.quote(order["contract"]))
            except Exception as exc:
                raise BrokerPreflightHold(str(exc)) from exc
        self.submissions.append(order)
        if self.error:
            raise self.error
        return self.result or dict(id="paper-" + order["client_order_id"], status="filled",
                                   filled_quantity=order["quantity"], fill_price=order["limit_price"])


class CoreChecks(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(__file__).resolve().parents[1]
        self.config = copy.deepcopy(json.loads((root / "config.example.json").read_text()))
        self.config["mode"] = "paper"
        self.config["database"] = str(Path(self.temp.name) / "test.sqlite3")
        self.config["kill_switch"] = str(Path(self.temp.name) / "STOP")
        self.config["risk"].update(entry_risk_min_fraction=".01", entry_risk_max_fraction=".01", fractional_kelly=".25",
                                   max_position_fraction=".05", max_total_exposure_fraction=".20",
                                   buying_power_reserve_fraction=".10", strategy_stats={}, min_confidence=.95)
        self.store = Store(self.config["database"])
        self.addCleanup(lambda: self.store.close())
        self.interpreter, self.broker = Interpreter(), Broker()
        self.now = NOW
        self.engine = Engine(self.config, self.store, self.interpreter, self.broker, lambda: self.now)
        self.next_id = 1545700000000000000

    def message(self, channel=0, **changes):
        self.next_id += 1
        source = self.config["channels"][channel]
        author_id = source["authors"][0] if source.get("authors") else SYNTHETIC_AUTHOR_ID
        raw = dict(id=str(self.next_id), channel_id=source["id"], author_id=author_id,
                   timestamp=NOW.isoformat(), source="browser", content="OPEN BAC 63 call 9/18 @ .80")
        raw.update(changes)
        return normalize(raw) | {"ingestion": "live"}

    def owned(self, quantity=1, group="source-a", contract=None):
        with self.store.db:
            self.store.db.execute("INSERT OR REPLACE INTO positions VALUES (?,?,?,?)",
                                  (group, contract_key(contract or CONTRACT), quantity, ".80"))

    def larger_orders(self, quantity=3):
        fraction = ".025" if quantity == 3 else ".017"
        self.config["risk"].update(entry_risk_min_fraction=fraction, entry_risk_max_fraction=fraction)

    async def held(self, message=None, reason=None):
        count = len(self.broker.submissions)
        result = await self.engine.handle(message or self.message())
        self.assertEqual(result["state"], "held", result)
        self.assertEqual(len(self.broker.submissions), count)
        if reason:
            self.assertIn(reason, result["reason"])
        return result

    async def test_missing_expiry_resolves_once_and_preserves_explicit_dates(self):
        from unittest.mock import AsyncMock
        self.broker.nearest_expiry = AsyncMock(return_value=CONTRACT)
        self.interpreter.decision["contract"] = CONTRACT | {"expiry": "nearest"}
        result = await self.engine.handle(self.message(content="OPEN BAC 63 call @ .80"))
        self.assertEqual(result["state"], "paper_order", result)
        self.assertEqual(self.broker.submissions[0]["contract"], CONTRACT)
        self.assertEqual(self.broker.nearest_expiry.call_args.args[0]["expiry"], NOW.date().isoformat())
        self.broker.nearest_expiry.reset_mock()
        decision = self.interpreter.decision | {"contract": CONTRACT}
        self.assertIs(await self.engine.resolve_expiry({}, decision), decision)
        self.broker.nearest_expiry.assert_not_awaited()

    async def test_implicit_expiry_anchors_original_new_york_day_and_cannot_roll(self):
        from unittest.mock import AsyncMock
        self.broker.nearest_expiry = AsyncMock(return_value=CONTRACT)
        message = self.message(timestamp="2026-09-09T00:30:00Z") | {"source_group": "source-a"}
        decision = self.interpreter.decision | {"contract": CONTRACT | {"expiry": "nearest"},
            "origin_message_id": message["id"], "evidence": [{"message_id": message["id"], "quote": message["content"]}]}
        self.now = datetime(2026, 9, 9, 1, tzinfo=timezone.utc)
        await self.engine.resolve_expiry(message, decision)
        self.assertEqual(self.broker.nearest_expiry.call_args.args[0]["expiry"], "2026-09-08")
        self.now = datetime(2026, 9, 9, 15, tzinfo=timezone.utc)
        self.broker.nearest_expiry.reset_mock()
        with self.assertRaisesRegex(Hold, "cannot roll"):
            await self.engine.resolve_expiry(message, decision)
        self.broker.nearest_expiry.assert_not_awaited()

    async def test_default_zero_dte_keeps_same_day_permission_gate(self):
        from unittest.mock import AsyncMock
        self.broker.nearest_expiry = AsyncMock(return_value=CONTRACT | {"expiry": NOW.date().isoformat()})
        self.interpreter.decision["contract"] = CONTRACT | {"expiry": "nearest"}
        await self.held(self.message(content="OPEN BAC 63 call @ .80"), "same-day expiry entries are disabled")

    async def test_open_and_duplicate_ids_or_equivalent_signal(self):
        message = self.message()
        self.assertEqual((await self.engine.handle(message))["state"], "paper_order")
        self.assertEqual((await self.engine.handle(message))["state"], "duplicate")
        await self.held(reason="duplicate")
        order = self.broker.submissions[0]
        self.assertEqual((order["side"], order["position_effect"], order["quantity"]), ("buy", "open", 1))
        self.assertEqual(self.store.positions()[0]["quantity"], 1)
        self.assertEqual(self.store.unresolved(), 0)

    async def test_baseline_import_and_edit_never_submit(self):
        for changes in ({"ingestion": "baseline"}, {"source": "export"}, {"ingestion": "import"}):
            message = self.message() | changes
            self.assertEqual((await self.engine.handle(message))["state"], "context")
            self.assertEqual((await self.engine.handle(message | {"revision": "edit"}))["state"], "context")
        message = self.message(edited_timestamp=NOW.isoformat())
        self.assertEqual((await self.engine.handle(message))["state"], "context")
        self.assertEqual(self.interpreter.calls, [])
        self.assertEqual(self.broker.submissions, [])

    async def test_history_analysis_and_context_channel_never_submit(self):
        result = await self.engine.handle(self.message() | {"ingestion": "import"}, analyze_history=True)
        self.assertEqual(result["state"], "held")
        self.assertEqual(len(self.interpreter.calls), 1)
        self.config["channels"][0]["role"] = "context"
        self.assertEqual((await self.engine.handle(self.message()))["state"], "context")
        self.assertEqual(self.broker.submissions, [])

    async def test_stale_future_invalid_and_late_model_response(self):
        for seconds in (-91, 6):
            await self.held(self.message(timestamp=(NOW + timedelta(seconds=seconds)).isoformat()), "timestamp")
        invalid = self.message() | {"timestamp": "2026-09-08T14:00:00"}
        self.assertEqual((await self.engine.handle(invalid))["state"], "invalid")
        original = self.interpreter.interpret

        async def delayed(*args):
            self.now += timedelta(seconds=91)
            return await original(*args)

        self.interpreter.interpret = delayed
        await self.held(reason="stale")

    async def test_identity_allowlist_and_kill_switch(self):
        for changes in ({"author_id": "999999999999999999"}, {"channel_id": "999999999999999999"}):
            self.assertEqual((await self.engine.handle(self.message(**changes)))["state"], "untrusted")
        Path(self.config["kill_switch"]).touch()
        await self.held(reason="kill switch")
        self.assertEqual(self.interpreter.calls, [])

    async def test_empty_author_filter_allows_unknown_author_in_processing_and_observation(self):
        channel = self.config["channels"][0]
        channel["authors"] = []
        message = self.message()
        self.engine.note_observation(message)
        self.assertEqual(self.engine.observations[message["id"]], message["revision"])
        self.assertTrue(channel_allows_author(channel, message["author_id"]))
        self.assertEqual((await self.engine.handle(message))["state"], "paper_order")

    async def test_explicit_author_filter_rejects_unknown_author_in_processing_and_observation(self):
        channel = self.config["channels"][0]
        message = self.message(author_id=SYNTHETIC_AUTHOR_ID)
        self.engine.note_observation(message)
        self.assertNotIn(message["id"], self.engine.observations)
        self.assertFalse(channel_allows_author(channel, SYNTHETIC_AUTHOR_ID))
        self.assertEqual((await self.engine.handle(message))["state"], "untrusted")

    async def test_confidence_ambiguity_and_unsupported_actions(self):
        for change in ({"confidence": .94}, {"confidence": True}, {"confidence": float("nan")},
                       {"ambiguous": True}, {"ambiguous": "false"}, {"action": "ADD"},
                       {"action": "UPDATE_STOP"}, {"action": "SELL_SHORT"}):
            with self.subTest(change=change):
                original = copy.deepcopy(self.interpreter.decision)
                self.interpreter.decision.update(change)
                await self.held()
                self.interpreter.decision = original

    async def test_expired_same_day_and_incomplete_contracts(self):
        for change in ({"expiry": "2026-09-07"}, {"expiry": "2026-09-08"}, {"expiry": "9/18"},
                       {"option_type": None}, {"strike": "0"}, {"strike": "NaN"}):
            with self.subTest(change=change):
                self.interpreter.decision["contract"] = CONTRACT | change
                await self.held()
        self.config["risk"]["allow_same_day_expiry"] = True
        self.interpreter.decision["contract"] = CONTRACT | {"expiry": "2026-09-08"}
        self.assertEqual((await self.engine.handle(self.message()))["state"], "paper_order")

    async def test_trim_one_contract_is_held_without_rounding_up(self):
        self.owned()
        self.interpreter.decision.update(action="REDUCE", fraction=.5)
        await self.held(reason="fractional")
        self.assertEqual(self.store.positions()[0]["quantity"], 1)

    async def test_close_only_owned_source_and_preserve_other_holdings(self):
        self.owned(2, "source-a")
        self.owned(3, "source-b")
        self.interpreter.decision.update(action="CLOSE", quantity=99)
        self.assertEqual((await self.engine.handle(self.message()))["state"], "paper_order")
        order = self.broker.submissions[0]
        self.assertEqual((order["side"], order["position_effect"], order["quantity"]), ("sell", "close", 2))
        self.assertEqual([(p["source_group"], p["quantity"]) for p in self.store.positions()], [("source-b", 3)])

    async def test_distinct_trims_are_not_blocked_by_action_frequency(self):
        self.owned(8)
        for fraction, content in ((.5, "Taking half here"), (.25, "Taking another quarter")):
            self.interpreter.decision.update(action="REDUCE", fraction=fraction)
            result = await self.engine.handle(self.message(content=content))
            self.assertEqual(result["state"], "paper_order", result)
        self.assertEqual([order["quantity"] for order in self.broker.submissions], [4, 1])
        self.assertEqual(self.store.positions()[0]["quantity"], 3)
        await self.held(self.message(content="Taking another quarter"), "duplicate")

    async def test_cross_source_close_and_unowned_sells_are_blocked(self):
        self.owned(group="source-b")
        self.interpreter.decision["action"] = "CLOSE"
        await self.held(self.message() | {"source_group": "source-b"}, "no position")
        self.assertEqual(self.store.positions()[0]["quantity"], 1)
        self.interpreter.decision.update(action="REDUCE", quantity=2)
        await self.held(self.message(channel=1), "exceeds")

    async def test_budget_chase_or_session_limits(self):
        self.config["risk"].update(entry_risk_min_fraction=".008", entry_risk_max_fraction=".008")
        await self.held(reason="maximum")
        self.config["risk"].update(entry_risk_min_fraction=".01", entry_risk_max_fraction=".01")
        self.interpreter.decision["alert_price"] = ".70"
        await self.held(reason="chase")
        self.interpreter.decision["alert_price"] = ".80"
        self.broker.account["buying_power"] = "1080"
        await self.held(reason="buying_power_reserve")
        self.broker.account["market_open"] = False
        await self.held(reason="session")

    async def test_publisher_lot_count_does_not_override_account_allocator(self):
        self.interpreter.decision["quantity"] = 999
        self.assertEqual((await self.engine.handle(self.message()))["state"], "paper_order")
        self.assertEqual(self.broker.submissions[0]["quantity"], 1)
        self.assertEqual(self.broker.submissions[0]["sizing"]["method"], "confidence_allocation_cap")

    async def test_quote_identity_tradability_price_and_freshness(self):
        variants = [{"tradable": False}, {"multiplier": 10}, {"currency": "EUR"},
                    {"asset_type": "future"}, {"contract": CONTRACT | {"strike": "64"}},
                    {"bid": ".90"}, {"bid": ".10"}, {"ask": "NaN"}, {"tick_size": "0"},
                    {"timestamp": (NOW - timedelta(seconds=16)).isoformat()},
                    {"timestamp": (NOW + timedelta(seconds=6)).isoformat()}]
        for quote in variants:
            with self.subTest(quote=quote):
                self.broker.quotes = quote
                await self.held()

    async def test_unknown_submission_blocks_every_source(self):
        self.broker.error = TimeoutError("fixture")
        self.assertEqual((await self.engine.handle(self.message()))["state"], "unknown")
        self.broker.error = None
        await self.held(self.message(channel=1), "unresolved")
        self.assertEqual(len(self.broker.submissions), 1)
        self.assertEqual(self.store.positions(), [])

    async def test_acknowledged_broker_id_survives_invalid_fill_payload(self):
        self.broker.result = dict(id="00000000-0000-0000-0000-000000000001", status="filled",
                                 filled_quantity="unreadable", fill_price=".80")
        self.assertEqual((await self.engine.handle(self.message()))["state"], "unknown")
        row = self.store.db.execute("SELECT broker_id,status FROM orders").fetchone()
        self.assertEqual((row["broker_id"], row["status"]), (self.broker.result["id"], "unknown"))
        self.assertEqual(self.store.positions(), [])

    async def test_reader_revision_and_newer_signal_abort_inflight_action(self):
        original = self.interpreter.interpret
        for newer in (False, True):
            message = self.message()

            async def superseded(*args):
                changed = self.message() if newer else message | {"revision": "changed"}
                self.engine.note_observation(changed)
                return await original(*args)

            self.interpreter.interpret = superseded
            await self.held(message, "newer" if newer else "revised")

    async def test_live_dispatch_requires_current_browser_verification(self):
        self.config["require_browser_verification"] = True
        await self.held(reason="verified")

        async def missing(message):
            return False

        self.engine.verify_current = missing
        await self.held(reason="verified")

        async def current(message):
            return True

        self.engine.verify_current = current
        self.assertEqual((await self.engine.handle(self.message()))["state"], "paper_order")

    async def test_quote_cannot_expire_during_browser_verification(self):
        self.config["require_browser_verification"] = True

        async def delayed(message):
            self.now += timedelta(seconds=16)
            return True

        self.engine.verify_current = delayed
        await self.held(reason="quote is stale")

    async def test_shadow_records_proposal_without_order_or_position_mutation(self):
        config = copy.deepcopy(self.config)
        config["mode"] = "shadow"
        config["robinhood"]["account_number"] = "00012345"
        store = Store(Path(self.temp.name) / "shadow.sqlite3")
        self.addCleanup(store.close)
        broker = Broker()
        broker.account["account_id"] = "00012345"
        engine = Engine(config, store, self.interpreter, broker, lambda: NOW)
        result = await engine.handle(self.message())
        self.assertEqual(result["state"], "shadow_order", result)
        self.assertEqual((broker.submissions, store.positions(), store.report()["orders"]), ([], [], {}))
        decision = json.loads(store.db.execute("SELECT decision FROM events").fetchone()[0])
        self.assertEqual(decision["order_proposal"]["quantity"], 1)
        self.assertEqual(decision["order_proposal"]["sizing"]["method"], "confidence_allocation_cap")
        broker.account["restrictions"] = ["fixture: options capability unavailable"]
        restricted = await engine.handle(self.message())
        self.assertEqual(restricted["state"], "held")
        self.assertIn("broker account restrictions", restricted["reason"])
        self.assertEqual(store.report()["events"].get("shadow_order"), 1)
        self.assertEqual((broker.submissions, store.report()["orders"]), ([], {}))
        with self.assertRaisesRegex(Hold, "another execution mode or account"):
            Engine(config | {"mode": "paper"}, store, self.interpreter, broker)
        config["robinhood"]["account_number"] = "00067890"
        with self.assertRaisesRegex(Hold, "another execution mode or account"):
            Engine(config, store, self.interpreter, broker)

    async def test_live_fake_requires_enabled_switch_and_matching_account(self):
        config = copy.deepcopy(self.config)
        config["mode"] = "live"
        config["robinhood"].update(account_number="00012345", enable_live_orders=False)
        store = Store(Path(self.temp.name) / "live-fixture.sqlite3")
        self.addCleanup(store.close)
        broker = Broker()
        with self.assertRaisesRegex(Hold, "not explicitly enabled"):
            Engine(config, store, self.interpreter, broker)
        config["robinhood"]["enable_live_orders"] = True
        engine = Engine(config, store, self.interpreter, broker, lambda: NOW)
        result = await engine.handle(self.message())
        self.assertEqual(result["state"], "held")
        self.assertIn("bound account", result["reason"])
        broker.account["account_id"] = "00012345"
        self.assertEqual((await engine.handle(self.message()))["state"], "broker_order")
        self.assertEqual(len(broker.submissions), 1)  # A local fake, never a network request.

    async def test_changes_during_native_review_prevent_placement_without_unknown_order(self):
        for change in ("kill", "revision", "newer", "expiry", "funds", "spread"):
            with self.subTest(change=change):
                config = copy.deepcopy(self.config)
                config["mode"] = "live"
                config["robinhood"].update(account_number="00012345", enable_live_orders=True)
                config["require_browser_verification"] = True
                store = Store(Path(self.temp.name) / (change + ".sqlite3"))
                self.addCleanup(store.close)
                broker = Broker()
                broker.account["account_id"] = "00012345"
                self.now = NOW
                engine = Engine(config, store, self.interpreter, broker, lambda: self.now)
                message = self.message()

                async def current(message):
                    return True

                async def review():
                    if change == "kill":
                        Path(config["kill_switch"]).touch()
                    elif change == "revision":
                        engine.note_observation(message | {"revision": "cancelled"})
                    elif change == "newer":
                        engine.note_observation(self.message(content="Cancel that entry"))
                    elif change == "expiry":
                        self.now += timedelta(seconds=91)
                    elif change == "spread":
                        broker.quotes["bid"] = ".10"
                    else:
                        broker.account["buying_power"] = "1000"

                engine.verify_current = current
                broker.review_hook = review
                result = await engine.handle(message)
                self.assertEqual(result["state"], "held", result)
                self.assertIn("no order submitted", result["reason"])
                self.assertEqual(broker.submissions, [])
                self.assertEqual((store.report()["orders"], store.unresolved(), store.positions()), ({"rejected": 1}, 0, []))
                self.assertIsNone(store.db.execute("SELECT broker_id FROM orders").fetchone()[0])
                Path(config["kill_switch"]).unlink(missing_ok=True)

    async def test_correction_cannot_refresh_old_entry_age(self):
        for seconds in (91, 10):
            origin = self.message(timestamp=(NOW - timedelta(seconds=seconds)).isoformat(),
                                  content="OPEN BAC 63 9/18 @ .80") | {"ingestion": "baseline"}
            self.assertEqual((await self.engine.handle(origin))["state"], "context")
            self.interpreter.decision.update(origin_message_id=origin["id"],
                                             evidence=[{"message_id": origin["id"], "quote": origin["content"]}])
            correction = self.message(content="call*")
            if seconds == 91:
                await self.held(correction, "stale")
            else:
                self.assertEqual((await self.engine.handle(correction))["state"], "paper_order")
        self.assertEqual(len(self.broker.submissions), 1)

    async def test_partial_cumulative_fills_reconcile_once(self):
        self.larger_orders()
        self.interpreter.decision["quantity"] = 3
        self.broker.result = dict(id="fixture", status="partially_filled", filled_quantity=1, fill_price=".75")
        self.assertEqual((await self.engine.handle(self.message()))["state"], "paper_order")
        order_id = self.broker.submissions[0]["client_order_id"]
        second = dict(id="fixture", status="partially_filled", filled_quantity=2, fill_price=".76")
        self.store.apply_result(order_id, second)
        self.store.apply_result(order_id, second)
        self.assertEqual((self.store.positions()[0]["quantity"], self.store.positions()[0]["average_price"]), (2, "0.76"))
        self.store.apply_result(order_id, second | {"status": "filled", "filled_quantity": 3, "fill_price": ".77"})
        self.assertEqual((self.store.positions()[0]["quantity"], self.store.unresolved()), (3, 0))
        with self.assertRaises(Hold):
            self.store.apply_result(order_id, second)

    async def test_incremental_fill_cannot_hide_behind_cumulative_average(self):
        self.larger_orders(2)
        self.interpreter.decision["quantity"] = 2
        self.broker.result = dict(id="fixture", status="partially_filled", filled_quantity=1, fill_price=".50")
        await self.engine.handle(self.message())
        order_id = self.broker.submissions[0]["client_order_id"]
        # The second fill would be 1.00, above the 0.80 limit, despite a 0.75 cumulative average.
        with self.assertRaises(Hold):
            self.store.apply_result(order_id, dict(id="fixture", status="filled", filled_quantity=2, fill_price=".75"))
        # A cumulative 0.25 average would imply a free second fill; reject that too.
        with self.assertRaises(Hold):
            self.store.apply_result(order_id, dict(id="fixture", status="filled", filled_quantity=2, fill_price=".25"))
        self.assertEqual(self.store.positions()[0]["quantity"], 1)

    async def test_partial_canceled_entry_still_consumes_concurrent_exposure(self):
        self.larger_orders(2)
        self.interpreter.decision["quantity"] = 2
        self.broker.result = dict(id="fixture", status="canceled", filled_quantity=1, fill_price=".80")
        self.assertEqual((await self.engine.handle(self.message()))["state"], "paper_order")
        self.assertEqual(self.broker.submissions[0]["quantity"], 2)
        self.config["risk"]["max_total_exposure_fraction"] = ".01"
        self.broker.account["option_exposure_by_symbol"] = {"BAC": "80"}
        self.interpreter.decision["contract"] = CONTRACT | {"symbol": "V"}
        self.broker.result = None
        await self.held(reason="total_option_exposure")

    async def test_repeated_entries_have_no_count_or_daily_turnover_limit(self):
        # Even stale legacy limits cannot reintroduce a hidden entry-count or turnover cap.
        self.config["risk"].update(max_entries_per_day=1, max_daily_entry_cost="1", max_contracts_per_order=1)
        for _ in range(6):
            for action in ("OPEN", "CLOSE"):
                self.interpreter.decision["action"] = action
                result = await self.engine.handle(self.message())
                self.assertEqual(result["state"], "paper_order", result)
        self.assertEqual(len(self.broker.submissions), 12)
        self.assertEqual(self.store.positions(), [])

    async def test_submitting_crash_reopens_as_unknown(self):
        message = self.message() | {"source_group": "source-a"}
        order = await self.engine.plan(message, self.interpreter.decision)
        self.store.reserve(message, self.interpreter.decision, order, NOW)
        self.store.close()
        self.store = Store(self.config["database"])
        self.engine.store = self.store
        self.assertEqual(self.store.report()["orders"], {"unknown": 1})
        await self.held(self.message(channel=1), "unresolved")

    async def test_owned_position_can_close_on_expiry_and_reenter_after_close(self):
        self.owned()
        self.interpreter.decision["action"] = "CLOSE"
        self.assertEqual((await self.engine.handle(self.message()))["state"], "paper_order")
        self.interpreter.decision["action"] = "OPEN"
        self.assertEqual((await self.engine.handle(self.message()))["state"], "paper_order")
        self.assertEqual([o["side"] for o in self.broker.submissions], ["sell", "buy"])
        same_day = CONTRACT | {"expiry": "2026-09-08"}
        self.owned(contract=same_day)
        self.interpreter.decision.update(action="CLOSE", contract=same_day)
        self.assertEqual((await self.engine.handle(self.message()))["state"], "paper_order")

    def test_config_cannot_enable_live_and_money_contract_validation(self):
        path = Path(self.temp.name) / "config.json"
        path.write_text(json.dumps(self.config | {"mode": "live"}))
        with self.assertRaises(Hold):
            load_config(path)
        for strike in (True, "Infinity", "-1"):
            with self.subTest(strike=strike), self.assertRaises(Hold):
                canonical_contract(CONTRACT | {"strike": strike})

    def test_loaded_confidence_is_numeric(self):
        config = copy.deepcopy(self.config)
        config["risk"]["min_confidence"] = "0.95"
        path = Path(self.temp.name) / "numeric-config.json"
        path.write_text(json.dumps(config))
        value = load_config(path)["risk"]["min_confidence"]
        self.assertIs(type(value), float)
        self.assertEqual(value, .95)

    def test_config_accepts_missing_or_empty_authors_and_rejects_malformed_filters(self):
        path = Path(self.temp.name) / "author-config.json"
        missing = copy.deepcopy(self.config)
        del missing["channels"][0]["authors"]
        path.write_text(json.dumps(missing))
        self.assertEqual(load_config(path)["channels"][0]["authors"], [])

        empty = copy.deepcopy(self.config)
        empty["channels"][0]["authors"] = []
        path.write_text(json.dumps(empty))
        self.assertEqual(load_config(path)["channels"][0]["authors"], [])

        for authors in (None, "2000000000000000001", [None], ["not-a-snowflake"], [True]):
            config = copy.deepcopy(self.config)
            config["channels"][0]["authors"] = authors
            path.write_text(json.dumps(config))
            with self.subTest(authors=authors), self.assertRaises(Hold):
                load_config(path)

    def test_sizing_floors_whole_contracts_and_preserves_cash_reserve(self):
        risk = self.config["risk"] | {"entry_risk_min_fraction": ".0081", "entry_risk_max_fraction": ".0081"}
        quantity, audit = entry_size(risk, self.broker.account, CONTRACT, 1, ".80", "source-a")
        self.assertEqual(quantity, 1)
        self.assertEqual(Decimal(audit["allocated_premium_risk"]), Decimal("81"))
        with self.assertRaises(Hold):
            entry_size(risk | {"entry_risk_min_fraction": ".00809999999999999999", "entry_risk_max_fraction": ".00809999999999999999"},
                       self.broker.account, CONTRACT, 1, ".80", "source-a")
        cash = self.broker.account | {"buying_power": "1081"}
        quantity, audit = entry_size(self.config["risk"], cash, CONTRACT, .99, ".80", "source-a")
        self.assertEqual(quantity, 1)
        self.assertEqual(audit["binding_limit"], "buying_power_reserve")
        self.assertGreaterEqual(Decimal(cash["buying_power"]) - Decimal(audit["allocated_premium_risk"]), Decimal("1000"))
        with self.assertRaises(Hold):
            entry_size(self.config["risk"], cash | {"buying_power": "1080.99"}, CONTRACT, .99, ".80", "source-a")

    def test_unknown_edge_uses_policy_cap_and_calibrated_kelly_only_reduces_it(self):
        risk = self.config["risk"]
        quantity, baseline = entry_size(risk, self.broker.account, CONTRACT, .99, ".80", "source-a")
        self.assertEqual((quantity, baseline["method"], Decimal(baseline["risk_fraction"])),
                         (1, "confidence_allocation_cap", Decimal(".01")))
        stats = {"calibrated": True, "win_probability": ".60", "payoff_ratio": "1"}
        calibrated = risk | {"entry_risk_min_fraction": ".10", "entry_risk_max_fraction": ".10",
                             "max_position_fraction": ".10", "strategy_stats": {"source-a": stats}}
        quantity, kelly = entry_size(calibrated, self.broker.account, CONTRACT, .99, ".80", "source-a")
        self.assertEqual((quantity, kelly["method"], Decimal(kelly["risk_fraction"])),
                         (6, "fractional_kelly", Decimal(".0495")))
        self.assertEqual(Decimal(kelly["confidence_cap_fraction"]), Decimal(".10"))
        # The other source cannot inherit a strategy's claimed edge.
        self.assertEqual(entry_size(calibrated, self.broker.account, CONTRACT, .99, ".80", "source-b")[0], 12)
        high_edge = calibrated | {"strategy_stats": {"source-a": stats | {"win_probability": ".9"}}}
        quantity, capped = entry_size(high_edge, self.broker.account, CONTRACT, .99, ".80", "source-a")
        self.assertEqual((quantity, Decimal(capped["risk_fraction"])), (12, Decimal(".10")))
        for probability in (".40", ".50"):
            with self.subTest(probability=probability), self.assertRaisesRegex(Hold, "no positive Kelly edge"):
                entry_size(risk | {"strategy_stats": {"source-a": stats | {"win_probability": probability}}},
                           self.broker.account, CONTRACT, .99, ".80", "source-a")

    def test_confidence_interpolates_endpoints_and_midpoint_without_second_discount(self):
        risk = self.config["risk"] | {"min_confidence": .8, "entry_risk_min_fraction": ".05",
                                      "entry_risk_max_fraction": ".10", "max_position_fraction": ".10",
                                      "buying_power_reserve_fraction": "0"}
        results = [entry_size(risk, self.broker.account, CONTRACT, c, ".80", "source-a") for c in (.8, .9, 1)]
        self.assertEqual([q for q, _ in results], [6, 9, 12])
        self.assertEqual([Decimal(a["budget"]) for _, a in results], [Decimal("500"), Decimal("750"), Decimal("1000")])
        self.assertEqual([Decimal(a["risk_fraction"]) for _, a in results], [Decimal(".05"), Decimal(".075"), Decimal(".10")])
        quantity, audit = entry_size(risk | {"min_confidence": 1}, self.broker.account, CONTRACT, 1, ".80", "source-a")
        self.assertEqual((quantity, Decimal(audit["risk_fraction"])), (12, Decimal(".10")))

    def test_small_affordable_contract_has_no_equity_floor_or_above_cap_override(self):
        risk = self.config["risk"] | {"min_confidence": .8, "entry_risk_min_fraction": ".05",
                                      "entry_risk_max_fraction": ".10", "max_position_fraction": ".10",
                                      "buying_power_reserve_fraction": "0"}
        account = self.broker.account | {"equity": "20", "buying_power": "20"}
        quantity, audit = entry_size(risk, account, CONTRACT, 1, ".01", "source-a")
        self.assertEqual((quantity, Decimal(audit["allocated_premium_risk"])), (1, Decimal("2")))
        for confidence, premium in ((.8, ".01"), (1, ".02")):
            with self.subTest(confidence=confidence, premium=premium), self.assertRaises(Hold):
                entry_size(risk, account, CONTRACT, confidence, premium, "source-a")

    def test_available_buying_power_reduces_quantity_below_confidence_cap(self):
        risk = self.config["risk"] | {"entry_risk_min_fraction": ".10", "entry_risk_max_fraction": ".10",
                                      "max_position_fraction": ".10", "buying_power_reserve_fraction": "0"}
        account = self.broker.account | {"buying_power": "200"}
        quantity, audit = entry_size(risk, account, CONTRACT, 1, ".80", "source-a")
        self.assertEqual((quantity, Decimal(audit["allocated_premium_risk"]), audit["binding_limit"]),
                         (2, Decimal("162"), "buying_power_reserve"))
        with self.assertRaises(Hold):
            entry_size(risk, account | {"buying_power": "80.99"}, CONTRACT, 1, ".80", "source-a")

    def test_complete_equity_and_account_exposure_are_required(self):
        for change in ({"equity": None}, {"equity": "NaN"}, {"equity": "0"}, {"equity": True},
                       {"buying_power": "-1"}, {"option_exposure_by_symbol": None},
                       {"option_exposure_by_symbol": {"bac": "10"}},
                       {"option_exposure_by_symbol": {"V": "-1"}},
                       {"option_exposure_by_symbol": {"BAC": "450"}},
                       {"option_exposure_by_symbol": {"V": "1990"}}):
            with self.subTest(change=change), self.assertRaises(Hold):
                entry_size(self.config["risk"], self.broker.account | change, CONTRACT, .99, ".80", "source-a")

    def test_sizing_config_rejects_uncalibrated_or_unbounded_inputs(self):
        for change in ({"entry_risk_min_fraction": "-.01"}, {"fractional_kelly": "1.01"},
                       {"entry_risk_min_fraction": ".10", "entry_risk_max_fraction": ".05"},
                       {"entry_risk_max_fraction": "1.01"},
                       {"max_position_fraction": True}, {"buying_power_reserve_fraction": "1"},
                       {"strategy_stats": {"source-a": {"calibrated": False, "win_probability": ".6", "payoff_ratio": "1"}}},
                       {"strategy_stats": {"source-a": {"calibrated": True, "win_probability": "1", "payoff_ratio": "1"}}}):
            config = copy.deepcopy(self.config)
            config["risk"].update(change)
            path = Path(self.temp.name) / "bad-sizing.json"
            path.write_text(json.dumps(config))
            with self.subTest(change=change), self.assertRaises(Hold):
                load_config(path)


if __name__ == "__main__":
    unittest.main()
