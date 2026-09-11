"""Connection telemetry must not change provider results or order outcomes."""
import json
import logging
from pathlib import Path


PROVIDER_COMPONENTS = frozenset({"discord", "codex", "broker"})
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


def publish_status(callback, component, state):
    if callback is not None:
        try:
            callback({"component": component, "state": state})
        except Exception as exc:
            logging.getLogger(__name__).warning("Status publication failed (%s)", type(exc).__name__)
