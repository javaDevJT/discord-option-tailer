import asyncio
import copy
import json
from pathlib import Path
import threading
import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import AsyncMock, patch

from relay.broker import BrokerError, RobinhoodBroker


CONTRACT = {
    "symbol": "SPY",
    "expiry": "2026-09-11",
    "strike": "500",
    "option_type": "call",
}


class FixtureBroker(RobinhoodBroker):
    def __init__(self):
        self.now = datetime(2026, 9, 8, 15, tzinfo=timezone.utc)
        super().__init__(
            {
                "mode": "live",
                "enable_live_orders": True,
                "robinhood": {"account_number": "TEST0001"},
            },
            clock=lambda: self.now,
        )
        self.currency = "USD"
        self.underlying_id = "8f92e76f-1e0e-4478-8580-16a6ffcf aef5".replace(" ", "")
        self.chain = {
            "id": "chain",
            "symbol": "SPY",
            "expiration_dates": ["2026-09-11", "2026-09-18"],
            "cash_component": None,
            "underlying_instruments": [
                {
                    "symbol": "SPY",
                    "instrument": f"https://fixture.invalid/instruments/{self.underlying_id}/",
                }
            ],
            "trade_value_multiplier": "100",
            "can_open_position": True,
        }
        self.instrument_by_expiry = {
            "2026-09-11": self._instrument(
                "11111111-1111-4111-8111-111111111111", "2026-09-11", "500.00"
            ),
            "2026-09-18": self._instrument(
                "22222222-2222-4222-8222-222222222222", "2026-09-18", "500.00"
            ),
        }
        self.raw_by_id = {
            option_id: {
                "instrument_id": option_id,
                "bid_price": "0.95",
                "ask_price": "1.00",
                "bid_size": 10,
                "ask_size": 10,
                "mark_price": "0.975",
                "updated_at": self.now.isoformat(),
            }
            for option_id in (instrument["id"] for instrument in self.instrument_by_expiry.values())
        }
        self.page_calls = []
        self.search_calls = 0
        self.raw_calls = []

    @staticmethod
    def _instrument(option_id, expiry, strike):
        return {
            "id": option_id,
            "chain_id": "chain",
            "chain_symbol": "SPY",
            "underlying_type": "equity",
            "expiration_date": expiry,
            "strike_price": strike,
            "type": "call",
            "trade_value_multiplier": "100",
            "min_ticks": {
                "above_tick": "0.05",
                "below_tick": "0.01",
                "cutoff_price": "3",
            },
            "state": "active",
            "tradability": "tradable",
            "sellout_datetime": "2026-09-11T19:30:00Z",
        }

    async def _pages(self, name, args, key):
        self.page_calls.append((name, copy.deepcopy(args)))
        if name == "get_option_chains":
            return [copy.deepcopy(self.chain)]
        if name == "get_option_instruments":
            if "ids" in args:
                return [copy.deepcopy(row) for row in self.instrument_by_expiry.values() if row["id"] == args["ids"]]
            if args.get("chain_symbol") == "SPY":
                return [copy.deepcopy(row) for row in self.instrument_by_expiry.values()
                        if row["expiration_date"] in args["expiration_dates"].split(",")]
            return [copy.deepcopy(self.instrument_by_expiry[args["expiration_dates"]])]
        raise AssertionError(f"unexpected page request: {name}")

    async def _data(self, name, args):
        if name == "search":
            self.search_calls += 1
            return {
                "results": [
                    {"symbol": "SPY", "instrument_id": self.underlying_id},
                ]
            }
        raise AssertionError(f"unexpected data request: {name}")

    async def _raw_quote(self, option_id):
        self.raw_calls.append(option_id)
        return dict(self.raw_by_id[option_id])


