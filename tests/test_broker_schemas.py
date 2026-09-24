"""Offline provider schema metadata; no credentials, account responses or network."""
import copy
import json
import tempfile
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock

from relay.broker import BrokerError, RobinhoodBroker
from relay.status import execution_failure, project_execution_diagnostic


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

    def test_reviewed_schemas_accept_only_exact_versions(self):
        account = json.loads((Path(__file__).parent / "fixtures/robinhood-account-schemas-20260923.json").read_text())
        options = json.loads((Path(__file__).parent / "fixtures/robinhood-option-schemas-20260924.json").read_text())
        for fixtures in (account, options["legacy"], options["current"]):
            for name, tool in fixtures.items():
                with self.subTest(tool=name):
                    self.broker.catalog[name] = copy.deepcopy(tool)
                    self.assertIs(self.broker._qualified(name), self.broker.catalog[name])
                    self.broker.catalog[name]["outputSchema"]["properties"]["guide"]["description"] += " Unreviewed."
                    with self.assertRaisesRegex(BrokerError, "schema changed"):
                        self.broker._qualified(name)

    async def test_option_pagination_preserves_opaque_cursors_and_legacy_urls(self):
        arguments = {"account_number": "TEST0001", "nonzero": True}
        options = json.loads((Path(__file__).parent / "fixtures/robinhood-option-schemas-20260924.json").read_text())
        for version, next_value, expected in (
            ("current", "cursor+/=%2Bopaque", "cursor+/=%2Bopaque"),
            ("current", "https://opaque.example/token", "https://opaque.example/token"),
            ("current", "https://opaque.example/?cursor=a%2Bb", "https://opaque.example/?cursor=a%2Bb"),
            ("legacy", "https://api.robinhood.com/options/positions/?cursor=a%2Bb%2F%3D", "a+b/="),
        ):
            with self.subTest(version=version, next_value=next_value):
                self.broker.catalog = options[version]
                self.broker._data = AsyncMock(side_effect=[
                    {"results": [1], "next": next_value}, {"results": [2], "next": ""},
                ])
                self.assertEqual(await self.broker._pages("get_option_positions", arguments, "results"), [1, 2])
                self.assertEqual(self.broker._data.call_args_list[1].args[1], arguments | {"cursor": expected})
                self.assertNotIn("cursor", arguments)
                self.broker._data = AsyncMock(return_value={"results": [], "next": next_value})
                with self.assertRaisesRegex(BrokerError, "pagination"):
                    await self.broker._pages("get_option_positions", arguments, "results")
                self.assertEqual(self.broker._data.await_count, 2)
        for version, malformed in (
            ("current", 12), ("legacy", 12),
            ("legacy", "https://api.robinhood.com/options/positions/"),
            ("legacy", "https://api.robinhood.com/options/positions/?cursor=a&cursor=b"),
        ):
            with self.subTest(version=version, malformed=malformed):
                self.broker.catalog = options[version]
                self.broker._data = AsyncMock(return_value={"results": [], "next": malformed})
                with self.assertRaisesRegex(BrokerError, "pagination"):
                    await self.broker._pages("get_option_positions", arguments, "results")

    async def test_reviewed_schema_still_validates_requests_and_responses(self):
        latest = json.loads((Path(__file__).parent / "fixtures/robinhood-account-schemas-20260923.json").read_text())
        for tool in (self.fixtures["get_accounts"]["tool"], latest["get_accounts"]):
            with self.subTest(digest=self.broker._schema_digest(tool)):
                self.broker.catalog["get_accounts"] = tool
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



    def test_schema_rejection_has_safe_tool_diagnostic(self):
        name = "get_accounts"
        self.broker.catalog[name]["outputSchema"]["properties"]["guide"]["description"] += " Unreviewed guidance."
        with self.assertRaises(BrokerError) as raised:
            self.broker._qualified(name)
        diagnostic = project_execution_diagnostic(getattr(raised.exception, "_relay_failure", {}))
        self.assertEqual(diagnostic["tool"], name)
        self.assertEqual(diagnostic["code"], "schema_incompatible")
        with self.assertLogs("relay.status", level="ERROR"):
            reason, projected = execution_failure(raised.exception, stage="snapshot")
        self.assertEqual(projected["code"], "schema_incompatible")
        self.assertEqual(projected["tool"], name)
        self.assertIn("code=schema_incompatible", reason)
        self.assertIn(name, str(raised.exception))
        self.assertNotIn("token", str(raised.exception).lower())

    async def test_discovery_persists_only_pinned_schemas_owner_only(self):
        fixtures = self.fixtures

        class Tool:
            def __init__(self, value):
                self.value = value

            def model_dump(self, **kwargs):
                return self.value

        class Session:
            async def list_tools(self, cursor=None):
                return SimpleNamespace(tools=[Tool(item["tool"]) for item in fixtures.values()], nextCursor=None)

        with tempfile.TemporaryDirectory() as directory:
            token_store = Path(directory) / "custom-oauth.json"
            self.broker.config["token_store"] = str(token_store)
            events = []
            self.broker.on_status = events.append
            self.broker.session = Session()
            await self.broker.discover()
            cache_path = token_store.with_name("robinhood-schemas.json")
            cache = json.loads(cache_path.read_text())
            self.assertTrue(cache["observed_at"].endswith("Z"))
            self.assertEqual({tool["name"] for tool in cache["tools"]}, set(fixtures))
            self.assertTrue(all(set(tool) == {"name", "inputSchema", "outputSchema"} for tool in cache["tools"]))
            self.assertEqual(cache_path.stat().st_mode & 0o777, 0o600)
            self.assertNotIn("TEST0001", cache_path.read_text())
            self.assertEqual(events[0]["state"], "schema_incompatible")
            self.assertTrue(events[0]["detail"].startswith("Unaccepted pinned tools: "))

    async def test_incompatible_readiness_suppresses_connected_but_keeps_errors(self):
        events = []
        self.broker.on_status = events.append
        self.broker._schema_incompatible = True
        self.broker._schema_incompatible_tools = ("get_accounts",)
        self.broker.session = SimpleNamespace(call_tool=AsyncMock(return_value=SimpleNamespace(isError=False)))
        await self.broker._call_tool("get_accounts", {})
        self.assertEqual(events[-1]["state"], "schema_incompatible")
        self.broker._connection_failed(BrokerError("401 Unauthorized"))
        self.assertEqual(events[-1]["state"], "auth_required")
        self.broker._connection_failed(BrokerError("connection unavailable"))
        self.assertEqual(events[-1]["state"], "unavailable")

if __name__ == "__main__":
    unittest.main()
