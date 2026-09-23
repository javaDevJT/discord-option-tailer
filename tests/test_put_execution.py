"""Offline call/put parity across direct entry and Robinhood order encoding."""

import unittest
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from relay.core import canonical_contract
from relay.entry_rules import deterministic_entry
import test_broker_live


class PutExecutionParityTests(unittest.IsolatedAsyncioTestCase):
    async def test_direct_call_and_put_resolve_and_encode_the_same_way(self):
        now = datetime(2026, 9, 23, 14, tzinfo=timezone.utc)

        live = test_broker_live.LiveBrokerChecks("test_actual_total_value_cash_cap_and_exact_quote")
        live.setUp()
        live.now = now

        original_data = live.broker._data.side_effect

        async def data(name, args=None):
            if name == "search":
                return {"results": [{
                    "symbol": "QQQ",
                    "instrument_id": "8f92e76f-1e0e-4478-8580-16a6ffcfaef5",
                    "name": "QQQ fixture",
                }]}
            if name == "get_option_instruments":
                self.assertEqual(args["chain_symbol"], "QQQ")
                self.assertEqual(args["strike_price"], "743")
                self.assertEqual(args["type"], live.instrument["type"])
                self.assertIn("2026-09-24", args["expiration_dates"].split(","))
            if name == "get_option_quotes":
                self.assertEqual(args["instrument_ids"], [live.instrument["id"]])
            return await original_data(name, args)

        live.broker._data.side_effect = data

        encoded = {}
        for option_type, suffix, option_id in (
            ("call", "C", "11111111-1111-4111-8111-111111111111"),
            ("put", "P", "22222222-2222-4222-8222-222222222222"),
        ):
            message = {
                "id": f"entry-{option_type}",
                "revision": "0",
                "timestamp": now.isoformat(),
                "source_group": "approved",
                "content": "",
                "embeds": [{
                    "title": "ENTRY",
                    "description": f"🖼️ Contract: QQQ $743{suffix}\n💰 Price: 1.55\n‼️ Comments: none",
                }],
                "attachments": [],
            }
            decision = deterministic_entry(message)
            self.assertIsNotNone(decision)
            self.assertEqual(decision["action"], "OPEN")
            self.assertEqual(decision["contract"]["option_type"], option_type)
            self.assertEqual(decision["alert_price"], "1.55")

            live.chain = live.chain | {
                "symbol": "QQQ",
                "expiration_dates": ["2026-09-24"],
                "underlying_instruments": [{"symbol": "QQQ", "instrument": "fixture"}],
            }
            live.instrument = live.instrument | {
                "id": option_id,
                "chain_symbol": "QQQ",
                "expiration_date": "2026-09-24",
                "strike_price": "743.00",
                "type": option_type,
            }
            live.quote = live.quote | {
                "instrument_id": option_id,
                "bid_price": "1.50",
                "ask_price": "1.55",
                "updated_at": now.isoformat(),
            }

            source_day = datetime.fromisoformat(message["timestamp"]).astimezone(
                ZoneInfo("America/New_York")
            ).date().isoformat()
            requested = decision["contract"] | {"expiry": source_day}
            contract = await live.broker.nearest_expiry(requested)
            self.assertEqual(contract["expiry"], "2026-09-24")
            self.assertEqual(contract["option_type"], option_type)

            args, quote = await live.broker._order_args({
                "contract": contract,
                "quantity": 1,
                "side": "buy",
                "position_effect": "open",
                "limit_price": "1.55",
            })
            self.assertEqual(canonical_contract(quote["contract"]), contract)
            self.assertEqual(quote["option_id"], option_id)
            self.assertEqual(args["legs"], [{
                "option_id": option_id,
                "side": "buy",
                "position_effect": "open",
                "ratio_quantity": 1,
            }])
            self.assertEqual((args["quantity"], args["price"], args["type"]), ("1", "1.55", "limit"))
            encoded[option_type] = args

        call_args, put_args = encoded["call"], encoded["put"]
        self.assertEqual(
            {key: value for key, value in call_args.items() if key != "legs"},
            {key: value for key, value in put_args.items() if key != "legs"},
        )
        self.assertEqual(
            {key: value for key, value in call_args["legs"][0].items() if key != "option_id"},
            {key: value for key, value in put_args["legs"][0].items() if key != "option_id"},
        )


if __name__ == "__main__":
    unittest.main()
