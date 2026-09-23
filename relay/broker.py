"""Official Robinhood MCP qualification and explicit, deterministic paper fills."""
from __future__ import annotations

import asyncio
import contextvars
import copy
from contextlib import AsyncExitStack, contextmanager
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
import functools
import json
import hmac
import hashlib
import os
from pathlib import Path
from zoneinfo import ZoneInfo
import stat
import tempfile
import time
from urllib.parse import parse_qs, urlsplit
import webbrowser
import uuid

from .status import annotate_failure, failure_detail, publish_status


ROBINHOOD_ENDPOINT = "https://agent.robinhood.com/mcp/trading"
READ_TOOLS = frozenset({
    "get_accounts", "get_portfolio", "get_realized_pnl", "get_pnl_trade_history",
    "search", "get_equity_quotes", "get_option_chains", "get_option_instruments", "get_option_quotes",
    "get_option_positions", "get_option_orders", "get_option_historicals",
    "get_option_level_upgrade_info",
})
_BROKER_READ_CONCURRENCY = 4
_EXECUTION_READ_REUSE_SECONDS = 1.0
_PREPARED_ENTRY_TTL_SECONDS = 60.0
_PREPARED_ENTRY_CACHE_LIMIT = 64


class BrokerError(RuntimeError):
    pass


class BrokerPreflightHold(BrokerError):
    """Final dispatch validation failed before any order transport began."""


class _ExecutionReadScope:
    def __init__(self):
        self.active = True
        self.handoffs = {}
        self.snapshot = None
        self.quotes = {}

    def deactivate(self):
        self.active = False
        self.handoffs.clear()
        self.snapshot = None
        self.quotes.clear()


def _scope_operation(method):
    @functools.wraps(method)
    async def wrapped(self, *args, **kwargs):
        try:
            return await method(self, *args, **kwargs)
        except BaseException as error:
            self._invalidate_execution_scope()
            if isinstance(error, Exception):
                annotate_failure(error, broker_operation=method.__name__)
            raise

    return wrapped


def regular_session(now=None):
    """Return the current XNYS regular-session bounds in UTC, or ``None``."""
    try:
        import exchange_calendars

        instant = now or datetime.now(timezone.utc)
        if not isinstance(instant, datetime) or instant.tzinfo is None:
            raise ValueError
        instant = instant.astimezone(timezone.utc)
        calendar = exchange_calendars.get_calendar("XNYS")
        session = instant.astimezone(ZoneInfo("America/New_York")).date().isoformat()
        if not calendar.is_session(session):
            return None
        opened = calendar.session_open(session).to_pydatetime().astimezone(timezone.utc)
        closed = calendar.session_close(session).to_pydatetime().astimezone(timezone.utc)
        return opened, closed
    except (ImportError, ValueError, KeyError, AttributeError):
        raise BrokerError("NYSE regular-session calendar is unavailable") from None


_AUTHENTICATION_MARKERS = (
    "authorization required",
    "authentication required",
    "authentication needs attention",
    "unauthorized",
    "unauthenticated",
    "invalid access token",
    "invalid token",
    "token expired",
    "expired token",
    "access token expired",
    "invalid_grant",
    "invalid_client",
    "insufficient_scope",
)
_AUTHENTICATION_CODES = frozenset({
    "invalid_token", "expired_token", "token_expired", "invalid_grant",
    "invalid_client", "unauthorized", "unauthenticated", "authentication_required",
    "authorization_required", "insufficient_scope",
})


def _status_code(value):
    """Return an integer HTTP status without trusting arbitrary provider data."""

    candidates = [getattr(value, "status_code", None), getattr(value, "status", None)]
    if isinstance(value, dict):
        candidates.extend(value.get(key) for key in ("status_code", "http_status", "status"))
    for candidate in candidates:
        if isinstance(candidate, bool):
            continue
        try:
            candidate = int(candidate)
        except (TypeError, ValueError):
            continue
        if 100 <= candidate <= 599:
            return candidate
    return None


def _auth_header(value):
    headers = getattr(value, "headers", None)
    if headers is None:
        return ""
    try:
        value = headers.get("WWW-Authenticate", headers.get("www-authenticate", ""))
    except (AttributeError, TypeError):
        return ""
    return value.lower()[:4096] if isinstance(value, str) else ""


def _auth_text(value, *, depth=0, seen=None):
    """Extract bounded auth indicators for classification only.

    The returned text is never published.  Restricting traversal keeps a malformed
    provider result from turning status handling into an unbounded recursive walk.
    """

    if value is None or depth > 4:
        return ""
    if seen is None:
        seen = set()
    identity = id(value)
    if identity in seen:
        return ""
    seen.add(identity)
    if isinstance(value, str):
        return value.lower()[:4096]
    if isinstance(value, (bytes, bytearray)):
        return value.decode("utf-8", errors="replace").lower()[:4096]
    if isinstance(value, dict):
        parts = []
        for key, child in list(value.items())[:64]:
            key_text = str(key).lower()[:256]
            parts.append(key_text)
            parts.append(_auth_text(child, depth=depth + 1, seen=seen))
        return " ".join(parts)[:8192]
    if isinstance(value, (list, tuple, set, frozenset)):
        return " ".join(_auth_text(child, depth=depth + 1, seen=seen) for child in list(value)[:64])[:8192]
    if hasattr(value, "model_dump"):
        try:
            return _auth_text(value.model_dump(mode="json", exclude_none=True), depth=depth + 1, seen=seen)
        except (AttributeError, TypeError, ValueError):
            pass
    return str(value).lower()[:4096]