class SubmitProbe(RobinhoodBroker):
    def __init__(self):
        super().__init__(
            {
                "mode": "live",
                "enable_live_orders": True,
                "robinhood": {"account_number": "TEST0001"},
            }
        )
        self.currency = "USD"
        self.release = asyncio.Event()
        self.snapshot_started = asyncio.Event()
        self.order_args_started = asyncio.Event()
        self.snapshot_cancelled = asyncio.Event()
        self.order_args_cancelled = asyncio.Event()

    def _live_enabled(self):
        return None

    def _market_open(self):
        return True

    async def snapshot(self):
        self.snapshot_started.set()
        try:
            await self.release.wait()
        except asyncio.CancelledError:
            self.snapshot_cancelled.set()
            raise
        return {
            "restrictions": [],
            "market_open": True,
            "buying_power": "1000",
            "positions": [],
        }

    async def _order_args(self, order):
        self.order_args_started.set()
        try:
            await self.release.wait()
        except asyncio.CancelledError:
            self.order_args_cancelled.set()
            raise
        return (
            {
                "option_id": "11111111-1111-4111-8111-111111111111",
                "side": "buy",
                "position_effect": "open",
                "quantity": 1,
                "price": "1.00",
            },
            {
                "option_id": "11111111-1111-4111-8111-111111111111",
                "timestamp": "2026-09-08T15:00:00+00:00",
            },
        )

    async def _review_args(self, args, quote):
        return {}, Decimal("0")

    def _fresh_quote(self, quote):
        return None

    def _check_quote_age(self, timestamp, label="Option quote"):
        return None

    async def _data(self, name, args):
        self.assert_name = name
        return {"order": {"id": "result", "status": "filled"}}

    def _order_result(self, raw, client_id, expected=None):
        return {"id": raw["id"], "status": raw["status"], "client_order_id": client_id}


class JevBrokerLatencyTests(unittest.IsolatedAsyncioTestCase):
    async def test_exact_contract_reuses_discovery_but_refreshes_quote_and_authority(self):
        broker = FixtureBroker()

        first = await broker.quote(CONTRACT)
        option_id = broker.instrument_by_expiry[CONTRACT["expiry"]]["id"]
        instrument = broker.instrument_by_expiry[CONTRACT["expiry"]]
        instrument["min_ticks"] = {
            "above_tick": "0.10",
            "below_tick": "0.05",
            "cutoff_price": "1",
        }
        instrument["state"] = "inactive"
        instrument["tradability"] = "untradable"
        broker.chain["can_open_position"] = False
        broker.raw_by_id[option_id].update(
            {
                "bid_price": "1.05",
                "ask_price": "1.10",
                "mark_price": "1.075",
            }
        )

        second = await broker.quote(dict(CONTRACT))

        self.assertEqual(first["bid"], "0.95")
        self.assertEqual(second["bid"], "1.05")
        self.assertEqual(second["tick_size"], "0.10")
        self.assertFalse(second["tradable"])
        self.assertFalse(second["can_open_position"])
        self.assertEqual(broker.raw_calls, [option_id, option_id])
        self.assertEqual(
            [name for name, _ in broker.page_calls],
            ["get_option_chains", "get_option_instruments", "get_option_chains", "get_option_instruments"],
        )
        instrument_queries = [
            args for name, args in broker.page_calls if name == "get_option_instruments"
        ]
        self.assertEqual(
            instrument_queries[0],
            {
                "chain_symbol": "SPY",
                "expiration_dates": "2026-09-11",
                "strike_price": "500",
                "type": "call",
                "state": "active",
                "tradability": "tradable",
            },
        )
        self.assertEqual(broker.page_calls[-1][1], {"ids": option_id})
        self.assertEqual(set(broker.contracts[next(iter(broker.contracts))]), {"chain_id", "option_id"})
        self.assertEqual(broker.search_calls, 0)

    async def test_changed_contract_does_not_reuse_cached_metadata(self):
        broker = FixtureBroker()

        first = await broker.quote(CONTRACT)
        changed = {**CONTRACT, "expiry": "2026-09-18"}
        second = await broker.quote(changed)

        first_id = broker.instrument_by_expiry[CONTRACT["expiry"]]["id"]
        second_id = broker.instrument_by_expiry[changed["expiry"]]["id"]
        self.assertNotEqual(first["option_id"], second["option_id"])
        self.assertEqual(first["option_id"], first_id)
        self.assertEqual(second["option_id"], second_id)
        self.assertEqual(
            [name for name, _ in broker.page_calls],
            [
                "get_option_chains",
                "get_option_instruments",
                "get_option_chains",
                "get_option_instruments",
            ],
        )
        self.assertEqual(broker.search_calls, 0)

    async def test_warm_submit_overlaps_reads_and_cleans_up_on_cancellation(self):
        broker = SubmitProbe()
        order = {
            "client_order_id": "warm-submit",
            "contract": CONTRACT,
            "side": "buy",
            "position_effect": "open",
            "quantity": 1,
            "limit_price": "1.00",
        }

        task = asyncio.create_task(broker.submit(order))
        await asyncio.wait_for(broker.snapshot_started.wait(), 1)
        await asyncio.wait_for(broker.order_args_started.wait(), 1)
        self.assertFalse(task.done())
        broker.release.set()
        result = await task
        self.assertEqual(result["status"], "filled")

        broker = SubmitProbe()
        task = asyncio.create_task(broker.submit(order | {"client_order_id": "cancel-submit"}))
        await asyncio.wait_for(broker.snapshot_started.wait(), 1)
        await asyncio.wait_for(broker.order_args_started.wait(), 1)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        await asyncio.wait_for(broker.snapshot_cancelled.wait(), 1)
        await asyncio.wait_for(broker.order_args_cancelled.wait(), 1)


