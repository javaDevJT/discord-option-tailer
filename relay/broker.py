"""Official Robinhood MCP qualification and explicit, deterministic paper fills."""
from __future__ import annotations

import asyncio
from contextlib import AsyncExitStack
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
import json
import hmac
import hashlib
import os
from pathlib import Path
import stat
import tempfile
from urllib.parse import parse_qs, urlsplit
import webbrowser
import uuid

from .status import publish_status


ROBINHOOD_ENDPOINT = "https://agent.robinhood.com/mcp/trading"
READ_TOOLS = frozenset({
    "get_accounts", "get_portfolio", "get_realized_pnl", "get_pnl_trade_history",
    "search", "get_option_chains", "get_option_instruments", "get_option_quotes",
    "get_option_positions", "get_option_orders", "get_option_historicals",
    "get_option_level_upgrade_info",
})


class BrokerError(RuntimeError):
    pass


class BrokerPreflightHold(BrokerError):
    """Final dispatch validation failed before any order transport began."""


def _decimal(value, field):
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise BrokerError(f"Invalid {field}") from None
    if not result.is_finite() or result < 0:
        raise BrokerError(f"Invalid {field}")
    return result


def _contract_key(contract):
    try:
        symbol = contract["symbol"]
        expiry = contract["expiry"]
        kind = contract["option_type"]
        strike = _decimal(contract["strike"], "strike")
        datetime.strptime(expiry, "%Y-%m-%d")
        if not isinstance(symbol, str) or not symbol or kind not in {"call", "put"} or strike <= 0:
            raise ValueError
        return symbol.upper(), expiry, str(strike.normalize()), kind
    except (KeyError, TypeError, ValueError):
        raise BrokerError("Contract requires symbol, expiry, strike, and option_type") from None


class PaperBroker:
    """Fills against explicit fixture quotes, never against an alert's premium."""

    def __init__(self, config):
        self.config = config.get("paper", config)
        self.buying_power = _decimal(self.config.get("buying_power", "1000"), "buying power")
        self.fee_reserve = _decimal(config.get("risk", {}).get("fee_reserve_per_contract", "0"), "paper fee reserve")
        self.positions = []
        self.orders = {}
        self.order_bodies = {}

    def restore_positions(self, positions):
        holdings = {}
        for position in positions:
            if not isinstance(position, dict):
                raise BrokerError("Invalid simulated position")
            quantity = position.get("quantity")
            if type(quantity) is not int or quantity < 0 or position.get("type", "long") != "long":
                raise BrokerError("Paper positions require nonnegative whole long contracts")
            key = _contract_key(position.get("contract"))
            average = _decimal(position.get("average_price"), "paper average price")
            if not quantity:
                continue
            held = holdings.setdefault(key, {"contract": dict(zip(
                ("symbol", "expiry", "strike", "option_type"), key)),
                "quantity": 0, "average_price": "0"})
            total = held["quantity"] + quantity
            held["average_price"] = str((Decimal(held["average_price"]) * held["quantity"]
                                         + average * quantity) / total)
            held["quantity"] = total
        self.positions = list(holdings.values())

    async def snapshot(self):
        from datetime import timezone
        timestamp = datetime.now(timezone.utc)
        if "timestamp" in self.config:
            try:
                timestamp = datetime.fromisoformat(self.config["timestamp"].replace("Z", "+00:00"))
                if timestamp.tzinfo is None:
                    raise ValueError
            except (AttributeError, TypeError, ValueError):
                raise BrokerError("Paper snapshot timestamp requires an explicit timezone") from None
        equity, exposures = self.buying_power, {}
        for position in self.positions:
            quote = await self.quote(position["contract"])
            bid, ask = _decimal(quote["bid"], "bid"), _decimal(quote["ask"], "ask")
            quantity = position["quantity"]
            equity += bid * 100 * quantity
            symbol = position["contract"]["symbol"]
            exposure = max(Decimal(position["average_price"]), ask) * 100 * quantity
            exposures[symbol] = exposures.get(symbol, Decimal(0)) + exposure
        return {"market_open": self.config.get("market_open") is True,
                "buying_power": str(self.buying_power), "equity": str(equity),
                "positions": [dict(position, contract=dict(position["contract"])) for position in self.positions],
                "option_exposure_by_symbol": {symbol: str(value) for symbol, value in exposures.items()},
                "account_id": "paper", "timestamp": timestamp.astimezone(timezone.utc).isoformat(),
                "currency": "USD", "simulated": True}

    async def quote(self, contract):
        path = self.config.get("quotes_file")
        if not path:
            raise BrokerError("paper.quotes_file must point to explicit quote fixtures")
        try:
            data = json.loads(Path(path).read_text())
            quotes = data["quotes"] if isinstance(data, dict) else data
            matches = [q for q in quotes if _contract_key(q["contract"]) == _contract_key(contract)]
            if len(matches) != 1:
                raise BrokerError("Paper quote missing or ambiguous for this exact contract")
            quote = dict(matches[0])
            bid, ask = _decimal(quote["bid"], "bid"), _decimal(quote["ask"], "ask")
            tick = _decimal(quote["tick_size"], "tick size")
            timestamp = datetime.fromisoformat(quote["timestamp"].replace("Z", "+00:00"))
            if ask <= 0 or bid > ask or tick <= 0 or timestamp.tzinfo is None:
                raise BrokerError("Invalid paper quote prices or timestamp")
            if quote.get("multiplier") != 100 or quote.get("currency") != "USD" or quote.get("asset_type") != "equity_option":
                raise BrokerError("Paper broker supports standard USD contracts with multiplier 100")
            if not isinstance(quote.get("tradable"), bool):
                raise BrokerError("Paper quote requires explicit tradable boolean")
            return quote
        except (OSError, ValueError, TypeError, KeyError) as exc:
            if isinstance(exc, BrokerError):
                raise
            raise BrokerError("Invalid or unreadable paper quote fixture") from None

    async def submit(self, order):
        order_id = order.get("client_order_id")
        if not isinstance(order_id, str) or not order_id:
            raise BrokerError("Paper order requires client_order_id")
        quantity = order.get("quantity")
        side, effect = order.get("side"), order.get("position_effect")
        if type(quantity) is not int or quantity < 1 or (side, effect) not in {("buy", "open"), ("sell", "close")}:
            raise BrokerError("Paper broker supports positive quantities of long opens and closes")
        limit = _decimal(order.get("limit_price"), "limit price")
        order_body = {"contract": _contract_key(order.get("contract")), "side": side,
                      "position_effect": effect, "quantity": quantity, "limit_price": limit}
        if order_id in self.orders:
            if order_body != self.order_bodies[order_id]:
                raise BrokerError("Paper client_order_id was reused with different order details")
            return dict(self.orders[order_id])
        position = next((position for position in self.positions
                         if _contract_key(position["contract"]) == order_body["contract"]), None)
        if side == "sell" and (position is None or quantity > position["quantity"]):
            raise BrokerError("Paper sell quantity exceeds simulated holdings")
        quote = await self.quote(order["contract"])
        if self.config.get("market_open") is not True or not quote["tradable"] or limit <= 0:
            raise BrokerError("Paper market closed, contract untradable, or limit invalid")
        price = _decimal(quote["ask" if side == "buy" else "bid"], "fill price")
        crossing = limit >= price if side == "buy" else limit <= price
        cost = price * quantity * 100
        fees = self.fee_reserve * quantity
        if crossing and side == "buy" and cost + fees > self.buying_power:
            raise BrokerError("Insufficient simulated buying power")
        # ponytail: immediate all-or-none fixture fills; add an exchange simulator only for execution research.
        result = {"id": order_id, "status": "filled" if crossing else "open",
                  "filled_quantity": quantity if crossing else 0,
                  "fill_price": str(price) if crossing else None}
        if crossing:
            if side == "buy":
                self.restore_positions([*self.positions, {"contract": order["contract"],
                    "quantity": quantity, "average_price": str(price)}])
            else:
                position["quantity"] -= quantity
                if not position["quantity"]:
                    self.positions.remove(position)
            # Configured simulation reserve, not Robinhood's actual commission or fee schedule.
            self.buying_power += (cost if side == "sell" else -cost) - fees
        self.orders[order_id] = result
        self.order_bodies[order_id] = order_body
        return dict(result)

    async def order_status(self, order_id):
        if order_id not in self.orders:
            raise BrokerError("Unknown paper order; reconcile with the durable application ledger")
        return dict(self.orders[order_id])


