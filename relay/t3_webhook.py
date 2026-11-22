"""Bounded, signed delivery to the T3 scheduled-task webhook endpoint."""

from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import hmac
import json
import os
import re
import socket
from typing import Mapping
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener


URL_ENV = "RELAY_T3_WEBHOOK_URL"
SECRET_ENV = "RELAY_T3_WEBHOOK_SIGNING_SECRET"

MAX_URL_BYTES = 2048
MAX_SECRET_BYTES = 4096
MAX_PAYLOAD_BYTES = 64 * 1024
HTTP_TIMEOUT_SECONDS = 10.0

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
    """A delivery failure carrying only a fixed code and optional HTTP status."""

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

    def __init__(self, code: str, status: int | None = None):
        self.code = code if isinstance(code, str) and code in _DELIVERY_CODES else "network_error"
        self.status = _valid_status(status)
        message = self._MESSAGES[self.code]
        if self.status is not None:
            message = f"{message} (status {self.status})"
        super().__init__(message)


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, new):
        return None


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


def _close_quietly(response: object) -> None:
    try:
        response.close()
    except Exception:
        pass


def _open_request(request: Request):
    """Return (response, failure), converting urllib errors to safe values."""

    try:
        opener = build_opener(ProxyHandler({}), _NoRedirect())
        return opener.open(request, timeout=HTTP_TIMEOUT_SECONDS), None
    except HTTPError as exc:
        status = _valid_status(getattr(exc, "code", None))
        _close_quietly(exc)
        return None, DeliveryError("http_error", status)
    except (TimeoutError, socket.timeout):
        return None, DeliveryError("timeout")
    except URLError as exc:
        reason = getattr(exc, "reason", None)
        if isinstance(reason, (TimeoutError, socket.timeout)):
            return None, DeliveryError("timeout")
        return None, DeliveryError("network_error")
    except Exception:
        return None, DeliveryError("network_error")


def _response_failure(response: object) -> DeliveryError | None:
    try:
        status = _valid_status(response.getcode())
    except Exception:
        status = None
    finally:
        _close_quietly(response)

    if status is None:
        return DeliveryError("invalid_response")
    if not 200 <= status < 300:
        return DeliveryError("http_error", status)
    return None


def deliver(config: T3WebhookConfig, payload: dict) -> None:
    """POST one bounded JSON body with an HMAC over the exact bytes sent.

    This function is synchronous by design. Callers running an async loop should
    run it in a worker thread. It does not retry, log, emit telemetry, or follow
    redirects. Response bodies are deliberately not read.
    """

    if (
        type(config) is not T3WebhookConfig
        or not _valid_url(getattr(config, "_url", None))
        or not _valid_secret(getattr(config, "_signing_secret", None))
    ):
        raise DeliveryError("invalid_configuration")

    body = _serialize_payload(payload)
    signature = hmac.new(config._signing_secret, body, hashlib.sha256).hexdigest()
    request = None
    request_failed = False
    try:
        request = Request(
            config._url,
            data=body,
            headers={
                "Content-Type": "application/json",
                "X-Hub-Signature-256": f"sha256={signature}",
            },
            method="POST",
        )
    except Exception:
        request_failed = True
    if request_failed:
        raise DeliveryError("request_error")

    response, failure = _open_request(request)
    if failure is not None:
        raise failure
    failure = _response_failure(response)
    if failure is not None:
        raise failure
