"""Bounded, local setup operations for the authenticated dashboard.

The setup service deliberately has a smaller surface than the relay itself.  It
can edit the two Discord channel definitions, start the two supported login
flows, select Shadow/Live, and create the worker control markers. It never
accepts a command, path, credential, or arbitrary execution setting.
"""

from __future__ import annotations

import asyncio
import copy
import http.client
import inspect
import json
import math
import os
import re
import shutil
import signal
import sqlite3
import subprocess
import tempfile
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

from .broker import SCHEMA_PINS, _OAuthStorage, login as broker_login
from .cli import inspect_broker
from .core import Hold, Store, instant, load_config, money
from .discord_session import (
    DEFAULT_TOKEN_ENV,
    DEFAULT_TOKEN_STORE,
    credential_configured,
    normalize_token,
    read_token,
    write_token,
)
from .interpreter import CodexInterpreter
from .status import failure_detail, is_auth_required_state


SNOWFLAKE = re.compile(r"\d{15,22}\Z")
ACCOUNT_NUMBER = re.compile(r"\d{5,20}\Z")
CHANNEL_URL = re.compile(r"/channels/(\d{15,22})/(\d{15,22})/?\Z")
SAFE_GROUP = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}\Z")
CODEX_DEVICE_HOSTS = {"auth.openai.com"}
CODEX_DEVICE_PATHS = {"/codex/device"}
AUTH_PROVIDERS = {"codex", "robinhood"}
PUBLIC_BROWSER_URL = "/browser/vnc.html?autoconnect=true&resize=scale&path=browser/websockify"
CODEX_AUTH_TIMEOUT_SECONDS = 900
PUBLIC_RISK_FIELDS = frozenset({
    "max_signal_age_seconds", "min_confidence", "entry_risk_min_fraction",
    "entry_risk_max_fraction", "fractional_kelly", "max_position_fraction",
    "max_total_exposure_fraction", "buying_power_reserve_fraction",
    "max_spread_fraction", "max_quote_age_seconds", "max_chase_fraction",
    "fee_reserve_per_contract", "allow_same_day_expiry",
    "duplicate_window_seconds", "max_pending_messages",
})

EVALUATION_MODES = frozenset({"codex", "jev_shadow", "jev"})
EVALUATION_DEFAULTS = {
    "mode": "codex",
    "direct_entries": True,
    "model": "jev-latest",
    "timeout_ms": 1200,
    "min_confidence": 0.95,
    "min_probability": 0.95,
    "min_eligibility": 0.98,
    "api_key_file": "state/typesafe.key",
}
EVALUATION_FIELDS = frozenset(EVALUATION_DEFAULTS)
EVALUATION_UPDATE_FIELDS = EVALUATION_FIELDS | {"typesafe_api_key"}


def _evaluation_number(value: object, name: str) -> float:
    if isinstance(value, bool):
        raise ValueError(f"{name} must be a number")
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be a number") from exc
    if not math.isfinite(number) or not 0 < number <= 1:
        raise ValueError(f"{name} must be greater than 0 and at most 1")
    return number


def _validated_evaluation_update(value: object, *, partial: bool = True) -> dict:
    if value is None and partial:
        return {}
    if not isinstance(value, dict):
        raise ValueError("evaluation settings must be an object")
    unknown = set(value) - EVALUATION_UPDATE_FIELDS
    if unknown:
        raise ValueError("Unsupported evaluation setting")
    result = {}
    if "mode" in value:
        if not isinstance(value["mode"], str) or value["mode"] not in EVALUATION_MODES:
            raise ValueError("Evaluation mode must be codex, jev_shadow, or jev")
        result["mode"] = value["mode"]
    if "direct_entries" in value:
        if type(value["direct_entries"]) is not bool:
            raise ValueError("direct_entries must be a boolean")
        result["direct_entries"] = value["direct_entries"]
    if "model" in value:
        model = value["model"]
        if not isinstance(model, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}", model):
            raise ValueError("JEV model must be a model identifier")
        result["model"] = model
    if "timeout_ms" in value:
        timeout = value["timeout_ms"]
        if type(timeout) is not int or not 1 <= timeout <= 1200:
            raise ValueError("JEV timeout must be between 1 and 1200 milliseconds")
        result["timeout_ms"] = timeout
    for name in ("min_confidence", "min_probability", "min_eligibility"):
        if name in value:
            result[name] = _evaluation_number(value[name], name)
    if "api_key_file" in value and value["api_key_file"] != EVALUATION_DEFAULTS["api_key_file"]:
        raise ValueError("TypeSafe API key file must be state/typesafe.key")
    if "api_key_file" in value:
        result["api_key_file"] = EVALUATION_DEFAULTS["api_key_file"]
    if "typesafe_api_key" in value:
        key = value["typesafe_api_key"]
        if not isinstance(key, str) or len(key) > 4096 or any(ord(char) < 32 or ord(char) == 127 for char in key):
            raise ValueError("TypeSafe API key must be a bounded single-line string")
        result["typesafe_api_key"] = key.strip()
    if not partial and set(result) - {"typesafe_api_key"} != EVALUATION_FIELDS:
        raise ValueError("Evaluation settings are incomplete")
    return result


def _evaluation_settings(raw: dict) -> dict:
    section = raw.get("evaluation", {}) if isinstance(raw, dict) else {}
    result = dict(EVALUATION_DEFAULTS)
    try:
        result.update({key: value for key, value in _validated_evaluation_update(section).items()
                       if key != "typesafe_api_key"})
    except ValueError:
        return result
    return result


class _AuthCancelled(RuntimeError):
    """The setup-owned provider coroutine was cancelled before completion."""


@dataclass
class _AuthJob:
    provider: str
    phase: str = "initialization"
    cancelled: threading.Event = field(default_factory=threading.Event)
    thread: threading.Thread | None = None
    process: subprocess.Popen[str] | None = None
    expects_device_code: bool = False
    schema_catalog: list = field(default_factory=list)
    failure_code: str | None = None
    timed_out: bool = False


