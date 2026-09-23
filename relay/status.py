"""Connection telemetry must not change provider results or order outcomes."""
import builtins
import json
import logging
import re
import traceback
from contextlib import contextmanager
from pathlib import Path


_FAILURE_STAGES = frozenset({
    "execution", "planning", "snapshot", "quote", "contract_resolution",
    "source_verification", "order_reservation", "submission", "result_recording",
    "recovery", "expiry", "stop", "stop_submission", "reconciliation",
})
_BROKER_OPERATIONS = frozenset({
    "snapshot", "quote", "nearest_expiry", "prepare_contract", "prepare_entry",
    "submit", "order_status", "cancel_order", "underlying_quote", "account_overview",
})
_FAILURE_TOOLS = frozenset({
    "search",
    "get_accounts", "get_portfolio", "get_option_chains", "get_option_instruments",
    "get_option_quotes", "get_option_positions", "get_option_orders", "get_equity_quotes",
    "get_realized_pnl", "get_pnl_trade_history", "review_option_order", "place_option_order",
    "cancel_option_order",
})
_FAILURE_EXCEPTIONS = frozenset(
    name for name, value in vars(builtins).items()
    if isinstance(value, type) and issubclass(value, BaseException)
) | frozenset({
    "BrokerError", "BrokerPreflightHold", "InterpretationError", "Hold", "RetryHold",
    "SourceContextChanged", "ImageTransportError", "JEVError", "InvalidOperation",
    "OperationalError", "IntegrityError", "DatabaseError", "InterfaceError",
    "TimeoutException", "ConnectTimeout", "ReadTimeout", "WriteTimeout", "PoolTimeout",
    "ConnectError", "ReadError", "WriteError", "HTTPStatusError", "HTTPError", "URLError",
    "RemoteProtocolError", "LocalProtocolError", "ProtocolError", "TransportError",
    "McpError", "ValidationError", "SchemaError", "SSLError", "gaierror",
})
_FAILURE_CODES = frozenset({
    "internal_error", "broker_error", "schema_incompatible", "dns_failed", "tls_failed", "timeout",
    "browser_profile_busy", "runtime_missing", "local_permission", "network_unavailable",
})


def annotate_failure(error, **fields):
    """Keep provenance on the exception, not shared concurrent task state."""
    allowed = {"stage": _FAILURE_STAGES, "broker_operation": _BROKER_OPERATIONS, "tool": _FAILURE_TOOLS, "code": _FAILURE_CODES}
    try:
        details = dict(getattr(error, "_relay_failure", {}))
        for key, value in fields.items():
            if key in allowed and isinstance(value, str) and value in allowed[key]:
                details.setdefault(key, value)
        error._relay_failure = details
    except Exception:
        pass  # Diagnostics must never replace the original failure.


@contextmanager
def failure_stage(stage):
    try:
        yield
    except Exception as error:
        annotate_failure(error, stage=stage)
        raise


def project_execution_diagnostic(value):
    """Project bounded metadata only; never accept exception text or provider bodies."""
    def project(item):
        if not isinstance(item, dict):
            return {}
        result = {}
        for key, choices in (("stage", _FAILURE_STAGES), ("broker_operation", _BROKER_OPERATIONS), ("tool", _FAILURE_TOOLS)):
            if isinstance(item.get(key), str) and item[key] in choices:
                result[key] = item[key]
        if isinstance(item.get("exception"), str):
            result["exception"] = item["exception"] if item["exception"] in _FAILURE_EXCEPTIONS else "Exception"
        code = item.get("code")
        if isinstance(code, str) and (code in _FAILURE_CODES or re.fullmatch(r"http_[45][0-9]{2}", code)):
            result["code"] = code
        frames = item.get("frames", [])
        if isinstance(frames, list):
            result["frames"] = [frame for frame in frames[:8] if isinstance(frame, str) and re.fullmatch(
                r"relay/[a-z_]+\.py:[1-9][0-9]{0,5}:[A-Za-z_][A-Za-z0-9_]{0,79}", frame
            )]
        return result
    result = project(value)
    if isinstance(value, dict) and isinstance(value.get("causes"), list):
        result["causes"] = [project(item) for item in value["causes"][:3] if isinstance(item, dict)]
    return result


def execution_failure(error, *, stage="execution"):
    """Return and log safe diagnostics without messages, source text, locals, or paths."""
    annotate_failure(error, stage=stage)
    pending, seen, items = [error], set(), []
    try:
        while pending and len(items) < 4:
            current = pending.pop(0)
            if not isinstance(current, BaseException) or id(current) in seen:
                continue
            seen.add(id(current))
            kind = type(current)
            trusted = kind.__module__.split(".")[0] in {
                "builtins", "relay", "sqlite3", "decimal", "json", "asyncio", "httpx",
                "httpcore", "anyio", "mcp", "jsonschema", "urllib", "ssl", "socket",
            }
            name = kind.__name__ if trusted else "Exception"
            detail = failure_detail(current, provider="Robinhood", phase="operation")
            code = re.search(r"\[([a-z0-9_]+)\]", detail).group(1)
            if code == "operation_failed":
                code = "broker_error" if name in {"BrokerError", "BrokerPreflightHold"} else "internal_error"
            frames = []
            for frame, line in traceback.walk_tb(current.__traceback__):
                path = Path(frame.f_code.co_filename)
                if path.parent.resolve() == Path(__file__).parent.resolve():
                    frames.append(f"relay/{path.name}:{line}:{frame.f_code.co_name}")
            attached = project_execution_diagnostic(getattr(current, "_relay_failure", {}))
            items.append(project_execution_diagnostic(attached | {
                "exception": name, "code": attached.get("code", code), "frames": frames[-8:],
            }))
            if isinstance(current, BaseExceptionGroup):
                pending.extend(current.exceptions[:4])
            pending.extend(item for item in (current.__cause__, current.__context__) if isinstance(item, BaseException))
        diagnostic = items[0] | ({"causes": items[1:]} if len(items) > 1 else {})
    except Exception:
        diagnostic = {"stage": stage if stage in _FAILURE_STAGES else "execution", "exception": "Exception", "code": "internal_error", "frames": []}
    parts = [f"{key}={diagnostic[key]}" for key in ("stage", "exception", "code", "broker_operation", "tool") if key in diagnostic]
    if diagnostic.get("frames"):
        parts.append("at=" + diagnostic["frames"][-1])
    try:
        logging.getLogger(__name__).error("Execution diagnostic %s", json.dumps(diagnostic, sort_keys=True))
    except Exception:
        pass
    return "; ".join(parts), diagnostic


