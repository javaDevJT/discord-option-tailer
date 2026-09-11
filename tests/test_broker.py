import asyncio
import json
import importlib.util
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, MagicMock, patch
from urllib.parse import parse_qs, urlsplit

from types import SimpleNamespace
from relay.broker import (
    BrokerError,
    PaperBroker,
    RobinhoodMCP,
    _OAuthStorage,
    broker_credentials_state,
    is_auth_required,
)


class BrokerChecks(unittest.IsolatedAsyncioTestCase):
    async def test_paper_nearest_expiry_uses_only_standard_matching_fixture_contracts(self):
        contract = {"symbol": "SPY", "expiry": "2026-09-10", "strike": "650", "option_type": "call"}
        quote = {"contract": contract, "tradable": True, "multiplier": 100,
                 "currency": "USD", "asset_type": "equity_option"}
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "quotes.json"
            rows = [quote | {"multiplier": 10},
                    quote | {"contract": contract | {"expiry": "2026-09-18"}},
                    quote | {"contract": contract | {"expiry": "2026-09-11"}}]
            path.write_text(json.dumps({"quotes": rows}))
            broker = PaperBroker({"paper": {"quotes_file": str(path)}})
            self.assertEqual((await broker.nearest_expiry(contract))["expiry"], "2026-09-11")
            path.write_text(json.dumps({"quotes": rows + [quote]}))
            self.assertEqual(await broker.nearest_expiry(contract), contract)

    async def test_status_sink_failure_preserves_broker_result_and_original_error(self):
        def failed_sink(_event):
            raise OSError("private status path")
        broker = RobinhoodMCP({}, on_status=failed_sink)
        response = MagicMock(isError=False)
        broker.session = MagicMock(call_tool=AsyncMock(return_value=response))
        self.assertIs(await broker._call_tool("get_accounts", {}), response)
        original = BrokerError("Robinhood authorization required")
        broker.session.call_tool.side_effect = original
        with self.assertRaises(BrokerError) as raised:
            await broker._call_tool("get_accounts", {})
        self.assertIs(raised.exception, original)

    async def test_initialization_and_catalog_failures_publish_provider_assistance(self):
        for stage in ("initialize", "list_tools"):
            for original, state in (
                (ExceptionGroup("transport", [BrokerError("Robinhood authorization required")]), "auth_required"),
                (ConnectionError("private transport detail"), "unavailable"),
            ):
                events = []
                broker = RobinhoodMCP({}, on_status=events.append)
                session = MagicMock(initialize=AsyncMock(), list_tools=AsyncMock())
                getattr(session, stage).side_effect = original
                broker.stack.enter_async_context = AsyncMock(side_effect=[MagicMock(), (None, None, None), session])
                with patch("httpx.AsyncClient"), patch("mcp.ClientSession"), patch("mcp.client.streamable_http.streamable_http_client"):
                    with self.assertRaises(type(original)) as raised:
                        await broker.__aenter__()
                self.assertIs(raised.exception, original)
                self.assertEqual(events[-1], {"component": "broker", "state": state})
                self.assertIsNone(broker.session)

    async def test_tool_calls_publish_auth_failure_and_recovery_without_error_text(self):
        events = []
        broker = RobinhoodMCP({}, on_status=events.append)
        broker.session = MagicMock()
        broker.session.call_tool = AsyncMock(side_effect=BrokerError("Robinhood authorization required; private details"))
        with self.assertRaises(BrokerError):
            await broker._call_tool("get_accounts", {})
        self.assertEqual(events[-1], {"component": "broker", "state": "auth_required"})
        broker.session.call_tool.side_effect = ConnectionError("private transport details")
        with self.assertRaises(ConnectionError):
            await broker._call_tool("get_accounts", {})
        self.assertEqual(events[-1], {"component": "broker", "state": "unavailable"})
        broker.session.call_tool.side_effect = None
        for failed, state in ((True, "unavailable"), (False, "connected")):
            broker.session.call_tool.return_value = MagicMock(isError=failed)
            await broker._call_tool("get_accounts", {})
            self.assertEqual(events[-1], {"component": "broker", "state": state})
        self.assertTrue(all(set(event) == {"component", "state"} for event in events))

    async def test_mcp_error_result_publishes_auth_failure_without_error_text(self):
        events = []
        broker = RobinhoodMCP({}, on_status=events.append)
        broker.session = MagicMock()
        broker.schemas["get_accounts"] = {}
        broker.session.call_tool = AsyncMock(return_value=SimpleNamespace(
            isError=True,
            structuredContent={"error": {"code": "invalid_token", "detail": "private token"}},
            content=[],
        ))
        with self.assertRaisesRegex(BrokerError, "tool error"):
            await broker.call("get_accounts", {})
        self.assertEqual(events[-1], {"component": "broker", "state": "auth_required"})

    async def test_mcp_error_result_recovers_to_connected(self):
        events = []
        broker = RobinhoodMCP({}, on_status=events.append)
        broker.session = MagicMock()
        broker.schemas["get_accounts"] = {}
        recovered = MagicMock(isError=False, structuredContent={"ok": True}, content=[])
        recovered.model_dump.return_value = {"structuredContent": {"ok": True}, "isError": False}
        broker.session.call_tool = AsyncMock(side_effect=[
            SimpleNamespace(isError=True, structuredContent={"error": "expired token"}, content=[]),
            recovered,
        ])
        with self.assertRaises(BrokerError):
            await broker.call("get_accounts", {})
        await broker.call("get_accounts", {})
        self.assertEqual(events[-2:], [
            {"component": "broker", "state": "auth_required"},
            {"component": "broker", "state": "connected"},
        ])

    async def test_explicit_quote_and_idempotent_paper_fill(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "quotes.json"
            contract = {"symbol": "SPY", "expiry": "2099-01-16", "strike": "500", "option_type": "call"}
            quote = {"contract": contract, "bid": "0.90", "ask": "1.00", "tick_size": "0.01",
                     "timestamp": "2026-09-06T00:00:00Z", "tradable": True, "multiplier": 100,
                     "currency": "USD", "asset_type": "equity_option"}
            path.write_text(json.dumps({"quotes": [quote]}))
            broker = PaperBroker({"paper": {"quotes_file": str(path), "buying_power": "200", "market_open": True}})
            order = {"client_order_id": "sample-1", "contract": contract, "side": "buy", "position_effect": "open",
                     "quantity": 1, "limit_price": "1.00"}
            first = await broker.submit(order)
            self.assertEqual(first["status"], "filled")
            self.assertEqual(first, await broker.submit(order))
            self.assertEqual(first, await broker.submit(dict(order, limit_price="1.0",
                contract=dict(contract, strike="500.00", symbol="spy"))))
            for changed in ({"quantity": 2}, {"limit_price": "1.01"}, {"contract": dict(contract, strike="501")}):
                with self.assertRaisesRegex(BrokerError, "reused"):
                    await broker.submit(dict(order, **changed))
            self.assertEqual((await broker.snapshot())["buying_power"], "100.00")
            self.assertEqual(await broker.order_status("sample-1"), first)
            with self.assertRaises(BrokerError):
                await broker.quote(dict(contract, strike="501"))
            del quote["timestamp"]
            path.write_text(json.dumps([quote]))
            with self.assertRaises(BrokerError):
                await broker.quote(contract)

    async def test_configured_paper_fees_on_fills_and_cash_check(self):
        broker = PaperBroker({"paper": {"buying_power": "200", "market_open": True},
                              "risk": {"fee_reserve_per_contract": "1.00"}})
        broker.quote = AsyncMock(return_value={"bid": "0.90", "ask": "1.00", "tradable": True})
        order = {"client_order_id": "buy-with-fee", "side": "buy", "position_effect": "open",
                 "quantity": 1, "limit_price": "1.00", "contract": {"symbol": "SPY",
                 "expiry": "2099-01-16", "strike": "500", "option_type": "call"}}
        await broker.submit(order)
        await broker.submit(order)
        self.assertEqual(str(broker.buying_power), "99.00")
        await broker.submit(dict(order, client_order_id="sell-with-fee", side="sell",
                                 position_effect="close", limit_price="0.90"))
        self.assertEqual(str(broker.buying_power), "188.00")
        broker.buying_power = 100
        with self.assertRaisesRegex(BrokerError, "buying power"):
            await broker.submit(dict(order, client_order_id="insufficient-for-fee"))
        self.assertEqual(broker.buying_power, 100)
        pending = await broker.submit(dict(order, client_order_id="noncrossing", limit_price="0.95"))
        self.assertEqual(pending["status"], "open")
        self.assertEqual(broker.buying_power, 100)

    async def test_no_fixture_no_invented_quote(self):
        broker = PaperBroker({"paper": {"market_open": True}})
        with self.assertRaises(BrokerError):
            await broker.quote({"symbol": "SPY", "expiry": "2099-01-16", "strike": "500", "option_type": "call"})

    async def test_live_mutations_are_denied_even_when_advertised(self):
        broker = RobinhoodMCP({})
        broker.session = object()
        broker.schemas = {"place_option_order": {"type": "object"}, "cancel_option_order": {"type": "object"}}
        for name in broker.schemas:
            with self.assertRaisesRegex(BrokerError, "not allowed"):
                await broker.call(name, {})

    async def test_secure_oauth_store_and_noninteractive_login(self):
        class Model:
            def model_dump(self, **kwargs):
                return {"access_token": "test-only"}
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "credentials.json"
            storage = _OAuthStorage(path)
            storage._write("tokens", Model())
            self.assertEqual(os.stat(path).st_mode & 0o777, 0o600)
            self.assertEqual(storage._read()["tokens"]["access_token"], "test-only")
            path.chmod(0o644)
            with self.assertRaises(BrokerError):
                storage._read()
        with self.assertRaisesRegex(BrokerError, "authorization required"):
            await RobinhoodMCP({})._redirect("https://robinhood.com/oauth?test=1")

    async def test_oauth_callback_rejects_wrong_state_and_reports_denial(self):
        broker = RobinhoodMCP({})
        broker.oauth_state = "expected-test-state"
        broker.callback = asyncio.get_running_loop().create_future()
        async def send(query):
            reader = asyncio.StreamReader()
            reader.feed_data(("GET /callback?" + query + " HTTP/1.1\r\n").encode())
            reader.feed_eof()
            writer = MagicMock()
            writer.drain = AsyncMock()
            writer.wait_closed = AsyncMock()
            await broker._receive_callback(reader, writer)
            return writer.write.call_args[0][0]
        await send("code=test-code&state=wrong-state")
        self.assertFalse(broker.callback.done())
        self.assertIn(b"400 Bad Request", await send("code=test-code&state=%C3%A9"))
        self.assertFalse(broker.callback.done())
        for query in ("code=a&code=b", "code=", "code=a&error=denied", "error=", "code=a&state="):
            response = await send(query + "&state=expected-test-state")
            self.assertIn(b"400 Bad Request", response)
            self.assertFalse(broker.callback.done())
        await send("error=access_denied&state=expected-test-state")
        with self.assertRaisesRegex(BrokerError, "denied"):
            await broker.callback
        broker.callback = asyncio.get_running_loop().create_future()
        response = await send("code=test-code&state=expected-test-state")
        self.assertIn(b"200 OK", response)
        self.assertIn(b"Relay Monitor Setup", response)
        self.assertIn(b"Cache-Control: no-store", response)
        self.assertEqual(await broker.callback, ("test-code", "expected-test-state"))
        self.assertIn(b"400 Bad Request", await send("code=test-code&state=expected-test-state"))

    async def test_setup_authorization_link_uses_callback_listener_without_browser(self):
        show_link = AsyncMock()
        broker = RobinhoodMCP({}, interactive=True, authorization_handler=show_link)
        listener = MagicMock()
        listener.wait_closed = AsyncMock()
        with patch.dict(os.environ, {"RELAY_OAUTH_CALLBACK_HOST": "0.0.0.0"}), \
                patch("relay.broker.asyncio.start_server", new_callable=AsyncMock, return_value=listener) as start, \
                patch("relay.broker.webbrowser.open") as browser:
            for url in ("https://evil.example/?state=a", "https://user@robinhood.com/?state=a",
                        "https://robinhood.com:443/?state=a", "https://robinhood.com/?state=",
                        "https://robinhood.com/?state=a&state=", "https://robinhood.com/?state=a#fragment"):
                with self.assertRaises(BrokerError):
                    await broker._redirect(url)
            start.assert_not_called()
            url = "https://robinhood.com/oauth/authorize?state=synthetic-state"
            await broker._redirect(url)
            show_link.assert_awaited_once_with(url)
            start.assert_awaited_once_with(broker._receive_callback, "0.0.0.0", 8766, limit=8192)
            browser.assert_not_called()
            await broker.__aexit__(None, None, None)
            listener.close.assert_called_once()

    async def test_redirect_environment_validation(self):
        for uri in ("", "file:///callback", "http:///callback", "http://relay.example:0/callback",
                    "http://relay.example:99999/callback", "http://relay.example/callback?code=x",
                    "https://relay.example/callback#fragment", "http://user@relay.example/callback",
                    "http://relay.example/other", "http://relay.example/\ncallback",
                    "http://relay.example/callback?", "http://relay.example/callback#",
                    "http://relay.example:/callback", "http://relay.example\\other/callback"):
            with self.subTest(uri=uri), patch.dict(os.environ, {"RELAY_ROBINHOOD_REDIRECT_URI": uri}):
                with self.assertRaisesRegex(BrokerError, "RELAY_ROBINHOOD_REDIRECT_URI"):
                    RobinhoodMCP({})

    @unittest.skipUnless(importlib.util.find_spec("mcp"), "Optional Robinhood SDK is not installed")
    async def test_remote_redirect_uses_saved_credentials_and_pkce(self):
        from mcp.client.auth import OAuthClientProvider
        from mcp.shared.auth import OAuthClientInformationFull, OAuthToken
        remote = "http://192.168.1.20:8787/callback"
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "oauth.json"
            storage = _OAuthStorage(path)
            await storage.set_client_info(OAuthClientInformationFull(
                client_id="synthetic-existing-client", redirect_uris=["http://127.0.0.1:8766/callback"],
                token_endpoint_auth_method="none"))
            await storage.set_tokens(OAuthToken(access_token="synthetic-existing-token", token_type="Bearer"))
            original = path.read_bytes()
            providers = []
            def capture_provider(**kwargs):
                providers.append(OAuthClientProvider(**kwargs))
                return providers[-1]
            with patch.dict(os.environ, {"RELAY_ROBINHOOD_REDIRECT_URI": remote}), \
                    patch("mcp.client.auth.OAuthClientProvider", side_effect=capture_provider), \
                    patch("httpx.AsyncClient", side_effect=RuntimeError("offline transport stop")):
                broker = RobinhoodMCP({"token_store": str(path)})
                with self.assertRaisesRegex(RuntimeError, "offline transport stop"):
                    await broker.__aenter__()
            provider = providers[0]
            await provider._initialize()
            shown = AsyncMock()
            provider.context.redirect_handler = shown
            async def callback():
                query = parse_qs(urlsplit(shown.call_args.args[0]).query)
                self.assertEqual(query["redirect_uri"], [remote])
                self.assertEqual(query["client_id"], ["synthetic-existing-client"])
                self.assertEqual(query["code_challenge_method"], ["S256"])
                return "synthetic-code", query["state"][0]
            provider.context.callback_handler = callback
            request = await provider._perform_authorization()
            body = parse_qs(request.content.decode())
            self.assertEqual(body["redirect_uri"], [remote])
            self.assertTrue(body["code_verifier"][0])
            self.assertEqual(provider.context.current_tokens.access_token, "synthetic-existing-token")
            self.assertEqual(path.read_bytes(), original)

    @unittest.skipUnless(importlib.util.find_spec("mcp") and importlib.util.find_spec("jsonschema"),
                         "Optional Robinhood SDK is not installed")
    async def test_offline_sdk_oauth_and_discovered_schema_validation(self):
        import httpx
        from mcp.client.auth import OAuthClientProvider
        from mcp.shared.auth import OAuthClientMetadata, OAuthToken
        from mcp.types import CallToolResult, ListToolsResult, Tool
        with tempfile.TemporaryDirectory() as directory:
            storage = _OAuthStorage(Path(directory) / "oauth.json")
            await storage.set_tokens(OAuthToken(access_token="test-only", token_type="Bearer", expires_in=3600))
            self.assertEqual((await storage.get_tokens()).access_token, "test-only")
            auth = OAuthClientProvider(
                server_url="https://agent.robinhood.com/mcp/trading",
                client_metadata=OAuthClientMetadata(client_name="Offline test",
                    redirect_uris=["http://127.0.0.1:8766/callback"],
                    grant_types=["authorization_code", "refresh_token"], response_types=["code"],
                    token_endpoint_auth_method="none", scope="internal"),
                storage=storage, redirect_handler=AsyncMock(), callback_handler=AsyncMock(),
            )
            async with httpx.AsyncClient(auth=auth):
                pass
        broker = RobinhoodMCP({})
        broker.session = AsyncMock()
        schema = {"type": "object", "required": ["fixture"],
                  "properties": {"fixture": {"type": "string"}}, "additionalProperties": False}
        broker.session.list_tools.return_value = ListToolsResult(tools=[Tool(name="get_option_quotes", inputSchema=schema)])
        broker.session.call_tool.return_value = CallToolResult(content=[])
        self.assertEqual((await broker.discover())[0]["inputSchema"], schema)
        with self.assertRaisesRegex(BrokerError, "schema"):
            await broker.call("get_option_quotes", {})
        self.assertFalse(broker.session.call_tool.called)
        await broker.call("get_option_quotes", {"fixture": "local test"})
        broker.session.call_tool.assert_awaited_once()


class BrokerAuthClassificationChecks(unittest.TestCase):
    def test_auth_classifier_handles_http_challenges_and_groups_without_false_positives(self):
        self.assertTrue(is_auth_required(SimpleNamespace(response=SimpleNamespace(status_code=401))))
        self.assertTrue(is_auth_required(SimpleNamespace(
            response=SimpleNamespace(status_code=403, headers={"WWW-Authenticate": "Bearer error=invalid_token"})
        )))
        self.assertTrue(is_auth_required({"status": 401, "message": "provider response"}))
        self.assertTrue(is_auth_required(SimpleNamespace(
            response=SimpleNamespace(status_code=403, headers={"www-authenticate": "Bearer error=invalid_token"})
        )))
        self.assertTrue(is_auth_required(ExceptionGroup("transport", [BrokerError("expired token")])) )
        self.assertFalse(is_auth_required(SimpleNamespace(
            response=SimpleNamespace(status_code=403, headers={}),
            detail="account quota exceeded",
        )))
        self.assertFalse(is_auth_required(SimpleNamespace(status_code=429, detail="invalid token in quota response")))
        self.assertFalse(is_auth_required(ConnectionError("network unavailable")))

    def test_local_broker_credential_preflight_does_not_treat_any_file_as_ready(self):
        with tempfile.TemporaryDirectory() as directory:
            token_store = Path(directory) / "oauth.json"
            config = {
                "mode": "shadow",
                "robinhood": {"account_number": "12345678", "token_store": str(token_store)},
            }
            self.assertEqual(broker_credentials_state(config), "auth_required")
            token_store.write_text(json.dumps({"tokens": {"access_token": "synthetic"}}), encoding="utf-8")
            token_store.chmod(0o600)
            self.assertEqual(broker_credentials_state(config), "configured")
            token_store.write_text("{}", encoding="utf-8")
            token_store.chmod(0o600)
            self.assertEqual(broker_credentials_state(config), "auth_required")
            token_store.write_text("not-json", encoding="utf-8")
            token_store.chmod(0o600)
            self.assertEqual(broker_credentials_state(config), "unavailable")
            self.assertEqual(broker_credentials_state({"mode": "paper"}), "paper")


if __name__ == "__main__":
    unittest.main()