class _OAuthStorage:
    """Owner-only atomic credential file; never include its content in diagnostics."""

    def __init__(self, path):
        self.path = Path(path)

    def _read(self):
        if not self.path.exists():
            return {}
        mode = self.path.stat().st_mode
        if self.path.is_symlink() or not stat.S_ISREG(mode) or stat.S_IMODE(mode) & 0o077:
            raise BrokerError("OAuth token store must be a regular owner-only file (chmod 600)")
        try:
            return json.loads(self.path.read_text())
        except (OSError, ValueError):
            raise BrokerError("OAuth token store cannot be read") from None

    def _write(self, key, value):
        data = self._read()
        data[key] = value.model_dump(mode="json", exclude_none=True)
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        name = None
        try:
            with tempfile.NamedTemporaryFile(mode="w", dir=self.path.parent, delete=False) as handle:
                name = handle.name
                os.fchmod(handle.fileno(), 0o600)
                json.dump(data, handle)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(name, self.path)
        finally:
            if name and os.path.exists(name):
                os.unlink(name)

    async def get_tokens(self):
        from mcp.shared.auth import OAuthToken
        value = self._read().get("tokens")
        return OAuthToken.model_validate(value) if value else None

    async def set_tokens(self, tokens):
        self._write("tokens", tokens)

    async def get_client_info(self):
        from mcp.shared.auth import OAuthClientInformationFull
        value = self._read().get("client_info")
        return OAuthClientInformationFull.model_validate(value) if value else None

    async def set_client_info(self, client_info):
        self._write("client_info", client_info)


