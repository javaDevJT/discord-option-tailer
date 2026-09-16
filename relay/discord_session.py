"""Credential-safe Discord personal-account gateway token storage.

The gateway adapter receives its credential from this module.  Tokens are kept
out of configuration and status projections; the configuration only names the
secure store and optional environment fallback.
"""

from __future__ import annotations

import os
import json
import stat
import tempfile
from pathlib import Path
from typing import Mapping


DEFAULT_TOKEN_STORE = "state/discord-user.json"
DEFAULT_TOKEN_ENV = "DISCORD_USER_TOKEN"
MAX_TOKEN_LENGTH = 4096

_OBVIOUSLY_INVALID = frozenset(
    {
        "null",
        "none",
        "undefined",
        "token",
        "your-token-here",
        "your_token_here",
        "changeme",
        "replace-me",
        "replace_me",
    }
)


def _section(config: Mapping[str, object] | None) -> Mapping[str, object]:
    if not isinstance(config, Mapping):
        return {}
    value = config.get("discord", {})
    return value if isinstance(value, Mapping) else {}


def token_store_path(config: Mapping[str, object] | None) -> Path:
    """Return the configured token-store path without following its final link.

    Callers that work with a raw configuration should resolve relative paths
    against the configuration directory before calling this module.  This
    helper intentionally does not do that resolution: following a final
    symlink would defeat the secure-file checks in :func:`write_token`.
    """

    value = _section(config).get("token_store", DEFAULT_TOKEN_STORE)
    if not isinstance(value, str) or not value.strip():
        value = DEFAULT_TOKEN_STORE
    return Path(value).expanduser()


def token_env_name(config: Mapping[str, object] | None) -> str:
    value = _section(config).get("token_env", DEFAULT_TOKEN_ENV)
    if not isinstance(value, str) or not value.strip():
        return DEFAULT_TOKEN_ENV
    return value.strip()


def normalize_token(value: object, *, allow_empty: bool = False) -> str:
    """Normalize and validate a gateway token without echoing its contents."""

    if not isinstance(value, str):
        raise ValueError("Discord gateway token must be a string")
    token = value.strip()
    if not token:
        if allow_empty:
            return ""
        raise ValueError("Discord gateway token must not be empty")
    if len(token) > MAX_TOKEN_LENGTH:
        raise ValueError("Discord gateway token is too long")
    if any(char.isspace() or ord(char) < 32 or ord(char) == 127 for char in token):
        raise ValueError("Discord gateway token contains whitespace or control characters")
    lowered = token.lower()
    if lowered in _OBVIOUSLY_INVALID or lowered.startswith("bearer "):
        raise ValueError("Discord gateway token value is invalid")
    return token


def _secure_path(path: Path) -> Path:
    """Reject an existing final symlink and ensure a private parent exists."""

    try:
        if path.is_symlink():
            raise ValueError("Discord token store must not be a symlink")
    except OSError as exc:
        raise RuntimeError("Discord token store is unavailable") from exc
    try:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if path.parent.is_symlink():
            raise ValueError("Discord token store directory must not be a symlink")
    except OSError as exc:
        raise RuntimeError("Discord token store is unavailable") from exc
    return path


def read_token(config: Mapping[str, object] | None) -> str | None:
    """Read the configured gateway token, falling back to its environment."""

    path = token_store_path(config)
    try:
        if path.is_symlink():
            raise ValueError("Discord token store must not be a symlink")
        if path.is_file():
            mode = stat.S_IMODE(path.stat().st_mode)
            if mode != 0o600:
                return None
            raw = path.read_text(encoding="utf-8")
            try:
                parsed = json.loads(raw)
            except (TypeError, ValueError):
                parsed = raw
            if isinstance(parsed, dict):
                parsed = parsed.get("token")
            token = normalize_token(parsed)
            return token
    except FileNotFoundError:
        pass
    except (OSError, UnicodeError, ValueError):
        # A malformed or unsafe local store must not be promoted to a usable
        # credential through a status check.  The setup writer still reports
        # the precise safe error when the user explicitly saves a token.
        return None

    try:
        environment_value = os.environ.get(token_env_name(config), "")
    except (TypeError, ValueError):
        return None
    if not environment_value:
        return None
    try:
        return normalize_token(environment_value)
    except ValueError:
        return None


def credential_configured(config: Mapping[str, object] | None) -> bool:
    """Return whether a usable local or environment gateway credential exists."""

    return read_token(config) is not None


def write_token(config: Mapping[str, object] | None, value: object) -> None:
    """Atomically write a private token file with owner-only permissions."""

    token = normalize_token(value)
    path = _secure_path(token_store_path(config))
    try:
        if path.is_symlink():
            raise ValueError("Discord token store must not be a symlink")
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{path.name}.", dir=str(path.parent)
        )
        temporary = Path(temporary_name)
        try:
            os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                descriptor = -1
                json.dump({"token": token}, stream, ensure_ascii=False, separators=(",", ":"))
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            if path.is_symlink():
                raise ValueError("Discord token store must not be a symlink")
            os.replace(temporary, path)
            path.chmod(0o600)
            try:
                directory_fd = os.open(path.parent, os.O_DIRECTORY)
            except OSError:
                directory_fd = None
            if directory_fd is not None:
                try:
                    os.fsync(directory_fd)
                finally:
                    os.close(directory_fd)
        finally:
            if descriptor >= 0:
                os.close(descriptor)
            temporary.unlink(missing_ok=True)
    except ValueError:
        raise
    except (OSError, UnicodeError) as exc:
        raise RuntimeError("Could not save Discord gateway credential") from exc


__all__ = [
    "DEFAULT_TOKEN_ENV",
    "DEFAULT_TOKEN_STORE",
    "MAX_TOKEN_LENGTH",
    "credential_configured",
    "normalize_token",
    "read_token",
    "token_env_name",
    "token_store_path",
    "write_token",
]
