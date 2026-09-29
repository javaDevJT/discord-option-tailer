"""Connection telemetry must not change provider results or order outcomes."""
import builtins
import json
import logging
import math
import os
import re
import time
import traceback
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path


_FAILURE_STAGES = frozenset({
    "execution", "planning", "snapshot", "quote", "contract_resolution",
    "source_verification", "order_reservation", "submission", "result_recording",
    "recovery", "expiry", "stop", "stop_submission", "reconciliation", "watch_preparation",
    "preflight", "review", "dispatch_guard", "placement", "response_validation", "cancellation",
    "interpretation", "worker", "discovery", "expiry_session",
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
_EXECUTION_CHECKS = frozenset({
    "dispatch_intent", "signal_freshness", "origin_evidence", "source_consistency",
    "execution_mode", "kill_switch", "entry_window", "prepared_watch", "quote_age",
    "snapshot_age", "account_session", "account_restrictions", "contract_tradability",
    "quote_spread", "entry_chase", "entry_capacity", "exit_profit", "source_verification",
    "origin_verification", "dispatch_persistence",
})


def project_execution_context(value):
    """Only explicitly named business facts, never raw requests, account IDs or tokens."""
    if not isinstance(value, dict):
        return {}
    result = {}
    numbers = {
        "quantity", "available_quantity", "calculated_quantity", "buying_power", "equity",
        "bid", "ask", "limit_price", "alert_price", "max_chase_fraction", "max_spread_fraction",
        "spread_fraction", "signal_age_seconds", "quote_age_seconds", "snapshot_age_seconds",
        "max_signal_age_seconds", "max_quote_age_seconds", "entry_remaining_seconds",
        "expected_source_generation", "actual_source_generation", "restriction_count",
        "filled_quantity", "requested_quantity", "fee", "strike", "duration_seconds",
    }
    flags = {"prepared_entry", "market_open", "account_matches", "tradable", "kill_switch_present",
             "watch_matches", "within_cap", "transport_attempted", "live_enabled", "submitted"}
    ids = {"message_id", "channel_id", "origin_message_id", "latest_message_id", "watch_message_id"}
    hashes = {"revision", "expected_revision", "actual_revision", "client_order_id",
              "expected_position_generation", "actual_position_generation", "diagnostic_id", "attempt_id"}
    enums = {"mode": {"live", "paper", "shadow"}, "side": {"buy", "sell"},
             "action": {"OPEN", "ADD", "REDUCE", "CLOSE", "HOLD", "IGNORE", "UPDATE_STOP"},
             "option_type": {"call", "put"}, "phase": {"before_review", "after_review", "after_source"},
             "state": {"held", "error", "unknown", "broker_order", "paper_order", "shadow_order", "filled",
                       "canceled", "rejected", "expired", "open", "partially_filled", "context"}}
    for key, item in value.items():
        if key in numbers:
            if type(item) in (int, float) and math.isfinite(item) and abs(item) <= 1e15:
                result[key] = item
            elif isinstance(item, str) and re.fullmatch(r"[+-]?[0-9]{1,16}(?:\.[0-9]{1,32})?", item):
                result[key] = item
        elif key in flags and type(item) is bool:
            result[key] = item
        elif key in ids and isinstance(item, str) and re.fullmatch(r"[0-9]{15,22}", item):
            result[key] = item
        elif key in hashes and isinstance(item, str) and re.fullmatch(r"[a-f0-9]{32,64}", item):
            result[key] = item
        elif key in enums and isinstance(item, str) and item in enums[key]:
            result[key] = item
        elif key == "symbol" and isinstance(item, str) and re.fullmatch(r"[A-Z][A-Z0-9.]{0,9}", item):
            result[key] = item
        elif key == "expiry" and isinstance(item, str) and re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", item):
            result[key] = item
    return result


def project_dispatch_checks(value):
    result = []
    for item in value[:96] if isinstance(value, list) else []:
        if not isinstance(item, dict) or not isinstance(item.get("check"), str) or item["check"] not in _EXECUTION_CHECKS:
            continue
        row = {"check": item["check"]}
        if isinstance(item.get("status"), str) and item["status"] in {"running", "passed", "failed"}:
            row["status"] = item["status"]
        duration = item.get("duration_seconds")
        if type(duration) in (int, float) and math.isfinite(duration) and 0 <= duration <= 86400:
            row["duration_seconds"] = duration
        at = item.get("started_at")
        if isinstance(at, str) and re.fullmatch(r"[0-9T:.+Z-]{20,40}", at):
            row["started_at"] = at
        row["context"] = project_execution_context(item.get("context"))
        result.append(row)
    return result


def log_execution(event, *, context=None, diagnostic=None, checks=None):
    """A safe log failure must never change an execution outcome."""
    if event not in {"check", "failure", "outcome", "reserved", "dispatch", "worker_start"}:
        return
    try:
        record = {"event": event, "at": datetime.now(timezone.utc).isoformat(),
                  "context": project_execution_context(context)}
        revision = os.environ.get("RELAY_SOURCE_REVISION", "")
        if re.fullmatch(r"[a-f0-9]{40}", revision):
            record["build_revision"] = revision
        if diagnostic is not None:
            record["diagnostic"] = project_execution_diagnostic(diagnostic)
        if checks is not None:
            record["checks"] = project_dispatch_checks(checks)
        writer = logging.getLogger(__name__).error if event == "failure" else logging.getLogger(__name__).info
        writer("Execution diagnostic %s", json.dumps(record, sort_keys=True))
    except Exception:
        pass


class _PrivateExecutionLog(RotatingFileHandler):
    def _open(self):
        fd = os.open(self.baseFilename, os.O_CREAT | os.O_APPEND | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
        os.fchmod(fd, 0o600)
        return os.fdopen(fd, "a", encoding="utf-8")

    def handleError(self, record):
        # Keep a second sink for a failed disk write, without duplicating every
        # successful record into the container's separately managed log storage.
        try:
            logging.getLogger().handle(record)
        except Exception:
            pass


def configure_execution_log(path):
    """Rotate only the safe execution logger on the persistent data volume."""
    logger = logging.getLogger(__name__)
    path = Path(path)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.is_symlink():
            raise OSError("execution log may not be a symlink")
        fd = os.open(path, os.O_CREAT | os.O_APPEND | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
        os.close(fd)
        os.chmod(path, 0o600)
        for handler in logger.handlers:
            if isinstance(handler, RotatingFileHandler) and handler.baseFilename == str(path.resolve()):
                return
        handler = _PrivateExecutionLog(path, maxBytes=10 * 1024 * 1024, backupCount=5, encoding="utf-8")
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)
        logger.propagate = False
    except Exception as error:
        # The container log remains a second sink if the volume is unavailable.
        execution_failure(error, stage="worker")


@contextmanager
def execution_check(trace, check, *, context=None):
    started = time.monotonic()
    row = {"check": check, "status": "running", "started_at": datetime.now(timezone.utc).isoformat()}
    try:
        yield context
    except BaseException as error:
        row["status"] = "failed"
        try:
            annotate_failure(error, stage="dispatch_guard", check=check, context=context)
        except Exception:
            pass
        raise
    else:
        row["status"] = "passed"
    finally:
        # This also runs on cancellation; it does not swallow cancellation.
        try:
            row.update(duration_seconds=round(time.monotonic() - started, 6),
                       context=project_execution_context(context))
            if isinstance(trace, list) and len(trace) < 96:
                trace.append(row)
            if row["status"] == "failed":
                log_execution("check", context=context, checks=[row])
        except Exception:
            pass


def annotate_failure(error, **fields):
    """Keep provenance on the exception, not shared concurrent task state."""
    allowed = {"stage": _FAILURE_STAGES, "broker_operation": _BROKER_OPERATIONS, "tool": _FAILURE_TOOLS, "code": _FAILURE_CODES, "check": _EXECUTION_CHECKS}
    try:
        details = dict(getattr(error, "_relay_failure", {}))
        for key, value in fields.items():
            if key in allowed and isinstance(value, str) and value in allowed[key]:
                details.setdefault(key, value)
        if isinstance(fields.get("context"), dict):
            details["context"] = project_execution_context(fields["context"]) | details.get("context", {})
        error._relay_failure = details
    except Exception:
        pass  # Diagnostics must never replace the original failure.


@contextmanager
def failure_stage(stage):
    try:
        yield
    except Exception as error:
        try:
            annotate_failure(error, stage=stage)
        except Exception:
            pass
        raise


def project_execution_diagnostic(value):
    """Project bounded metadata only; never accept exception text or provider bodies."""
    def project(item):
        if not isinstance(item, dict):
            return {}
        result = {}
        for key, choices in (("stage", _FAILURE_STAGES), ("broker_operation", _BROKER_OPERATIONS), ("tool", _FAILURE_TOOLS), ("check", _EXECUTION_CHECKS)):
            if isinstance(item.get(key), str) and item[key] in choices:
                result[key] = item[key]
        if isinstance(item.get("exception"), str):
            result["exception"] = item["exception"] if item["exception"] in _FAILURE_EXCEPTIONS else "Exception"
        code = item.get("code")
        if isinstance(code, str) and (code in _FAILURE_CODES or re.fullmatch(r"http_[45][0-9]{2}", code)):
            result["code"] = code
        frames = item.get("frames", [])
        if isinstance(frames, list):
            result["frames"] = [frame for frame in frames[:16] if isinstance(frame, str) and re.fullmatch(
                r"relay/[a-z_]+\.py:[1-9][0-9]{0,5}:[A-Za-z_][A-Za-z0-9_]{0,79}", frame
            )]
        if isinstance(item.get("id"), str) and re.fullmatch(r"[a-f0-9]{32}", item["id"]):
            result["id"] = item["id"]
        if isinstance(item.get("build_revision"), str) and re.fullmatch(r"[a-f0-9]{40}", item["build_revision"]):
            result["build_revision"] = item["build_revision"]
        if isinstance(item.get("at"), str) and re.fullmatch(r"[0-9T:.+Z-]{20,40}", item["at"]):
            result["at"] = item["at"]
        if isinstance(item.get("context"), dict):
            result["context"] = project_execution_context(item["context"])
        return result
    result = project(value)
    if isinstance(value, dict) and isinstance(value.get("causes"), list):
        result["causes"] = [project(item) for item in value["causes"][:8] if isinstance(item, dict)]
    return result


def execution_failure(error, *, stage="execution", context=None):
    """Return and log safe diagnostics without messages, source text, locals, or paths."""
    pending, seen, items = [error], set(), []
    try:
        annotate_failure(error, stage=stage, context=context)
        while pending and len(items) < 9:
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
                "exception": name, "code": attached.get("code", code), "frames": frames[-16:],
            }))
            if isinstance(current, BaseExceptionGroup):
                pending.extend(current.exceptions[:8])
            pending.extend(item for item in (current.__cause__, current.__context__) if isinstance(item, BaseException))
        diagnostic = items[0] | ({"causes": items[1:]} if len(items) > 1 else {})
    except Exception:
        diagnostic = {"stage": stage if stage in _FAILURE_STAGES else "execution", "exception": "Exception", "code": "internal_error", "frames": []}
    diagnostic["id"] = uuid.uuid4().hex
    diagnostic["at"] = datetime.now(timezone.utc).isoformat()
    revision = os.environ.get("RELAY_SOURCE_REVISION", "")
    if re.fullmatch(r"[a-f0-9]{40}", revision):
        diagnostic["build_revision"] = revision
    # Keep the innermost named check in the summary even through generic wrappers.
    checks = [item.get("check") for item in [diagnostic] + diagnostic.get("causes", []) if item.get("check")]
    if checks:
        diagnostic["check"] = checks[-1]
    parts = [f"{key}={diagnostic[key]}" for key in ("id", "stage", "check", "exception", "code", "broker_operation", "tool") if key in diagnostic]
    if diagnostic.get("frames"):
        parts.append("at=" + diagnostic["frames"][-1])
    log_execution("failure", context=context, diagnostic=diagnostic)
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
