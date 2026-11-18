"""Configuration isolation for read-only Codex authentication inspection."""

from contextlib import contextmanager
import fcntl
import os
from pathlib import Path
import tempfile
import time


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


@contextmanager
def inference_auth_lock(source_home, timeout):
    """Serialize relay CLI runs that share one subscription credential file."""
    path = Path(source_home) / ".relay-inference.lock"
    fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        deadline = time.monotonic() + timeout
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise TimeoutError("Codex subscription session is busy") from None
                time.sleep(0.05)
        yield
    finally:
        os.close(fd)