def is_auth_required(error):
    """Classify an upstream auth failure without exposing its response details.

    HTTP 401 is an authentication failure.  HTTP 403 is classified only when the
    provider supplies an authentication challenge or an explicit auth marker; a
    plain forbidden, quota, network, schema, or runtime error stays unavailable.
    """

    seen = set()

    def classify(value, depth=0):
        if value is None or depth > 6 or id(value) in seen:
            return False
        seen.add(id(value))
        if isinstance(value, BaseExceptionGroup):
            return any(classify(child, depth + 1) for child in value.exceptions)

        response = getattr(value, "response", None)
        code = _status_code(value) or _status_code(response)
        challenge = _auth_header(value) or _auth_header(response)
        text = _auth_text(value)
        if code == 401:
            return True
        if code in {408, 425, 429} or (code is not None and 500 <= code <= 599):
            return False
        text_markers = _AUTHENTICATION_MARKERS + tuple(_AUTHENTICATION_CODES)
        if code == 403 and (
            "bearer" in challenge and any(marker in challenge for marker in _AUTHENTICATION_CODES)
            or any(marker in text for marker in text_markers)
        ):
            return True
        if any(marker in text for marker in text_markers):
            return True
        if isinstance(value, dict):
            code_value = value.get("code") or value.get("error") or value.get("error_code")
            if isinstance(code_value, str) and code_value.lower() in _AUTHENTICATION_CODES:
                return True
        for child in (response, getattr(value, "__cause__", None), getattr(value, "__context__", None)):
            if classify(child, depth + 1):
                return True
        return False

    return classify(error)


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

    async def nearest_expiry(self, contract):
        """Resolve an omitted entry expiry using only explicit paper fixtures."""
        target = _contract_key(contract)
        path = self.config.get("quotes_file")
        if not path:
            raise BrokerError("paper.quotes_file must point to explicit quote fixtures")
        data = json.loads(Path(path).read_text())
        quotes = data["quotes"] if isinstance(data, dict) else data
        expiries = []
        for quote in quotes:
            key = _contract_key(quote["contract"])
            if key[0] == target[0] and key[2:] == target[2:] and key[1] >= target[1] and quote.get("tradable") is True and quote.get("multiplier") == 100 and quote.get("currency") == "USD" and quote.get("asset_type") == "equity_option":
                expiries.append(key[1])
        if not expiries:
            raise BrokerError("No listed expiration for the requested option")
        return dict(contract, expiry=min(expiries))

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

    async def underlying_quote(self, symbol):
        """Read an explicit per-symbol underlying quote from the paper fixture."""
        if not isinstance(symbol, str) or not symbol.strip():
            raise BrokerError("Underlying quote requires a symbol")
        symbol = symbol.strip().upper()
        path = self.config.get("quotes_file")
        if not path:
            raise BrokerError("paper.quotes_file must point to explicit quote fixtures")
        try:
            data = json.loads(Path(path).read_text())
            quotes = data["quotes"] if isinstance(data, dict) else data
            matches = []
            for quote in quotes:
                contract = quote.get("contract") if isinstance(quote, dict) else None
                if not isinstance(contract, dict):
                    continue
                if str(contract.get("symbol", "")).upper() != symbol:
                    continue
                if "underlying_price" not in quote or "underlying_timestamp" not in quote:
                    continue
                price = _decimal(quote["underlying_price"], "underlying price")
                timestamp = datetime.fromisoformat(
                    str(quote["underlying_timestamp"]).replace("Z", "+00:00")
                )
                if price <= 0 or timestamp.tzinfo is None:
                    raise BrokerError("Invalid paper underlying quote")
                matches.append((price, timestamp.astimezone(timezone.utc)))
            if not matches or len(set(matches)) != 1:
                raise BrokerError("Paper underlying quote missing or ambiguous for symbol")
            price, timestamp = matches[0]
            return {"symbol": symbol, "price": str(price), "timestamp": timestamp.isoformat()}
        except BrokerError:
            raise
        except (OSError, ValueError, TypeError, KeyError):
            raise BrokerError("Invalid or unreadable paper quote fixture") from None

    async def submit(self, order):
        order_id = order.get("client_order_id")
        quantity = order.get("quantity")
        side, effect = order.get("side"), order.get("position_effect")
        order_type = order.get("order_type", "limit")
        if not isinstance(order_id, str) or not order_id:
            raise BrokerError("Paper order requires a client_order_id")
        if type(quantity) is not int or quantity < 1 or (side, effect) not in {("buy", "open"), ("sell", "close")}:
            raise BrokerError("Paper broker supports positive quantities for long opens and closes")
        if order_type not in {"limit", "market", "stop_market"}:
            raise BrokerError("Paper broker supports limit, market, and stop_market orders")
        if order_type == "limit":
            if order.get("time_in_force", "gfd") != "gfd" or order.get("stop_price") is not None:
                raise BrokerError("Paper limit orders require gfd and no stop price")
            limit = _decimal(order.get("limit_price"), "limit price")
            if limit <= 0:
                raise BrokerError("Paper limit price must be positive")
            stop = None
        elif order_type == "market":
            if (side, effect) != ("sell", "close") or order.get("limit_price") is not None or order.get("stop_price") is not None:
                raise BrokerError("Paper market orders only support sell-to-close without prices")
            if order.get("time_in_force", "gfd") != "gfd":
                raise BrokerError("Paper market orders require gfd")
            limit = None
            stop = None
        else:
            if (side, effect) != ("sell", "close") or order.get("limit_price") is not None:
                raise BrokerError("Paper stop_market orders only support sell-to-close without a limit")
            if order.get("time_in_force") != "gtc":
                raise BrokerError("Paper stop_market orders require gtc")
            stop = _decimal(order.get("stop_price"), "stop price")
            if stop <= 0:
                raise BrokerError("Paper stop price must be positive")
            limit = None
        order_body = {
            "contract": _contract_key(order.get("contract")),
            "side": side,
            "position_effect": effect,
            "quantity": quantity,
            "order_type": order_type,
            "limit_price": limit,
            "stop_price": stop,
            "time_in_force": order.get("time_in_force", "gfd"),
        }
        if order_id in self.orders:
            if order_body != self.order_bodies[order_id]:
                raise BrokerError("Paper client_order_id was reused with different order details")
            return dict(self.orders[order_id])
        position = next((position for position in self.positions if _contract_key(position["contract"]) == order_body["contract"]), None)
        if side == "sell":
            reserved = sum(
                self.order_bodies[existing_id]["quantity"] - self.orders[existing_id]["filled_quantity"]
                for existing_id in self.orders
                if self.order_bodies[existing_id]["contract"] == order_body["contract"]
                and self.order_bodies[existing_id]["side"] == "sell"
                and self.order_bodies[existing_id]["position_effect"] == "close"
                and self.orders[existing_id]["status"] in {"open", "partially_filled"}
            )
            if position is None or quantity + reserved > position["quantity"]:
                raise BrokerError("Paper sell quantity exceeds simulated holdings")
        quote = await self.quote(order["contract"])
        if self.config.get("market_open") is not True or not quote["tradable"]:
            raise BrokerError("Paper market closed or contract untradable")
        if order_type == "stop_market":
            # ponytail: resting paper stops never self-trigger; add an execution simulator only when paper fills need realism.
            result = {
                "id": order_id,
                "status": "open",
                "filled_quantity": 0,
                "fill_price": None,
                "order_type": "stop_market",
                "stop_price": str(stop),
                "time_in_force": "gtc",
            }
            self.orders[order_id] = result
            self.order_bodies[order_id] = order_body
            return dict(result)
        price = _decimal(quote["ask" if side == "buy" else "bid"], "fill price")
        crossing = order_type == "market" or (limit >= price if side == "buy" else limit <= price)
        cost = price * quantity * 100
        fees = self.fee_reserve * quantity
        if crossing and side == "buy" and cost + fees > self.buying_power:
            raise BrokerError("Insufficient simulated buying power")
        # ponytail: immediate all-or-none fixture fills; add an exchange simulator only for execution research.
        result = {"id": order_id, "status": "filled" if crossing else "open", "filled_quantity": quantity if crossing else 0, "fill_price": str(price) if crossing else None}
        if crossing:
            if side == "buy":
                self.restore_positions([*self.positions, {"contract": order["contract"], "quantity": quantity, "average_price": str(price)}])
            else:
                position["quantity"] -= quantity
                if not position["quantity"]:
                    self.positions.remove(position)
            # Configured simulation reserve, not Robinhood's actual commission fee schedule.
            self.buying_power += (cost if side == "sell" else -cost) - fees
        self.orders[order_id] = result
        self.order_bodies[order_id] = order_body
        return dict(result)

    async def order_status(self, order_id, *, expected_order=None):
        if order_id not in self.orders:
            raise BrokerError("Unknown paper order; reconcile durable application ledger")
        if expected_order is not None:
            body = self.order_bodies[order_id]
            if _order_kind(expected_order) != body["order_type"] or _contracts(expected_order.get("quantity")) != body["quantity"]:
                raise BrokerError("Paper order does not match the persisted identity")
            expected_contract = expected_order.get("contract")
            if expected_contract is not None and _contract_key(expected_contract) != body["contract"]:
                raise BrokerError("Paper order contract does not match the persisted identity")
            if body["order_type"] == "stop_market" and _decimal(expected_order.get("stop_price"), "stop price") != body["stop_price"]:
                raise BrokerError("Paper stop price does not match the persisted identity")
        return dict(self.orders[order_id])

    async def cancel_order(self, order_id, *, broker_order_id=None, expected_order=None):
        target = broker_order_id or order_id
        if target != order_id or target not in self.orders:
            raise BrokerError("Unknown paper order; cancellation requires a persisted order")
        result = await self.order_status(target, expected_order=expected_order)
        if result["status"] in {"filled", "canceled", "rejected"}:
            return result
        result["status"] = "canceled"
        self.orders[target] = result
        return dict(result)


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
        self._schema_incompatible = False
        self._schema_incompatible_tools = ()
        self.server = None
        self.callback = None
        self.oauth_state = None
        self.redirect_uri = os.environ.get(
            "RELAY_ROBINHOOD_REDIRECT_URI", "http://127.0.0.1:8766/callback"
        )
        try:
            redirect = urlsplit(self.redirect_uri)
            if (redirect.scheme not in {"http", "https"} or not redirect.hostname
                    or redirect.username is not None or redirect.password is not None
                    or redirect.path != "/callback" or any(char in self.redirect_uri for char in "?#\\")
                    or redirect.netloc.endswith(":")
                    or any(char.isspace() or ord(char) < 32 or ord(char) == 127 for char in self.redirect_uri)
                    or redirect.port == 0):
                raise ValueError
        except ValueError:
            raise BrokerError("RELAY_ROBINHOOD_REDIRECT_URI must be an HTTP(S) URL ending in /callback, without credentials, query or fragment") from None

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
                error = BrokerError("Robinhood access token environment variable is not set")
                self._connection_failed(error)
                raise error
            headers["Authorization"] = f"Bearer {token}"
        elif auth_mode == "oauth":
            from mcp.client.auth import OAuthClientProvider
            from mcp.shared.auth import OAuthClientMetadata
            auth = OAuthClientProvider(
                server_url=ROBINHOOD_ENDPOINT,
                client_metadata=OAuthClientMetadata(
                    client_name="Discord Options Relay",
                    redirect_uris=[self.redirect_uri],
                    grant_types=["authorization_code", "refresh_token"],
                    response_types=["code"], token_endpoint_auth_method="none", scope="internal",
                ),
                storage=_OAuthStorage(self.config.get("token_store", "state/robinhood-oauth.json")),
                redirect_handler=self._redirect, callback_handler=self._callback,
            )
        else:
            error = BrokerError("robinhood.auth must be oauth or token")
            self._connection_failed(error)
            raise error
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
            try:
                await self.__aexit__(None, None, None)
            except BaseException as cleanup_error:
                # MCP may surface the useful HTTP/auth cause only while its
                # task group is unwinding.  Publish it, but preserve the
                # cleanup failure so the worker can retain its safe leaf diagnostic.
                self._connection_failed(cleanup_error)
                raise
            raise

    async def __aexit__(self, exc_type, exc, tb):
        if self.server:
            self.server.close()
            await self.server.wait_closed()
            self.server = None
        self.session = None
        await self.stack.aclose()

    @staticmethod
    def _schema_digest(tool):
        schemas = {key: tool.get(key) for key in ("inputSchema", "outputSchema")}
        return hashlib.sha256(json.dumps(schemas, sort_keys=True, separators=(",", ":")).encode()).hexdigest()

    def _persist_schema_cache(self, tools):
        temporary = None
        try:
            path = Path(self.config.get("token_store", "state/robinhood-oauth.json")).expanduser().with_name("robinhood-schemas.json")
            schemas = [
                {key: tool.get(key) for key in ("name", "inputSchema", "outputSchema")}
                for tool in tools if isinstance(tool, dict) and tool.get("name") in SCHEMA_PINS
            ]
            path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                os.fchmod(handle.fileno(), 0o600)
                json.dump({"observed_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"), "tools": schemas}, handle)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
            temporary = None
        except Exception:
            pass  # Cache failures must not affect broker operations.
        finally:
            if temporary is not None:
                try:
                    os.unlink(temporary)
                except OSError:
                    pass

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
        self._persist_schema_cache(tools)
        incompatible = [
            name for name, accepted in SCHEMA_PINS.items()
            if not self.catalog.get(name) or self._schema_digest(self.catalog[name]) not in accepted
        ]
        self._schema_incompatible_tools = tuple(incompatible)
        self._schema_incompatible = bool(self._schema_incompatible_tools)
        if incompatible:
            publish_status(self.on_status, "broker", "schema_incompatible", detail="Unaccepted pinned tools: " + ", ".join(incompatible))
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
            error = BrokerError("Arguments do not satisfy the authenticated tool schema")
            annotate_failure(error, tool=name)
            raise error from None
        result = await self._call_tool(name, args)
        if result.isError:
            error = BrokerError("Robinhood returned a tool error; no result has been accepted")
            annotate_failure(error, tool=name)
            raise error
        return result.model_dump(mode="json", exclude_none=True)

    async def _call_tool(self, name, args):
        try:
            result = await self.session.call_tool(name, arguments=args)
        except Exception as exc:
            annotate_failure(exc, tool=name)
            self._connection_failed(exc)
            raise
        if result.isError:
            state = "auth_required" if is_auth_required(result) else "unavailable"
        else:
            state = "connected"
        detail = None
        if result.isError:
            detail = ("Robinhood rejected the operation because authorization needs attention [auth_required]. Use Robinhood sign-in and verify account access."
                      if state == "auth_required" else "Robinhood reported an operation error [tool_error]. Check the transaction's recorded reason and account permissions before retrying.")
        if state == "connected" and self._schema_incompatible:
            state = "schema_incompatible"
            detail = "Unaccepted pinned tools: " + ", ".join(self._schema_incompatible_tools)
        publish_status(self.on_status, "broker", state, detail=detail)
        return result

    def _connection_failed(self, exc):
        auth_required = is_auth_required(exc)
        detail = ("Robinhood authorization failed or expired [auth_required]. Use Robinhood sign-in to renew access to the bound account."
                  if auth_required else failure_detail(exc, provider="Robinhood", phase="connection"))
        publish_status(self.on_status, "broker", "auth_required" if auth_required else "unavailable", detail=detail)


