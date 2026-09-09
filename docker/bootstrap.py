#!/usr/bin/env python3
"""Create only missing container state; never import host credentials."""

from __future__ import annotations

import json
import os
from pathlib import Path
import re
import secrets
import subprocess
import tempfile
from datetime import datetime, timezone


DATA = Path(os.environ.get("RELAY_DATA_DIR", "/data")).resolve()
APP_UID = 1001
APP_GID = 1001


def secure_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.chmod(0o700)
    try:
        os.chown(path, APP_UID, APP_GID)
    except PermissionError:
        pass


def atomic_write(path: Path, content: str, mode: int, uid: int | None = None, gid: int | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        os.fchmod(fd, mode)
        if uid is not None and gid is not None:
            os.fchown(fd, uid, gid)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(name, path)
        path.chmod(mode)
    finally:
        try:
            os.unlink(name)
        except FileNotFoundError:
            pass


def fresh_config(path: Path) -> None:
    source = Path("/app/config.example.json")
    config = json.loads(source.read_text(encoding="utf-8"))
    config["mode"] = "shadow"
    config["database"] = "/data/state/relay-shadow.sqlite3"
    config["kill_switch"] = "/data/state/STOP"
    config["runtime_status_file"] = "/data/state/runtime-status.json"
    config.setdefault("browser", {})["profile_dir"] = "/data/discord-browser"
    config.setdefault("llm", {})["executable"] = "/usr/local/bin/codex"
    broker = config.setdefault("robinhood", {})
    broker["account_number"] = None
    broker["enable_live_orders"] = False
    broker["token_store"] = "/data/state/robinhood-oauth.json"
    atomic_write(path, json.dumps(config, indent=2) + "\n", 0o600, APP_UID, APP_GID)


def dashboard_credentials() -> None:
    user = os.environ.get("DASHBOARD_USER", "relay")
    if not re.fullmatch(r"[A-Za-z0-9._-]{1,64}", user):
        raise SystemExit("DASHBOARD_USER contains unsupported characters")
    password = os.environ.get("DASHBOARD_PASSWORD", "")
    password_file = DATA / "dashboard.password"
    if not password:
        if password_file.exists():
            if password_file.is_symlink() or not password_file.is_file():
                raise SystemExit("/data/dashboard.password must be a regular file")
            password = password_file.read_text(encoding="utf-8").rstrip("\n")
            password_file.chmod(0o600)
            os.chown(password_file, 0, 0)
        else:
            password = secrets.token_urlsafe(24)
            atomic_write(password_file, password + "\n", 0o600, 0, 0)
    if not password:
        raise SystemExit("DASHBOARD_PASSWORD cannot be empty")
    result = subprocess.run(
        ["openssl", "passwd", "-apr1", "-stdin"],
        input=(password + "\n").encode(),
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        check=True,
    )
    digest = result.stdout.decode("ascii").strip()
    atomic_write(Path("/etc/nginx/.htpasswd"), f"{user}:{digest}\n", 0o640, 0, 33)


def main() -> None:
    if DATA.is_symlink() or not DATA.is_dir():
        raise SystemExit("RELAY_DATA_DIR must be a real directory")
    for name in ("state", "discord-browser", "codex", "home", "logs", "run"):
        secure_dir(DATA / name)
    config = DATA / "config.json"
    if config.is_symlink():
        raise SystemExit("/data/config.json must be a regular file")
    if not config.exists():
        fresh_config(config)
        status = {
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "state": "setup_required",
            "detail": "Configure Discord channels and bind a Robinhood account before read-only shadow monitoring.",
            "discord": {"state": "not_configured", "channels": []},
            "codex": {"state": "not_configured"},
            "broker": {"state": "unbound"},
        }
        atomic_write(DATA / "state/runtime-status.json", json.dumps(status, indent=2) + "\n", 0o660, 0, APP_GID)
    dashboard_credentials()


if __name__ == "__main__":
    main()
