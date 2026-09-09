"""Offline provider schema metadata; no credentials, account responses or network."""
import copy
import json
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock

from relay.broker import BrokerError, RobinhoodBroker


class SchemaQualificationChecks(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.fixtures = json.loads((Path(__file__).parent / "fixtures/robinhood-account-schemas.json").read_text())
        self.broker = RobinhoodBroker({"robinhood": {"account_number": "TEST0001"}})
        self.broker.catalog = {name: item["tool"] for name, item in self.fixtures.items()}

    def test_original_and_reviewed_schemas_accept_only_exact_versions(self):
        for name, item in self.fixtures.items():
            for original in (False, True):
                with self.subTest(tool=name, original=original):
                    tool = copy.deepcopy(item["tool"])
                    if original:
                        tool["outputSchema"]["properties"]["guide"]["description"] = item["original_guide_description"]
                    self.broker.catalog[name] = tool
                    self.assertIs(self.broker._qualified(name), tool)
                    tool["outputSchema"]["properties"]["guide"]["description"] += " Unreviewed guidance."
                    with self.assertRaisesRegex(BrokerError, "schema changed"):
                        self.broker._qualified(name)
            tool = copy.deepcopy(item["tool"])
            tool["inputSchema"]["additionalProperties"] = True
            self.broker.catalog[name] = tool
            with self.assertRaisesRegex(BrokerError, "schema changed"):
                self.broker._qualified(name)

    async def test_reviewed_schema_still_validates_requests_and_responses(self):
        self.broker.session = object()
        result = SimpleNamespace(isError=False, structuredContent={"data": {"accounts": []}, "guide": "Synthetic fixture"})
        self.broker._call_tool = AsyncMock(return_value=result)
        with self.assertRaisesRegex(BrokerError, "arguments"):
            await self.broker._data("get_accounts", {"unexpected": True})
        self.broker._call_tool.assert_not_awaited()
        self.assertEqual(await self.broker._data("get_accounts", {}), {"accounts": []})
        result.structuredContent["data"]["accounts"] = "invalid account list"
        with self.assertRaisesRegex(BrokerError, "response does not match"):
            await self.broker._data("get_accounts", {})


if __name__ == "__main__":
    unittest.main()