class RobinhoodMCP:
    """Schema discovery and allowlisted reads only. Live order submission is unqualified."""

    def __init__(self, config, *, interactive=False, authorization_handler=None, on_status=None):
        self.config = config.get("robinhood", config)
        self.on_status = on_status
        self.interactive = interactive
        self.authorization_handler = authorization_handler
        self.timeout = float(self.config.get("timeout_seconds", 30))
        if not 0 < self.timeout <= 300:
            raise BrokerError("Robinhood timeout must be between 0 and 300 seconds")
        self.stack = AsyncExitStack()
        self.session = None
        self.schemas = {}
        self.catalog = {}
        self.server = None
        self.callback = None
        self.oauth_state = None

    async def _redirect(self, url):
        if not self.interactive:
            raise BrokerError("Robinhood authorization required; run the interactive broker login command")
        destination = urlsplit(url)
        if (destination.scheme != "https" or destination.netloc != "robinhood.com"
                or destination.fragment or any(ord(char) < 32 for char in url)):
            raise BrokerError("Unexpected Robinhood OAuth authorization destination")
        states = parse_qs(destination.query, keep_blank_values=True).get("state", [])
        if len(states) != 1 or not states[0]:
            raise BrokerError("Robinhood OAuth authorization is missing its state binding")
        self.oauth_state = states[0]
        self.callback = asyncio.get_running_loop().create_future()
        callback_host = os.environ.get("RELAY_OAUTH_CALLBACK_HOST", "127.0.0.1")
        self.server = await asyncio.start_server(self._receive_callback, callback_host, 8766, limit=8192)
        if self.authorization_handler is not None:
            await self.authorization_handler(url)
        elif not webbrowser.open(url):
            raise BrokerError("Could not open the desktop browser for Robinhood authorization")

    async def _receive_callback(self, reader, writer):
        try:
            request = await asyncio.wait_for(reader.readline(), 5)
            parts = request.decode("ascii").split()
            if len(parts) != 3 or parts[0] != "GET":
                raise ValueError
            uri = urlsplit(parts[1])
            params = parse_qs(uri.query, keep_blank_values=True)
            if uri.path != "/callback" or len(params.get("state", [])) != 1:
                raise ValueError
            state = params["state"][0]
            if not self.oauth_state or not hmac.compare_digest(state.encode(), self.oauth_state.encode()):
                raise ValueError
            if self.callback is None or self.callback.done():
                raise ValueError
            if len(params.get("error", [])) == 1 and params["error"][0] and "code" not in params:
                self.callback.set_exception(BrokerError("Robinhood authorization was denied"))
                body = b"Authorization was denied. Close this tab and return to Relay Monitor Setup."
            elif len(params.get("code", [])) == 1 and params["code"][0] and "error" not in params:
                self.callback.set_result((params["code"][0], state))
                body = b"Authorization received. Close this tab and return to Relay Monitor Setup; account inspection will finish there."
            else:
                raise ValueError
            writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: text/plain\r\nConnection: close\r\nCache-Control: no-store\r\nContent-Length: " + str(len(body)).encode() + b"\r\n\r\n" + body)
            await writer.drain()
        except (ValueError, UnicodeError, asyncio.TimeoutError):
            writer.write(b"HTTP/1.1 400 Bad Request\r\nConnection: close\r\nContent-Length: 0\r\n\r\n")
        finally:
            writer.close()
            await writer.wait_closed()

    async def _callback(self):
        try:
            return await asyncio.wait_for(self.callback, 300)
        finally:
            if self.server:
                self.server.close()
                await self.server.wait_closed()
                self.server = None

    async def __aenter__(self):
        import httpx
        from mcp import ClientSession
        from mcp.client.streamable_http import streamable_http_client
        headers, auth = {}, None
        auth_mode = self.config.get("auth", "oauth")
        if auth_mode == "token":
            token = os.environ.get(self.config.get("access_token_env", "ROBINHOOD_ACCESS_TOKEN"))
            if not token:
                raise BrokerError("Robinhood access token environment variable is not set")
            headers["Authorization"] = f"Bearer {token}"
        elif auth_mode == "oauth":
            from mcp.client.auth import OAuthClientProvider
            from mcp.shared.auth import OAuthClientMetadata
            auth = OAuthClientProvider(
                server_url=ROBINHOOD_ENDPOINT,
                client_metadata=OAuthClientMetadata(
                    client_name="Discord Options Relay",
                    redirect_uris=["http://127.0.0.1:8766/callback"],
                    grant_types=["authorization_code", "refresh_token"],
                    response_types=["code"], token_endpoint_auth_method="none", scope="internal",
                ),
                storage=_OAuthStorage(self.config.get("token_store", "state/robinhood-oauth.json")),
                redirect_handler=self._redirect, callback_handler=self._callback,
            )
        else:
            raise BrokerError("robinhood.auth must be oauth or token")
        try:
            http = await self.stack.enter_async_context(httpx.AsyncClient(
                auth=auth, headers=headers, follow_redirects=True,
                timeout=httpx.Timeout(self.timeout, read=360 if self.interactive else self.timeout),
            ))
            read, write, _ = await self.stack.enter_async_context(
                streamable_http_client(ROBINHOOD_ENDPOINT, http_client=http, terminate_on_close=False)
            )
            self.session = await self.stack.enter_async_context(ClientSession(
                read, write, read_timeout_seconds=timedelta(seconds=360 if self.interactive else self.timeout)
            ))
            await self.session.initialize()
            await self.discover()
            return self
        except BaseException as exc:
            if isinstance(exc, Exception):
                self._connection_failed(exc)
            await self.__aexit__(None, None, None)
            raise

    async def __aexit__(self, exc_type, exc, tb):
        if self.server:
            self.server.close()
            await self.server.wait_closed()
            self.server = None
        self.session = None
        await self.stack.aclose()

    async def discover(self):
        if self.session is None:
            raise BrokerError("Open RobinhoodMCP using async with before discovery")
        tools, cursor, seen = [], None, set()
        while True:
            result = await self.session.list_tools(cursor=cursor)
            tools.extend(tool.model_dump(mode="json", exclude_none=True) for tool in result.tools)
            cursor = result.nextCursor
            if not cursor:
                break
            if cursor in seen:
                raise BrokerError("Robinhood repeated a tool discovery cursor")
            seen.add(cursor)
        self.schemas = {tool["name"]: tool["inputSchema"] for tool in tools}
        self.catalog = {tool["name"]: tool for tool in tools}
        return tools

    async def call(self, name, args):
        if self.session is None:
            raise BrokerError("Open RobinhoodMCP using async with before calling tools")
        if name not in self.schemas:
            raise BrokerError("Tool is absent from the authenticated Robinhood schema catalog")
        if name not in READ_TOOLS:
            raise BrokerError("This tool is not allowed; live submission and cancellation require a qualified adapter")
        if not isinstance(args, dict):
            raise BrokerError("MCP tool arguments must be a JSON object")
        import jsonschema
        try:
            jsonschema.validate(args, self.schemas[name])
        except (jsonschema.ValidationError, jsonschema.SchemaError):
            raise BrokerError("Arguments do not satisfy the authenticated tool schema") from None
        result = await self._call_tool(name, args)
        if result.isError:
            raise BrokerError("Robinhood returned a tool error; no result has been accepted")
        return result.model_dump(mode="json", exclude_none=True)

    async def _call_tool(self, name, args):
        try:
            result = await self.session.call_tool(name, arguments=args)
        except Exception as exc:
            self._connection_failed(exc)
            raise
        publish_status(self.on_status, "broker", "unavailable" if result.isError else "connected")
        return result

    def _connection_failed(self, exc):
        def auth_required(error):
            if isinstance(error, BaseExceptionGroup):
                return any(auth_required(child) for child in error.exceptions)
            return (getattr(getattr(error, "response", None), "status_code", None) == 401
                    or isinstance(error, BrokerError) and "authorization required" in str(error))
        publish_status(self.on_status, "broker", "auth_required" if auth_required(exc) else "unavailable")


