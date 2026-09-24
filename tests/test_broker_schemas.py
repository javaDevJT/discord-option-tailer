"""Offline provider schema metadata; no credentials, account responses or network."""
import copy
import json
import tempfile
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock

from relay.broker import BrokerError, RobinhoodBroker, SCHEMA_PINS
from relay.status import execution_failure, project_execution_diagnostic


class SchemaQualificationChecks(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.fixtures = json.loads((Path(__file__).parent / "fixtures/robinhood-account-schemas.json").read_text())
        self.broker = RobinhoodBroker({"robinhood": {"account_number": "TEST0001"}})
        self.broker.catalog = {name: item["tool"] for name, item in self.fixtures.items()}

    @staticmethod
    def _compatible_schema_drift(tool):
        changed = copy.deepcopy(tool)
        for schema in (changed["inputSchema"], changed["outputSchema"]):
            schema.update({"description": "Updated documentation.", "title": "Updated title", "examples": [], "$comment": "Documentation only"})
        guide = changed["outputSchema"]["properties"]["guide"]
        guide.update({"description": guide["description"] + " Updated.", "title": "Updated guide", "examples": ["example"], "$comment": "Guide metadata"})

        def reorder_required(node):
            if isinstance(node, dict):
                if isinstance(node.get("required"), list):
                    node["required"] = list(reversed(node["required"]))
                for value in node.values():
                    reorder_required(value)
            elif isinstance(node, list):
                for value in node:
                    reorder_required(value)

        reorder_required(changed["inputSchema"])
        reorder_required(changed["outputSchema"])
        data_properties = changed["outputSchema"]["properties"]["data"].get("properties", {})
        data_properties["provider_future_field"] = {"type": "string"}
        for value in data_properties.values():
            item_properties = value.get("items", {}).get("properties", {})
            if item_properties:
                item_properties["provider_future_field"] = {"type": "boolean"}
                break
        return changed

    def test_original_and_reviewed_schemas_allow_documentation_updates(self):
        for name, item in self.fixtures.items():
            for original in (False, True):
                with self.subTest(tool=name, original=original):
                    tool = copy.deepcopy(item["tool"])
                    if original:
                        tool["outputSchema"]["properties"]["guide"]["description"] = item["original_guide_description"]
                    self.broker.catalog[name] = tool
                    self.assertIs(self.broker._qualified(name), tool)
                    tool["outputSchema"]["properties"]["guide"]["description"] += " Unreviewed guidance."
                    self.assertIs(self.broker._qualified(name), tool)
            tool = copy.deepcopy(item["tool"])
            tool["inputSchema"]["additionalProperties"] = True
            self.broker.catalog[name] = tool
            with self.assertRaisesRegex(BrokerError, "schema changed"):
                self.broker._qualified(name)

    def test_reviewed_schemas_allow_documentation_updates(self):
        account = json.loads((Path(__file__).parent / "fixtures/robinhood-account-schemas-20260923.json").read_text())
        options = json.loads((Path(__file__).parent / "fixtures/robinhood-option-schemas-20260924.json").read_text())
        for fixtures in (account, options["legacy"], options["current"]):
            for name, tool in fixtures.items():
                with self.subTest(tool=name):
                    tool = self._compatible_schema_drift(tool)
                    self.broker.catalog[name] = copy.deepcopy(tool)
                    self.assertIs(self.broker._qualified(name), self.broker.catalog[name])
                    self.broker.catalog[name]["outputSchema"]["properties"]["guide"]["description"] += " Unreviewed."
                    self.assertIs(self.broker._qualified(name), self.broker.catalog[name])

    def test_schema_changes_are_rejected_and_name_the_incompatible_field(self):
        account = json.loads((Path(__file__).parent / "fixtures/robinhood-account-schemas-20260923.json").read_text())
        cases = [
            ("input property named description", "get_portfolio", account["get_portfolio"], lambda t: t["inputSchema"]["properties"].update({"description": {"type": "string"}}), "description"),
            ("changed type", "get_portfolio", account["get_portfolio"], lambda t: t["inputSchema"]["properties"]["account_number"].update({"type": "integer"}), "account_number"),
            ("changed enum", "get_portfolio", account["get_portfolio"], lambda t: t["inputSchema"]["properties"]["account_number"].update({"enum": ["TEST0001"]}), "account_number"),
            ("changed default", "get_portfolio", account["get_portfolio"], lambda t: t["inputSchema"]["properties"]["account_number"].update({"default": "TEST0001"}), "account_number"),
            ("changed constraint", "get_portfolio", account["get_portfolio"], lambda t: t["inputSchema"]["properties"]["account_number"].update({"minLength": 2}), "account_number"),
            ("removed request field", "get_portfolio", account["get_portfolio"], lambda t: t["inputSchema"]["properties"].pop("account_number"), "account_number"),
            ("removed response field", "get_portfolio", account["get_portfolio"], lambda t: t["outputSchema"]["properties"]["data"]["properties"].pop("equity_value"), "equity_value"),
            ("added required response field", "get_accounts", account["get_accounts"], lambda t: t["outputSchema"]["properties"]["data"]["properties"]["accounts"]["items"]["required"].append("nickname"), "nickname"),
            ("removed required request field", "get_portfolio", account["get_portfolio"], lambda t: t["inputSchema"]["required"].remove("account_number"), "account_number"),
            ("removed required response field", "get_accounts", account["get_accounts"], lambda t: t["outputSchema"]["required"].remove("guide"), "guide"),
            ("changed response type", "get_accounts", account["get_accounts"], lambda t: t["outputSchema"]["properties"]["data"]["properties"]["accounts"]["items"]["properties"]["account_number"].update({"type": "integer"}), "account_number"),
        ]
        for label, name, baseline, mutate, field in cases:
            with self.subTest(change=label):
                tool = copy.deepcopy(baseline)
                mutate(tool)
                self.broker.catalog[name] = tool
                with self.assertRaisesRegex(BrokerError, "schema changed") as raised:
                    self.broker._qualified(name)
                self.assertIn(field, str(raised.exception))
        unknown = copy.deepcopy(account["get_portfolio"])
        unknown["name"] = "experimental_tool"
        self.broker.catalog["experimental_tool"] = unknown
        with self.assertRaisesRegex(BrokerError, "qualified catalog"):
            self.broker._qualified("experimental_tool")

    def test_option_pagination_schema_preserves_known_modes_and_rejects_unknown(self):
        options = json.loads((Path(__file__).parent / "fixtures/robinhood-option-schemas-20260924.json").read_text())
        for version in ("legacy", "current"):
            with self.subTest(version=version):
                tool = self._compatible_schema_drift(options[version]["get_option_positions"])
                self.broker.catalog["get_option_positions"] = tool
                self.assertIs(self.broker._qualified("get_option_positions"), tool)
                next_schema = tool["outputSchema"]["properties"]["data"]["properties"]["next"]
                next_schema["description"] = "Unknown next-page protocol."
                with self.assertRaisesRegex(BrokerError, "schema changed") as raised:
                    self.broker._qualified("get_option_positions")
                self.assertIn("next", str(raised.exception))

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
        self.broker.catalog[name]["outputSchema"]["properties"]["data"]["properties"]["accounts"]["items"]["properties"]["account_number"]["type"] = "integer"
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
        fixtures = json.loads((Path(__file__).parent / "fixtures/robinhood-account-schemas-20260923.json").read_text())
        fixtures = {name: self._compatible_schema_drift(tool) for name, tool in fixtures.items()}
        options = json.loads((Path(__file__).parent / "fixtures/robinhood-option-schemas-20260924.json").read_text())
        fixtures.update({name: self._compatible_schema_drift(tool) for name, tool in options["current"].items()})

        class Tool:
            def __init__(self, value):
                self.value = value

            def model_dump(self, **kwargs):
                return self.value

        class Session:
            async def list_tools(self, cursor=None):
                return SimpleNamespace(tools=[Tool(item) for item in fixtures.values()], nextCursor=None)

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
            self.assertTrue(events[0]["detail"].startswith("Incompatible broker schemas: "))
            self.assertEqual(set(self.broker._schema_incompatible_tools), set(SCHEMA_PINS) - set(fixtures))
            for name in fixtures:
                self.assertIs(self.broker._qualified(name), self.broker.catalog[name])

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
