"""Configuration isolation for read-only Codex authentication inspection."""

from contextlib import contextmanager
from pathlib import Path
import tempfile


@contextmanager
def isolated_auth_home(source_home):
    """Ignore inference configuration for login status, without copying tokens.

    Never use this home for login or logout: those commands unlink auth.json
    before writing new credentials. The temporary directory owns only the link
    and non-credential scratch files; the persistent file is left untouched.
    """
    credential = Path(source_home).resolve() / "auth.json"
    with tempfile.TemporaryDirectory(prefix="discord-relay-codex-auth-") as directory:
        home = Path(directory)
        (home / "auth.json").symlink_to(credential)
        yield home