async def login(config, *, authorization_handler=None):
    """Interactive OAuth followed only by authenticated schema discovery."""
    async with RobinhoodMCP(config, interactive=True, authorization_handler=authorization_handler) as broker:
        return await broker.discover()


# Authenticated official schemas observed 2026-09-06; changes require renewed qualification.
SCHEMA_PINS = {
    "search": "577c2e161dec698d9efdb2d203a42d99798057035a277cbbb349c26477e7b28e",
    "get_accounts": "3df90562b040c920c73ba68b1685508a9db806266b0e98903bef0b9b04c83042",
    "get_portfolio": "b1d5f51ec0e84c8a62181dee3daa7a5d2ab93c7ece8d0c8373d482715455a2f5",
    "get_option_chains": "661824e1e339fdc16e61a5192a37ebb664de935fc493887f9825e16fbcc119ba",
    "get_option_instruments": "e27cf1cb98aeecf5940b23c6ff02dada0f07f5e90866d77e63a843b983f8d503",
    "get_option_quotes": "ac069476d02f1b401fc9f2f1a65d402a5d7cb352df2f4b06cff87307fb846508",
    "get_option_positions": "f9ee54d7cee627f491189d66330d1662954d6ef7b9ae889e90a27e8dea11cabd",
    "get_option_orders": "3d1a33c36ac93d9e3dd202f10b7597bf74fb91d491aa00c0b41ed460d7d51277",
    "review_option_order": "a3e359eb4e73e46d77f8fc9a3ab90ba4d88f0d36b96d58225fe8fbdde69b4dc0",
    "place_option_order": "2b6e3ecd2997e8a58d36b5b77c8a4883b551255d05d39511995a4245d7f37cb3",
}


def _instant(value):
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            raise ValueError
        return parsed
    except (ValueError, AttributeError):
        raise BrokerError("Broker timestamp must include a timezone") from None


def _contracts(value):
    quantity = _decimal(value, "contract quantity")
    if quantity != quantity.to_integral_value():
        raise BrokerError("Fractional option quantities are unsupported")
    return int(quantity)


