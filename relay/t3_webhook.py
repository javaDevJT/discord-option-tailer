"""Bounded, signed delivery to the T3 scheduled-task webhook endpoint."""

from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import hmac
import json
import os
import re
import subprocess
from typing import Mapping


URL_ENV = "RELAY_T3_WEBHOOK_URL"
SECRET_ENV = "RELAY_T3_WEBHOOK_SIGNING_SECRET"

MAX_URL_BYTES = 2048
MAX_SECRET_BYTES = 4096
MAX_PAYLOAD_BYTES = 64 * 1024
_CURL_ARGS = (
    "curl", "--disable", "--config", "-", "--request", "POST", "--proto", "=https",
    "--noproxy", "*", "--silent", "--output", os.devnull, "--connect-timeout", "8", "--max-time", "10",
    "--write-out", "%{http_code}\n%header{cf-ray}\n",
)
_CF_RAY = re.compile(rb"[0-9a-fA-F]{16,32}-[A-Z0-9]{3}")

_ENDPOINT = re.compile(
    r"\Ahttps://relay\.t3\.codes/v1/hooks/"
    r"(?P<environment>[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)"
    r"/scheduled-task%3A"
    r"(?P<task_id>[0-9A-Fa-f]{8}-(?:[0-9A-Fa-f]{4}-){3}[0-9A-Fa-f]{12})/"
    r"(?P<token>[A-Za-z0-9._~-]{1,512})\Z"
)
_DELIVERY_CODES = frozenset(
    {
        "invalid_configuration",
        "invalid_payload",
        "payload_too_large",
        "request_error",
        "timeout",
        "network_error",
        "http_error",
        "invalid_response",
    }
)


def _valid_status(value: object) -> int | None:
    if type(value) is int and 100 <= value <= 599:
        return value
    return None


def _valid_url(value: object) -> bool:
    if not isinstance(value, str):
        return False
    try:
        if not value or len(value.encode("ascii")) > MAX_URL_BYTES:
            return False
    except UnicodeError:
        return False
    return _ENDPOINT.fullmatch(value) is not None


def _valid_secret(value: object) -> bool:
    if not isinstance(value, bytes):
        return False
    return 1 <= len(value) <= MAX_SECRET_BYTES


@dataclass(frozen=True, slots=True, repr=False)
class T3WebhookConfig:
    """Validated endpoint credentials whose repr never exposes secret values."""

    _url: str = field(repr=False)
    _signing_secret: bytes = field(repr=False)

    def __post_init__(self) -> None:
        if not _valid_url(self._url) or not _valid_secret(self._signing_secret):
            raise ValueError("invalid T3 webhook configuration")

    def __repr__(self) -> str:
        return "T3WebhookConfig(configured=True)"


class DeliveryError(RuntimeError):
    """A delivery failure carrying a fixed code and sanitized response metadata."""

    _MESSAGES = {
        "invalid_configuration": "T3 webhook configuration is invalid",
        "invalid_payload": "T3 webhook payload is invalid",
        "payload_too_large": "T3 webhook payload exceeds the size limit",
        "request_error": "T3 webhook request could not be prepared",
        "timeout": "T3 webhook delivery timed out",
        "network_error": "T3 webhook delivery failed",
        "http_error": "T3 webhook returned an unsuccessful HTTP status",
        "invalid_response": "T3 webhook returned an invalid response",
    }

    def __init__(self, code: str, status: int | None = None, *, cf_ray: bytes = b""):
        self.code = code if isinstance(code, str) and code in _DELIVERY_CODES else "network_error"
        self.status = _valid_status(status)
        self.cf_ray = cf_ray.decode("ascii") if isinstance(cf_ray, bytes) and _CF_RAY.fullmatch(cf_ray) else None
        message = self._MESSAGES[self.code]
        if self.status is not None:
            message = f"{message} (status {self.status})"
        super().__init__(message)


def load_config(environ: Mapping[str, object] | None = None) -> T3WebhookConfig | None:
    """Load and validate the optional endpoint settings from an environment mapping."""

    source = os.environ if environ is None else environ
    read_failed = False
    try:
        url = source.get(URL_ENV)
        secret = source.get(SECRET_ENV)
    except Exception:
        read_failed = True
    if read_failed:
        raise ValueError("invalid T3 webhook configuration")

    url_unset = url is None or (isinstance(url, str) and url == "")
    secret_unset = secret is None or (isinstance(secret, str) and secret == "")
    if url_unset and secret_unset:
        return None
    if not isinstance(url, str) or not isinstance(secret, str) or not url or not secret:
        raise ValueError("invalid T3 webhook configuration") from None

    config = None
    invalid = False
    try:
        secret_bytes = secret.encode("utf-8")
        config = T3WebhookConfig(url, secret_bytes)
    except (UnicodeError, ValueError):
        invalid = True
    if invalid:
        raise ValueError("invalid T3 webhook configuration")
    return config


def _serialize_payload(payload: object) -> bytes:
    if type(payload) is not dict:
        raise DeliveryError("invalid_payload")
    body = None
    serialization_failed = False
    try:
        body = json.dumps(
            payload,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
            allow_nan=False,
        ).encode("utf-8")
    except Exception:
        serialization_failed = True
    if serialization_failed:
        raise DeliveryError("invalid_payload")
    if len(body) > MAX_PAYLOAD_BYTES:
        raise DeliveryError("payload_too_large")
    return body


def deliver(config: T3WebhookConfig, payload: dict) -> None:
    """POST one bounded JSON body with an HMAC over the exact bytes sent.

    This function is synchronous by design. Callers running an async loop should
    run it in a worker thread. It does not retry, log, emit telemetry, or follow
    redirects. curl discards the response body.
    """

    if (
        type(config) is not T3WebhookConfig
        or not _valid_url(getattr(config, "_url", None))
        or not _valid_secret(getattr(config, "_signing_secret", None))
    ):
        raise DeliveryError("invalid_configuration")

    body = _serialize_payload(payload)
    signature = hmac.new(config._signing_secret, body, hashlib.sha256).hexdigest()
    options = (
        ("url", config._url),
        ("header", "Content-Type: application/json"),
        ("header", f"X-Hub-Signature-256: sha256={signature}"),
        ("data-binary", body.decode("utf-8")),
    )
    # Supply the bearer URL, signature, and exact UTF-8 body privately on stdin.
    request = "".join(f"{key} = {json.dumps(value, ensure_ascii=False)}\n" for key, value in options).encode("utf-8")
    failure = None
    try:
        response = subprocess.run(
            _CURL_ARGS,
            input=request,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=12,
            check=False,
            env={"PATH": os.environ.get("PATH", os.defpath)},
        )
    except subprocess.TimeoutExpired:
        failure = DeliveryError("timeout")
    except (OSError, subprocess.SubprocessError):
        failure = DeliveryError("network_error")
    if failure is not None:
        raise failure

    status_line, _, headers = response.stdout.partition(b"\n")
    cf_ray = headers.partition(b"\n")[0]
    status = _valid_status(int(status_line)) if len(status_line) == 3 and status_line.isdigit() else None
    if status is not None and not 200 <= status < 300:
        raise DeliveryError("http_error", status, cf_ray=cf_ray)
    if response.returncode:
        code = "timeout" if response.returncode == 28 else "network_error"
        raise DeliveryError(code, status, cf_ray=cf_ray)
    if status is None:
        raise DeliveryError("invalid_response")
