"""Connection telemetry must not change provider results or order outcomes."""
import json
import logging
import re
from pathlib import Path


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