class RobinhoodBroker(RobinhoodMCP):
    """Normalized Agentic account reads and explicitly enabled single-leg limit orders."""

    def __init__(self, config, *, interactive=False, clock=None, on_status=None):
        super().__init__(config, interactive=interactive, on_status=on_status)
        self.runtime = config
        self.account_number = self.config.get("account_number")
        if not isinstance(self.account_number, str) or not self.account_number.strip():
            raise BrokerError("Bind robinhood.account_number to the selected Agentic account")
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.currency = None
        self.instruments = {}
        self.order_bodies = {}
        self.order_results = {}
        self.attempted = set()

    def _qualified(self, name):
        tool = self.catalog.get(name)
        if not tool or name not in SCHEMA_PINS:
            raise BrokerError("Required broker tool is missing from the qualified catalog")
        schemas = {key: tool.get(key) for key in ("inputSchema", "outputSchema")}
        digest = hashlib.sha256(json.dumps(schemas, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        if digest != SCHEMA_PINS[name]:
            raise BrokerError("Robinhood schema changed; qualify the new schema before continuing")
        return tool

    async def _data(self, name, args):
        import jsonschema
        tool = self._qualified(name)
        if self.session is None:
            raise BrokerError("Open RobinhoodBroker using async with")
        try:
            jsonschema.validate(args, tool["inputSchema"])
        except (jsonschema.ValidationError, jsonschema.SchemaError):
            raise BrokerError("Normalized broker arguments do not match the qualified schema") from None
        result = await self._call_tool(name, args)
        if result.isError or not isinstance(result.structuredContent, dict):
            raise BrokerError("Robinhood returned an error or omitted its structured response")
        try:
            jsonschema.validate(result.structuredContent, tool["outputSchema"])
        except (jsonschema.ValidationError, jsonschema.SchemaError):
            raise BrokerError("Robinhood response does not match the qualified schema") from None
        return result.structuredContent["data"]

    async def _pages(self, name, args, key):
        rows, seen = [], set()
        while True:
            data = await self._data(name, args)
            page = data.get(key)
            if page is not None and not isinstance(page, list):
                raise BrokerError("Unexpected broker result page")
            rows.extend(page or [])
            next_url = data.get("next")
            if not next_url:
                return rows
            cursors = parse_qs(urlsplit(next_url).query).get("cursor", [])
            if len(cursors) != 1 or cursors[0] in seen:
                raise BrokerError("Broker pagination is incomplete or cyclic")
            seen.add(cursors[0])
            args = dict(args, cursor=cursors[0])

    def _market_open(self):
        try:
            import exchange_calendars
            calendar = exchange_calendars.get_calendar("XNYS")
            now = self.clock().astimezone(timezone.utc)
            session = now.date().isoformat()
            return bool(calendar.is_session(session) and
                        calendar.session_open(session).to_pydatetime() <= now < calendar.session_close(session).to_pydatetime())
        except (ImportError, ValueError, KeyError):
            raise BrokerError("NYSE regular-session calendar is unavailable") from None

    async def _portfolio(self):
        data = await self._data("get_portfolio", {"account_number": self.account_number})
        power = data.get("buying_power")
        if not isinstance(power, dict) or data.get("currency") != "USD" or power.get("display_currency") != "USD":
            raise BrokerError("Authoritative USD account equity and buying power are required")
        self.currency = data["currency"]
        return data, min(_decimal(power["buying_power"], "buying power"),
                         _decimal(power["unleveraged_buying_power"], "unleveraged buying power"))

    async def _instrument(self, option_id):
        if option_id not in self.instruments:
            rows = await self._pages("get_option_instruments", {"ids": option_id}, "instruments")
            matches = [row for row in rows if row["id"] == option_id]
            if len(matches) != 1:
                raise BrokerError("An account option cannot be resolved to one instrument")
            self.instruments[option_id] = matches[0]
        return self.instruments[option_id]

    @staticmethod
    def _instrument_contract(instrument):
        if instrument["underlying_type"] != "equity" or _decimal(instrument["trade_value_multiplier"], "multiplier") != 100:
            raise BrokerError("Only standard equity and ETF option contracts are supported")
        contract = {"symbol": instrument["chain_symbol"], "expiry": instrument["expiration_date"],
                    "strike": instrument["strike_price"], "option_type": instrument["type"]}
        _contract_key(contract)
        return contract

    async def _raw_quote(self, option_id):
        data = await self._data("get_option_quotes", {"instrument_ids": [option_id]})
        matches = [row["quote"] for row in data["results"] or []
                   if row and row.get("quote") and row["quote"]["instrument_id"] == option_id]
        if len(matches) != 1:
            raise BrokerError("Option quote is missing or ambiguous")
        _instant(matches[0]["updated_at"])
        return matches[0]

    async def snapshot(self):
        observed_at = self.clock()
        account_data, portfolio_result, positions, orders = await asyncio.gather(
            self._data("get_accounts", {}), self._portfolio(),
            self._pages("get_option_positions", {"account_number": self.account_number, "nonzero": True}, "positions"),
            self._pages("get_option_orders", {"account_number": self.account_number}, "orders"),
        )
        matches = [row for row in account_data["accounts"] or [] if row["account_number"] == self.account_number]
        if len(matches) != 1:
            raise BrokerError("Bound Robinhood account is missing or ambiguous")
        account = matches[0]
        portfolio, power = portfolio_result
        exposure, normalized = {}, []
        reported_pending_buys = {}
        for position in positions:
            quantity = _contracts(position["quantity"])
            if not quantity:
                continue
            if position["type"] != "long" or _decimal(position["trade_value_multiplier"], "multiplier") != 100:
                raise BrokerError("Short or adjusted option positions require separate risk qualification")
            instrument = await self._instrument(position["option_id"])
            contract = self._instrument_contract(instrument)
            quote = await self._raw_quote(position["option_id"])
            self._check_quote_age(quote["updated_at"])
            average_cost = _decimal(position["average_price"], "position contract cost")
            mark = _decimal(quote["mark_price"], "option mark")
            risk_value = max(average_cost, _decimal(quote["ask_price"], "ask") * 100, mark * 100) * quantity
            symbol = contract["symbol"]
            exposure[symbol] = exposure.get(symbol, Decimal(0)) + risk_value
            unavailable = sum(_contracts(position[key]) for key in ("pending_sell_quantity", "pending_exercise_quantity",
                              "pending_assignment_quantity", "pending_expiration_quantity"))
            if unavailable > quantity:
                raise BrokerError("Pending option activity exceeds the reported inventory")
            normalized.append({"contract": contract, "option_id": position["option_id"], "quantity": quantity,
                               "available_quantity": quantity - unavailable, "average_price": str(average_cost / 100),
                               "market_value": str(mark * 100 * quantity), "quote_timestamp": quote["updated_at"],
                               "pending_sell_quantity": _contracts(position["pending_sell_quantity"]),
                               "pending_other_quantity": unavailable - _contracts(position["pending_sell_quantity"])})
            reported_pending_buys[position["option_id"]] = _contracts(position["pending_buy_quantity"])
        working, pending_buys, pending_sells = [], {}, {}
        for order in orders:
            if order["state"] in {"filled", "rejected", "cancelled", "failed", "voided"}:
                continue
            if order["state"] not in {"queued", "confirmed", "partially_filled", "pending_cancelled"}:
                raise BrokerError("An external option order has an unknown lifecycle state")
            legs = order["legs"] or []
            if len(legs) != 1 or legs[0]["ratio_quantity"] != 1 or order["type"] != "limit" or order["trigger"] != "immediate":
                raise BrokerError("Complex external option orders require separate risk qualification")
            leg = legs[0]
            if (leg["side"], leg["position_effect"]) not in {("buy", "open"), ("sell", "close")}:
                raise BrokerError("An external option order can create an unsupported short position")
            if _decimal(order["trade_value_multiplier"], "multiplier") != 100:
                raise BrokerError("An external option order has an unsupported multiplier")
            option_id = leg.get("option_id")
            if not option_id:
                raise BrokerError("An external option order cannot be resolved to its exact contract")
            pending = _contracts(order["pending_quantity"])
            if leg["side"] == "buy":
                symbol = order["chain_symbol"]
                exposure[symbol] = exposure.get(symbol, Decimal(0)) + _decimal(order["price"], "working limit") * 100 * pending
                pending_buys[option_id] = pending_buys.get(option_id, 0) + pending
            else:
                pending_sells[option_id] = pending_sells.get(option_id, 0) + pending
            working.append(order["id"])
        for position in normalized:
            option_id = position["option_id"]
            if reported_pending_buys[option_id] > pending_buys.get(option_id, 0):
                raise BrokerError("Account pending buys are not fully represented in its working orders")
            unavailable = position["pending_other_quantity"] + max(position["pending_sell_quantity"], pending_sells.get(option_id, 0))
            if unavailable > position["quantity"]:
                raise BrokerError("External working sells exceed the held long inventory")
            position["available_quantity"] = position["quantity"] - unavailable
        if any(option_id not in {position["option_id"] for position in normalized} for option_id in pending_sells):
            raise BrokerError("An external working sell has no matching long position")
        restrictions = []
        if account.get("agentic_allowed") is not True:
            restrictions.append("account is not enabled for Agentic trading")
        if account["state"] != "active" or account["deactivated"] or account["permanently_deactivated"]:
            restrictions.append("account is not active")
        if account.get("option_level") not in {"option_level_2", "option_level_3"}:
            restrictions.append("long options approval is missing")
        for position in normalized:
            self._check_quote_age(position["quote_timestamp"])
        return {"account_id": self.account_number, "equity": str(_decimal(portfolio["total_value"], "total equity")),
                "buying_power": str(power), "reported_buying_power": portfolio["buying_power"]["buying_power"],
                "currency": self.currency, "positions": normalized,
                "option_exposure_by_symbol": {symbol: str(value) for symbol, value in exposure.items()},
                "working_order_ids": working, "market_open": self._market_open(),
                "market_open_source": "exchange_calendars:XNYS regular session",
                "timestamp": observed_at.isoformat(), "timestamp_source": "local_fetch_started",
                "agentic_allowed": account.get("agentic_allowed") is True, "option_level": account.get("option_level"),
                "account_type": account["type"], "account_state": account["state"], "restrictions": restrictions,
                "simulated": False, "sandbox_status": "not_reported", "source": "Robinhood Agentic MCP"}

    async def quote(self, contract):
        target = _contract_key(contract)
        chains = await self._pages("get_option_chains", {"underlying_symbol": target[0]}, "chains")
        candidates = []
        for chain in chains:
            if chain["symbol"] != target[0] or target[1] not in (chain["expiration_dates"] or []):
                continue
            rows = await self._pages("get_option_instruments", {"chain_id": chain["id"], "expiration_dates": target[1],
                "strike_price": format(Decimal(target[2]), "f"), "type": target[3], "state": "active", "tradability": "tradable"}, "instruments")
            for instrument in rows:
                if instrument["chain_id"] == chain["id"] and _contract_key(self._instrument_contract(instrument)) == target:
                    candidates.append((chain, instrument))
        if len(candidates) != 1:
            raise BrokerError("Exact option contract is missing or ambiguous across chains")
        chain, instrument = candidates[0]
        underlyings = chain["underlying_instruments"] or []
        if (chain["cash_component"] is not None and _decimal(chain["cash_component"], "cash component") != 0) or len(underlyings) != 1:
            raise BrokerError("Adjusted or non-equity chains are unsupported")
        if underlyings[0]["symbol"] != target[0]:
            if underlyings[0]["symbol"]:
                raise BrokerError("Underlying equity symbol conflicts with the option chain")
            # Upstream sometimes omits this symbol. Parse identity locally; never fetch its internal URL.
            try:
                underlying_id = str(uuid.UUID(urlsplit(underlyings[0]["instrument"]).path.rstrip("/").split("/")[-1]))
            except (ValueError, TypeError):
                raise BrokerError("Underlying equity identity is unavailable") from None
            search = await self._data("search", {"query": target[0], "asset_type": "instrument", "limit": 20})
            matches = [row for row in search.get("results") or []
                       if row["symbol"] == target[0] and row["instrument_id"] == underlying_id]
            if len(matches) != 1:
                raise BrokerError("Official equity search could not verify the chain's underlying instrument")
        if _decimal(chain["trade_value_multiplier"], "chain multiplier") != 100:
            raise BrokerError("Nonstandard chain multiplier")
        self.instruments[instrument["id"]] = instrument
        raw = await self._raw_quote(instrument["id"])
        bid, ask = _decimal(raw["bid_price"], "bid"), _decimal(raw["ask_price"], "ask")
        if bid > ask:
            raise BrokerError("Option book is crossed")
        ticks = instrument["min_ticks"]
        cutoff = _decimal(ticks["cutoff_price"], "tick cutoff")
        tick = max(_decimal(ticks["above_tick" if price >= cutoff else "below_tick"], "tick") for price in (bid, ask))
        if tick <= 0:
            raise BrokerError("Option tick is invalid")
        if self.currency is None:
            await self._portfolio()
        return {"contract": self._instrument_contract(instrument), "option_id": instrument["id"], "chain_id": chain["id"],
                "bid": str(bid), "ask": str(ask), "tick_size": str(tick), "min_ticks": ticks,
                "bid_size": raw["bid_size"], "ask_size": raw["ask_size"], "mark": raw["mark_price"],
                "timestamp": raw["updated_at"], "currency": self.currency, "multiplier": 100,
                "tradable": instrument["state"] == "active" and instrument["tradability"] == "tradable" and ask > 0 and bid > 0 and raw["ask_size"] > 0 and raw["bid_size"] > 0,
                "can_open_position": chain["can_open_position"], "asset_type": "equity_option",
                "sellout_datetime": instrument.get("sellout_datetime"), "source": "Robinhood Agentic MCP"}

    def _live_enabled(self):
        if self.runtime.get("mode") != "live" or self.config.get("enable_live_orders") is not True:
            raise BrokerError("Live Robinhood orders require live mode and explicit enable_live_orders")
        stop = self.runtime.get("kill_switch")
        if stop and Path(stop).exists():
            raise BrokerError("Kill switch is present")

    def _check_quote_age(self, timestamp):
        age = (self.clock() - _instant(timestamp)).total_seconds()
        maximum = _decimal(self.runtime.get("risk", {}).get("max_quote_age_seconds", 15), "quote age limit")
        if maximum <= 0 or age < -2 or age > maximum:
            raise BrokerError("Option quote is stale")

    def _fresh_quote(self, quote):
        self._check_quote_age(quote["timestamp"])
        if not quote["tradable"]:
            raise BrokerError("Option quote is stale or untradable")

    async def _order_args(self, order):
        quantity = order.get("quantity")
        side, effect = order.get("side"), order.get("position_effect")
        if type(quantity) is not int or quantity <= 0 or (side, effect) not in {("buy", "open"), ("sell", "close")}:
            raise BrokerError("Only positive whole-contract long opens and closes are supported")
        quote = await self.quote(order["contract"])
        self._fresh_quote(quote)
        price = _decimal(order.get("limit_price"), "limit price")
        ticks = quote["min_ticks"]
        tick = _decimal(ticks["above_tick" if price >= _decimal(ticks["cutoff_price"], "tick cutoff") else "below_tick"], "tick")
        if price <= 0 or tick <= 0 or price % tick:
            raise BrokerError("Order price does not satisfy the option's tick rule")
        if side == "buy" and not quote["can_open_position"]:
            raise BrokerError("This option chain is closed to new positions")
        args = {"account_number": self.account_number, "legs": [{"option_id": quote["option_id"], "side": side,
                "position_effect": effect, "ratio_quantity": 1}], "quantity": str(quantity), "type": "limit",
                "price": format(price, "f"), "time_in_force": "gfd", "market_hours": "regular_hours"}
        return args, quote

    async def _review_args(self, args, quote):
        review = await self._data("review_option_order", dict(args, chain_symbol=quote["contract"]["symbol"], underlying_type="equity"))
        if review.get("order_checks") != {}:
            raise BrokerError("Robinhood pre-trade review reported an alert; order was not placed")
        for key in ("account_number", "type", "time_in_force", "market_hours"):
            if review.get(key) != args[key]:
                raise BrokerError("Robinhood review does not match the intended order")
        legs = [dict(leg, ratio_quantity=leg.get("ratio_quantity", 1)) for leg in review.get("legs") or []]
        if legs != args["legs"]:
            raise BrokerError("Robinhood review legs do not match the intended order")
        if (_contracts(review.get("quantity")) != _contracts(args["quantity"]) or
                _decimal(review.get("price"), "review price") != _decimal(args["price"], "intended price") or
                review.get("direction") != ("debit" if args["legs"][0]["side"] == "buy" else "credit")):
            raise BrokerError("Robinhood review amount or direction does not match the intended order")
        collateral = review.get("collateral")
        if not collateral or collateral["account_number"] != self.account_number or collateral["cash"]["infinite"]:
            raise BrokerError("Robinhood review has missing or unsupported collateral")
        _decimal(collateral["cash"].get("amount"), "review collateral")
        if not isinstance(review.get("fees"), dict):
            raise BrokerError("Robinhood review did not supply actual fees")
        try:
            fee = Decimal(review["fees"]["total_fee"])
            if not fee.is_finite():
                raise InvalidOperation
        except (InvalidOperation, KeyError):
            raise BrokerError("Robinhood review fee is invalid") from None
        return review, fee

    async def review(self, order):
        self._live_enabled()
        args, quote = await self._order_args(order)
        review, _ = await self._review_args(args, quote)
        return review

    async def submit(self, order, before_submit=None):
        self._live_enabled()
        client_id = order.get("client_order_id")
        if not isinstance(client_id, str) or not client_id:
            raise BrokerError("A persistent client_order_id is required")
        body = json.dumps(dict(order, contract=_contract_key(order.get("contract")),
            limit_price=str(_decimal(order.get("limit_price"), "limit price").normalize())), sort_keys=True, separators=(",", ":"))
        if client_id in self.order_bodies and self.order_bodies[client_id] != body:
            raise BrokerError("client_order_id was reused with different order details")
        if client_id in self.order_results:
            return dict(self.order_results[client_id])
        if client_id in self.attempted:
            raise BrokerError("Prior submission outcome is unknown; reconcile without resubmitting")
        snapshot = await self.snapshot()
        if snapshot["restrictions"] or not snapshot["market_open"]:
            raise BrokerError("Account restrictions or closed market prevent order placement")
        args, quote = await self._order_args(order)
        if order["side"] == "sell":
            available = sum(position["available_quantity"] for position in snapshot["positions"]
                            if position["option_id"] == quote["option_id"])
            if order["quantity"] > available:
                raise BrokerError("Order would exceed available long option inventory")
        review, fee = await self._review_args(args, quote)
        if order["side"] == "buy" and _decimal(args["price"], "price") * 100 * order["quantity"] + max(fee, Decimal(0)) > _decimal(snapshot["buying_power"], "buying power"):
            raise BrokerError("Actual review fees and premium exceed available buying power")
        self._fresh_quote(quote)
        for position in snapshot["positions"]:
            self._check_quote_age(position["quote_timestamp"])
        self._live_enabled()
        if not self._market_open():
            raise BrokerError("Regular session ended before dispatch")
        ref_id = str(uuid.uuid5(uuid.NAMESPACE_URL, "discord-options-relay:" + self.account_number + ":" + client_id))
        self.order_bodies[client_id] = body
        if before_submit is not None:
            try:
                await before_submit(snapshot, quote)
                self._live_enabled()
                self._fresh_quote(quote)
                for position in snapshot["positions"]:
                    self._check_quote_age(position["quote_timestamp"])
                if not self._market_open():
                    raise BrokerError("Regular session ended before dispatch")
            except Exception as exc:
                raise BrokerPreflightHold("Final dispatch validation held this order; nothing was submitted") from None
        self.attempted.add(client_id)
        result = await self._data("place_option_order", dict(args, ref_id=ref_id))
        normalized = self._order_result(result.get("order"), client_id, args)
        normalized["ref_id"] = ref_id
        self.order_results[client_id] = normalized
        return dict(normalized)

    def _order_result(self, raw, client_id, expected=None):
        if not isinstance(raw, dict) or _decimal(raw["trade_value_multiplier"], "multiplier") != 100:
            raise BrokerError("Missing or unsupported submitted order response")
        legs = raw["legs"] or []
        if len(legs) != 1 or legs[0]["ratio_quantity"] != 1 or raw["type"] != "limit" or raw["trigger"] != "immediate":
            raise BrokerError("Broker order differs from the supported single-leg limit strategy")
        if (legs[0]["side"], legs[0]["position_effect"]) not in {("buy", "open"), ("sell", "close")} or raw["direction"] != ("debit" if legs[0]["side"] == "buy" else "credit"):
            raise BrokerError("Broker order has an unexpected option strategy or cash direction")
        if expected:
            leg = expected["legs"][0]
            if any(legs[0].get(key) != leg[key] for key in ("option_id", "side", "position_effect", "ratio_quantity")) or _contracts(raw["quantity"]) != _contracts(expected["quantity"]) or _decimal(raw["price"], "order price") != _decimal(expected["price"], "expected price"):
                raise BrokerError("Broker order does not match the reviewed order")
        states = {"queued": "open", "confirmed": "open", "partially_filled": "partially_filled", "filled": "filled",
                  "rejected": "rejected", "cancelled": "canceled", "failed": "rejected", "voided": "rejected", "pending_cancelled": "open"}
        if raw["state"] not in states:
            raise BrokerError("Unknown Robinhood order status; reconciliation is required")
        filled = _contracts(raw["processed_quantity"])
        premium = _decimal(raw["processed_premium"], "filled premium")
        if filled > _contracts(raw["quantity"]) or (not filled and premium):
            raise BrokerError("Inconsistent cumulative Robinhood fills")
        if (raw["state"] == "filled" and filled != _contracts(raw["quantity"])) or (filled and not premium):
            raise BrokerError("Robinhood fill quantity or premium is inconsistent with its status")
        return {"id": raw["id"], "client_order_id": client_id, "broker_order_id": raw["id"], "status": states[raw["state"]], "filled_quantity": filled,
                "fill_price": str(premium / 100 / filled) if filled else None,
                "timestamp": raw.get("updated_at") or raw["created_at"]}

    async def order_status(self, order_id, *, broker_order_id=None):
        known = self.order_results.get(order_id)
        broker_id = broker_order_id or (known or {}).get("broker_order_id")
        if not broker_id:
            try:
                broker_id = str(uuid.UUID(order_id))
            except (ValueError, TypeError):
                raise BrokerError("Persisted broker order UUID is required; unknown submissions must not be repeated") from None
        rows = await self._pages("get_option_orders", {"account_number": self.account_number, "order_id": broker_id}, "orders")
        if len(rows) != 1 or rows[0]["id"] != broker_id:
            raise BrokerError("Broker order could not be reconciled on the bound account")
        result = self._order_result(rows[0], order_id)
        self.order_results[order_id] = result
        return result