class SnapshotProbe(RobinhoodBroker):
    def __init__(self):
        self.now = datetime(2026, 9, 8, 15, tzinfo=timezone.utc)
        super().__init__(
            {
                "mode": "live",
                "enable_live_orders": True,
                "robinhood": {"account_number": "TEST0001"},
                "risk": {"max_quote_age_seconds": 15},
            },
            clock=lambda: self.now,
        )
        self.instrument_started = asyncio.Event()
        self.release_instruments = asyncio.Event()
        self.quote_started = asyncio.Event()
        self.release_quotes = asyncio.Event()
        self.quote_cancelled = asyncio.Event()
        self.instrument_calls = []
        self.quote_calls = []
        self.quote_age_checks = []
        self.fixture_instruments = {
            "opt-a": self._fixture_instrument("opt-a", "2026-09-11", "500"),
            "opt-b": self._fixture_instrument("opt-b", "2026-09-18", "505"),
        }
        self.fixture_quotes = {
            option_id: {
                "instrument_id": option_id,
                "bid_price": "0.95",
                "ask_price": "1.00",
                "mark_price": "0.975",
                "updated_at": self.now.isoformat(),
            }
            for option_id in self.fixture_instruments
        }

    @staticmethod
    def _fixture_instrument(option_id, expiry, strike):
        return {
            "id": option_id,
            "chain_id": "chain",
            "chain_symbol": "SPY",
            "underlying_type": "equity",
            "expiration_date": expiry,
            "strike_price": strike,
            "type": "call",
            "trade_value_multiplier": "100",
        }

    @staticmethod
    def _position(option_id):
        return {
            "option_id": option_id,
            "quantity": "1",
            "type": "long",
            "trade_value_multiplier": "100",
            "average_price": "1.00",
            "pending_buy_quantity": "0",
            "pending_sell_quantity": "0",
            "pending_exercise_quantity": "0",
            "pending_assignment_quantity": "0",
            "pending_expiration_quantity": "0",
        }

    async def _data(self, name, args):
        if name == "get_accounts":
            return {
                "accounts": [
                    {
                        "account_number": "TEST0001",
                        "type": "individual",
                        "agentic_allowed": True,
                        "state": "active",
                        "deactivated": False,
                        "permanently_deactivated": False,
                        "option_level": "option_level_2",
                    }
                ]
            }
        if name == "get_portfolio":
            return {
                "currency": "USD",
                "total_value": "1000",
                "buying_power": {
                    "display_currency": "USD",
                    "buying_power": "1000",
                    "unleveraged_buying_power": "1000",
                },
            }
        raise AssertionError(f"unexpected data request: {name}")

    async def _pages(self, name, args, key):
        if name == "get_option_positions":
            return [self._position("opt-a"), self._position("opt-b")]
        if name == "get_option_orders":
            return []
        raise AssertionError(f"unexpected page request: {name}")

    async def _instrument(self, option_id):
        self.instrument_calls.append(option_id)
        if len(self.instrument_calls) == 2:
            self.instrument_started.set()
        await self.release_instruments.wait()
        return copy.deepcopy(self.fixture_instruments[option_id])

    async def _raw_quote(self, option_id):
        self.quote_calls.append(option_id)
        if len(self.quote_calls) == 2:
            self.quote_started.set()
        try:
            await self.release_quotes.wait()
        except asyncio.CancelledError:
            self.quote_cancelled.set()
            raise
        return copy.deepcopy(self.fixture_quotes[option_id])

    def _check_quote_age(self, timestamp, label="Option quote"):
        self.quote_age_checks.append(timestamp)
        return super()._check_quote_age(timestamp, label)

    def _market_open(self):
        return True