async def login(config, *, authorization_handler=None):
    """Interactive OAuth followed only by authenticated schema discovery."""
    async with RobinhoodMCP(config, interactive=True, authorization_handler=authorization_handler) as broker:
        return await broker.discover()


def broker_credentials_state(config):
    """Return a local, credential-free Robinhood readiness state.

    This is deliberately only a local preflight.  It cannot prove that an access
    token is still accepted by Robinhood; a real broker connection must publish
    ``auth_required`` when the provider rejects an expired or revoked token.
    """

    root = config if isinstance(config, dict) else {}
    section = root.get("robinhood", root)
    if not isinstance(section, dict):
        return "unavailable"
    if root.get("mode") == "paper":
        return "paper"
    account = section.get("account_number")
    if (not isinstance(account, str) or not account.isascii() or not account.isdigit()
            or not 5 <= len(account) <= 20):
        return "auth_required"
    auth_mode = section.get("auth", "oauth")
    if auth_mode == "token":
        env_name = section.get("access_token_env", "ROBINHOOD_ACCESS_TOKEN")
        return "configured" if isinstance(env_name, str) and bool(os.environ.get(env_name)) else "auth_required"
    if auth_mode != "oauth":
        return "unavailable"
    try:
        path = Path(section.get("token_store", "state/robinhood-oauth.json")).expanduser()
        if not path.is_file() or path.is_symlink():
            return "auth_required"
        if stat.S_IMODE(path.stat().st_mode) & 0o077:
            return "unavailable"
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        return "unavailable"
    tokens = value.get("tokens") if isinstance(value, dict) else None
    access_token = tokens.get("access_token") if isinstance(tokens, dict) else None
    return "configured" if isinstance(access_token, str) and bool(access_token.strip()) else "auth_required"


# Authenticated official schemas observed 2026-09-06; changes require renewed qualification.
SCHEMA_PINS = {
    "search": {"577c2e161dec698d9efdb2d203a42d99798057035a277cbbb349c26477e7b28e"},
    # Reviewed 2026-09-09: guide text only; 2026-09-23: caller permissions and portfolio text.
    "get_accounts": {"3df90562b040c920c73ba68b1685508a9db806266b0e98903bef0b9b04c83042",
                     "4ae8734970c9dd300d627ea117b185fb5744e1af16970d6f79267e55acf97603",
                     "6502644731a3f3d9002eae39e3efdad4848ec1d77ed36f275e5c4b88fe5bd0b1"},
    "get_portfolio": {"b1d5f51ec0e84c8a62181dee3daa7a5d2ab93c7ece8d0c8373d482715455a2f5",
                      "a0b873691e9b5e7f8843f94f02073e56b9958f59b540fd4efac5cc3347af960a",
                      "2cf447c73a813f2a1119d0a9ffddc9cb98595df62d378842e138dd45b6994ecc"},
    "get_equity_quotes": {"6a64ff3f6ae5e6e3a536177b74e58e65ed538a771ff7c0cd9f23b472707d567f"},
    "get_option_chains": {"661824e1e339fdc16e61a5192a37ebb664de935fc493887f9825e16fbcc119ba"},
    "get_option_instruments": {"e27cf1cb98aeecf5940b23c6ff02dada0f07f5e90866d77e63a843b983f8d503"},
    "get_option_quotes": {"ac069476d02f1b401fc9f2f1a65d402a5d7cb352df2f4b06cff87307fb846508"},
    "get_option_positions": {"f9ee54d7cee627f491189d66330d1662954d6ef7b9ae889e90a27e8dea11cabd"},
    "get_option_orders": {"3d1a33c36ac93d9e3dd202f10b7597bf74fb91d491aa00c0b41ed460d7d51277"},
    "review_option_order": {"a3e359eb4e73e46d77f8fc9a3ab90ba4d88f0d36b96d58225fe8fbdde69b4dc0"},
    "place_option_order": {"2b6e3ecd2997e8a58d36b5b77c8a4883b551255d05d39511995a4245d7f37cb3"},
    "cancel_option_order": {"b6b90ce0d295d72292c699ee6336a8ac9e0f13ed29e8a91acc46ec788c64d104"},
}


def _caller_option_restriction(account):
    """Trust callers need their own approval at least equal to the account level."""
    if "user_option_level" not in account:
        if account.get("brokerage_account_type") == "trust_revocable":
            return "caller options approval is missing for trust account"
        return None
    levels = {"": 0, "option_level_0": 0, "option_level_2": 2, "option_level_3": 3}
    caller = account.get("user_option_level")
    approved = account.get("option_level")
    if caller not in {"option_level_2", "option_level_3"} or approved not in levels:
        return "caller options approval is unrecognized or unavailable"
    if levels[caller] < levels[approved]:
        return "caller is view-only for options on this account"
    return None


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


def _order_kind(order):
    if not isinstance(order, dict):
        return None
    requested = order.get("order_type")
    if requested in {"limit", "market", "stop_market"}:
        return requested
    order_type = order.get("type")
    trigger = order.get("trigger")
    if order_type == "limit" and trigger in (None, "immediate"):
        return "limit"
    if order_type == "market" and (trigger == "stop" or order.get("stop_price") is not None):
        return "stop_market"
    if order_type == "market" and trigger in (None, "immediate"):
        return "market"
    return None


def _request_order_kind(order):
    """Return the user-facing kind represented by a provider request body."""
    if not isinstance(order, dict):
        return None
    requested = order.get("order_type")
    if requested in {"limit", "market", "stop_market"}:
        return requested
    request_type = order.get("type")
    if request_type in {"limit", "market", "stop_market"}:
        return request_type
    return _order_kind(order)


def _order_ref_id(account_number, client_order_id):
    return str(
        uuid.uuid5(
            uuid.NAMESPACE_URL,
            "discord-options-relay:" + account_number + ":" + client_order_id,
        )
    )


