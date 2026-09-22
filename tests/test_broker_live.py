"""Offline checks only: every Robinhood response is supplied by a local fake."""
import copy
import json
from datetime import datetime, timezone
from decimal import Decimal
import importlib.util
from pathlib import Path
import unittest
from unittest.mock import AsyncMock

from relay.broker import BrokerError, BrokerPreflightHold, RobinhoodBroker, regular_session


@unittest.skipUnless(importlib.util.find_spec("exchange_calendars"), "Robinhood calendar extra is not installed")
class LiveBrokerChecks(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.now = datetime(2026, 9, 8, 15, tzinfo=timezone.utc)
        self.broker = RobinhoodBroker({"mode": "live", "risk": {"max_quote_age_seconds": 15},
            "robinhood": {"account_number": "TEST0001", "enable_live_orders": True}}, clock=lambda: self.now)
        self.contract = {"symbol": "SPY", "expiry": "2026-09-11", "strike": "500", "option_type": "call"}
        self.instrument = {"id": "11111111-1111-4111-8111-111111111111", "chain_id": "chain", "chain_symbol": "SPY",
            "underlying_type": "equity", "expiration_date": "2026-09-11", "strike_price": "500.00", "type": "call",
            "trade_value_multiplier": "100", "min_ticks": {"above_tick": "0.05", "below_tick": "0.01", "cutoff_price": "3"},
            "state": "active", "tradability": "tradable", "sellout_datetime": "2026-09-11T19:30:00Z"}
        self.chain = {"id": "chain", "symbol": "SPY", "expiration_dates": ["2026-09-11"], "cash_component": None,
            "underlying_instruments": [{"symbol": "SPY", "instrument": "fixture"}], "trade_value_multiplier": "100", "can_open_position": True}
        self.quote = {"instrument_id": self.instrument["id"], "bid_price": "0.95", "ask_price": "1.00", "bid_size": 10,
            "ask_size": 10, "mark_price": "0.975", "updated_at": self.now.isoformat()}
        self.equity_quote = {
            "symbol": "SPY", "last_trade_price": "501.00", "venue_last_trade_time": self.now.isoformat(),
            "has_traded": True, "state": "active",
        }
        self.account = {"account_number": "TEST0001", "agentic_allowed": True, "state": "active", "deactivated": False,
            "permanently_deactivated": False, "option_level": "option_level_2", "type": "cash"}
        self.portfolio = {"total_value": "1000", "equity_value": "20", "options_value": "0",
            "futures_value": "0", "event_contracts_value": "0", "crypto_value": "0", "cash": "20",
            "pending_deposits": "0", "mutual_funds_value": "0", "fixed_income_value": "0", "currency": "USD",
            "buying_power": {"buying_power": "900", "unleveraged_buying_power": "800", "display_currency": "USD"}}
        self.positions, self.orders, self.calls = [], [], []
        self.review_alert = {}
        self.omit_review_ratio = False
        self.place_error = False
        self.order = {"client_order_id": "local-order-1", "contract": self.contract, "side": "buy", "position_effect": "open",
            "quantity": 1, "limit_price": "1.00"}
        async def data(name, args):
            self.calls.append((name, copy.deepcopy(args)))
            if name == "get_accounts":
                return {"accounts": [self.account]}
            if name == "get_portfolio":
                self.assertEqual(args["account_number"], "TEST0001")
                return self.portfolio
            if name == "get_option_positions":
                return {"positions": self.positions}
            if name == "get_option_orders":
                return {"orders": [o for o in self.orders if not args.get("order_id") or o["id"] == args["order_id"]]}
            if name == "get_option_chains":
                return {"chains": [self.chain]}
            if name == "get_option_instruments":
                return {"instruments": [self.instrument]}
            if name == "get_option_quotes":
                return {"results": [{"quote": self.quote}]}
            if name == "get_equity_quotes":
                self.assertEqual(args, {"symbols": ["SPY"]})
                return {"results": [{"quote": self.equity_quote}]}
            if name == "search":
                return {"results": [{"symbol": "SPY", "instrument_id": "8f92e76f-1e0e-4478-8580-16a6ffcfaef5", "name": "SPY fixture"}]}
            if name == "review_option_order":
                response = dict(args, direction="debit" if args["legs"][0]["side"] == "buy" else "credit",
                    order_checks=self.review_alert, fees={"total_fee": "0.10"},
                    collateral={"account_number": "TEST0001", "cash": {"infinite": False, "amount": "0.00"}})
                if self.omit_review_ratio:
                    response["legs"] = [{key: value for key, value in leg.items() if key != "ratio_quantity"} for leg in args["legs"]]
                return response
            if name == "place_option_order":
                if self.place_error:
                    raise TimeoutError("fixture timeout")
                result = {"id": "22222222-2222-4222-8222-222222222222", "legs": args["legs"], "trade_value_multiplier": "100",
                    "type": "limit", "trigger": "immediate", "direction": "debit" if args["legs"][0]["side"] == "buy" else "credit",
                    "quantity": args["quantity"], "price": args["price"],
                    "processed_quantity": "1", "processed_premium": "100.00", "state": "filled", "created_at": self.now.isoformat()}
                self.orders.append(result)
                return {"order": result}
            raise AssertionError(name)
        self.broker._data = AsyncMock(side_effect=data)

    async def test_actual_total_value_cash_cap_and_exact_quote(self):
        snapshot = await self.broker.snapshot()
        self.assertEqual(snapshot["equity"], "1000")
        self.assertEqual(snapshot["buying_power"], "800")
        self.assertEqual(snapshot["option_exposure_by_symbol"], {})
        self.assertTrue(snapshot["market_open"])
        quote = await self.broker.quote(self.contract)
        self.assertEqual(quote["option_id"], self.instrument["id"])
        self.assertEqual(quote["timestamp"], self.quote["updated_at"])
        self.assertEqual(quote["tick_size"], "0.01")
        self.instrument["strike_price"] = "501"
        with self.assertRaisesRegex(BrokerError, "missing or ambiguous"):
            await self.broker.quote(self.contract)

    async def test_underlying_quote_uses_exact_active_regular_trade(self):
        quote = await self.broker.underlying_quote("spy")
        self.assertEqual(quote, {"symbol": "SPY", "price": "501.00", "timestamp": self.now.isoformat()})
        self.equity_quote["has_traded"] = False
        with self.assertRaisesRegex(BrokerError, "inactive or has not traded"):
            await self.broker.underlying_quote("SPY")

    def test_equity_quote_schema_pin_matches_private_fixture(self):
        fixture = json.loads(
            (Path(__file__).parent / "fixtures" / "robinhood-equity-quote-schema.json").read_text()
        )
        tool = fixture["get_equity_quotes"]["tool"]
        self.broker.catalog["get_equity_quotes"] = tool
        self.assertIs(self.broker._qualified("get_equity_quotes"), tool)

    def test_regular_session_returns_xnys_early_close(self):
        regular = regular_session(datetime(2026, 9, 8, 15, tzinfo=timezone.utc))
        self.assertEqual(regular[0], datetime(2026, 9, 8, 13, 30, tzinfo=timezone.utc))
        self.assertEqual(regular[1], datetime(2026, 9, 8, 20, tzinfo=timezone.utc))
        early = regular_session(datetime(2026, 11, 27, 17, 5, tzinfo=timezone.utc))
        self.assertEqual(early[1], datetime(2026, 11, 27, 18, tzinfo=timezone.utc))
        self.assertIsNone(regular_session(datetime(2026, 11, 26, 15, tzinfo=timezone.utc)))

    async def test_account_overview_projects_negative_short_adjusted_and_stale(self):
        self.positions = [{
            "option_id": self.instrument["id"],
            "quantity": "2",
            "type": "short",
            "trade_value_multiplier": "150",
            "average_price": "-120",
        }]
        self.quote["updated_at"] = "2026-09-01T15:00:00+00:00"

        overview = await self.broker.account_overview()

        self.assertEqual(overview["currency"], "USD")
        self.assertEqual(overview["equity"], "1000")
        self.assertEqual(overview["cash"], "20")
        self.assertEqual(overview["buying_power"], "900")
        self.assertEqual(overview["unleveraged_buying_power"], "800")
        position = overview["positions"][0]
        self.assertEqual(position["position_type"], "short")
        self.assertEqual(position["quantity"], "2")
        self.assertEqual(position["average_price"], "-0.8")
        self.assertEqual(Decimal(position["market_value"]), Decimal("-292.500"))
        self.assertEqual(position["multiplier"], "150")
        self.assertEqual(position["quote_timestamp"], self.quote["updated_at"])

    async def test_account_overview_keeps_position_when_quote_unavailable_and_propagates_auth(self):
        self.positions = [{
            "option_id": self.instrument["id"],
            "quantity": "1",
            "type": "long",
            "trade_value_multiplier": "100",
            "average_price": "120",
        }]
        self.broker._raw_quote = AsyncMock(side_effect=BrokerError("quote unavailable"))
        overview = await self.broker.account_overview()
        self.assertEqual(len(overview["positions"]), 1)
        self.assertIsNone(overview["positions"][0]["market_value"])
        self.assertIsNone(overview["positions"][0]["quote_timestamp"])

        self.broker._raw_quote = AsyncMock(
            side_effect=BrokerError("Robinhood authorization required")
        )
        with self.assertRaisesRegex(BrokerError, "authorization"):
            await self.broker.account_overview()

    async def test_account_changed_only_tracks_actual_submission_and_changed_status(self):
        result = await self.broker.submit(self.order)
        self.assertTrue(self.broker.account_changed.is_set())

        self.broker.account_changed.clear()
        self.assertEqual(result, await self.broker.submit(self.order))
        self.order["entry_evaluation"] = {"ask": "1.14", "ask_deviation_percent": "+14"}
        self.assertEqual(result, await self.broker.submit(self.order))
        with self.assertRaisesRegex(BrokerError, "reused"):
            await self.broker.submit(self.order | {"quantity": 2})
        self.assertFalse(self.broker.account_changed.is_set())

        self.broker.account_changed.clear()
        await self.broker.order_status(result["id"])
        self.assertTrue(self.broker.account_changed.is_set())
        self.orders[0]["state"] = "partially_filled"
        self.orders[0]["processed_quantity"] = "0"
        self.orders[0]["processed_premium"] = "0"
        self.broker.account_changed.clear()
        await self.broker.order_status(result["id"])
        self.assertTrue(self.broker.account_changed.is_set())

        self.broker.account_changed.clear()
        await self.broker.order_status(result["id"])
        self.assertFalse(self.broker.account_changed.is_set())

    async def test_account_changed_marks_unknown_submission_but_not_preflight_hold(self):
        self.broker.account_changed.clear()
        self.place_error = True
        with self.assertRaises(TimeoutError):
            await self.broker.submit(dict(self.order, client_order_id="unknown-result"))
        self.assertTrue(self.broker.account_changed.is_set())

        self.broker.account_changed.clear()
        async def block_before_submit(snapshot, quote):
            raise ValueError("fixture changed")

        with self.assertRaises(BrokerPreflightHold):
            await self.broker.submit(
                dict(self.order, client_order_id="preflight-hold"),
                before_submit=block_before_submit,
            )
        self.assertFalse(self.broker.account_changed.is_set())

    async def test_captured_blank_underlying_symbol_is_verified_without_url_fetch(self):
        # Sanitized shape of the actual SPY chain response observed on 2026-09-06.
        self.chain["underlying_instruments"] = [{"symbol": "", "instrument":
            "http://internal.invalid/instruments/8f92e76f-1e0e-4478-8580-16a6ffcfaef5/"}]
        quote = await self.broker.quote(self.contract)
        self.assertEqual(quote["option_id"], self.instrument["id"])
        self.assertTrue(any(name == "search" and args == {"query": "SPY", "asset_type": "instrument", "limit": 20}
                            for name, args in self.calls))
        self.chain["underlying_instruments"][0]["instrument"] = "http://internal.invalid/instruments/99999999-9999-4999-8999-999999999999/"
        with self.assertRaisesRegex(BrokerError, "could not verify"):
            await self.broker.quote(self.contract)

    async def test_account_positions_and_external_buy_commitments_are_included(self):
        self.positions = [{"option_id": self.instrument["id"], "quantity": "1", "type": "long", "trade_value_multiplier": "100",
            "average_price": "120", "pending_sell_quantity": "1", "pending_exercise_quantity": "0",
            "pending_assignment_quantity": "0", "pending_expiration_quantity": "0", "pending_buy_quantity": "0"}]
        self.orders = [{"id": "working", "state": "confirmed", "type": "limit", "trigger": "immediate",
            "legs": [{"option_id": self.instrument["id"], "side": "buy", "position_effect": "open", "ratio_quantity": 1}], "trade_value_multiplier": "100",
            "chain_symbol": "SPY", "price": "1.50", "pending_quantity": "2"}]
        snapshot = await self.broker.snapshot()
        self.assertEqual(snapshot["option_exposure_by_symbol"], {"SPY": "420.00"})
        self.assertEqual(snapshot["positions"][0]["available_quantity"], 0)
        self.positions[0]["pending_sell_quantity"] = "0"
        self.orders = [dict(self.orders[0], legs=[{"option_id": self.instrument["id"], "side": "sell", "position_effect": "close", "ratio_quantity": 1}], pending_quantity="1")]
        self.assertEqual((await self.broker.snapshot())["positions"][0]["available_quantity"], 0)
        self.positions[0]["type"] = "short"
        with self.assertRaisesRegex(BrokerError, "Short"):
            await self.broker.snapshot()
        self.positions[0]["type"] = "long"
        self.quote["updated_at"] = "2026-09-07T15:00:00Z"
        with self.assertRaisesRegex(BrokerError, "stale"):
            await self.broker.snapshot()

    async def test_review_then_place_uses_account_and_uuid_idempotency(self):
        self.omit_review_ratio = True
        result = await self.broker.submit(self.order)
        self.assertEqual(result["id"], "22222222-2222-4222-8222-222222222222")
        self.assertEqual(result["fill_price"], "1.00")
        self.assertEqual(result, await self.broker.submit(self.order))
        calls = [(name, args) for name, args in self.calls if name in {"review_option_order", "place_option_order"}]
        self.assertEqual([name for name, _ in calls], ["review_option_order", "place_option_order"])
        self.assertEqual(calls[1][1]["ref_id"], result["ref_id"])
        self.assertEqual(calls[1][1]["account_number"], "TEST0001")
        self.assertNotIn("chain_symbol", calls[1][1])
        self.assertNotIn("direction", calls[1][1])
        reconciled = await self.broker.order_status(result["id"])
        self.assertEqual(reconciled["filled_quantity"], 1)

    async def test_review_alert_shadow_and_uncertain_dispatch_do_not_repeat(self):
        self.review_alert = {"alertType": "fixture_rejection"}
        with self.assertRaisesRegex(BrokerError, "pre-trade"):
            await self.broker.submit(self.order)
        self.assertFalse(any(name == "place_option_order" for name, _ in self.calls))
        self.review_alert = {}
        self.place_error = True
        with self.assertRaises(TimeoutError):
            await self.broker.submit(self.order)
        with self.assertRaisesRegex(BrokerError, "unknown"):
            await self.broker.submit(self.order)
        self.assertEqual(sum(name == "place_option_order" for name, _ in self.calls), 1)
        self.broker.runtime["mode"] = "shadow"
        with self.assertRaisesRegex(BrokerError, "Live Robinhood"):
            await self.broker.submit(dict(self.order, client_order_id="different"))

    async def test_final_callback_blocks_after_review_before_any_order_transport(self):
        async def final_check(snapshot, quote):
            self.assertEqual(self.calls[-1][0], "review_option_order")
            self.assertEqual(snapshot["account_id"], "TEST0001")
            self.assertEqual(quote["option_id"], self.instrument["id"])
            raise ValueError("fixture changed Discord message")
        with self.assertRaises(BrokerPreflightHold):
            await self.broker.submit(self.order, before_submit=final_check)
        self.assertNotIn(self.order["client_order_id"], self.broker.attempted)
        self.assertFalse(any(name == "place_option_order" for name, _ in self.calls))
        final = AsyncMock()
        result = await self.broker.submit(self.order, before_submit=final)
        final.assert_awaited_once()
        self.assertEqual(result["status"], "filled")

    async def test_calendar_holidays_and_schema_drift_fail_closed(self):
        for day in (6, 7):
            self.now = datetime(2026, 9, day, 15, tzinfo=timezone.utc)
            self.assertFalse(self.broker._market_open())
        self.broker.catalog["get_accounts"] = {"inputSchema": {}, "outputSchema": {}}
        with self.assertRaisesRegex(BrokerError, "schema changed"):
            self.broker._qualified("get_accounts")

    async def test_nearest_expiry_uses_listed_matching_standard_contracts(self):
        dates = ["2026-09-18", "2026-09-08", "2026-09-11"]
        chain = self.chain | {"expiration_dates": dates}
        rows = [self.instrument | {"expiration_date": expiry, "state": "active", "tradability": "tradable"}
                for expiry in dates]
        async def pages(name, args, key):
            if name == "get_option_chains":
                return [chain]
            self.assertEqual(args["strike_price"], "500")
            self.assertEqual(args["type"], "call")
            requested_dates = args["expiration_dates"].split(",")
            self.assertLessEqual(len(requested_dates), 7)
            self.assertEqual(args["chain_symbol"], "SPY")
            return [row for row in rows if row["expiration_date"] in requested_dates]
        self.broker._pages = AsyncMock(side_effect=pages)
        request = self.contract | {"expiry": "2026-09-08"}
        self.assertEqual((await self.broker.nearest_expiry(request))["expiry"], "2026-09-08")
        rows[1]["tradability"] = "untradable"
        self.assertEqual((await self.broker.nearest_expiry(request))["expiry"], "2026-09-11")
        rows[2]["strike_price"] = "501"
        self.assertEqual((await self.broker.nearest_expiry(request))["expiry"], "2026-09-18")
        rows[0]["type"] = "put"
        with self.assertRaisesRegex(BrokerError, "No listed expiration"):
            await self.broker.nearest_expiry(request)


if __name__ == "__main__":
    unittest.main()