class FailingSnapshotProbe(SnapshotProbe):
    def __init__(self):
        super().__init__()
        self.sibling_cancelled = asyncio.Event()

    async def _instrument(self, option_id):
        self.instrument_calls.append(option_id)
        if len(self.instrument_calls) == 2:
            self.instrument_started.set()
        if option_id == "opt-b":
            await self.instrument_started.wait()
            raise BrokerError("fixture instrument failure")
        try:
            await self.release_instruments.wait()
        except asyncio.CancelledError:
            self.sibling_cancelled.set()
            raise
        return copy.deepcopy(self.fixture_instruments[option_id])


class ExpiryProbe(RobinhoodBroker):
    def __init__(self, *, failing_chain=None):
        self.now = datetime(2026, 9, 8, 15, tzinfo=timezone.utc)
        super().__init__(
            {
                "mode": "live",
                "enable_live_orders": True,
                "robinhood": {"account_number": "TEST0001"},
            },
            clock=lambda: self.now,
        )
        self.failing_chain = failing_chain
        self.page_calls = []
        self.instrument_started = []
        self.all_instruments_started = asyncio.Event()
        self.release_instruments = asyncio.Event()
        self.sibling_cancelled = asyncio.Event()
        self.chains = [
            self._chain("chain-a", ["2026-09-11", "2026-09-18"]),
            self._chain("chain-b", ["2026-09-11", "2026-09-20"]),
        ]
        self.rows_by_chain = {
            chain["id"]: [
                self._instrument(chain["id"], expiry, f"{chain['id']}-{expiry}")
            for expiry in chain["expiration_dates"]
            ]
            for chain in self.chains
        }
        self.rows_by_chain["chain-b"][0]["strike_price"] = "501.00"

    @staticmethod
    def _chain(chain_id, expiries):
        return {
            "id": chain_id,
            "symbol": "SPY",
            "expiration_dates": expiries,
            "cash_component": None,
            "underlying_instruments": [{"symbol": "SPY", "instrument": "fixture"}],
            "trade_value_multiplier": "100",
        }

    @staticmethod
    def _instrument(chain_id, expiry, option_id):
        return {
            "id": option_id,
            "chain_id": chain_id,
            "chain_symbol": "SPY",
            "underlying_type": "equity",
            "expiration_date": expiry,
            "strike_price": "500.00",
            "type": "call",
            "trade_value_multiplier": "100",
            "state": "active",
            "tradability": "tradable",
        }

    async def _pages(self, name, args, key):
        self.page_calls.append((name, copy.deepcopy(args)))
        if name == "get_option_chains":
            return copy.deepcopy(self.chains)
        if name != "get_option_instruments":
            raise AssertionError(f"unexpected page request: {name}")
        if args.get("chain_symbol") == "SPY":
            self.instrument_started.append("combined")
            self.all_instruments_started.set()
            try:
                if self.failing_chain is not None:
                    raise BrokerError("fixture instrument failure")
                await self.release_instruments.wait()
            except asyncio.CancelledError:
                self.sibling_cancelled.set()
                raise
            expiries = args["expiration_dates"].split(",")
            return copy.deepcopy([
                instrument
                for rows in self.rows_by_chain.values()
                for instrument in rows
                if instrument["expiration_date"] in expiries
            ])
        chain_id = args["chain_id"]
        self.instrument_started.append(chain_id)
        if len(set(self.instrument_started)) == len(self.chains):
            self.all_instruments_started.set()
        try:
            if chain_id == self.failing_chain:
                await self.all_instruments_started.wait()
                raise BrokerError("fixture instrument failure")
            await self.release_instruments.wait()
        except asyncio.CancelledError:
            self.sibling_cancelled.set()
            raise
        return copy.deepcopy(self.rows_by_chain[chain_id])