class RobinhoodBroker(RobinhoodMCP):
    """Normalized Agentic account reads and explicitly enabled single-leg limit orders."""

    def _init_execution_scope(self):
        self._execution_scope_var = contextvars.ContextVar(
            f"robinhood_execution_reads_{id(self)}", default=None
        )

    def _active_execution_scope(self):
        scope = self._execution_scope_var.get()
        return scope if scope is not None and scope.active else None

    def _invalidate_execution_scope(self):
        scope = self._execution_scope_var.get()
        if scope is not None:
            scope.deactivate()

    @contextmanager
    def execution_reads(self):
        current = self._execution_scope_var.get()
        if current is not None and current.active:
            try:
                yield current
            except BaseException:
                current.deactivate()
                raise
            return

        scope = _ExecutionReadScope()
        token = self._execution_scope_var.set(scope)
        try:
            yield scope
        except BaseException:
            scope.deactivate()
            raise
        finally:
            scope.deactivate()
            self._execution_scope_var.reset(token)

    def _remember_execution_handoff(self, contract, chain, instrument):
        scope = self._active_execution_scope()
        if scope is not None:
            scope.handoffs[_contract_key(contract)] = (
                time.monotonic(), copy.deepcopy(chain), copy.deepcopy(instrument)
            )

    def _take_execution_handoff(self, target):
        scope = self._active_execution_scope()
        if scope is None:
            return None
        if self.account_changed.is_set():
            scope.handoffs.pop(target, None)
            return None
        entry = scope.handoffs.pop(target, None)
        if entry is None:
            return None
        created_at, chain, instrument = entry
        if time.monotonic() - created_at > _EXECUTION_READ_REUSE_SECONDS:
            return None
        return chain, instrument

    def _remember_execution_snapshot(self, snapshot):
        scope = self._active_execution_scope()
        if scope is not None:
            scope.snapshot = (time.monotonic(), copy.deepcopy(snapshot))

    def _execution_snapshot(self):
        scope = self._active_execution_scope()
        if scope is None or scope.snapshot is None:
            return None
        completed_at, snapshot = scope.snapshot
        if self.account_changed.is_set() or time.monotonic() - completed_at > _EXECUTION_READ_REUSE_SECONDS:
            scope.snapshot = None
            return None
        try:
            self._check_quote_age(snapshot["timestamp"], "Account snapshot")
            for position in snapshot.get("positions", []):
                self._check_quote_age(position["quote_timestamp"])
        except (BrokerError, KeyError, TypeError):
            scope.snapshot = None
            return None
        return copy.deepcopy(snapshot)

    def _remember_execution_quote(self, target, quote):
        scope = self._active_execution_scope()
        if scope is None:
            return
        try:
            self._fresh_quote(quote)
        except (BrokerError, KeyError, TypeError):
            return
        scope.quotes[target] = (time.monotonic(), copy.deepcopy(quote))

    def _execution_quote(self, target):
        scope = self._active_execution_scope()
        if scope is None:
            return None
        entry = scope.quotes.get(target)
        if entry is None:
            return None
        completed_at, quote = entry
        if (self.account_changed.is_set()
                or target not in self.contracts
                or time.monotonic() - completed_at > _EXECUTION_READ_REUSE_SECONDS):
            scope.quotes.pop(target, None)
            return None
        try:
            if _contract_key(quote["contract"]) != target:
                raise BrokerError("Execution quote contract changed")
            self._fresh_quote(quote)
        except (BrokerError, KeyError, TypeError):
            scope.quotes.pop(target, None)
            return None
        return copy.deepcopy(quote)

    async def _bounded_reads(self, reader, values):
        semaphore = asyncio.Semaphore(_BROKER_READ_CONCURRENCY)
        tasks = []

        async def read(value):
            async with semaphore:
                return await reader(value)

        try:
            for value in values:
                tasks.append(asyncio.create_task(read(value)))
            return await asyncio.gather(*tasks)
        except BaseException:
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            raise

    async def _snapshot_position_data(self, positions):
        option_ids = []
        seen = set()
        for position in positions:
            quantity = _contracts(position["quantity"])
            if not quantity:
                continue
            if position["type"] != "long" or _decimal(position["trade_value_multiplier"], "multiplier") != 100:
                raise BrokerError("Short or adjusted option positions require separate risk qualification")
            option_id = position["option_id"]
            if option_id not in seen:
                seen.add(option_id)
                option_ids.append(option_id)

        if not option_ids:
            return {}

        async def read(item):
            reader, option_id = item
            return await reader(option_id)

        results = await self._bounded_reads(read, [
            (reader, option_id) for option_id in option_ids
            for reader in (self._instrument, self._raw_quote)
        ])
        instruments = dict(zip(option_ids, results[0::2]))
        contracts = {option_id: self._instrument_contract(instrument) for option_id, instrument in instruments.items()}
        quotes = dict(zip(option_ids, results[1::2]))
        return {option_id: (contracts[option_id], quotes[option_id]) for option_id in option_ids}

    def __init__(self, config, *, interactive=False, clock=None, on_status=None):
        super().__init__(config, interactive=interactive, on_status=on_status)
        self._init_execution_scope()
        self.runtime = config
        self.account_number = self.config.get("account_number")
        if not isinstance(self.account_number, str) or not self.account_number.strip():
            raise BrokerError("Bind robinhood.account_number to the selected Agentic account")
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.currency = None
        self.instruments = {}
        self.contracts = {}
        self.order_bodies = {}
        self.order_inputs = {}
        self.order_results = {}
        self._prepared_entries = {}
        self.attempted = set()
        self.account_changed = asyncio.Event()
        self._portfolio_task = None
        self._portfolio_waiters = 0

    async def __aenter__(self):
        self._invalidate_execution_scope()
        self.contracts.clear()
        self.instruments.clear()
        self.currency = None
        self._portfolio_task = None
        self._portfolio_waiters = 0
        await asyncio.to_thread(regular_session, self.clock())
        return await super().__aenter__()

    def _qualified(self, name):
        tool = self.catalog.get(name)
        if not tool or name not in SCHEMA_PINS:
            error = BrokerError(f"Required broker tool {name} missing from qualified catalog")
            annotate_failure(error, tool=name, code="schema_incompatible")
            raise error
        if self._schema_digest(tool) not in SCHEMA_PINS[name]:
            error = BrokerError(f"schema changed for {name}; qualify before continuing")
            annotate_failure(error, tool=name, code="schema_incompatible")
            raise error
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
        now = self.clock().astimezone(timezone.utc)
        session = regular_session(now)
        return bool(session and session[0] <= now < session[1])

    async def _fetch_portfolio(self):
        data = await self._data("get_portfolio", {"account_number": self.account_number})
        power = data.get("buying_power")
        if not isinstance(power, dict) or data.get("currency") != "USD" or power.get("display_currency") != "USD":
            raise BrokerError("Authoritative USD account equity and buying power are required")
        self.currency = data["currency"]
        return data, min(_decimal(power["buying_power"], "buying power"),
                         _decimal(power["unleveraged_buying_power"], "unleveraged buying power"))

    async def _portfolio(self):
        task = self._portfolio_task
        if task is None or task.done():
            task = asyncio.create_task(self._fetch_portfolio())
            self._portfolio_task = task

            def clear(completed):
                if self._portfolio_task is completed:
                    self._portfolio_task = None

            task.add_done_callback(clear)
        self._portfolio_waiters += 1
        try:
            return await asyncio.shield(task)
        finally:
            self._portfolio_waiters -= 1
            if self._portfolio_waiters == 0 and not task.done():
                if self._portfolio_task is task:
                    self._portfolio_task = None
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)

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

    async def underlying_quote(self, symbol):
        """Return one fresh-session equity trade for an exact underlying symbol."""
        if not isinstance(symbol, str) or not symbol.strip():
            raise BrokerError("Underlying quote requires a symbol")
        symbol = symbol.strip().upper()
        now = self.clock().astimezone(timezone.utc)
        session = regular_session(now)
        if session is None or not session[0] <= now < session[1]:
            raise BrokerError("Underlying quote requires an open regular session")
        data = await self._data("get_equity_quotes", {"symbols": [symbol]})
        results = data.get("results") if isinstance(data, dict) else None
        if not isinstance(results, list):
            raise BrokerError("Underlying quote results are missing")
        matches = []
        for result in results:
            quote = result.get("quote") if isinstance(result, dict) else None
            if isinstance(quote, dict) and quote.get("symbol") == symbol:
                matches.append(quote)
        if len(matches) != 1:
            raise BrokerError("Underlying quote is missing or ambiguous")
        quote = matches[0]
        if quote.get("state") != "active" or quote.get("has_traded") is not True:
            raise BrokerError("Underlying quote is inactive or has not traded")
        price = _decimal(quote.get("last_trade_price"), "underlying price")
        timestamp = _instant(quote.get("venue_last_trade_time")).astimezone(timezone.utc)
        if price <= 0 or not session[0] <= timestamp < session[1] or timestamp > now:
            raise BrokerError("Underlying quote is not a current regular-session trade")
        self._check_quote_age(timestamp.isoformat(), "Underlying quote")
        return {"symbol": symbol, "price": str(price), "timestamp": timestamp.isoformat()}

    async def account_overview(self):
        """Return display-only account balances and option positions.

        This deliberately does not use the trading snapshot: closed markets,
        stale quotes, negative balances, and short or adjusted positions are
        valid account display states.
        """
        portfolio, positions = await asyncio.gather(
            self._data("get_portfolio", {"account_number": self.account_number}),
            self._pages(
                "get_option_positions",
                {"account_number": self.account_number, "nonzero": True},
                "positions",
            ),
        )
        if portfolio.get("currency") != "USD":
            raise BrokerError("Robinhood account overview requires USD currency")

        def display_decimal(value, field):
            try:
                result = Decimal(str(value))
            except (InvalidOperation, TypeError, ValueError):
                raise BrokerError(f"Invalid {field}") from None
            if not result.is_finite():
                raise BrokerError(f"Invalid {field}")
            return result

        def display_string(value, field):
            return format(display_decimal(value, field), "f")

        power = portfolio.get("buying_power")
        buying_power = unleveraged_buying_power = None
        if power is not None:
            if not isinstance(power, dict) or power.get("display_currency") != "USD":
                raise BrokerError("Robinhood account overview requires USD buying power")
            buying_power = display_string(power.get("buying_power"), "buying power")
            unleveraged_buying_power = display_string(
                power.get("unleveraged_buying_power"), "unleveraged buying power"
            )

        asset_names = (
            "equity_value",
            "options_value",
            "futures_value",
            "event_contracts_value",
            "crypto_value",
            "mutual_funds_value",
            "fixed_income_value",
        )
        asset_values = {
            name: display_string(portfolio.get(name), name) for name in asset_names
        }

        normalized = []
        for position in positions:
            quantity = display_decimal(position.get("quantity"), "position quantity")
            if quantity < 0 or quantity != quantity.to_integral_value():
                raise BrokerError("Invalid option position quantity")
            if not quantity:
                continue
            position_type = position.get("type")
            if position_type not in {"long", "short"}:
                raise BrokerError("Invalid option position type")
            multiplier = display_decimal(
                position.get("trade_value_multiplier"), "position multiplier"
            )
            if multiplier <= 0:
                raise BrokerError("Invalid position multiplier")

            instrument = await self._instrument(position["option_id"])
            contract = {
                "symbol": instrument.get("chain_symbol") or position.get("chain_symbol"),
                "expiry": instrument.get("expiration_date"),
                "strike": instrument.get("strike_price"),
                "option_type": instrument.get("type"),
            }
            _contract_key(contract)

            average_price = display_decimal(
                position.get("average_price"), "position average price"
            ) / multiplier
            market_value = None
            quote_timestamp = None
            try:
                quote = await self._raw_quote(position["option_id"])
            except Exception as exc:
                if is_auth_required(exc):
                    raise
            else:
                quote_timestamp = quote.get("updated_at")
                mark_value = quote.get("mark_price")
                if mark_value is None:
                    mark_value = quote.get("adjusted_mark_price")
                if mark_value is None:
                    try:
                        bid = display_decimal(quote.get("bid_price"), "option bid")
                        ask = display_decimal(quote.get("ask_price"), "option ask")
                        if bid >= 0 and ask >= 0 and (bid or ask):
                            mark_value = (bid + ask) / Decimal(2)
                    except BrokerError:
                        mark_value = None
                if mark_value is not None:
                    try:
                        mark = display_decimal(mark_value, "option mark")
                        if mark < 0:
                            raise BrokerError("Invalid option mark")
                        value = mark * multiplier * quantity
                        if position_type == "short":
                            value = -value
                        market_value = format(value, "f")
                    except BrokerError:
                        market_value = None

            normalized.append(
                {
                    "contract": contract,
                    "quantity": format(quantity, "f"),
                    "average_price": format(average_price, "f"),
                    "market_value": market_value,
                    "position_type": position_type,
                    "multiplier": format(multiplier, "f"),
                    "quote_timestamp": quote_timestamp,
                }
            )

        return {
            "currency": "USD",
            "equity": display_string(portfolio.get("total_value"), "total equity"),
            "cash": display_string(portfolio.get("cash"), "cash"),
            "buying_power": buying_power,
            "unleveraged_buying_power": unleveraged_buying_power,
            "asset_values": asset_values,
            "positions": normalized,
            "scope": "option_positions",
        }

    @_scope_operation
    async def snapshot(self):
        cached = self._execution_snapshot()
        if cached is not None:
            return cached
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
        position_data = await self._snapshot_position_data(positions)
        for position in positions:
            quantity = _contracts(position["quantity"])
            if not quantity:
                continue
            if position["type"] != "long" or _decimal(position["trade_value_multiplier"], "multiplier") != 100:
                raise BrokerError("Short or adjusted option positions require separate risk qualification")
            contract, quote = position_data[position["option_id"]]
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
            if (
                len(legs) != 1
                or legs[0]["ratio_quantity"] != 1
                or not (
                    (order["type"] == "limit" and order["trigger"] == "immediate")
                    or (
                        order["type"] == "market"
                        and order["trigger"] in {"immediate", "stop"}
                        and legs[0]["side"] == "sell"
                        and legs[0]["position_effect"] == "close"
                        and (
                            (order["trigger"] == "immediate" and order["time_in_force"] == "gfd" and order.get("stop_price") is None)
                            or (order["trigger"] == "stop" and order["time_in_force"] == "gtc" and order.get("stop_price") is not None)
                        )
                    )
                )
            ):
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
        caller_restriction = _caller_option_restriction(account)
        if caller_restriction:
            restrictions.append(caller_restriction)
        for position in normalized:
            self._check_quote_age(position["quote_timestamp"])
        result = {"account_id": self.account_number, "equity": str(_decimal(portfolio["total_value"], "total equity")),
                "buying_power": str(power), "reported_buying_power": portfolio["buying_power"]["buying_power"],
                "currency": self.currency, "positions": normalized,
                "option_exposure_by_symbol": {symbol: str(value) for symbol, value in exposure.items()},
                "working_order_ids": working, "market_open": self._market_open(),
                "market_open_source": "exchange_calendars:XNYS regular session",
                "timestamp": observed_at.isoformat(), "timestamp_source": "local_fetch_started",
                "agentic_allowed": account.get("agentic_allowed") is True, "option_level": account.get("option_level"),
                "account_type": account["type"], "account_state": account["state"], "restrictions": restrictions,
                "simulated": False, "sandbox_status": "not_reported", "source": "Robinhood Agentic MCP"}
        self._remember_execution_snapshot(result)
        return result

    @_scope_operation
    async def nearest_expiry(self, contract):
        """Choose the first listed standard contract at the requested strike/type."""
        target = _contract_key(contract)
        requested_day = datetime.strptime(target[1], "%Y-%m-%d").date()
        # One bounded near-term query removes the chain dependency for daily/weekly entries.
        # Later dates still use individual exact queries, with complete pagination.
        prefetched_dates = [(requested_day + timedelta(days=day)).isoformat() for day in range(7)]
        chains, instruments = await self._bounded_reads(lambda query: self._pages(*query), [
            ("get_option_chains", {"underlying_symbol": target[0]}, "chains"),
            ("get_option_instruments", {
                "chain_symbol": target[0], "expiration_dates": ",".join(prefetched_dates),
                "strike_price": format(Decimal(target[2]), "f"), "type": target[3],
                "state": "active", "tradability": "tradable",
            }, "instruments"),
        ])

        eligible_chains = [
            chain for chain in chains
            if chain["symbol"] == target[0]
            and _decimal(chain["trade_value_multiplier"], "chain multiplier") == 100
            and len(chain["underlying_instruments"] or []) == 1
            and (chain["cash_component"] is None or _decimal(chain["cash_component"], "cash component") == 0)
        ]

        eligible_ids = {chain["id"] for chain in eligible_chains}
        expiries = sorted({
            expiry for chain in eligible_chains
            for expiry in (chain["expiration_dates"] or [])
            if expiry >= target[1]
        } | {row["expiration_date"] for row in instruments
             if row["chain_id"] in eligible_ids and row["expiration_date"] in prefetched_dates})
        for expiry in expiries:
            rows = instruments if expiry in prefetched_dates else await self._pages(
                    "get_option_instruments",
                    {
                        "chain_symbol": target[0],
                        "expiration_dates": expiry,
                        "strike_price": format(Decimal(target[2]), "f"),
                        "type": target[3],
                        "state": "active",
                        "tradability": "tradable",
                    },
                    "instruments",
                )

            candidates = []
            for chain in eligible_chains:
                for instrument in rows:
                    if (instrument["chain_id"] != chain["id"]
                            or instrument.get("state") != "active"
                            or instrument.get("tradability") != "tradable"
                            or instrument["underlying_type"] != "equity"
                            or _decimal(instrument["trade_value_multiplier"], "multiplier") != 100):
                        continue
                    key = _contract_key(self._instrument_contract(instrument))
                    if key[0] == target[0] and key[1] == expiry and key[2:] == target[2:]:
                        if expiry not in (chain["expiration_dates"] or []):
                            raise BrokerError("Broker option expiry metadata is inconsistent; refusing to skip an available date")
                        candidates.append((chain, instrument))
            if len(candidates) > 1:
                raise BrokerError("Option expiration is ambiguous across chains")
            if candidates:
                resolved = dict(contract, expiry=expiry)
                self._remember_execution_handoff(resolved, *candidates[0])
                return resolved
        raise BrokerError("No listed expiration for the requested option")

    @_scope_operation
    async def quote(self, contract):
        target = _contract_key(contract)
        cached_quote = self._execution_quote(target)
        if cached_quote is not None:
            return cached_quote
        handoff = self._take_execution_handoff(target)
        candidates = [handoff] if handoff is not None else []
        cached = None if handoff is not None else self.contracts.get(target)
        if cached is not None:
            # Cache identity only. Refresh execution authority and raw prices every time.
            reads = await asyncio.gather(
                self._pages("get_option_chains", {"underlying_symbol": target[0]}, "chains"),
                self._pages("get_option_instruments", {"ids": cached["option_id"]}, "instruments"),
                return_exceptions=True,
            )
            for result in reads:
                if isinstance(result, BaseException):
                    raise result
            chains, instruments = reads
            candidates = [
                (chain, instrument) for chain in chains for instrument in instruments
                if chain["id"] == cached["chain_id"] and chain["symbol"] == target[0]
                and target[1] in (chain["expiration_dates"] or [])
                and instrument["id"] == cached["option_id"]
                and instrument["chain_id"] == chain["id"]
                and _contract_key(self._instrument_contract(instrument)) == target
            ]
            if len(candidates) != 1:
                self.contracts.pop(target, None)
                candidates = []
        if not candidates:
            chains, instruments = await self._bounded_reads(lambda query: self._pages(*query), [
                ("get_option_chains", {"underlying_symbol": target[0]}, "chains"),
                ("get_option_instruments", {
                    "chain_symbol": target[0], "expiration_dates": target[1],
                    "strike_price": format(Decimal(target[2]), "f"), "type": target[3],
                    "state": "active", "tradability": "tradable",
                }, "instruments"),
            ])
            matching_chains = [
                chain for chain in chains
                if chain["symbol"] == target[0] and target[1] in (chain["expiration_dates"] or [])
            ]

            for chain in matching_chains:
                for instrument in instruments:
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
        self.contracts[target] = {
            "chain_id": chain["id"],
            "option_id": instrument["id"],
        }
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
        result = {"contract": self._instrument_contract(instrument), "option_id": instrument["id"], "chain_id": chain["id"],
                "bid": str(bid), "ask": str(ask), "tick_size": str(tick), "min_ticks": ticks,
                "bid_size": raw["bid_size"], "ask_size": raw["ask_size"], "mark": raw["mark_price"],
                "timestamp": raw["updated_at"], "currency": self.currency, "multiplier": 100,
                "tradable": instrument["state"] == "active" and instrument["tradability"] == "tradable" and ask > 0 and bid > 0 and raw["ask_size"] > 0 and raw["bid_size"] > 0,
                "can_open_position": chain["can_open_position"], "asset_type": "equity_option",
                "sellout_datetime": instrument.get("sellout_datetime"), "source": "Robinhood Agentic MCP"}
        self._remember_execution_quote(target, result)
        return result

    async def _resolve_prepared_entry(self, contract):
        """Resolve contract identity and trading metadata without reading a quote."""
        target = _contract_key(contract)
        queries = [
            (
                "get_option_chains",
                {"underlying_symbol": target[0]},
                "chains",
            ),
            (
                "get_option_instruments",
                {
                    "chain_symbol": target[0],
                    "expiration_dates": target[1],
                    "strike_price": format(Decimal(target[2]), "f"),
                    "type": target[3],
                    "state": "active",
                    "tradability": "tradable",
                },
                "instruments",
            ),
        ]
        chains, instruments = await self._bounded_reads(
            lambda query: self._pages(*query), queries
        )
        matching_chains = [
            chain
            for chain in chains
            if chain.get("symbol") == target[0]
            and target[1] in (chain.get("expiration_dates") or [])
        ]
        candidates = [
            (chain, instrument)
            for chain in matching_chains
            for instrument in instruments
            if instrument.get("chain_id") == chain.get("id")
            and _contract_key(self._instrument_contract(instrument)) == target
        ]
        if len(candidates) != 1:
            raise BrokerError("Exact option contract missing or ambiguous across chains")
        chain, instrument = candidates[0]

        underlyings = chain.get("underlying_instruments") or []
        if (
            chain.get("cash_component") is not None
            and _decimal(chain["cash_component"], "cash component") != 0
        ) or len(underlyings) != 1:
            raise BrokerError("Adjusted or non-equity chains unsupported")
        underlying = underlyings[0]
        if underlying.get("symbol") != target[0]:
            if underlying.get("symbol"):
                raise BrokerError("Underlying equity symbol conflicts with option chain")
            try:
                underlying_id = str(
                    uuid.UUID(urlsplit(underlying["instrument"]).path.rstrip("/").split("/")[-1])
                )
            except (ValueError, TypeError, KeyError):
                raise BrokerError("Underlying equity identity unavailable") from None
            search = await self._data(
                "search", {"query": target[0], "asset_type": "instrument", "limit": 20}
            )
            matches = [
                row
                for row in search.get("results") or []
                if row.get("symbol") == target[0] and row.get("instrument_id") == underlying_id
            ]
            if len(matches) != 1:
                raise BrokerError("Official equity search could not verify chain's underlying instrument")

        if _decimal(chain.get("trade_value_multiplier"), "chain multiplier") != 100:
            raise BrokerError("Nonstandard chain multiplier")
        if instrument.get("state") != "active" or instrument.get("tradability") != "tradable":
            raise BrokerError("Option contract is inactive or untradable")
        ticks = instrument.get("min_ticks") or chain.get("min_ticks")
        if not isinstance(ticks, dict):
            raise BrokerError("Option tick metadata is missing")
        for field in ("above_tick", "below_tick", "cutoff_price"):
            if _decimal(ticks.get(field), f"tick {field}") <= 0:
                raise BrokerError("Option tick metadata is invalid")
        if self.currency is None:
            await self._portfolio()
        if self.currency != "USD":
            raise BrokerError("Prepared option entries require a USD account")

        canonical = self._instrument_contract(instrument)
        metadata = {
            "contract": canonical,
            "option_id": instrument["id"],
            "chain_id": chain["id"],
            "min_ticks": copy.deepcopy(ticks),
            "tradable": True,
            "can_open_position": chain.get("can_open_position") is True,
            "multiplier": 100,
            "currency": "USD",
            "asset_type": "equity_option",
            "sellout_datetime": instrument.get("sellout_datetime"),
            "prepared_at": self.clock().astimezone(timezone.utc).isoformat(),
            "source": "Robinhood Agentic MCP",
        }
        self.instruments[instrument["id"]] = copy.deepcopy(instrument)
        self.contracts[target] = {"option_id": instrument["id"], "chain_id": chain["id"]}
        return metadata

    async def prewarm_entry(self, contract):
        """Cache bounded contract metadata for a short-lived prepared entry."""
        metadata = await self._resolve_prepared_entry(contract)
        target = _contract_key(metadata["contract"])
        self._prepared_entries[target] = (time.monotonic(), metadata)
        while len(self._prepared_entries) > _PREPARED_ENTRY_CACHE_LIMIT:
            oldest = min(self._prepared_entries, key=lambda key: self._prepared_entries[key][0])
            self._prepared_entries.pop(oldest, None)
        return copy.deepcopy(metadata)

    def prepared_entry(self, contract, price):
        """Return prepared entry metadata with the price-specific tick, if fresh."""
        try:
            target = _contract_key(contract)
            cached = self._prepared_entries.get(target)
            if cached is None:
                return None
            cached_at, metadata = cached
            prepared_at = _instant(metadata.get("prepared_at"))
            age = (self.clock().astimezone(timezone.utc) - prepared_at).total_seconds()
            if age < 0 or age > _PREPARED_ENTRY_TTL_SECONDS or (
                time.monotonic() - cached_at > _PREPARED_ENTRY_TTL_SECONDS
            ):
                self._prepared_entries.pop(target, None)
                return None
            if (
                _contract_key(metadata.get("contract")) != target
                or metadata.get("tradable") is not True
                or metadata.get("multiplier") != 100
                or metadata.get("currency") != "USD"
                or metadata.get("asset_type") != "equity_option"
            ):
                return None
            entry_price = _decimal(price, "entry price")
            if entry_price <= 0 or metadata.get("can_open_position") is not True:
                return None
            ticks = metadata.get("min_ticks")
            cutoff = _decimal(ticks.get("cutoff_price"), "tick cutoff")
            tick = _decimal(
                ticks["above_tick" if entry_price >= cutoff else "below_tick"],
                "tick",
            )
            if tick <= 0:
                return None
            prepared = copy.deepcopy(metadata)
            prepared["tick_size"] = format(tick, "f")
            return prepared
        except (BrokerError, AttributeError, KeyError, TypeError, ValueError):
            return None

    def _prepared_order_args(self, order):
        if (
            order.get("order_type", "limit") != "limit"
            or order.get("side") != "buy"
            or order.get("position_effect") != "open"
        ):
            raise BrokerError("Prepared entries only support buy-open limit orders")
        if order.get("time_in_force", "gfd") != "gfd" or order.get("stop_price") is not None:
            raise BrokerError("Prepared entry limit orders require gfd and no stop price")
        quantity = order.get("quantity")
        if type(quantity) is not int or quantity <= 0:
            raise BrokerError("Prepared entry quantity must be a positive whole number")
        metadata = self.prepared_entry(order.get("contract"), order.get("limit_price"))
        if metadata is None:
            raise BrokerError("Prepared entry metadata is absent or stale")
        if metadata["can_open_position"] is not True:
            raise BrokerError("This option chain is closed to new positions")
        if _decimal(order["limit_price"], "limit price") % _decimal(metadata["tick_size"], "tick"):
            raise BrokerError("Prepared entry price does not satisfy the option's tick rule")
        return {
            "account_number": self.account_number,
            "legs": [{
                "option_id": metadata["option_id"],
                "side": "buy",
                "position_effect": "open",
                "ratio_quantity": 1,
            }],
            "quantity": str(quantity),
            "type": "limit",
            "price": format(_decimal(order["limit_price"], "limit price"), "f"),
            "time_in_force": "gfd",
            "market_hours": "regular_hours",
        }, metadata

    def _review_option_quote(self, review, metadata):
        quotes = review.get("option_quotes") if isinstance(review, dict) else None
        if not isinstance(quotes, list) or len(quotes) != 1 or not isinstance(quotes[0], dict):
            raise BrokerError("Robinhood review did not supply one option quote")
        raw = quotes[0]
        if raw.get("instrument_id") != metadata["option_id"]:
            raise BrokerError("Robinhood review quote does not match the prepared option")
        try:
            bid = _decimal(raw.get("bid_price"), "review bid")
            ask = _decimal(raw.get("ask_price"), "review ask")
            bid_size = raw.get("bid_size")
            ask_size = raw.get("ask_size")
            if bid <= 0 or ask <= 0 or bid > ask:
                raise BrokerError("Robinhood review quote has invalid bid or ask")
            if type(bid_size) is not int or bid_size <= 0 or type(ask_size) is not int or ask_size <= 0:
                raise BrokerError("Robinhood review quote has invalid book sizes")
            timestamp = _instant(raw.get("updated_at"))
            max_spread = _decimal(
                self.runtime.get("risk", {}).get("max_spread_fraction", "0.15"),
                "spread limit",
            )
            if max_spread <= 0 or (ask - bid) / ask > max_spread:
                raise BrokerError("Robinhood review quote spread exceeds configured limit")
        except (InvalidOperation, TypeError, ValueError):
            raise BrokerError("Robinhood review quote is malformed") from None
        return {
            "contract": copy.deepcopy(metadata["contract"]),
            "option_id": metadata["option_id"],
            "chain_id": metadata["chain_id"],
            "bid": str(bid),
            "ask": str(ask),
            "tick_size": metadata["tick_size"],
            "min_ticks": copy.deepcopy(metadata["min_ticks"]),
            "bid_size": bid_size,
            "ask_size": ask_size,
            "mark": raw.get("adjusted_mark_price") or raw.get("mark_price"),
            "timestamp": timestamp.isoformat(),
            "currency": "USD",
            "multiplier": 100,
            "tradable": metadata["tradable"],
            "can_open_position": metadata["can_open_position"],
            "asset_type": "equity_option",
            "sellout_datetime": metadata.get("sellout_datetime"),
            "source": "Robinhood review_option_order",
        }

    def _live_enabled(self):
        if self.runtime.get("mode") != "live" or self.config.get("enable_live_orders") is not True:
            raise BrokerError("Live Robinhood orders require live mode and explicit enable_live_orders")
        stop = self.runtime.get("kill_switch")
        if stop and Path(stop).exists():
            raise BrokerError("Kill switch is present")

    def _check_quote_age(self, timestamp, label="Option quote"):
        age = (self.clock() - _instant(timestamp)).total_seconds()
        maximum = _decimal(self.runtime.get("risk", {}).get("max_quote_age_seconds", 15), "quote age limit")
        if maximum <= 0 or age < -2 or age > maximum:
            raise BrokerError(f"{label} is stale")

    def _fresh_quote(self, quote):
        self._check_quote_age(quote["timestamp"])
        if not quote["tradable"]:
            raise BrokerError("Option quote is stale or untradable")

    async def _order_args(self, order):
        quantity = order.get("quantity")
        side, effect = order.get("side"), order.get("position_effect")
        order_type = order.get("order_type", "limit")
        if type(quantity) is not int or quantity <= 0 or (side, effect) not in {("buy", "open"), ("sell", "close")}:
            raise BrokerError("Only positive whole-contract long opens and closes are supported")
        if order_type not in {"limit", "market", "stop_market"}:
            raise BrokerError("Only limit, market sell-close, and stop_market sell-close orders are supported")
        quote = await self.quote(order["contract"])
        self._fresh_quote(quote)
        ticks = quote["min_ticks"]
        cutoff = _decimal(ticks["cutoff_price"], "tick cutoff")
        if order_type == "limit":
            if order.get("time_in_force", "gfd") != "gfd" or order.get("stop_price") is not None:
                raise BrokerError("Limit orders require gfd and no stop price")
            price = _decimal(order.get("limit_price"), "limit price")
            tick = _decimal(ticks["above_tick" if price >= cutoff else "below_tick"], "tick")
            if price <= 0 or tick <= 0 or price % tick:
                raise BrokerError("Order price does not satisfy the option's tick rule")
            if side == "buy" and not quote["can_open_position"]:
                raise BrokerError("This option chain is closed to new positions")
            return {
                "account_number": self.account_number,
                "legs": [{"option_id": quote["option_id"], "side": side, "position_effect": effect, "ratio_quantity": 1}],
                "quantity": str(quantity),
                "type": "limit",
                "price": format(price, "f"),
                "time_in_force": "gfd",
                "market_hours": "regular_hours",
            }, quote
        if (side, effect) != ("sell", "close"):
            raise BrokerError("Market and stop_market orders only support sell-to-close")
        if order.get("limit_price") is not None:
            raise BrokerError("Market and stop_market orders omit limit_price")
        if order_type == "market":
            if order.get("stop_price") is not None or order.get("time_in_force", "gfd") != "gfd":
                raise BrokerError("Market sell-close orders require gfd and no stop price")
            return {
                "account_number": self.account_number,
                "legs": [{"option_id": quote["option_id"], "side": "sell", "position_effect": "close", "ratio_quantity": 1}],
                "quantity": str(quantity),
                "type": "market",
                "time_in_force": "gfd",
                "market_hours": "regular_hours",
            }, quote
        if order.get("time_in_force") != "gtc":
            raise BrokerError("stop_market orders require gtc")
        stop_price = _decimal(order.get("stop_price"), "stop price")
        tick = _decimal(ticks["above_tick" if stop_price >= cutoff else "below_tick"], "stop tick")
        if stop_price <= 0 or tick <= 0 or stop_price % tick:
            raise BrokerError("Stop price does not satisfy the option's tick rule")
        ask = _decimal(quote.get("ask"), "ask")
        if stop_price >= ask:
            raise BrokerError("Sell stop price must be below the current ask")
        return {
            "account_number": self.account_number,
            "legs": [{"option_id": quote["option_id"], "side": "sell", "position_effect": "close", "ratio_quantity": 1}],
            "quantity": str(quantity),
            "type": "stop_market",
            "stop_price": format(stop_price, "f"),
            "time_in_force": "gtc",
            "market_hours": "regular_hours",
        }, quote

    async def _review_args(self, args, quote, *, review=None):
        if review is None:
            review = await self._data(
                "review_option_order",
                dict(args, chain_symbol=quote["contract"]["symbol"], underlying_type="equity"),
            )
        if review.get("order_checks") != {}:
            raise BrokerError("Robinhood pre-trade review reported an alert; order was not placed")
        order_kind = args["type"]
        expected_trigger = "stop" if order_kind == "stop_market" else "immediate"
        for key in ("account_number", "time_in_force", "market_hours"):
            if review.get(key) != args[key]:
                raise BrokerError("Robinhood review does not match the intended order")
        if _order_kind(review) != order_kind:
            raise BrokerError("Robinhood review order type does not match the intended order")
        if "trigger" in review and review.get("trigger") != expected_trigger:
            raise BrokerError("Robinhood review trigger does not match the intended order")
        legs = [dict(leg, ratio_quantity=leg.get("ratio_quantity", 1)) for leg in review.get("legs") or []]
        if legs != args["legs"]:
            raise BrokerError("Robinhood review legs do not match the intended order")
        if (_contracts(review.get("quantity")) != _contracts(args["quantity"])
                or review.get("direction") != ("debit" if args["legs"][0]["side"] == "buy" else "credit")):
            raise BrokerError("Robinhood review quantity or direction does not match the intended order")
        if order_kind == "limit":
            if _decimal(review.get("price"), "review price") != _decimal(args["price"], "intended price"):
                raise BrokerError("Robinhood review price does not match the intended order")
            if review.get("stop_price") is not None:
                raise BrokerError("Robinhood review unexpectedly supplied a stop price")
        else:
            if review.get("price") is not None:
                raise BrokerError("Robinhood review unexpectedly supplied a limit price")
            if order_kind == "stop_market":
                if _decimal(review.get("stop_price"), "review stop price") != _decimal(args["stop_price"], "intended stop price"):
                    raise BrokerError("Robinhood review stop price does not match the intended order")
            elif review.get("stop_price") is not None:
                raise BrokerError("Robinhood review unexpectedly supplied a stop price")
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

    @_scope_operation
    async def review(self, order):
        self._live_enabled()
        args, quote = await self._order_args(order)
        review, _ = await self._review_args(args, quote)
        return review

    @_scope_operation
    async def submit(self, order, before_submit=None):
        self._live_enabled()
        client_id = order.get("client_order_id")
        if not isinstance(client_id, str) or not client_id:
            raise BrokerError("A persistent client_order_id is required")
        order_type = order.get("order_type", "limit")
        identity = {
            key: value for key, value in order.items()
            if key not in {"entry_evaluation", "limit_price", "stop_price", "time_in_force", "order_type"}
        }
        identity["contract"] = _contract_key(order.get("contract"))
        identity["order_type"] = order_type
        identity["time_in_force"] = order.get("time_in_force", "gtc" if order_type == "stop_market" else "gfd")
        if order_type == "limit":
            identity["limit_price"] = str(_decimal(order.get("limit_price"), "limit price").normalize())
        elif order_type == "stop_market":
            identity["stop_price"] = str(_decimal(order.get("stop_price"), "stop price").normalize())
        body = json.dumps(identity, sort_keys=True, separators=(",", ":"))
        if client_id in self.order_bodies and self.order_bodies[client_id] != body:
            raise BrokerError("client_order_id was reused with different order details")
        if client_id in self.order_results:
            return dict(self.order_results[client_id])
        if client_id in self.attempted:
            raise BrokerError("Prior submission outcome is unknown; reconcile without resubmitting")
        self.order_bodies[client_id] = body
        self.order_inputs[client_id] = copy.deepcopy(order)
        try:
            prepared_entry = order.get("prepared_entry") is True
            if prepared_entry:
                snapshot = await self.snapshot()
                args, metadata = self._prepared_order_args(order)
                review_payload = await self._data(
                    "review_option_order",
                    dict(
                        args,
                        chain_symbol=metadata["contract"]["symbol"],
                        underlying_type="equity",
                    ),
                )
                quote = self._review_option_quote(review_payload, metadata)
                review, fee = await self._review_args(
                    args,
                    metadata,
                    review=review_payload,
                )
            elif self.currency is None:
                snapshot = await self.snapshot()
                args, quote = await self._order_args(order)
            else:
                snapshot_task = asyncio.create_task(self.snapshot())
                order_args_task = asyncio.create_task(self._order_args(order))
                try:
                    snapshot, order_result = await asyncio.gather(snapshot_task, order_args_task)
                except BaseException:
                    snapshot_task.cancel()
                    order_args_task.cancel()
                    await asyncio.gather(snapshot_task, order_args_task, return_exceptions=True)
                    raise
                args, quote = order_result
            if snapshot["restrictions"] or not snapshot["market_open"]:
                raise BrokerError("Account restrictions or closed market prevent order placement")
            if order["side"] == "sell":
                available = sum(
                    position["available_quantity"]
                    for position in snapshot["positions"]
                    if position["option_id"] == quote["option_id"]
                )
                if order["quantity"] > available:
                    raise BrokerError("Order would exceed available long option inventory")
            if not prepared_entry:
                review, fee = await self._review_args(args, quote)
            if order["side"] == "buy" and _decimal(args["price"], "price") * 100 * order["quantity"] + max(fee, Decimal(0)) > _decimal(snapshot["buying_power"], "buying power"):
                raise BrokerError("Actual review fees and premium exceed available buying power")
            self._fresh_quote(quote)
            for position in snapshot["positions"]:
                self._check_quote_age(position["quote_timestamp"])
            self._live_enabled()
            if not self._market_open():
                raise BrokerError("Regular session ended before dispatch")
        except BrokerPreflightHold:
            raise
        except BrokerError as exc:
            raise BrokerPreflightHold(f"Order preflight validation held the order: {exc}") from exc
        ref_id = _order_ref_id(self.account_number, client_id)
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
                raise BrokerPreflightHold("Final dispatch validation held the order; nothing was submitted") from exc
        self.attempted.add(client_id)
        try:
            result = await self._data("place_option_order", dict(args, ref_id=ref_id))
        finally:
            self.account_changed.set()
            self._invalidate_execution_scope()
        normalized = self._order_result(result.get("order"), client_id, args)
        normalized["ref_id"] = ref_id
        self.order_results[client_id] = normalized
        return dict(normalized)

    def _order_matches_expected(self, raw, expected):
        if not isinstance(raw, dict) or not isinstance(expected, dict):
            return False
        if (
            raw.get("account_number") is not None
            and expected.get("account_number") is not None
            and raw.get("account_number") != expected.get("account_number")
        ):
            return False
        if raw.get("placed_agent") not in (None, "agentic"):
            return False
        try:
            if expected.get("quantity") is not None and _contracts(raw.get("quantity")) != _contracts(expected["quantity"]):
                return False
        except BrokerError:
            return False
        legs = raw.get("legs") or []
        if len(legs) != 1:
            return False
        leg = legs[0]
        expected_leg = None
        if isinstance(expected.get("legs"), list) and len(expected["legs"]) == 1:
            expected_leg = expected["legs"][0]
        expected_option_id = expected.get("option_id") or expected.get("instrument_id") or (expected_leg or {}).get("option_id")
        if expected_option_id is None and expected.get("contract") is not None:
            cached = self.contracts.get(_contract_key(expected["contract"]))
            expected_option_id = cached.get("option_id") if cached else None
        if expected.get("contract") is not None and expected_option_id is None:
            if any(value is None for value in (raw.get("chain_symbol"), leg.get("expiration_date"),
                                               leg.get("strike_price"), leg.get("option_type"))):
                return False
        if expected_option_id is not None and leg.get("option_id") != expected_option_id:
            return False
        if expected.get("side") is not None and leg.get("side") != expected.get("side"):
            return False
        if expected.get("position_effect") is not None and leg.get("position_effect") != expected.get("position_effect"):
            return False
        if expected_leg is not None:
            for key in ("option_id", "side", "position_effect", "ratio_quantity"):
                if expected_leg.get(key) is not None and leg.get(key) != expected_leg.get(key):
                    return False
        contract = expected.get("contract")
        if contract is not None:
            fields = {
                "symbol": raw.get("chain_symbol"),
                "expiry": leg.get("expiration_date"),
                "strike": leg.get("strike_price"),
                "option_type": leg.get("option_type"),
            }
            if any(value is None for value in fields.values()):
                if expected_option_id is None:
                    return False
            else:
                try:
                    if _contract_key(fields) != _contract_key(contract):
                        return False
                except BrokerError:
                    return False
        expected_kind = _request_order_kind(expected)
        if expected_kind is not None and _order_kind(raw) != expected_kind:
            return False
        expected_price = expected.get("price", expected.get("limit_price"))
        if expected_kind == "limit" and expected_price is not None:
            try:
                if _decimal(raw.get("price"), "order price") != _decimal(expected_price, "expected price"):
                    return False
            except BrokerError:
                return False
        try:
            kind = _request_order_kind(expected) or "limit"
            if _order_kind(raw) != kind:
                return False
            tif = expected.get("time_in_force", "gtc" if kind == "stop_market" else "gfd")
            if raw.get("time_in_force", tif) != tif:
                return False
            hours = expected.get("market_hours", "regular_hours")
            if raw.get("market_hours", hours) != hours:
                return False
        except (BrokerError, TypeError, ValueError):
            return False
        return True

    def _order_result(self, raw, client_id, expected=None):
        if not isinstance(raw, dict) or _decimal(raw.get("trade_value_multiplier"), "multiplier") != 100:
            raise BrokerError("Missing or unsupported submitted order response")
        legs = raw.get("legs") or []
        if len(legs) != 1 or legs[0].get("ratio_quantity") != 1:
            raise BrokerError("Broker order differs from the supported single-leg strategy")
        kind = _order_kind(raw)
        if kind is None:
            raise BrokerError("Broker order has an unsupported type or trigger")
        leg = legs[0]
        if (leg.get("side"), leg.get("position_effect")) not in {("buy", "open"), ("sell", "close")}:
            raise BrokerError("Broker order has an unexpected option strategy")
        if raw.get("direction") != ("debit" if leg.get("side") == "buy" else "credit"):
            raise BrokerError("Broker order has an unexpected cash direction")
        trigger = raw.get("trigger")
        broker_tif = raw.get("time_in_force") or ("gfd" if kind in {"limit", "market"} else None)
        if kind == "limit":
            if trigger not in (None, "immediate") or raw.get("stop_price") is not None or broker_tif != "gfd":
                raise BrokerError("Broker order differs from the supported single-leg limit strategy")
            if _decimal(raw.get("price"), "order price") <= 0:
                raise BrokerError("Broker limit order has an invalid price")
        elif kind == "market":
            if (leg.get("side"), leg.get("position_effect")) != ("sell", "close") or trigger not in (None, "immediate") or raw.get("stop_price") is not None or raw.get("price") is not None or broker_tif != "gfd":
                raise BrokerError("Broker market order differs from the supported sell-close strategy")
        else:
            if (leg.get("side"), leg.get("position_effect")) != ("sell", "close") or trigger != "stop" or raw.get("price") is not None or raw.get("time_in_force") != "gtc":
                raise BrokerError("Broker stop order differs from the supported sell-close strategy")
            if _decimal(raw.get("stop_price"), "stop price") <= 0:
                raise BrokerError("Broker stop order has an invalid stop price")
        expected_kind = _request_order_kind(expected) if expected else None
        if expected_kind is not None and expected_kind != kind:
            raise BrokerError("Broker order type does not match the persisted order")
        if expected:
            expected_quantity = expected.get("quantity")
            if expected_quantity is not None and _contracts(raw.get("quantity")) != _contracts(expected_quantity):
                raise BrokerError("Broker order quantity does not match the persisted order")
            expected_tif = expected.get("time_in_force")
            if expected_tif is None:
                expected_tif = "gtc" if kind == "stop_market" else "gfd"
            if broker_tif != expected_tif:
                raise BrokerError("Broker order time_in_force does not match the persisted order")
            expected_leg = None
            expected_legs = expected.get("legs")
            if isinstance(expected_legs, list) and len(expected_legs) == 1:
                expected_leg = expected_legs[0]
            expected_option_id = expected.get("option_id") or expected.get("instrument_id") or (expected_leg or {}).get("option_id")
            expected_contract = expected.get("contract")
            if expected_contract is not None:
                raw_contract_fields = {
                    "symbol": raw.get("chain_symbol"),
                    "expiry": leg.get("expiration_date"),
                    "strike": leg.get("strike_price"),
                    "option_type": leg.get("option_type"),
                }
                if all(value is not None for value in raw_contract_fields.values()):
                    if _contract_key(raw_contract_fields) != _contract_key(expected_contract):
                        raise BrokerError("Broker order contract does not match the persisted order")
                elif expected_option_id is None:
                    cached = self.contracts.get(_contract_key(expected_contract))
                    expected_option_id = cached.get("option_id") if cached else None
                    if expected_option_id is None:
                        raise BrokerError("Persisted order lacks exact option instrument identity")
            if expected_option_id is not None and leg.get("option_id") != expected_option_id:
                raise BrokerError("Broker order option instrument does not match the persisted order")
            expected_price = expected.get("price", expected.get("limit_price"))
            if kind == "limit":
                if expected_price is None or _decimal(raw.get("price"), "order price") != _decimal(expected_price, "expected price"):
                    raise BrokerError("Broker order price does not match the persisted order")
            elif expected_price is not None:
                raise BrokerError("Persisted market order unexpectedly has a limit price")
            expected_stop = expected.get("stop_price")
            if kind == "stop_market":
                if expected_stop is None or _decimal(raw.get("stop_price"), "stop price") != _decimal(expected_stop, "expected stop price"):
                    raise BrokerError("Broker stop price does not match the persisted order")
            elif expected_stop is not None:
                raise BrokerError("Persisted immediate order unexpectedly has a stop price")
            if expected_leg is not None:
                for key in ("option_id", "side", "position_effect", "ratio_quantity"):
                    if expected_leg.get(key) is not None and leg.get(key) != expected_leg[key]:
                        raise BrokerError("Broker order leg does not match the persisted order")
        states = {
            "queued": "open",
            "confirmed": "open",
            "partially_filled": "partially_filled",
            "filled": "filled",
            "rejected": "rejected",
            "cancelled": "canceled",
            "canceled": "canceled",
            "failed": "rejected",
            "voided": "rejected",
            "pending_cancelled": "open",
        }
        if raw.get("state") not in states:
            raise BrokerError("Unknown Robinhood order status; reconciliation is required")
        filled = _contracts(raw.get("processed_quantity"))
        premium = _decimal(raw.get("processed_premium"), "filled premium")
        if filled > _contracts(raw.get("quantity")) or (not filled and premium):
            raise BrokerError("Inconsistent cumulative Robinhood fills")
        if (raw["state"] == "filled" and filled != _contracts(raw["quantity"])) or (filled and not premium):
            raise BrokerError("Robinhood fill quantity or premium is inconsistent with its status")
        return {
            "id": raw["id"],
            "client_order_id": client_id,
            "broker_order_id": raw["id"],
            "status": states[raw["state"]],
            "filled_quantity": filled,
            "fill_price": str(premium / 100 / filled) if filled else None,
            "timestamp": raw.get("updated_at") or raw["created_at"],
        }

    async def order_status(self, order_id, *, broker_order_id=None, expected_order=None):
        direct_known = self.order_results.get(order_id)
        known = direct_known
        client_id = order_id
        if known is None:
            for candidate_id, candidate in self.order_results.items():
                if candidate.get("broker_order_id") == order_id:
                    client_id, known = candidate_id, candidate
                    break
        if known is not None and known.get("client_order_id") in self.order_inputs:
            client_id = known["client_order_id"]
        persisted = expected_order or self.order_inputs.get(client_id) or self.order_inputs.get(order_id)
        persisted = copy.deepcopy(persisted) if persisted else None
        known_ref = (known or {}).get("ref_id")
        persisted_ref = (persisted or {}).get("ref_id")
        if known_ref and persisted_ref and known_ref != persisted_ref:
            raise BrokerError("Persisted order ref_id does not match the known order")
        expected_ref = known_ref or persisted_ref or _order_ref_id(self.account_number, client_id)
        broker_id = broker_order_id or (known or {}).get("broker_order_id")
        if persisted and persisted.get("broker_order_id") and broker_id != persisted["broker_order_id"]:
            raise BrokerError("Persisted broker order identity does not match the requested broker order")
        if known and broker_order_id and known.get("broker_order_id") != broker_order_id:
            raise BrokerError("Requested broker order does not match the persisted order")
        matched_row = None
        if not broker_id:
            if expected_ref:
                lookup = {"account_number": self.account_number, "placed_agent": "agentic"}
                created_at_floor = None
                anchor = (persisted or {}).get("created_at") or (persisted or {}).get("submitted_at")
                if anchor is None and persisted and persisted.get("entry_cancel_at"):
                    try:
                        seconds = _decimal(persisted.get("entry_cancel_after_seconds"), "entry cancel window")
                        anchor = (
                            _instant(persisted["entry_cancel_at"]) - timedelta(seconds=float(seconds))
                        ).isoformat()
                    except (BrokerError, TypeError, ValueError):
                        anchor = None
                if anchor is not None:
                    try:
                        created_at_floor = _instant(anchor) - timedelta(seconds=5)
                        lookup["created_at_gte"] = (
                            created_at_floor
                        ).isoformat()
                    except BrokerError:
                        pass
                rows = await self._pages(
                    "get_option_orders",
                    lookup,
                    "orders",
                )
                def in_submission_window(row):
                    if created_at_floor is None:
                        return False
                    try:
                        created = _instant(row.get("created_at"))
                        ceiling = min(created_at_floor + timedelta(seconds=125),
                                      self.clock().astimezone(timezone.utc) + timedelta(seconds=5))
                        return created_at_floor <= created <= ceiling
                    except (BrokerError, TypeError, ValueError):
                        return False

                matches = [
                    row
                    for row in rows
                    if row.get("placed_agent") == "agentic"
                    and row.get("ref_id") == expected_ref
                ]
                if not matches:
                    matches = [
                        row
                        for row in rows
                        if row.get("placed_agent") == "agentic"
                        and in_submission_window(row)
                        and all(row.get(key) is not None for key in ("type", "trigger", "time_in_force", "market_hours"))
                        and self._order_matches_expected(row, persisted or {})
                    ]
                if len(matches) != 1:
                    raise BrokerError("Broker order could not be uniquely reconciled on the bound account")
                if created_at_floor is not None:
                    try:
                        created_at = _instant(matches[0].get("created_at"))
                        if created_at < created_at_floor or created_at > min(created_at_floor + timedelta(seconds=125), self.clock().astimezone(timezone.utc) + timedelta(seconds=5)):
                            raise BrokerError("Broker order is outside the persisted reconciliation window")
                    except (BrokerError, TypeError, ValueError):
                        raise BrokerError("Broker order timestamp is outside the persisted reconciliation window") from None
                matched_row = matches[0]
                broker_id = matched_row.get("id")
            else:
                try:
                    broker_id = str(uuid.UUID(order_id))
                except (ValueError, TypeError):
                    raise BrokerError("Persisted broker order UUID is required; unknown submissions must not be repeated") from None
        rows = [matched_row] if matched_row is not None else await self._pages(
            "get_option_orders",
            {"account_number": self.account_number, "order_id": broker_id},
            "orders",
        )
        if len(rows) != 1 or rows[0].get("id") != broker_id:
            raise BrokerError("Broker order could not be reconciled on the bound account")
        if rows[0].get("account_number") not in (None, self.account_number):
            raise BrokerError("Broker order account does not match the bound account")
        if rows[0].get("placed_agent") not in (None, "agentic"):
            raise BrokerError("Broker order is not an agentic order")
        if expected_ref and rows[0].get("ref_id") not in (None, expected_ref):
            raise BrokerError("Broker order ref_id does not match the persisted order")
        if persisted is None:
            persisted = {}
        if expected_ref:
            persisted["ref_id"] = expected_ref
        persisted["broker_order_id"] = broker_id
        if persisted and not self._order_matches_expected(rows[0], persisted):
            raise BrokerError("Broker order does not match the persisted contract and quantity")
        previous = direct_known if direct_known is not None else (
            self.order_results.get(client_id) if order_id == client_id else None
        )
        result = self._order_result(rows[0], client_id, persisted)
        self.order_results[client_id] = result
        if order_id != client_id:
            self.order_results[order_id] = result
        if previous is None or (
            result["filled_quantity"] != previous.get("filled_quantity")
            or result["status"] != previous.get("status")
        ):
            self.account_changed.set()
            self._invalidate_execution_scope()
        return result

    async def cancel_order(self, order_id, *, broker_order_id=None, expected_order=None):
        self._live_enabled()
        known = self.order_results.get(order_id)
        persisted = expected_order or self.order_inputs.get(order_id)
        broker_id = broker_order_id or (known or {}).get("broker_order_id")
        if persisted and persisted.get("broker_order_id") and broker_id != persisted["broker_order_id"]:
            raise BrokerError("Persisted broker order identity does not match the requested broker order")
        if known and broker_order_id and known.get("broker_order_id") != broker_order_id:
            raise BrokerError("Requested broker order does not match the persisted order")
        if not known and not broker_order_id:
            raise BrokerError("Cancellation requires a persisted broker order identity")
        current = await self.order_status(order_id, broker_order_id=broker_id, expected_order=persisted)
        if current["status"] in {"filled", "canceled", "rejected"}:
            return current
        accounts = (await self._data("get_accounts", {}))["accounts"] or []
        matches = [account for account in accounts if account["account_number"] == self.account_number]
        if len(matches) != 1:
            raise BrokerError("Bound Robinhood account is missing or ambiguous")
        restriction = _caller_option_restriction(matches[0])
        if restriction:
            raise BrokerError(f"Cancellation blocked: {restriction}")
        acknowledged = await self._data("cancel_option_order", {"account_number": self.account_number, "order_id": broker_id})
        if not isinstance(acknowledged.get("accepted"), bool):
            raise BrokerError("Robinhood cancellation response omitted accepted")
        return await self.order_status(order_id, broker_order_id=broker_id, expected_order=persisted)