PROVIDER_COMPONENTS = frozenset({"discord", "codex", "broker", "jev"})
AUTH_REQUIRED_STATES = frozenset({
    "auth_required", "authentication_required", "login_required", "reauth_required",
    "unauthorized", "unauthenticated",
})
HEALTHY_STATES = frozenset({"connected", "ready", "paper"})


def is_auth_required_state(state):
    return isinstance(state, str) and state.lower() in AUTH_REQUIRED_STATES


def previous_auth_state(path, component):
    """Read only a previously published provider auth state.

    Runtime status is operator-visible state, so this helper accepts only a small
    bounded JSON projection and never carries forward provider details or errors.
    """

    if component not in PROVIDER_COMPONENTS:
        return None
    path = Path(path)
    try:
        if path.is_symlink() or not path.is_file() or path.stat().st_size > 1024 * 1024:
            return None
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return None
    section = value.get(component) if isinstance(value, dict) else None
    state = section.get("state") if isinstance(section, dict) else None
    return state.lower() if is_auth_required_state(state) else None


def failure_detail(error, *, provider, phase):
    """Describe a failure without publishing provider payloads or credentials."""
    provider = provider if type(provider) is str and provider in {"Discord", "Codex", "Robinhood"} else "Worker"
    phase = phase if type(phase) is str and re.fullmatch(r"[a-zA-Z -]{1,64}", phase) else "operation"

    def safe_text(value):
        try:
            return str(value)[:4096].lower()
        except Exception:
            return ""

    def safe_getattr(value, name):
        try:
            return getattr(value, name, None)
        except Exception:
            return None

    pending, seen = [error], set()
    code, explanation, action = "operation_failed", "failed", "Retry once; if it persists, report this code and the operation shown here."
    while pending and len(seen) < 8:
        item = pending.pop(0)
        if id(item) in seen:
            continue
        seen.add(id(item))
        if isinstance(item, BaseExceptionGroup):
            pending.extend(item.exceptions[:8])
            continue
        pending.extend(value for value in (safe_getattr(item, "__cause__"), safe_getattr(item, "__context__"))
                       if isinstance(value, BaseException))
        kind, text = type(item).__name__, safe_text(item)
        response = safe_getattr(item, "response")
        http_status = safe_getattr(response, "status_code")
        if http_status is None:
            http_status = safe_getattr(item, "status_code")
        if type(http_status) is int and 400 <= http_status < 600:
            code, explanation = f"http_{http_status}", f"was rejected with HTTP {http_status}"
            action = ("Sign in again and verify this account has access." if http_status in {401, 403}
                      else "The provider is rate limiting requests; wait before retrying." if http_status == 429
                      else "Check the provider service status, then retry." if http_status >= 500
                      else "Check the configured endpoint and authorization, then retry.")
        elif "err_name_not_resolved" in text or any(term in text for term in ("name or service not known", "name resolution", "nodename nor servname")):
            code, explanation, action = "dns_failed", "could not resolve the provider hostname", "Check DNS and internet access from the NAS."
        elif any(term in text for term in ("certificate_verify_failed", "err_cert_", "certificate verify failed")) or "sslerror" in kind.lower():
            code, explanation, action = "tls_failed", "could not verify the HTTPS connection", "Check the NAS clock, trusted certificates, and any HTTPS proxy."
        elif "timeout" in kind.lower() or any(term in text for term in ("timed out", "err_timed_out", "err_connection_timed_out")):
            code, explanation, action = "timeout", "timed out", "Check the NAS internet connection and provider service status; retry when they recover."
        elif any(term in text for term in ("processsingleton", "profile appears to be in use", "user data directory is already in use")):
            code, explanation, action = "browser_profile_busy", "could not open the saved browser profile", "Stop the duplicate browser worker, then reconnect. Keep the saved profile."
        elif isinstance(item, FileNotFoundError) or "executable doesn't exist" in text:
            code, explanation, action = "runtime_missing", "could not find a required executable", "Check the container installation and configured executable."
        elif isinstance(item, PermissionError):
            code, explanation, action = "local_permission", "could not access a required local file", "Check the persistent volume ownership and permissions."
        elif "connect" in kind.lower() or any(term in text for term in ("err_connection_", "err_internet_disconnected", "network is unreachable", "connection refused")):
            code, explanation, action = "network_unavailable", "could not connect to the provider", "Check NAS internet access, firewall, proxy, and provider service status."
        else:
            continue
        break
    return f"{provider} {phase} {explanation} [{code}]. {action}"


def publish_status(callback, component, state, *, detail=None, channel_id=None):
    if callback is not None:
        try:
            event = {"component": component, "state": state}
            if channel_id is not None:
                event["channel_id"] = str(channel_id)
            if detail is not None:
                event["detail"] = detail
            callback(event)
        except Exception as exc:
            logging.getLogger(__name__).warning("Status publication failed (%s)", type(exc).__name__)