class PortfolioProbe(RobinhoodBroker):
    def __init__(self):
        self.now = datetime(2026, 9, 8, 15, tzinfo=timezone.utc)
        super().__init__(
            {
                "mode": "live",
                "enable_live_orders": True,
                "robinhood": {"account_number": "TEST0001"},
            },
            clock=lambda: self.now,
        )
        self.portfolio_started = asyncio.Event()
        self.release_portfolio = asyncio.Event()
        self.portfolio_cancelled = asyncio.Event()
        self.portfolio_calls = 0

    async def _data(self, name, args):
        if name != "get_portfolio":
            raise AssertionError(f"unexpected data request: {name}")
        self.portfolio_calls += 1
        self.portfolio_started.set()
        try:
            await self.release_portfolio.wait()
        except asyncio.CancelledError:
            self.portfolio_cancelled.set()
            raise
        return {
            "currency": "USD",
            "total_value": "1000",
            "buying_power": {
                "display_currency": "USD",
                "buying_power": "1000",
                "unleveraged_buying_power": "1000",
            },
        }


class SnapshotLatencyTests(unittest.IsolatedAsyncioTestCase):
    async def test_snapshot_overlaps_position_reads_and_refreshes_quotes(self):
        broker = SnapshotProbe()
        task = asyncio.create_task(broker.snapshot())

        await asyncio.wait_for(broker.instrument_started.wait(), 1)
        self.assertFalse(task.done())
        await asyncio.wait_for(broker.quote_started.wait(), 1)
        self.assertFalse(task.done())
        broker.release_instruments.set()
        broker.release_quotes.set()

        first = await task
        first_value = first["positions"][0]["market_value"]
        self.assertEqual(broker.instrument_calls, ["opt-a", "opt-b"])
        self.assertEqual(broker.quote_calls, ["opt-a", "opt-b"])
        self.assertEqual(len(broker.quote_age_checks), 4)
        self.assertTrue(first["agentic_allowed"])

        broker.fixture_quotes["opt-a"]["mark_price"] = "1.50"
        second = await broker.snapshot()
        self.assertNotEqual(second["positions"][0]["market_value"], first_value)
        self.assertEqual(broker.quote_calls.count("opt-a"), 2)
        self.assertEqual(len(broker.quote_age_checks), 8)

        broker.fixture_quotes["opt-b"]["updated_at"] = "2026-09-08T14:00:00+00:00"
        with self.assertRaises(BrokerError):
            await broker.snapshot()

    async def test_snapshot_cancels_sibling_reads_on_instrument_failure(self):
        broker = FailingSnapshotProbe()

        with self.assertRaises(BrokerError):
            await broker.snapshot()

        await asyncio.wait_for(broker.sibling_cancelled.wait(), 1)
        await asyncio.wait_for(broker.quote_cancelled.wait(), 1)
        self.assertEqual(broker.quote_calls, ["opt-a", "opt-b"])