class SetupManager:
    """Expose fixed setup actions while preserving the rest of the config.

    The manager is synchronous at its public boundary because the dashboard
    uses a synchronous HTTP server.  Authentication work runs in daemon
    threads, with each job retaining its own cancellation event and (for the
    Codex flow) its own process group.
    """

    def __init__(self, config_path: str | os.PathLike[str]):
        self.config_path = Path(config_path).expanduser().resolve()
        if self.config_path.exists() and not self.config_path.is_file():
            raise ValueError("setup configuration must be a regular file")

        self._lock = threading.RLock()
        self._closed = False
        self._jobs: dict[str, _AuthJob] = {}
        self._auth_state = {
            "codex": {
                "state": "not_connected",
                "detail": "Codex login is not configured.",
            },
            "robinhood": {
                "state": "not_connected",
                "detail": "Robinhood login is not configured.",
            },
        }
        self._codex_home = self._resolve_codex_home()
        self._codex_check = (None, 0.0, False)

    # ------------------------------------------------------------------
    # Public API

    def status(self) -> dict:
        """Return a small, credential-free setup projection."""

        with self._lock:
            return self._status_locked()

    def save_channels(self, payload: dict) -> dict:
        """Validate and atomically save only channels and poll settings."""

        with self._lock:
            self._ensure_open_locked()
            latest = self._read_raw_locked()
            channels, poll_seconds = self._validated_channel_update(payload, latest)

            candidate = copy.deepcopy(latest)
            candidate["channels"] = channels
            browser = candidate.get("browser")
            if not isinstance(browser, dict):
                browser = {}
                candidate["browser"] = browser
            browser["poll_seconds"] = poll_seconds

            # The candidate now has valid channel input.  Run the existing
            # validator when the rest of the private configuration permits it;
            # bootstrap files intentionally begin with placeholder guild IDs.
            self._validate_candidate(candidate)
            self._atomic_write_config_locked(candidate)
            return self._action_response_locked("channels_saved")

    def save_notifications(self, payload: dict) -> dict:
        """Keep the optional webhook token private; omission preserves its saved value."""
        from .notifications import validate_webhook_url
        if not isinstance(payload, dict) or set(payload) - {"enabled", "webhook_url"} or type(payload.get("enabled")) is not bool:
            raise ValueError("Notifications require enabled as a boolean and an optional webhook_url")
        with self._lock:
            self._ensure_open_locked()
            candidate = self._read_raw_locked()
            settings = candidate.get("notifications", {})
            url = settings.get("webhook_url", "") if isinstance(settings, dict) else ""
            if "webhook_url" in payload:
                if not isinstance(payload["webhook_url"], str):
                    raise ValueError("Webhook URL must be a string")
                url = payload["webhook_url"].strip()
            if url:
                url = validate_webhook_url(url)
            if payload["enabled"] and not url:
                raise ValueError("Add a Discord webhook URL before enabling notifications")
            candidate["notifications"] = {"enabled": payload["enabled"], "webhook_url": url}
            self._atomic_write_config_locked(candidate)
            return self._action_response_locked("notifications_saved")

    def save_discord(self, payload: dict) -> dict:
        """Save Discord transport settings and an optional gateway token.

        The token is deliberately written to the private token store before
        the response is built.  The response goes through the normal public
        status projection, which contains only a boolean credential marker.
        """

        if not isinstance(payload, dict):
            raise ValueError("Discord setup payload must be an object")
        unknown = set(payload) - {"transport", "token"}
        if unknown:
            raise ValueError("Unsupported Discord setup field")
        transport = payload.get("transport", "browser")
        if transport not in {"browser", "gateway"}:
            raise ValueError("Discord transport must be browser or gateway")
        token_supplied = "token" in payload
        token_value = ""
        if token_supplied:
            if not isinstance(payload["token"], str):
                raise ValueError("Discord gateway token must be a string")
            token_value = normalize_token(payload["token"], allow_empty=True)

        with self._lock:
            self._ensure_open_locked()
            latest = self._read_raw_locked()
            old_discord = self._discord_section(latest)
            old_transport = old_discord["transport"]
            old_config = self._resolved_discord_config(latest)
            old_token = read_token(old_config)

            candidate = copy.deepcopy(latest)
            candidate["discord"] = {**candidate.get("discord", {}),
                "transport": transport,
                "token_store": old_discord["token_store"],
                "token_env": old_discord["token_env"],
            }
            self._validate_candidate(candidate)

            token_changed = bool(token_value) and token_value != old_token
            if token_changed:
                write_token(self._resolved_discord_config(candidate), token_value)
            self._atomic_write_config_locked(candidate)

            # A changed transport or token must be observed by the worker.  A
            # blank token is intentionally a no-op so an accidental clear in
            # the form cannot remove the saved credential.
            if transport != old_transport or token_changed:
                self._request_reconnect_locked()
            return self._action_response_locked("discord_saved")

    def discover_discord(self, payload: dict) -> dict:
        """Ask the existing browser worker for visible servers, channels or authors."""
        from .discovery import request_discovery
        with self._lock:
            self._ensure_open_locked()
            raw = self._read_raw_locked()
            runtime_path = self._configured_path(raw, "runtime_status_file", self.config_path.parent / "state/runtime-status.json")
            request_discovery(runtime_path, payload)
            return self._action_response_locked("discovery_requested")

    def robinhood_schemas(self) -> dict:
        """Expose cached schema metadata, including after dashboard restarts."""
        with self._lock:
            job = self._jobs.get("robinhood")
            if job and job.schema_catalog:
                return {"tools": copy.deepcopy(job.schema_catalog)}
            raw = self._read_raw_locked(allow_missing=True)
            cache = self._configured_path(
                raw, "token_store", self.config_path.parent / "state" / "robinhood-oauth.json",
                section="robinhood",
            ).with_name("robinhood-schemas.json")
            try:
                if cache.is_symlink() or cache.stat().st_size > 2 * 1024 * 1024:
                    return {"tools": []}
                value = json.loads(cache.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                return {"tools": []}
            tools = value.get("tools") if isinstance(value, dict) else None
            if not isinstance(tools, list):
                return {"tools": []}
            return {"tools": [
                {key: copy.deepcopy(tool.get(key)) for key in ("name", "inputSchema", "outputSchema")}
                for tool in tools[:len(SCHEMA_PINS)]
                if isinstance(tool, dict) and tool.get("name") in SCHEMA_PINS
                and isinstance(tool.get("inputSchema"), dict) and isinstance(tool.get("outputSchema"), dict)
            ]}

    def complete_robinhood_callback(self, payload: dict) -> dict:
        """Forward a user-pasted callback to the active, state-bound local listener."""
        if (not isinstance(payload, dict) or set(payload) != {"callback_url"}
                or not isinstance(payload["callback_url"], str)
                or not 1 <= len(payload["callback_url"]) <= 8192):
            raise ValueError("Paste the complete returned Robinhood callback URL")
        supplied = payload["callback_url"].strip()
        with self._lock:
            self._ensure_open_locked()
            active = self._auth_state.get("robinhood", {})
            job = self._jobs.get("robinhood")
            if (active.get("state") != "waiting" or not active.get("authorization_url")
                    or job is None or job.cancelled.is_set()):
                raise RuntimeError("No Robinhood sign-in is waiting. Start sign-in again.")
            authorization_url = active["authorization_url"]
        try:
            expected_uris = parse_qs(urlsplit(authorization_url).query).get("redirect_uri", [])
            if len(expected_uris) != 1:
                raise ValueError
            expected, actual = urlsplit(expected_uris[0]), urlsplit(supplied)
            if (actual.scheme not in {"http", "https"}
                    or (actual.scheme, actual.netloc, actual.path) != (expected.scheme, expected.netloc, expected.path)
                    or actual.username is not None or actual.password is not None
                    or actual.path != "/callback" or not actual.query
                    or any(char in supplied for char in "#\\")
                    or any(char.isspace() or ord(char) < 32 or ord(char) == 127 for char in supplied)):
                raise ValueError
        except ValueError:
            raise ValueError(
                "Robinhood callback mismatch [callback_mismatch]. Action: paste the complete callback address from this sign-in attempt."
            ) from None
        # The destination is fixed: pasted URLs never control a network host.
        connection = http.client.HTTPConnection("127.0.0.1", 8766, timeout=5)
        try:
            connection.request("GET", "/callback?" + actual.query)
            response = connection.getresponse()
            status = response.status
            response.read(4096)
        except (OSError, http.client.HTTPException, UnicodeError):
            raise RuntimeError(
                "Robinhood callback listener is unavailable [callback_unreachable]. Action: start Robinhood sign-in again."
            ) from None
        finally:
            connection.close()
        if status != 200:
            raise ValueError(
                "Robinhood callback was rejected [auth_rejected]. Action: copy the complete address from this sign-in attempt."
            )
        if "error" in parse_qs(actual.query):
            raise ValueError(
                "Robinhood authorization was denied [auth_rejected]. Action: start sign-in again."
            )
        with self._lock:
            if self._jobs.get("robinhood") is job and job.phase == "callback":
                job.phase = "token_exchange"
                self._auth_state["robinhood"] = {"state": "waiting", "detail": "Callback received. Completing Robinhood authorization."}
            return self._action_response_locked("robinhood_callback_received")

    def start_auth(self, provider: str, payload: dict | None = None) -> dict:
        """Start one of the two fixed, bounded authentication jobs."""

        with self._lock:
            self._ensure_open_locked()
            if provider not in AUTH_PROVIDERS:
                raise ValueError("unsupported setup provider")
            if payload is None:
                payload = {}
            if not isinstance(payload, dict):
                raise ValueError("setup payload must be an object")

            if provider == "codex":
                if payload:
                    raise ValueError("Codex setup does not accept payload fields")
            else:
                account_number = self._account_number_from_payload(payload)
                bound = self._read_raw_locked().get("robinhood", {}).get("account_number")
                if account_number and bound and account_number != str(bound):
                    raise ValueError("This relay is already bound to another account; reconnect without changing the account number")

            old_job = self._jobs.get(provider)
            if old_job is not None and old_job.thread is not None and old_job.thread.is_alive():
                raise RuntimeError(f"{provider} setup is already running")

            job = _AuthJob(provider=provider)
            self._jobs[provider] = job
            self._auth_state[provider] = {
                "state": "starting",
                "detail": (
                    "Starting Codex device login."
                    if provider == "codex"
                    else "Starting Robinhood authorization."
                ),
            }
            target = self._run_codex_job if provider == "codex" else self._run_robinhood_job
            args = () if provider == "codex" else (account_number,)
            thread = threading.Thread(
                target=target,
                args=(job, *args),
                name=f"relay-setup-{provider}",
                daemon=True,
            )
            job.thread = thread
            thread.start()
            return self._action_response_locked(f"{provider}_auth_started")

    def cancel_auth(self, provider: str) -> dict:
        """Cancel only the manager-owned authentication job."""

        thread = None
        with self._lock:
            self._ensure_open_locked()
            if provider not in AUTH_PROVIDERS:
                raise ValueError("unsupported setup provider")
            job = self._jobs.get(provider)
            if self._auth_state[provider].get("state") == "connected":
                return self._action_response_locked(f"{provider}_auth_finished")
            if job is None or job.thread is None or not job.thread.is_alive():
                if self._auth_state[provider].get("state") == "failed":
                    return self._action_response_locked(f"{provider}_auth_finished")
                self._auth_state[provider] = {
                    "state": "cancelled",
                    "detail": f"{provider.title()} setup was cancelled.",
                }
            else:
                job.cancelled.set()
                if provider == "codex":
                    self._terminate_codex_process_locked(job)
                thread = job.thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(5.0)
        with self._lock:
            if thread is not None and thread.is_alive():
                # Do not claim cancellation while the provider coroutine may
                # still own an OAuth listener or be able to bind an account.
                self._auth_state[provider] = {
                    "state": "failed",
                    "detail": f"{provider.title()} setup is still stopping; retry cancellation.",
                }
            elif self._auth_state[provider].get("state") not in {"connected", "failed"}:
                self._auth_state[provider] = {
                    "state": "cancelled",
                    "detail": f"{provider.title()} setup was cancelled.",
                }
            return self._action_response_locked(f"{provider}_auth_cancelled")

    def set_paused(self, paused: bool) -> dict:
        """Create or remove the configured worker STOP marker."""

        if type(paused) is not bool:
            raise ValueError("paused must be boolean")
        with self._lock:
            self._ensure_open_locked()
            latest = self._read_raw_locked()
            stop_path = self._configured_path(latest, "kill_switch", self.config_path.parent / "state" / "STOP")
            if stop_path.is_symlink():
                raise RuntimeError("configured STOP path must not be a symlink")
            if paused:
                stop_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                fd = os.open(stop_path, os.O_WRONLY | os.O_CREAT, 0o600)
                try:
                    os.fchmod(fd, 0o600)
                finally:
                    os.close(fd)
            else:
                if self._public_trading(latest)["pending"]:
                    raise RuntimeError("Wait for the worker to load the selected mode before resuming.")
                if latest.get("mode") == "live":
                    self._require_live_ready_locked(latest)
                stop_path.unlink(missing_ok=True)
            return self._action_response_locked("paused" if paused else "resumed")

    def set_expiry_policy(self, payload: dict) -> dict:
        """Change the single same-day entry permission while execution is paused."""
        if not isinstance(payload, dict) or set(payload) != {"allow_same_day_expiry"} or type(payload["allow_same_day_expiry"]) is not bool:
            raise ValueError("allow_same_day_expiry must be a boolean")
        with self._lock:
            self._ensure_open_locked()
            latest = self._read_raw_locked()
            if not self._is_paused(latest):
                raise RuntimeError("Pause the relay before changing same-day entry permission.")
            if self._public_trading(latest)["pending"]:
                raise RuntimeError("Wait for the worker to load the previous settings change.")
            enabled = payload["allow_same_day_expiry"]
            if latest.get("risk", {}).get("allow_same_day_expiry") is enabled:
                return self._action_response_locked("expiry_policy_unchanged")
            candidate = copy.deepcopy(latest)
            candidate.setdefault("risk", {})["allow_same_day_expiry"] = enabled
            candidate["mode_change_id"] = uuid.uuid4().hex
            self._validate_candidate(candidate)
            self._atomic_write_config_locked(candidate)
            return self._action_response_locked("expiry_policy_saved")

    def save_evaluation(self, payload: dict) -> dict:
        """Save bounded interpreter preferences and chase tolerance while paused."""
        fields = {"model", "reasoning_effort", "service_tier", "max_chase_fraction"}
        if not isinstance(payload, dict) or set(payload) not in (fields, fields | {"evaluation"}):
            raise ValueError("Provide model, reasoning_effort, service_tier, and max_chase_fraction")
        model = payload["model"]
        if model is not None and (not isinstance(model, str) or (model and not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}", model))):
            raise ValueError("Model must be a model identifier or blank for the Codex default")
        if payload["reasoning_effort"] not in ("minimal", "low", "medium", "high", "xhigh"):
            raise ValueError("Unsupported reasoning effort")
        if payload["service_tier"] not in ("standard", "fast"):
            raise ValueError("Service tier must be standard or fast")
        chase = money(payload["max_chase_fraction"])
        if not 0 <= chase <= 1:
            raise ValueError("Chase tolerance must be between 0% and 100%")
        evaluation_payload = _validated_evaluation_update(payload.get("evaluation"), partial=True)
        typesafe_api_key = evaluation_payload.pop("typesafe_api_key", None)
        with self._lock:
            self._ensure_open_locked()
            latest = self._read_raw_locked()
            if not self._is_paused(latest):
                raise RuntimeError("Pause the relay before changing evaluation settings.")
            candidate = copy.deepcopy(latest)
            candidate.setdefault("llm", {}).update(model=model or None,
                reasoning_effort=payload["reasoning_effort"], service_tier=payload["service_tier"])
            candidate.setdefault("risk", {})["max_chase_fraction"] = str(chase)
            if "evaluation" in payload or isinstance(latest.get("evaluation"), dict):
                settings = _evaluation_settings(latest)
                settings.update(evaluation_payload)
                candidate["evaluation"] = settings
            changed = candidate != latest or bool(typesafe_api_key)
            if not changed:
                return self._action_response_locked("evaluation_unchanged")
            candidate["mode_change_id"] = uuid.uuid4().hex
            self._validate_candidate(candidate)
            if typesafe_api_key:
                self._write_typesafe_key_locked(typesafe_api_key)
            self._atomic_write_config_locked(candidate)
            return self._action_response_locked("evaluation_saved")

    def test_evaluation(self, payload: dict | None = None) -> dict:
        """Explicitly run the provider's synthetic, non-trading probe."""
        if payload not in (None, {}):
            raise ValueError("Synthetic evaluation test does not accept parameters")
        started = time.monotonic()
        try:
            from .evaluation import synthetic_test
            with self._lock:
                self._ensure_open_locked()
                raw = self._read_raw_locked()
            provider_config = copy.deepcopy(raw)
            provider_config["_config_dir"] = str(self.config_path.parent)
            result = asyncio.run(synthetic_test(provider_config))
            if not isinstance(result, dict):
                raise RuntimeError("provider returned an invalid synthetic result")
            state = result.get("state") if isinstance(result.get("state"), str) else "failed"
            detail = result.get("detail") if isinstance(result.get("detail"), str) else "Synthetic classification completed."
            latency = result.get("latency_ms")
            if not isinstance(latency, (int, float)) or isinstance(latency, bool) or not math.isfinite(latency) or latency < 0:
                latency = max(0.0, time.monotonic() - started) * 1000
            return {
                "state": state,
                "provider": "jev",
                "configured": self._typesafe_credential_configured(),
                "synthetic": True,
                "latency_ms": round(float(latency), 3),
                "detail": detail[:400],
            }
        except Exception:
            return {
                "state": "failed",
                "provider": "jev",
                "configured": self._typesafe_credential_configured(),
                "synthetic": True,
                "latency_ms": round(max(0.0, time.monotonic() - started) * 1000, 3),
                "detail": "Synthetic classification failed.",
            }

    def set_mode(self, payload: dict) -> dict:
        """Select a mode and its own ledger; activation remains a separate Resume."""
        if (not isinstance(payload, dict) or set(payload) - {"mode", "confirm_live"}
                or not isinstance(payload.get("mode"), str) or payload["mode"] not in {"shadow", "live"}
                or ("confirm_live" in payload and type(payload["confirm_live"]) is not bool)):
            raise ValueError("Choose shadow or live; only mode and confirm_live are accepted.")
        mode = payload["mode"]
        if mode == "live" and payload.get("confirm_live") is not True:
            raise ValueError("Confirm that Live can submit real orders before selecting it.")
        with self._lock:
            self._ensure_open_locked()
            latest = self._read_raw_locked()
            if not self._is_paused(latest):
                raise RuntimeError("Pause the relay before changing trading mode.")
            if any(job.thread and job.thread.is_alive() for job in self._jobs.values()):
                raise RuntimeError("Finish or cancel the active sign-in before changing mode.")
            current = latest.get("mode")
            if current not in {"paper", "shadow", "live"}:
                raise RuntimeError("The current trading mode is invalid.")
            if current == mode and latest.get("robinhood", {}).get("enable_live_orders") is (mode == "live"):
                return self._action_response_locked("mode_unchanged")
            if self._public_trading(latest)["pending"] and not (current == "live" and mode == "shadow"):
                raise RuntimeError("Wait for the worker to load the previous mode change.")
            if mode == "live":
                self._require_live_ready_locked(latest)

            candidate = copy.deepcopy(latest)
            databases = candidate.setdefault("mode_databases", {})
            if not isinstance(databases, dict) or set(databases) - {"paper", "shadow", "live"}:
                raise RuntimeError("Saved trading ledgers are invalid.")
            databases[current] = latest.get("database")
            databases.setdefault(mode, f"state/relay-{mode}.sqlite3")
            paths = {key: self._mode_database_path(value) for key, value in databases.items()}
            for key, path in paths.items():
                for other, other_path in paths.items():
                    if key != other and (path == other_path or
                            (path.exists() and other_path.exists() and path.samefile(other_path))):
                        raise RuntimeError("Each trading mode must use a separate ledger.")
            account = latest.get("robinhood", {}).get("account_number")
            self._check_mode_ledger(paths[current], current, account, leaving_live=current == "live")
            self._check_mode_ledger(paths[mode], mode, account)
            candidate["mode"] = mode
            candidate.setdefault("robinhood", {})["enable_live_orders"] = mode == "live"
            candidate["database"] = databases[mode]
            candidate["mode_change_id"] = uuid.uuid4().hex
            self._validate_candidate(candidate)
            self._prepare_mode_ledger(paths[mode], mode, account)
            self._atomic_write_config_locked(candidate)
            return self._action_response_locked("mode_saved")

    def _require_live_ready_locked(self, raw: dict) -> None:
        evaluator_ready = self._evaluation_ready_locked(raw)
        if (not self._public_channels(raw)[1]
                or not evaluator_ready
                or self._public_robinhood_locked(raw)["state"] != "connected"
                or self._public_discord(raw)["state"] != "connected"):
            raise RuntimeError("Connect Discord, the selected evaluator and Robinhood and save both channels before using Live.")

    def _evaluation_ready_locked(self, raw: dict) -> bool:
        if self._public_codex_locked()["state"] == "connected":
            return True
        settings = _evaluation_settings(raw)
        return settings["mode"] == "jev" and self._typesafe_credential_configured()

    def _mode_database_path(self, value: str) -> Path:
        if not isinstance(value, str) or not value.strip():
            raise RuntimeError("A saved trading ledger path is missing.")
        path = self.config_path.parent / Path(value).expanduser()
        if any(part.is_symlink() for part in (path, *path.parents)):
            raise RuntimeError("Trading ledger paths must not contain symlinks.")
        path = path.resolve()
        if path.exists() and not path.is_file():
            raise RuntimeError("A trading ledger must be a regular file.")
        return path

    @staticmethod
    def _prepare_mode_ledger(path: Path, mode: str, account: str | None) -> None:
        """Publish a new, bound ledger without modifying or replacing an existing one."""
        if path.exists():
            return
        temporary = None
        try:
            path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            fd, name = tempfile.mkstemp(prefix=".mode-ledger-", suffix=".sqlite3", dir=path.parent)
            os.close(fd)
            temporary = Path(name)
            writer = Store(temporary)
            try:
                writer.bind_execution(mode, account)
            finally:
                writer.close()
            try:
                os.link(temporary, path)
            except FileExistsError:
                SetupManager._check_mode_ledger(path, mode, account)
        except (OSError, sqlite3.Error, Hold):
            raise RuntimeError("The selected trading ledger could not be prepared; no mode change was saved.") from None
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
                temporary.with_suffix(temporary.suffix + ".lock").unlink(missing_ok=True)

    @staticmethod
    def _check_mode_ledger(path: Path, mode: str, account: str | None, *, leaving_live=False) -> None:
        if not path.exists():
            if leaving_live:
                raise RuntimeError("The Live ledger is unavailable; its positions and orders cannot be checked.")
            return
        reader = None
        try:
            # One read snapshot prevents a fill between the order and position checks.
            reader = Store(path, read_only=True)
            reader.db.execute("BEGIN")
            row = reader.db.execute("SELECT value FROM metadata WHERE key='execution_binding'").fetchone()
            expected = json.dumps({"mode": mode, "account": None if mode == "paper" else account}, sort_keys=True)
            if row and row[0] != expected:
                raise RuntimeError("A trading ledger belongs to another mode or Robinhood account.")
            positions = reader.db.execute("SELECT COUNT(*) FROM positions WHERE quantity != 0").fetchone()[0]
            if not row and (positions or reader.db.execute("SELECT COUNT(*) FROM orders").fetchone()[0]):
                raise RuntimeError("An unbound trading ledger contains prior positions or orders.")
            if leaving_live and (positions or reader.unresolved()):
                raise RuntimeError("Live has open relay positions or unresolved orders. Keep Live selected; Pause stops new actions.")
        except (OSError, sqlite3.Error):
            raise RuntimeError("A trading ledger could not be checked; no mode change was saved.") from None
        finally:
            if reader is not None:
                reader.close()

    def _public_trading(self, raw: dict) -> dict:
        runtime = self._read_runtime(raw)
        try:
            age = (datetime.now(timezone.utc) - instant(runtime.get("heartbeat_at"))).total_seconds()
            available = -5 <= age <= 30 and runtime.get("state") not in {"error", "stopped"}
        except Hold:
            available = False
        worker_mode = runtime.get("mode")
        if not isinstance(worker_mode, str) or worker_mode not in {"paper", "shadow", "live"} or not available:
            worker_mode = None
        mode = raw.get("mode")
        enabled = raw.get("robinhood", {}).get("enable_live_orders") is True
        pending = bool(raw.get("mode_change_id") and (
            worker_mode != mode or runtime.get("mode_change_id") != raw["mode_change_id"]
            or runtime.get("live_enabled") is not enabled))
        return {"mode": mode, "live_enabled": enabled, "worker_mode": worker_mode, "pending": pending}

    def reconnect(self) -> dict:
        """Touch the worker's fixed state/RECONNECT marker."""

        with self._lock:
            self._ensure_open_locked()
            latest = self._read_raw_locked()
            reconnect_path = self.config_path.parent / "state" / "RECONNECT"
            reconnect_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            if reconnect_path.is_symlink():
                raise RuntimeError("RECONNECT path must not be a symlink")
            reconnect_path.touch(mode=0o600, exist_ok=True)
            reconnect_path.chmod(0o600)
            # Reading the latest configuration above is intentional: it keeps
            # reconnect a serialized operation with config writes.
            del latest
            return self._action_response_locked("reconnect_requested")

    def close(self) -> dict:
        """Stop only authentication jobs started by this manager instance."""

        jobs: list[_AuthJob]
        with self._lock:
            if self._closed:
                return self._status_locked()
            self._closed = True
            jobs = list(self._jobs.values())
            for job in jobs:
                job.cancelled.set()
                if job.provider == "codex":
                    self._terminate_codex_process_locked(job)

        # A Robinhood worker may be inside an async provider call.  Do not
        # attempt process-wide cancellation; daemon threads let the interpreter
        # finish without disturbing the relay or its Codex interpreter.
        deadline = time.monotonic() + 5.0
        for job in jobs:
            thread = job.thread
            if thread is None or thread is threading.current_thread():
                continue
            remaining = max(0.0, deadline - time.monotonic())
            thread.join(remaining)
        with self._lock:
            for job in jobs:
                if job.thread is not None and not job.thread.is_alive():
                    self._auth_state[job.provider] = {
                        "state": "cancelled",
                        "detail": f"{job.provider.title()} setup was cancelled.",
                    }
            return self._status_locked()

    # ------------------------------------------------------------------
    # Public status projection

    def _status_locked(self) -> dict:
        from .notifications import notification_status
        raw = self._read_raw_locked(allow_missing=True)
        channels, channels_valid, poll_seconds = self._public_channels(raw)
        paused = self._is_paused(raw)
        codex = self._public_codex_locked()
        robinhood = self._public_robinhood_locked(raw)
        discord = self._public_discord(raw)
        risk = self._public_risk(raw)
        configured = bool(channels_valid and self._evaluation_ready_locked(raw)
                          and robinhood["state"] == "connected")
        return {
            "channels": channels,
            "poll_seconds": poll_seconds,
            "configured": configured,
            "paused": paused,
            "codex": codex,
            "robinhood": robinhood,
            "discord": discord,
            "risk": risk,
            "evaluation": self._public_evaluation(raw),
            "trading": self._public_trading(raw),
            "notifications": notification_status(self.config_path),
        }

    def _public_evaluation(self, raw: dict) -> dict:
        llm = raw.get("llm", {})
        if not isinstance(llm, dict):
            llm = {}
        model = llm.get("model")
        if not isinstance(model, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}", model):
            model = None
        effort = llm.get("reasoning_effort", "medium")
        tier = llm.get("service_tier", "standard")
        risk = raw.get("risk", {})
        if not isinstance(risk, dict):
            risk = {}
        settings = _evaluation_settings(raw)
        configured = self._typesafe_credential_configured()
        runtime = self._read_runtime(raw)
        runtime_evaluation = runtime.get("jev", runtime.get("evaluation", {})) if isinstance(runtime, dict) else {}
        if not isinstance(runtime_evaluation, dict):
            runtime_evaluation = {}
        if runtime_evaluation.get("state"):
            state = runtime_evaluation.get("state")
            detail = runtime_evaluation.get("detail")
        elif settings["mode"] == "codex":
            state, detail = "disabled", "JEV is disabled; Codex handles evaluation."
        elif not configured:
            state, detail = "needs_attention", "TypeSafe credential is not configured."
        else:
            state, detail = "unknown", "JEV runtime status is unavailable."
        if not isinstance(state, str) or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,64}", state):
            state = "unknown"
        if not isinstance(detail, str) or not 1 <= len(detail) <= 400 or any(ord(char) < 32 or ord(char) == 127 for char in detail):
            detail = "JEV runtime status is unavailable."
        status = {"state": state, "detail": detail}
        jev = {key: value for key, value in settings.items() if key != "api_key_file"}
        jev.update(configured=configured, credential_configured=configured, state=state, detail=detail, status=status)
        return {"model": model,
                "reasoning_effort": effort if effort in ("minimal", "low", "medium", "high", "xhigh") else "medium",
                "service_tier": tier if tier in ("standard", "fast") else "standard",
                "max_chase_fraction": risk.get("max_chase_fraction", "0.10"),
                "direct_entries": settings["direct_entries"],
                "mode": settings["mode"], "configured": configured, "status": status, "jev": jev}

    def _action_response_locked(self, action: str) -> dict:
        value = self._status_locked()
        value["accepted"] = action
        return value

    def _public_channels(self, raw: dict) -> tuple[list[dict], bool, int]:
        channels = raw.get("channels", []) if isinstance(raw, dict) else []
        browser = raw.get("browser", {}) if isinstance(raw, dict) else {}
        poll = browser.get("poll_seconds", 3) if isinstance(browser, dict) else 3
        if type(poll) is not int or not 2 <= poll <= 60:
            poll = 3
        public: list[dict] = []
        valid = isinstance(channels, list) and len(channels) == 2
        if not isinstance(channels, list):
            channels = []
        for index, channel in enumerate(channels[:2]):
            if not isinstance(channel, dict):
                channel = {}
            channel_id = str(channel.get("id", ""))
            guild_id = str(channel.get("guild_id", ""))
            authors = channel.get("authors", [])
            authors_valid = isinstance(authors, list) and all(SNOWFLAKE.fullmatch(str(author)) for author in authors)
            if not isinstance(authors, list):
                authors = []
            authors = [str(author) for author in authors if SNOWFLAKE.fullmatch(str(author))]
            channel_url = (
                f"https://discord.com/channels/{guild_id}/{channel_id}"
                if SNOWFLAKE.fullmatch(guild_id) and SNOWFLAKE.fullmatch(channel_id)
                else ""
            )
            row = {
                "url": channel_url,
                "name": self._safe_text(channel.get("name"), f"Channel {index + 1}"),
                "role": channel.get("role") if channel.get("role") in {"signals", "context"} else "",
                "authors": authors,
            }
            public.append(row)
            valid = bool(
                valid
                and channel_url
                and row["role"]
                and authors_valid
                and len(set(authors)) == len(authors)
            )
        while len(public) < 2:
            public.append({"url": "", "name": f"Channel {len(public) + 1}", "role": "", "authors": []})
            valid = False
        return public, valid, poll

    @staticmethod
    def _runtime_is_failed(runtime: object) -> bool:
        return isinstance(runtime, dict) and str(runtime.get("state", "")).lower() in {
            "unavailable", "error", "failed", "needs_attention", "schema_incompatible"
        }

    @staticmethod
    def _runtime_auth_detail(runtime: dict, provider: str, fallback: str) -> str:
        """Reuse the dashboard's bounded, credential-safe runtime projection."""
        from .dashboard import _safe_detail
        detail = runtime.get("detail")
        if not isinstance(detail, str):
            return fallback
        detail = detail.strip()
        if (
            not 1 <= len(detail) <= 400
            or not (detail.startswith(provider + " ")
                    or (provider == "Codex" and detail.startswith("evaluation failed:")))
            or "http://" in detail.lower()
            or "https://" in detail.lower()
            or any(ord(char) < 32 or ord(char) == 127 for char in detail)
        ):
            return fallback
        detail = _safe_detail(detail)
        return detail if detail and detail != "details withheld" else fallback

    def _public_codex_locked(self) -> dict:
        state = copy.deepcopy(self._auth_state["codex"])
        if state["state"] in {"starting", "waiting", "failed", "cancelled"}:
            return self._safe_auth_projection(state)
        runtime = self._read_runtime(self._read_raw_locked(allow_missing=True)).get("codex", {})
        if isinstance(runtime, dict) and is_auth_required_state(runtime.get("state")):
            return {"state": "auth_required", "detail": self._runtime_auth_detail(
                runtime,
                "Codex",
                "Codex needs ChatGPT reauthentication [auth_required]. Action: Use Codex sign-in below.",
            )}
        if self._runtime_is_failed(runtime):
            return {"state": "failed", "detail": self._runtime_auth_detail(
                runtime,
                "Codex",
                "Codex runtime is unavailable [runtime_unavailable]. Check worker diagnostics and provider availability; reconnect if authentication is required.",
            )}
        if self._codex_ready():
            state = {"state": "connected", "detail": "Codex ChatGPT login is available locally."}
        else:
            state = {"state": "not_connected", "detail": "Connect Codex with your ChatGPT subscription."}
        return self._safe_auth_projection(state)

    def _codex_ready(self) -> bool:
        """Cache official local login inspection; never inspect credential contents."""
        try:
            stat = (self._codex_home / "auth.json").stat()
            signature = (stat.st_mtime_ns, stat.st_size)
        except OSError:
            return False
        previous, checked_at, ready = self._codex_check
        if signature == previous and time.monotonic() - checked_at < 15:
            return ready
        try:
            raw = self._read_raw_locked()
            config = raw | {"llm": dict(raw.get("llm", {}), executable=self._codex_executable(), timeout_seconds=5)}
            status = asyncio.run(CodexInterpreter(config).subscription_status())
            ready = status.get("authenticated") is True and status.get("isolated_execution_available") is True
        except Exception:
            ready = False
        self._codex_check = (signature, time.monotonic(), ready)
        return ready

    def _public_robinhood_locked(self, raw: dict) -> dict:
        config = raw.get("robinhood", {}) if isinstance(raw, dict) else {}
        if not isinstance(config, dict):
            config = {}
        account_number = config.get("account_number")
        account_number = account_number if isinstance(account_number, str) and ACCOUNT_NUMBER.fullmatch(account_number) else None
        token_store = self._configured_path(
            raw,
            "token_store",
            self.config_path.parent / "state" / "robinhood-oauth.json",
            section="robinhood",
        )
        token_ready = self._token_store_ready(token_store)
        if config.get("auth") == "token":
            env_name = config.get("access_token_env", "ROBINHOOD_ACCESS_TOKEN")
            token_ready = token_ready or (isinstance(env_name, str) and bool(os.environ.get(env_name)))

        state = copy.deepcopy(self._auth_state["robinhood"])
        job = self._jobs.get("robinhood")
        if job is not None and job.thread is not None and job.thread.is_alive():
            state.setdefault("state", "starting")
        if state["state"] in {"starting", "waiting", "failed", "cancelled"}:
            pass
        elif account_number and (token_ready or state.get("state") == "connected"):
            state = {"state": "connected", "detail": "Robinhood account authorization is available locally."}
        elif job is None or job.thread is None or not job.thread.is_alive():
            if state.get("state") == "connected":
                state = {"state": "not_connected", "detail": "Robinhood login is not configured."}

        runtime = self._read_runtime(raw).get("broker", {})
        if state["state"] not in {"starting", "waiting", "failed", "cancelled"} and isinstance(runtime, dict) and is_auth_required_state(runtime.get("state")):
            state = {"state": "auth_required", "detail": self._runtime_auth_detail(
                runtime,
                "Robinhood",
                "Robinhood needs reauthentication [auth_required]. Action: Use Robinhood sign-in below.",
            )}
        elif state["state"] not in {"starting", "waiting", "failed", "cancelled"} and self._runtime_is_failed(runtime):
            state = {"state": "failed", "detail": self._runtime_auth_detail(
                runtime,
                "Robinhood",
                "Robinhood runtime is unavailable [runtime_unavailable]. Check worker diagnostics and provider availability; reconnect if authentication is required.",
            )}
        result = self._safe_auth_projection(state)
        result["token_present"] = token_ready
        if account_number:
            result["last_four"] = account_number[-4:]
        report = self._read_safe_report(raw)
        if report is not None:
            result["account"] = report
        return result

    @staticmethod
    def _token_store_ready(path: Path) -> bool:
        """Validate the local OAuth token through the broker's token store."""

        if not path.is_file() or path.is_symlink():
            return False

        async def read_tokens():
            return await _OAuthStorage(path).get_tokens()

        result: list[object] = []

        def read_in_thread() -> None:
            try:
                result.append(asyncio.run(read_tokens()))
            except Exception:
                result.append(None)

        try:
            asyncio.get_running_loop()
        except RuntimeError:
            read_in_thread()
        else:
            thread = threading.Thread(target=read_in_thread, name="relay-token-check", daemon=True)
            thread.start()
            thread.join(2.0)
        return bool(result and result[0] is not None)

    @staticmethod
    def _discord_section(raw: dict) -> dict:
        value = raw.get("discord", {}) if isinstance(raw, dict) else {}
        if not isinstance(value, dict):
            value = {}
        transport = value.get("transport", "browser")
        if transport not in {"browser", "gateway"}:
            transport = "browser"
        token_store = value.get("token_store", DEFAULT_TOKEN_STORE)
        if not isinstance(token_store, str) or not token_store.strip():
            token_store = DEFAULT_TOKEN_STORE
        token_env = value.get("token_env", DEFAULT_TOKEN_ENV)
        if not isinstance(token_env, str) or not token_env.strip():
            token_env = DEFAULT_TOKEN_ENV
        return {
            "transport": transport,
            "token_store": token_store,
            "token_env": token_env,
        }

    def _resolved_discord_config(self, raw: dict) -> dict:
        section = self._discord_section(raw)
        store = Path(section["token_store"]).expanduser()
        if not store.is_absolute():
            store = self.config_path.parent / store
        # abspath normalizes the caller-relative path while preserving a final
        # symlink so discord_session can reject it safely.
        section["token_store"] = os.path.abspath(str(store))
        return {"discord": section}

    def _request_reconnect_locked(self) -> None:
        reconnect_path = self.config_path.parent / "state" / "RECONNECT"
        reconnect_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if reconnect_path.is_symlink():
            raise RuntimeError("RECONNECT path must not be a symlink")
        reconnect_path.touch(mode=0o600, exist_ok=True)
        reconnect_path.chmod(0o600)

    @staticmethod
    def _safe_discord_runtime_detail(value: object, fallback: str) -> str:
        if not isinstance(value, str):
            return fallback
        detail = value.strip()
        if not 1 <= len(detail) <= 400:
            return fallback
        if any(ord(char) < 32 or ord(char) == 127 for char in detail):
            return fallback
        lowered = detail.lower()
        if "http://" in lowered or "https://" in lowered:
            return fallback
        if any(marker in lowered for marker in ("token=", "token:", "authorization:", "password=")):
            return fallback
        return detail

    def _public_discord(self, raw: dict) -> dict:
        section = self._discord_section(raw)
        if section["transport"] == "gateway":
            from .dashboard import _safe_runtime
            from .discovery import discovery_status

            runtime_path = self._configured_path(
                raw,
                "runtime_status_file",
                self.config_path.parent / "state" / "runtime-status.json",
            )
            runtime = _safe_runtime(raw, runtime_path)
            runtime_discord = runtime.get("discord", {}) if isinstance(runtime, dict) else {}
            if not isinstance(runtime_discord, dict):
                runtime_discord = {}
            credential = False
            try:
                credential = credential_configured(self._resolved_discord_config(raw))
            except (OSError, RuntimeError, ValueError, TypeError):
                credential = False
            source_state = str(runtime_discord.get("state", "not_connected"))
            if runtime.get("stale"):
                source_state = "unavailable"
            mapped = {
                "connected": "connected",
                "ready": "connected",
                "starting": "starting",
                "connecting": "starting",
                "reconnecting": "starting",
                "login_required": "not_connected",
                "not_configured": "not_connected",
                "needs_attention": "failed",
                "error": "failed",
                "failed": "failed",
                "unavailable": "unknown",
            }.get(source_state, "not_connected")
            if not credential:
                mapped = "not_connected"
                detail = (
                    "Discord gateway token is not configured [gateway_token_missing]. "
                    "Save a token or select Browser."
                )
            elif mapped == "connected":
                detail = self._safe_discord_runtime_detail(runtime_discord.get("detail"), "Discord gateway is connected.")
            elif mapped == "starting":
                detail = self._safe_discord_runtime_detail(runtime_discord.get("detail"), "Discord gateway worker is reconnecting.")
            elif mapped == "failed":
                detail = self._safe_discord_runtime_detail(
                    runtime_discord.get("detail"),
                    "Discord gateway worker needs attention.",
                )
            elif mapped == "unknown":
                detail = "Discord gateway status is unavailable. Reconnect to retry."
            else:
                detail = self._safe_discord_runtime_detail(
                    runtime_discord.get("detail"),
                    "Discord gateway is configured; reconnect to start it.",
                )
            return {
                "state": mapped,
                "detail": detail,
                "transport": "gateway",
                "credential_configured": credential,
                "discovery": discovery_status(runtime_path),
            }

        from .dashboard import _safe_runtime
        from .discovery import discovery_status
        runtime_path = self._configured_path(raw, "runtime_status_file", self.config_path.parent / "state/runtime-status.json")
        runtime = _safe_runtime(raw, runtime_path)
        section = runtime.get("discord") if isinstance(runtime, dict) else None
        if not isinstance(section, dict):
            section = {}
        source_state = str(section.get("state", "not_connected"))
        if runtime["stale"]:
            source_state = "unavailable"
        mapped = {
            "connected": "connected",
            "ready": "connected",
            "starting": "starting",
            "connecting": "starting",
            "reconnecting": "starting",
            "login_required": "not_connected",
            "needs_attention": "failed",
            "error": "failed",
            "not_configured": "not_connected",
            "unavailable": "unknown",
        }.get(source_state, "not_connected")
        details = {
            "connected": "Discord is signed in. You can return to Setup.",
            "starting": "Discord browser worker is reconnecting.",
            "not_connected": "Complete Discord sign-in and any verification in the browser.",
            "failed": "Discord browser worker needs attention.",
            "unknown": "Discord login status is unavailable. Wait for the browser worker to reconnect.",
        }
        detail = details[mapped]
        if not runtime["stale"] and mapped != "connected":
            detail = section.get("detail") or detail
        if not runtime["stale"] and runtime.get("state") == "error":
            mapped = "failed"
            detail = runtime.get("detail") or "Discord browser worker stopped. Use Reconnect to retry the saved session."
        if mapped == "connected":
            _, channels_valid, _ = self._public_channels(raw)
            if not channels_valid:
                detail += " Save both channel URLs to start monitoring."
            elif not section.get("channels"):
                detail += " Waiting for channel monitoring to start."
        credential = False
        try:
            credential = credential_configured(self._resolved_discord_config(raw))
        except (OSError, RuntimeError, ValueError, TypeError):
            credential = False
        return {
            "state": mapped,
            "detail": detail,
            "transport": "browser",
            "credential_configured": credential,
            "browser_url": PUBLIC_BROWSER_URL,
            "discovery": discovery_status(runtime_path),
        }

    def _public_risk(self, raw: dict) -> dict:
        risk = raw.get("risk", {}) if isinstance(raw, dict) else {}
        if not isinstance(risk, dict):
            return {}
        result: dict = {}
        for key, value in risk.items():
            if key not in PUBLIC_RISK_FIELDS:
                continue
            if isinstance(value, (str, int, float, bool)) or value is None:
                result[key] = value
        return result

    # ------------------------------------------------------------------
    # Configuration and validation

    def _validated_channel_update(self, payload: dict, latest: dict) -> tuple[list[dict], int]:
        if not isinstance(payload, dict):
            raise ValueError("channels payload must be an object")
        unknown = set(payload) - {"channels", "poll_seconds"}
        if unknown:
            raise ValueError("unsupported channel setup field")
        incoming = payload.get("channels")
        if not isinstance(incoming, list) or len(incoming) != 2:
            raise ValueError("configure exactly two Discord channels")
        poll = payload.get("poll_seconds", (latest.get("browser", {}) or {}).get("poll_seconds", 3))
        if type(poll) is not int or not 2 <= poll <= 60:
            raise ValueError("poll_seconds must be an integer from 2 through 60")

        existing = latest.get("channels", [])
        if not isinstance(existing, list):
            existing = []
        result: list[dict] = []
        seen: set[str] = set()
        for index, value in enumerate(incoming):
            if not isinstance(value, dict):
                raise ValueError("each channel must be an object")
            if set(value) - {"url", "name", "role", "authors"}:
                raise ValueError("unsupported channel field")
            url = value.get("url")
            guild_id, channel_id = self._parse_channel_url(url)
            if channel_id in seen:
                raise ValueError("Discord channel URLs must be distinct")
            seen.add(channel_id)
            role = value.get("role")
            if role not in {"signals", "context"}:
                raise ValueError("channel role must be signals or context")
            authors = value.get("authors", [])
            if not isinstance(authors, list) or len(authors) > 100:
                raise ValueError("authors must be a list of at most 100 Discord IDs; leave it empty for all authors")
            clean_authors: list[str] = []
            for author in authors:
                if not isinstance(author, str) or not SNOWFLAKE.fullmatch(author):
                    raise ValueError("trusted authors must be Discord snowflakes")
                if author in clean_authors:
                    raise ValueError("trusted author IDs must be distinct")
                clean_authors.append(author)
            name = value.get("name", "")
            if name is None:
                name = ""
            if not isinstance(name, str) or len(name) > 120 or any(ord(char) < 32 for char in name):
                raise ValueError("channel name is invalid")

            old = self._matching_channel(existing, index, guild_id, channel_id)
            source_group = self._source_group(old, name, role)
            channel = {
                "id": channel_id,
                "guild_id": guild_id,
                "role": role,
                "authors": clean_authors,
                "source_group": source_group,
            }
            if name.strip():
                channel["name"] = name.strip()
            elif isinstance(old, dict) and isinstance(old.get("name"), str) and old["name"].strip():
                channel["name"] = old["name"].strip()
            result.append(channel)
        return result, poll

    @staticmethod
    def _parse_channel_url(value: object) -> tuple[str, str]:
        if not isinstance(value, str) or len(value) > 300:
            raise ValueError("Discord channel URL is invalid")
        try:
            parsed = urlsplit(value)
        except ValueError as exc:
            raise ValueError("Discord channel URL is invalid") from exc
        if (
            parsed.scheme != "https"
            or parsed.hostname != "discord.com"
            or parsed.username
            or parsed.password
            or parsed.port is not None
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("Discord channel URL must be an HTTPS discord.com URL")
        match = CHANNEL_URL.fullmatch(parsed.path)
        if not match:
            raise ValueError("Discord channel URL must identify a guild and channel")
        return match.group(1), match.group(2)

    @staticmethod
    def _matching_channel(existing: list, index: int, guild_id: str, channel_id: str) -> dict | None:
        for value in existing:
            if isinstance(value, dict) and str(value.get("id")) == channel_id and str(value.get("guild_id")) == guild_id:
                return value
        if index < len(existing) and isinstance(existing[index], dict):
            return existing[index]
        return None

    @staticmethod
    def _source_group(old: dict | None, name: str, role: str) -> str:
        if isinstance(old, dict) and isinstance(old.get("source_group"), str) and SAFE_GROUP.fullmatch(old["source_group"]):
            return old["source_group"]
        candidate = re.sub(r"[^A-Za-z0-9._-]+", "-", name.strip()).strip("-._")
        if not SAFE_GROUP.fullmatch(candidate):
            candidate = role
        return candidate[:64]

    @staticmethod
    def _account_number_from_payload(payload: dict) -> str | None:
        if set(payload) - {"account_number"}:
            raise ValueError("unsupported Robinhood setup field")
        value = payload.get("account_number")
        if value is None or value == "":
            return None
        if not isinstance(value, str) or not re.fullmatch(r"\d{5,20}\Z", value):
            raise ValueError("account_number must contain 5 to 20 digits")
        return value

    def _validate_candidate(self, candidate: dict) -> None:
        # Validate through the existing core rules without allowing the
        # validator to rewrite the caller's in-memory candidate.
        fd, temporary_name = tempfile.mkstemp(prefix=".setup-validate-", dir=self.config_path.parent)
        temporary = Path(temporary_name)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump(candidate, stream)
                stream.flush()
                os.fsync(stream.fileno())
            try:
                load_config(temporary, allow_unbound=True)
            except (Hold, KeyError, TypeError, ValueError) as exc:
                raise ValueError(str(exc)) from exc
        except OSError as exc:
            raise RuntimeError("could not validate setup configuration") from exc
        finally:
            temporary.unlink(missing_ok=True)

    def _read_raw_locked(self, *, allow_missing: bool = False) -> dict:
        try:
            text = self.config_path.read_text(encoding="utf-8")
        except FileNotFoundError:
            if allow_missing:
                return {}
            raise
        except OSError as exc:
            raise RuntimeError("setup configuration is unavailable") from exc
        try:
            value = json.loads(text)
        except (TypeError, ValueError) as exc:
            raise RuntimeError("setup configuration is invalid") from exc
        if not isinstance(value, dict):
            raise RuntimeError("setup configuration must be an object")
        return value

    def _atomic_write_config_locked(self, value: dict) -> None:
        self.config_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd, temporary_name = tempfile.mkstemp(prefix=f".{self.config_path.name}.", dir=self.config_path.parent)
        temporary = Path(temporary_name)
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump(value, stream, indent=2, sort_keys=False)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.config_path)
            try:
                directory_fd = os.open(self.config_path.parent, os.O_DIRECTORY)
            except OSError:
                directory_fd = None
            if directory_fd is not None:
                try:
                    os.fsync(directory_fd)
                finally:
                    os.close(directory_fd)
            self.config_path.chmod(0o600)
        except OSError as exc:
            raise RuntimeError("could not save setup configuration") from exc
        finally:
            temporary.unlink(missing_ok=True)

    # ------------------------------------------------------------------
    # Codex authentication

    def _run_codex_job(self, job: _AuthJob) -> None:
        process: subprocess.Popen[str] | None = None
        timeout_timer: threading.Timer | None = None
        try:
            self._codex_home.mkdir(parents=True, exist_ok=True, mode=0o700)
            self._codex_home.chmod(0o700)
            executable = self._codex_executable()
            if not executable:
                raise FileNotFoundError("Codex executable is unavailable")
            environment = os.environ.copy()
            environment["CODEX_HOME"] = str(self._codex_home)
            # Device authentication must not inherit an API key or desktop
            # app control channel into the fixed login command.
            for key in list(environment):
                if key.startswith("OPENAI_") or key in {"CODEX_APP_TOOLS_PIPE_PATH", "CODEX_SESSION_ID", "CODEX_THREAD_ID"}:
                    environment.pop(key, None)
            process = subprocess.Popen(
                [executable, "login", "--device-auth"],
                cwd=str(self.config_path.parent),
                env=environment,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
                start_new_session=True,
            )
            with self._lock:
                job.process = process
                if job.cancelled.is_set():
                    self._terminate_codex_process_locked(job)
                else:
                    self._auth_state["codex"] = {
                        "state": "starting",
                        "detail": "Waiting for Codex device login output.",
                    }

            def expire_login() -> None:
                with self._lock:
                    if job.thread is not None and job.thread.is_alive() and job.process is process:
                        job.timed_out = True
                        self._terminate_codex_process_locked(job, force=True)

            timeout_timer = threading.Timer(CODEX_AUTH_TIMEOUT_SECONDS, expire_login)
            timeout_timer.daemon = True
            timeout_timer.start()

            if process.stdout is not None:
                while True:
                    line = process.stdout.readline(4096)
                    if line:
                        self._consume_codex_line(job, line)
                        continue
                    if process.poll() is not None:
                        break
                    if job.cancelled.wait(0.05):
                        with self._lock:
                            self._terminate_codex_process_locked(job)
                        break
            try:
                return_code = process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                with self._lock:
                    self._terminate_codex_process_locked(job, force=True)
                return_code = process.wait(timeout=2)
            with self._lock:
                if job.cancelled.is_set():
                    self._auth_state["codex"] = {
                        "state": "cancelled",
                        "detail": "Codex setup was cancelled.",
                    }
                elif return_code == 0 and self._codex_ready():
                    self._auth_state["codex"] = {
                        "state": "connected",
                        "detail": "Codex ChatGPT login is available locally.",
                    }
                elif return_code == 0:
                    code = job.failure_code or "auth_incomplete"
                    self._auth_state["codex"] = {
                        "state": "failed",
                        "detail": self._codex_failure_detail(code, "device login"),
                        "failure": {"phase": "device_login", "code": code},
                    }
                else:
                    code = "timeout" if job.timed_out else (job.failure_code or "cli_failed")
                    self._auth_state["codex"] = {
                        "state": "failed",
                        "detail": self._codex_failure_detail(code, "device login"),
                        "failure": {"phase": "device_login", "code": code},
                    }
        except Exception as exc:
            with self._lock:
                if job.cancelled.is_set():
                    self._auth_state["codex"] = {
                        "state": "cancelled",
                        "detail": "Codex setup was cancelled.",
                    }
                else:
                    code = self._codex_exception_code(exc)
                    self._auth_state["codex"] = {
                        "state": "failed",
                        "detail": self._codex_failure_detail(code, "initialization"),
                        "failure": {"phase": "initialization", "code": code},
                    }
        finally:
            if timeout_timer is not None:
                timeout_timer.cancel()
            with self._lock:
                if process is not None and job.process is process:
                    self._terminate_codex_process_locked(job, force=True)
                    job.process = None
            if process is not None and process.stdout is not None:
                process.stdout.close()

    def _consume_codex_line(self, job: _AuthJob, line: str) -> None:
        if job.cancelled.is_set():
            return
        # Pinned CLI prints ANSI-colored URL/code on separate lines.
        line = re.sub(r"\x1b\[[0-9;]*m", "", line).strip()
        code = self._codex_line_code(line)
        if code is not None and code != "auth_required":
            job.failure_code = code
        url = self._whitelisted_device_url(line)
        code = self._device_code(line)
        if job.expects_device_code:
            code = line if re.fullmatch(r"[A-Z0-9]{3,12}(?:-[A-Z0-9]{3,12})?", line) else None
            job.expects_device_code = False
        if "Enter this one-time code" in line:
            job.expects_device_code = True
            code = None
        if url is None and code is None:
            return
        with self._lock:
            state = self._auth_state["codex"]
            state["state"] = "waiting"
            state["detail"] = "Complete Codex device login in your browser."
            if url is not None:
                state["verification_url"] = url
            if code is not None:
                state["user_code"] = code

    @staticmethod
    def _codex_line_code(line: str) -> str | None:
        text = line[:4096].lower()
        if any(term in text for term in ("certificate verify failed", "certificate_verify_failed", "ssl error", "tls error")):
            return "tls_failed"
        if any(term in text for term in ("name or service not known", "failed to lookup", "dns", "could not resolve")):
            return "dns_failed"
        if any(term in text for term in ("timed out", "timeout", "connection timed out")):
            return "timeout"
        if any(term in text for term in ("network is unreachable", "connection refused", "connection error", "network error")):
            return "network_unavailable"
        if any(term in text for term in ("expired", "invalid code", "authorization denied", "access denied", "unauthorized")):
            return "auth_rejected"
        if any(term in text for term in ("authentication required", "not authenticated", "please log in", "reauth")):
            return "auth_required"
        if any(term in text for term in ("command not found", "unknown option", "no such file", "could not start")):
            return "runtime_unavailable"
        if any(term in text for term in ("login failed", "device login failed", "unexpected response")):
            return "cli_failed"
        return None

    @staticmethod
    def _codex_exception_code(error: Exception) -> str:
        if isinstance(error, FileNotFoundError):
            return "runtime_unavailable"
        if isinstance(error, subprocess.TimeoutExpired):
            return "timeout"
        detail = failure_detail(error, provider="Codex", phase="initialization")
        match = re.search(r"\[([a-z0-9_]+)\]", detail)
        return match.group(1) if match else "cli_failed"

    @staticmethod
    def _codex_failure_detail(code: str, phase: str) -> str:
        details = {
            "runtime_unavailable": ("the Codex CLI is unavailable", "Check the configured Codex executable and install it if needed."),
            "runtime_missing": ("a required executable is missing", "Check the container installation and configured executable."),
            "local_permission": ("Codex could not access a required local file", "Check persistent volume ownership and permissions."),
            "tls_failed": ("the HTTPS connection could not be verified", "Check the NAS clock, trusted certificates, and HTTPS proxy."),
            "dns_failed": ("the Codex hostname could not be resolved", "Check DNS and internet access from the NAS."),
            "network_unavailable": ("the Codex service could not be reached", "Check NAS internet access, firewall, proxy, and provider status."),
            "timeout": ("device login timed out", "Start a new Codex sign-in and finish it before the device code expires."),
            "auth_rejected": ("Codex rejected the device sign-in", "Start a new sign-in and use the current device code."),
            "auth_required": ("Codex authentication is required", "Complete ChatGPT sign-in, then retry."),
            "auth_incomplete": ("device login did not create a local login", "Start Codex sign-in again and complete it in the browser."),
            "cli_failed": ("the Codex CLI could not complete device login", "Check the CLI installation, then retry sign-in."),
        }
        explanation, action = details.get(code, details["cli_failed"])
        return f"Codex {phase} failed [{code}]. {explanation}. Action: {action}"

    @staticmethod
    def _whitelisted_device_url(line: str) -> str | None:
        for candidate in re.findall(r"https://[^\s<>\"']+", line):
            candidate = candidate.rstrip(".,);]")
            try:
                parsed = urlsplit(candidate)
            except ValueError:
                continue
            if parsed.scheme != "https" or parsed.hostname not in CODEX_DEVICE_HOSTS:
                continue
            if parsed.path not in CODEX_DEVICE_PATHS:
                continue
            if parsed.query or parsed.fragment or parsed.username or parsed.password or parsed.netloc != parsed.hostname:
                continue
            return candidate
        return None

    @staticmethod
    def _device_code(line: str) -> str | None:
        match = re.search(
            r"(?:Enter code|User code:|Device code:)\s+"
            r"([A-Z0-9]{3,12}(?:-[A-Z0-9]{3,12})?)\s*\Z",
            line,
        )
        if match is None:
            return None
        candidate = match.group(1)
        if candidate.lower() in {"device", "code", "login", "auth", "authentication"}:
            return None
        return candidate

    def _codex_executable(self) -> str | None:
        try:
            raw = self._read_raw_locked(allow_missing=True)
        except RuntimeError:
            raw = {}
        llm = raw.get("llm", {}) if isinstance(raw, dict) else {}
        configured = llm.get("executable") if isinstance(llm, dict) else None
        candidates = [configured, "/Applications/Codex.app/Contents/Resources/codex", shutil.which("codex")]
        for value in candidates:
            if isinstance(value, str) and value and Path(value).is_file():
                return value
        return None

    def _terminate_codex_process_locked(self, job: _AuthJob, *, force: bool = False) -> None:
        process = job.process
        if process is None:
            return
        try:
            if process.poll() is None:
                pid = getattr(process, "pid", None)
                if isinstance(pid, int) and pid > 1:
                    try:
                        # start_new_session=True above gives this job a unique
                        # process group.  Never use process-name or global kills.
                        os.killpg(pid, signal.SIGKILL if force else signal.SIGTERM)
                    except (ProcessLookupError, PermissionError):
                        process.terminate()
                else:
                    process.terminate()
        except (OSError, AttributeError):
            pass

    # ------------------------------------------------------------------
    # Robinhood authentication

    def _run_robinhood_job(self, job: _AuthJob, requested_account: str | None) -> None:
        temporary_report: Path | None = None
        try:
            with self._lock:
                latest = self._read_raw_locked()
                state_dir = self.config_path.parent / "state"
                state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
                temporary_report = state_dir / f".robinhood-capabilities-{uuid.uuid4().hex}.json"
                temporary_config = copy.deepcopy(latest)
            if job.cancelled.is_set():
                raise _AuthCancelled
            if not isinstance(temporary_config.get("robinhood"), dict):
                temporary_config["robinhood"] = {}
            if requested_account is not None:
                temporary_config["robinhood"]["account_number"] = requested_account
            temporary_config = self._normalized_private_paths(temporary_config)
            with self._lock:
                if job.cancelled.is_set():
                    raise _AuthCancelled
                self._auth_state["robinhood"] = {"state": "starting", "detail": "Preparing Robinhood authorization."}

            # The broker login and inspector are the existing fixed helpers.
            # This config is only an in-memory inspection config; bind=None
            # prevents inspect_broker from writing the application config.
            job.phase = "authorization"
            asyncio.run(self._robinhood_flow(job, temporary_config, temporary_report))
            job.phase = "account_binding"
            selected = temporary_config.get("robinhood", {}).get("account_number")
            if not isinstance(selected, str) or not ACCOUNT_NUMBER.fullmatch(selected):
                raise RuntimeError("Robinhood account selection was invalid")
            self._validate_report_for_account(temporary_report, selected)

            with self._lock:
                if job.cancelled.is_set():
                    raise _AuthCancelled
                latest = self._read_raw_locked()
                robinhood = latest.get("robinhood")
                if not isinstance(robinhood, dict):
                    robinhood = {}
                    latest["robinhood"] = robinhood
                existing = robinhood.get("account_number")
                if existing not in (None, "") and str(existing) != selected:
                    raise RuntimeError("Robinhood account selection conflicts with the existing binding")
                fresh_binding = existing in (None, "")
                if fresh_binding:
                    # A first binding gets its own shadow ledger.  Existing
                    # account mode and live flags are left completely intact.
                    robinhood["account_number"] = selected
                    robinhood["enable_live_orders"] = False
                    latest["mode"] = "shadow"
                    latest["database"] = "state/relay-shadow.sqlite3"
                self._atomic_write_config_locked(latest)
                if job.cancelled.is_set():
                    raise _AuthCancelled
                self._promote_report_locked(temporary_report)
                self._auth_state["robinhood"] = {
                    "state": "connected",
                    "detail": "Robinhood account authorization is available locally.",
                }
        except _AuthCancelled:
            with self._lock:
                self._auth_state["robinhood"] = {
                    "state": "cancelled",
                    "detail": "Robinhood setup was cancelled.",
                }
        except Exception as exc:
            with self._lock:
                if job.cancelled.is_set():
                    self._auth_state["robinhood"] = {
                        "state": "cancelled",
                        "detail": "Robinhood setup was cancelled.",
                    }
                else:
                    self._auth_state["robinhood"] = {
                        "state": "failed",
                        **self._robinhood_failure(exc, job.phase),
                    }
        finally:
            if temporary_report is not None:
                temporary_report.unlink(missing_ok=True)

    async def _robinhood_flow(self, job: _AuthJob, config: dict, report: Path) -> None:
        async def show_authorization(url: str) -> None:
            with self._lock:
                if job.cancelled.is_set():
                    raise _AuthCancelled
                job.phase = "callback"
                self._auth_state["robinhood"] = {
                    "state": "waiting",
                    "detail": "Open the Robinhood link in your browser. Account inspection follows authorization automatically.",
                    "authorization_url": url,
                }

        catalog = await self._await_with_cancel(
            lambda: self._invoke_fixed(broker_login, config, authorization_handler=show_authorization),
            job.cancelled,
        )
        with self._lock:
            if job.cancelled.is_set():
                raise _AuthCancelled
            job.schema_catalog = [{key: copy.deepcopy(tool.get(key)) for key in ("name", "inputSchema", "outputSchema")}
                                  for tool in (catalog if isinstance(catalog, list) else [])
                                  if isinstance(tool, dict) and tool.get("name") in SCHEMA_PINS]
            job.phase = "account_inspection"
            self._auth_state["robinhood"] = {"state": "waiting", "detail": "Authorization complete. Inspecting your Robinhood account."}
        args = SimpleNamespace(bind=None, output=str(report))
        await self._await_with_cancel(
            lambda: self._invoke_fixed(inspect_broker, args, config),
            job.cancelled,
        )

    @staticmethod
    def _robinhood_failure(error: Exception, phase: str) -> dict:
        """Expose stage/type/source, never exception text, requests, locals or tokens."""
        pending, leaves = [error], []
        for _ in range(20):
            if not pending:
                break
            current = pending.pop(0)
            if isinstance(current, BaseExceptionGroup):
                pending.extend(current.exceptions[:20])
            else:
                leaves.append(current)
        current = next((item for item in leaves if not isinstance(item, asyncio.CancelledError)), error)
        kind = type(current).__name__
        kind = kind if re.fullmatch(r"[A-Za-z_][A-Za-z_0-9]{0,63}", kind) else "Exception"
        failure = {"phase": phase, "type": kind}
        trace = current.__traceback__
        while trace is not None:
            source = Path(trace.tb_frame.f_code.co_filename)
            if source.parent == Path(__file__).parent and source.name in {"setup.py", "broker.py", "cli.py"}:
                failure.update(source=source.name, line=trace.tb_lineno)
            trace = trace.tb_next
        status = getattr(getattr(current, "response", None), "status_code", None)
        if type(status) is int and 400 <= status <= 599:
            failure["http_status"] = status
        label = {"initialization": "initialization", "authorization": "authorization", "callback": "callback wait",
                 "token_exchange": "token exchange", "account_inspection": "account inspection", "account_binding": "account binding"}.get(phase, "setup")
        code, action, explanation = "operation_failed", "Retry the operation; if it persists, report this code.", "the setup operation failed"
        try:
            text = str(current)[:4096].lower()
        except Exception:
            text = ""
        if isinstance(current, TimeoutError) and phase == "callback":
            code, explanation = "auth_expired", "sign-in expired before its callback arrived"
            action = "Start a new attempt and finish Robinhood sign-in within five minutes."
        elif kind == "OAuthTokenError" or status in (401, 403) or any(term in text for term in (
            "invalid token", "token expired", "invalid_grant", "unauthorized", "authentication required",
        )):
            code, explanation = "auth_rejected", "Robinhood rejected authorization"
            action = "Start Robinhood sign-in again; if it repeats, verify the account is authorized."
            if kind == "OAuthTokenError":
                failure["phase"] = "token_exchange"
                label = "token exchange"
        elif any(term in text for term in (
            "must be oauth or token", "unsupported setup", "tool is absent", "not allowed",
        )):
            code, explanation = "setup_unsupported", "the Robinhood setup or authenticated tool contract is unsupported"
            action = "Use the supported OAuth setup and reconnect to refresh the authenticated tool contract."
        elif any(term in text for term in ("capability report is missing", "capability report is invalid", "report does not match")):
            code, explanation = "report_mismatch", "the Robinhood capability report does not match the selected account"
            action = "Start Robinhood sign-in again and keep the selected account unchanged."
        elif any(term in text for term in ("account selection was invalid", "account selection conflicts", "not unique")):
            code, explanation = "account_selection", "the Robinhood account selection is unsupported"
            action = "Configure one active Agentic account, then start Robinhood sign-in again."
        else:
            safe = failure_detail(current, provider="Robinhood", phase=label)
            match = re.search(r"\[([a-z0-9_]+)\]\.\s*(.+)$", safe)
            if match:
                code, action = match.group(1), match.group(2)
                explanation = "the provider operation failed"
        failure.update(code=code, action=action)
        detail = f"Robinhood {label} failed ({kind}) [{code}]. {explanation}. Action: {action} Saved credentials were kept."
        return {"detail": detail, "failure": failure}

    @staticmethod
    async def _invoke_fixed(operation, *args, **kwargs):
        result = operation(*args, **kwargs)
        if inspect.isawaitable(result):
            return await result
        return result

    @staticmethod
    async def _wait_for_cancel(event: threading.Event) -> None:
        while not event.is_set():
            await asyncio.sleep(0.05)

    @classmethod
    async def _await_with_cancel(cls, operation, event: threading.Event):
        if event.is_set():
            raise _AuthCancelled
        task = asyncio.create_task(operation())
        watcher = asyncio.create_task(cls._wait_for_cancel(event))
        try:
            done, _ = await asyncio.wait({task, watcher}, return_when=asyncio.FIRST_COMPLETED)
            if watcher in done and event.is_set():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
                raise _AuthCancelled
            return await task
        finally:
            if not watcher.done():
                watcher.cancel()
            await asyncio.gather(watcher, return_exceptions=True)

    def _validate_report_for_account(self, path: Path | None, account_number: str) -> None:
        if path is None:
            raise RuntimeError("Robinhood capability report is missing")
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
            account = value["account"]
            last_four = account["last_four"]
        except (OSError, ValueError, TypeError, KeyError):
            raise RuntimeError("Robinhood capability report is invalid") from None
        if not isinstance(last_four, str) or not re.fullmatch(r"\d{4}\Z", last_four) or last_four != account_number[-4:]:
            raise RuntimeError("Robinhood capability report does not match the selected account")

    def _promote_report_locked(self, temporary: Path | None) -> None:
        if temporary is None or temporary.is_symlink() or not temporary.is_file():
            raise RuntimeError("Robinhood capability report is missing")
        destination = self.config_path.parent / "state" / "robinhood-capabilities.json"
        os.replace(temporary, destination)
        destination.chmod(0o600)

    # ------------------------------------------------------------------
    # Local status helpers

    def _resolve_codex_home(self) -> Path:
        value = os.environ.get("CODEX_HOME")
        return Path(value or (Path.home() / ".codex")).expanduser().resolve()

    def _normalized_private_paths(self, value: dict) -> dict:
        """Apply core.load_config's path interpretation to an in-memory copy."""

        result = copy.deepcopy(value)
        for section, key in (
            (result, "database"),
            (result, "kill_switch"),
            (result, "runtime_status_file"),
            (result.get("browser", {}), "profile_dir"),
            (result.get("robinhood", {}), "token_store"),
            (result.get("paper", {}), "quotes_file"),
        ):
            if isinstance(section, dict) and isinstance(section.get(key), str) and section[key]:
                path = Path(section[key]).expanduser()
                section[key] = str(path if path.is_absolute() else (self.config_path.parent / path).resolve())
        return result

    def _configured_path(
        self,
        raw: dict,
        key: str,
        default: Path,
        *,
        section: str | None = None,
    ) -> Path:
        source = raw
        if section is not None:
            candidate = raw.get(section, {}) if isinstance(raw, dict) else {}
            source = candidate if isinstance(candidate, dict) else {}
        value = source.get(key)
        if not isinstance(value, str) or not value:
            return default.resolve()
        path = Path(value).expanduser()
        return path.resolve() if path.is_absolute() else (self.config_path.parent / path).resolve()

    def _typesafe_key_path(self) -> Path:
        return self.config_path.parent / EVALUATION_DEFAULTS["api_key_file"]

    def _typesafe_credential_configured(self) -> bool:
        path = self._typesafe_key_path()
        try:
            if path.is_symlink() or path.parent.is_symlink() or not path.is_file():
                file_configured = False
            else:
                file_configured = bool(path.read_text(encoding="utf-8").strip())
        except (OSError, UnicodeError):
            file_configured = False
        env_configured = any(bool(os.environ.get(name, "").strip())
                             for name in ("TYPESAFE_API_KEY", "TYPESAFE_AI_API_KEY"))
        return file_configured or env_configured

    def _write_typesafe_key_locked(self, value: str) -> None:
        path = self._typesafe_key_path()
        state_dir = path.parent
        if state_dir.is_symlink() or path.is_symlink():
            raise RuntimeError("TypeSafe API key path must not be a symlink")
        if state_dir.exists() and not state_dir.is_dir():
            raise RuntimeError("TypeSafe API key directory must be a directory")
        state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd, temporary_name = tempfile.mkstemp(prefix=".typesafe.key.", dir=state_dir)
        temporary = Path(temporary_name)
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                stream.write(value)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
            path.chmod(0o600)
            directory_fd = os.open(state_dir, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        except OSError as exc:
            raise RuntimeError("could not save TypeSafe API key") from exc
        finally:
            temporary.unlink(missing_ok=True)

    def _is_paused(self, raw: dict) -> bool:
        path = self._configured_path(raw, "kill_switch", self.config_path.parent / "state" / "STOP")
        return path.is_file() and not path.is_symlink()

    def _read_runtime(self, raw: dict) -> dict:
        path = self._configured_path(raw, "runtime_status_file", self.config_path.parent / "state" / "runtime-status.json")
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            return {}
        return value if isinstance(value, dict) else {}

    def _read_safe_report(self, raw: dict) -> dict | None:
        path = self.config_path.parent / "state" / "robinhood-capabilities.json"
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            return None
        account = value.get("account") if isinstance(value, dict) else None
        if not isinstance(account, dict):
            return None
        configured = raw.get("robinhood", {}) if isinstance(raw, dict) else {}
        configured_account = configured.get("account_number") if isinstance(configured, dict) else None
        if not isinstance(configured_account, str) or not ACCOUNT_NUMBER.fullmatch(configured_account):
            return None
        if (
            isinstance(account.get("last_four"), str)
            and account["last_four"] != configured_account[-4:]
        ):
            return None
        clean: dict = {}
        for key in ("nickname", "last_four", "type", "state", "option_level", "agentic_allowed"):
            item = account.get(key)
            if key == "agentic_allowed":
                if isinstance(item, bool):
                    clean[key] = item
            elif isinstance(item, str) and len(item) <= 120 and not any(ord(char) < 32 for char in item):
                if key != "last_four" or re.fullmatch(r"\d{4}\Z", item):
                    clean[key] = item
        return clean or None

    @staticmethod
    def _safe_auth_projection(value: dict) -> dict:
        result = {
            "state": value.get("state", "not_connected"),
            "detail": value.get("detail", "Setup status unavailable."),
        }
        if isinstance(value.get("failure"), dict):
            result["failure"] = {
                key: value["failure"][key]
                for key in ("phase", "type", "code", "action", "source", "line", "http_status")
                if key in value["failure"]
            }
        if value.get("verification_url"):
            result["verification_url"] = value["verification_url"]
        if value.get("user_code"):
            result["user_code"] = value["user_code"]
        if value.get("state") == "waiting" and value.get("authorization_url"):
            result["authorization_url"] = value["authorization_url"]
        return result

    @staticmethod
    def _safe_text(value: object, default: str) -> str:
        if isinstance(value, str) and value.strip() and len(value) <= 120 and not any(ord(char) < 32 for char in value):
            return value.strip()
        return default

    def _ensure_open_locked(self) -> None:
        if self._closed:
            raise RuntimeError("setup manager is closed")


__all__ = ["SetupManager", "PUBLIC_BROWSER_URL"]