class BrokerReadLatencyTests(unittest.IsolatedAsyncioTestCase):
    async def test_calendar_preloads_off_event_loop_before_broker_connection(self):
        broker = FixtureBroker()
        main_thread = threading.get_ident()
        initialized = []

        def preload(now):
            self.assertEqual(now, broker.now)
            self.assertNotEqual(threading.get_ident(), main_thread)
            initialized.append(True)

        async def connect():
            self.assertEqual(initialized, [True])
            return broker

        with patch("relay.broker.regular_session", side_effect=preload), patch(
                "relay.broker.RobinhoodMCP.__aenter__", new=AsyncMock(side_effect=connect)):
            self.assertIs(await broker.__aenter__(), broker)

    async def test_nearest_expiry_overlaps_eligible_chain_reads(self):
        broker = ExpiryProbe()
        request = CONTRACT | {"expiry": "2026-09-08"}
        task = asyncio.create_task(broker.nearest_expiry(request))

        await asyncio.wait_for(broker.all_instruments_started.wait(), 1)
        self.assertFalse(task.done())
        broker.release_instruments.set()

        resolved = await task
        self.assertEqual(resolved["expiry"], "2026-09-11")
        self.assertEqual(broker.instrument_started, ["combined"])

    async def test_nearest_expiry_cancels_sibling_chain_read_on_failure(self):
        broker = ExpiryProbe(failing_chain="chain-b")
        request = CONTRACT | {"expiry": "2026-09-08"}

        original = broker._pages
        chain_started, chain_cancelled = asyncio.Event(), asyncio.Event()

        async def pages(name, args, key):
            if name == "get_option_chains":
                chain_started.set()
                try:
                    await asyncio.Event().wait()
                finally:
                    chain_cancelled.set()
            await chain_started.wait()
            return await original(name, args, key)

        broker._pages = pages

        with self.assertRaisesRegex(BrokerError, "fixture instrument failure"):
            await asyncio.wait_for(broker.nearest_expiry(request), .5)

        self.assertEqual(broker.instrument_started, ["combined"])
        self.assertTrue(chain_cancelled.is_set())

    async def test_same_day_precedes_later_dates_and_inconsistent_listing_holds(self):
        broker = FixtureBroker()
        today = broker.now.date().isoformat()
        broker.chain["expiration_dates"].append(today)
        broker.instrument_by_expiry[today] = broker._instrument("same-day", today, "500.00")
        request = CONTRACT | {"expiry": today}
        self.assertEqual((await broker.nearest_expiry(request))["expiry"], today)
        queries = [args for name, args in broker.page_calls if name == "get_option_instruments"]
        self.assertEqual(len(queries), 1)
        self.assertEqual(len(queries[0]["expiration_dates"].split(",")), 7)
        broker.chain["expiration_dates"].remove(today)
        with self.assertRaisesRegex(BrokerError, "metadata is inconsistent"):
            await broker.nearest_expiry(request)

    async def test_prefetched_instruments_follow_all_pages_before_selecting(self):
        fixture = FixtureBroker()
        broker = RobinhoodBroker(fixture.runtime, clock=lambda: fixture.now)
        broker.catalog = json.loads((Path(__file__).parent / "fixtures/robinhood-option-schemas-20260924.json").read_text())["legacy"]
        first = fixture.instrument_by_expiry["2026-09-18"]
        second = fixture.instrument_by_expiry["2026-09-11"]

        async def data(name, args):
            if name == "get_option_chains":
                return {"chains": [fixture.chain]}
            self.assertEqual(name, "get_option_instruments")
            if args.get("cursor") == "second":
                return {"instruments": [second], "next": None}
            return {"instruments": [first], "next": "https://fixture.invalid/instruments?cursor=second"}

        broker._data = AsyncMock(side_effect=data)
        resolved = await broker.nearest_expiry(CONTRACT | {"expiry": "2026-09-08"})
        self.assertEqual(resolved["expiry"], "2026-09-11")
        self.assertEqual(broker._data.await_count, 3)

    async def test_nearest_expiry_advances_dates_only_after_no_exact_match(self):
        broker = ExpiryProbe()
        broker.rows_by_chain["chain-a"][0]["strike_price"] = "501.00"
        request = CONTRACT | {"expiry": "2026-09-08"}

        task = asyncio.create_task(broker.nearest_expiry(request))
        await asyncio.sleep(0)
        broker.release_instruments.set()

        resolved = await task
        self.assertEqual(resolved["expiry"], "2026-09-18")
        instrument_queries = [
            args for name, args in broker.page_calls if name == "get_option_instruments"
        ]
        self.assertEqual(
            [args["expiration_dates"] for args in instrument_queries],
            ["2026-09-08,2026-09-09,2026-09-10,2026-09-11,2026-09-12,2026-09-13,2026-09-14", "2026-09-18"],
        )

    async def test_nearest_expiry_holds_ambiguous_earliest_date(self):
        broker = ExpiryProbe()
        broker.rows_by_chain["chain-b"][0]["strike_price"] = "500.00"
        request = CONTRACT | {"expiry": "2026-09-08"}

        broker.release_instruments.set()
        with self.assertRaisesRegex(BrokerError, "ambiguous across chains"):
            await broker.nearest_expiry(request)

    async def test_execution_scope_hands_nearest_contract_to_first_quote(self):
        broker = FixtureBroker()
        request = CONTRACT | {"expiry": "2026-09-08"}

        with broker.execution_reads():
            resolved = await broker.nearest_expiry(request)
            quote = await broker.quote(resolved)
            self.assertEqual(quote["option_id"], broker.instrument_by_expiry[resolved["expiry"]]["id"])

        self.assertEqual(
            [name for name, _ in broker.page_calls],
            ["get_option_chains", "get_option_instruments"],
        )
        self.assertEqual(broker.raw_calls, [quote["option_id"]])

    async def test_execution_scope_quote_reuse_expires_and_does_not_cross_scopes(self):
        broker = FixtureBroker()

        with broker.execution_reads():
            first = await broker.quote(CONTRACT)
            second = await broker.quote(CONTRACT)
            self.assertEqual(first, second)
            self.assertEqual(len(broker.raw_calls), 1)

        self.assertEqual(len(broker.raw_calls), 1)
        with broker.execution_reads():
            await broker.quote(CONTRACT)
        self.assertEqual(len(broker.raw_calls), 2)

        broker = FixtureBroker()
        scopes = []

        async def read_twice(contract):
            with broker.execution_reads() as scope:
                scopes.append(scope)
                await broker.quote(contract)
                await broker.quote(contract)

        await asyncio.gather(read_twice(CONTRACT), read_twice({**CONTRACT, "expiry": "2026-09-18"}))
        self.assertIsNot(scopes[0], scopes[1])
        self.assertEqual(len(broker.raw_calls), 2)

        broker = FixtureBroker()
        with broker.execution_reads():
            await broker.quote(CONTRACT)
            await asyncio.sleep(1.01)
            await broker.quote(CONTRACT)
        self.assertEqual(len(broker.raw_calls), 2)

    async def test_execution_scope_snapshot_reuse_requires_fresh_timestamp(self):
        broker = SnapshotProbe()
        broker.release_instruments.set()
        broker.release_quotes.set()

        with broker.execution_reads():
            first = await broker.snapshot()
            second = await broker.snapshot()
            self.assertEqual(first, second)
            self.assertEqual(broker.instrument_calls, ["opt-a", "opt-b"])
            self.assertEqual(broker.quote_calls, ["opt-a", "opt-b"])

            await asyncio.sleep(1.01)
            broker.now += timedelta(seconds=2)
            third = await broker.snapshot()

        self.assertEqual(third["timestamp"], "2026-09-08T15:00:02+00:00")
        self.assertEqual(broker.instrument_calls.count("opt-a"), 2)
        self.assertEqual(broker.quote_calls.count("opt-a"), 2)

    async def test_execution_scope_cancellation_invalidates_copied_child_scope(self):
        broker = FixtureBroker()
        started = asyncio.Event()
        release = asyncio.Event()
        original_raw_quote = broker._raw_quote

        async def blocked_raw_quote(option_id):
            started.set()
            await release.wait()
            return await original_raw_quote(option_id)

        broker._raw_quote = blocked_raw_quote
        with broker.execution_reads():
            task = asyncio.create_task(broker.quote(CONTRACT))
            await asyncio.wait_for(started.wait(), 1)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
            release.set()
            await broker.quote(CONTRACT)

        self.assertEqual(len(broker.raw_calls), 1)

    async def test_overlapping_portfolio_reads_single_flight_without_completed_cache(self):
        broker = PortfolioProbe()
        first = asyncio.create_task(broker._portfolio())
        await asyncio.wait_for(broker.portfolio_started.wait(), 1)
        second = asyncio.create_task(broker._portfolio())
        await asyncio.sleep(0)
        self.assertEqual(broker.portfolio_calls, 1)

        broker.release_portfolio.set()
        self.assertEqual(await first, await second)
        self.assertIsNone(broker._portfolio_task)

        broker.portfolio_started.clear()
        broker.release_portfolio.clear()
        third = asyncio.create_task(broker._portfolio())
        await asyncio.wait_for(broker.portfolio_started.wait(), 1)
        self.assertEqual(broker.portfolio_calls, 2)
        broker.release_portfolio.set()
        await third

    async def test_last_cancelled_portfolio_waiter_cancels_shared_read(self):
        broker = PortfolioProbe()
        task = asyncio.create_task(broker._portfolio())
        await asyncio.wait_for(broker.portfolio_started.wait(), 1)
        task.cancel()

        with self.assertRaises(asyncio.CancelledError):
            await task
        await asyncio.wait_for(broker.portfolio_cancelled.wait(), 1)
        self.assertIsNone(broker._portfolio_task)


if __name__ == "__main__":
    unittest.main()
