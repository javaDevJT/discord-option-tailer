"""Bounded, anonymous transport for Discord-hosted option images."""

from __future__ import annotations

import os
import re
import socket
import tempfile
import time
from collections.abc import Mapping, Sequence
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

MAX_IMAGES = 4
MAX_TOTAL_BYTES = 16 * 1024 * 1024
DOWNLOAD_DEADLINE_SECONDS = 15.0
REQUEST_TIMEOUT_SECONDS = 5.0
CHUNK_SIZE = 64 * 1024

IMAGE_DIAGNOSTIC_DETAILS = {
    "image_access_denied": "Image access denied",
    "image_not_found": "Image not found",
    "image_rate_limited": "Image service rate limited",
    "image_server_error": "Image service unavailable",
    "image_network_error": "Image network unavailable",
    "image_timeout": "Image download timed out",
    "image_invalid_url": "Image URL is invalid",
    "image_invalid_type": "Image type is unsupported",
    "image_invalid_data": "Image data is invalid",
    "image_limits": "Image limits exceeded",
    "image_local_io": "Image local storage unavailable",
}

_RETRYABLE_CODES = frozenset(
    {
        "image_rate_limited",
        "image_server_error",
        "image_network_error",
        "image_timeout",
    }
)
_FALLBACK_CODES = frozenset(
    {
        "image_access_denied",
        "image_not_found",
        "image_rate_limited",
        "image_server_error",
        "image_network_error",
        "image_timeout",
    }
)
_ALLOWED_HOSTS = {"cdn.discordapp.com", "media.discordapp.net"}
_ATTACHMENT_PATH = re.compile(r"^/attachments/[0-9]+/[0-9]+(?:/[^/]+)+$")
_IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp"}
_MEDIA_SUFFIXES = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/jpg": ".jpg",
    "image/webp": ".webp",
}


class ImageTransportError(ValueError):
    """A bounded image transport failure with a safe, fixed diagnostic."""

    def __init__(
        self,
        message: str | None = None,
        *,
        code: str | None = None,
        retryable: bool | None = None,
        bytes_read: int = 0,
    ) -> None:
        if code is None and isinstance(message, str) and message in IMAGE_DIAGNOSTIC_DETAILS:
            code = message
        if not isinstance(code, str) or code not in IMAGE_DIAGNOSTIC_DETAILS:
            code = _legacy_code(message)
        self.code = code
        self.retryable = code in _RETRYABLE_CODES if retryable is None else bool(retryable)
        self.bytes_read = bytes_read if type(bytes_read) is int and bytes_read >= 0 else 0
        self.detail = IMAGE_DIAGNOSTIC_DETAILS[code]
        super().__init__(self.detail)


def _legacy_code(message: object) -> str:
    text = str(message or "").lower()
    if "redirect" in text or "url" in text:
        return "image_invalid_url"
    if "type" in text or "media" in text or "format" in text:
        return "image_invalid_type"
    if "size" in text or "many" in text or "limit" in text:
        return "image_limits"
    if "directory" in text or "owner-only" in text or "local" in text:
        return "image_local_io"
    return "image_invalid_data"


def _error(code: str, *, bytes_read: int = 0) -> ImageTransportError:
    return ImageTransportError(code=code, bytes_read=bytes_read)


class _NoRedirectHandler(HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, msg, headers, new_url):
        raise _error("image_invalid_url")


_OPENER = build_opener(_NoRedirectHandler(), ProxyHandler({}))


def _safe_url(value: object) -> str:
    if not isinstance(value, str) or not value or len(value) > 4096:
        raise _error("image_invalid_url")
    if any(ord(char) < 0x20 or ord(char) == 0x7F for char in value):
        raise _error("image_invalid_url")
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError:
        raise _error("image_invalid_url") from None
    if (
        parsed.scheme != "https"
        or parsed.hostname not in _ALLOWED_HOSTS
        or parsed.username is not None
        or parsed.password is not None
        or port not in (None, 443)
        or "#" in value
        or not _ATTACHMENT_PATH.fullmatch(parsed.path)
    ):
        raise _error("image_invalid_url")
    return value


def _identity(value: str) -> str:
    return urlsplit(value).path


def attachment_identity(value: object) -> str:
    """Return the validated, query-free attachment pathname for a URL."""
    return _identity(_safe_url(value))


def _media_type(value: object) -> str:
    return value.split(";", 1)[0].strip().lower() if isinstance(value, str) else ""


def _extension(value: object) -> str:
    return Path(value.split("?", 1)[0]).suffix.lower() if isinstance(value, str) else ""


def _optional_fallbacks(primary: str, values: object) -> list[str]:
    if values is None:
        return []
    if isinstance(values, str) or not isinstance(values, Sequence):
        return []
    identity = _identity(primary)
    fallbacks: list[str] = []
    for value in values:
        try:
            candidate = _safe_url(value)
        except ImageTransportError:
            continue
        if candidate == primary or _identity(candidate) != identity or candidate in fallbacks:
            continue
        fallbacks.append(candidate)
        if len(fallbacks) == 2:
            break
    return fallbacks


def collect_images(messages: Sequence[Mapping[str, object]]) -> list[dict[str, object]]:
    """Collect safe image URLs, merging same-attachment proxy candidates."""
    if not isinstance(messages, Sequence) or isinstance(messages, (str, bytes)):
        raise _error("image_invalid_data")

    result: list[dict[str, object]] = []
    by_identity: dict[tuple[str, str], dict[str, object]] = {}
    declared_total = 0

    for message in messages:
        if not isinstance(message, Mapping):
            raise _error("image_invalid_data")
        attachments = message.get("attachments") or []
        embeds = message.get("embeds") or []
        if not isinstance(attachments, list) or not isinstance(embeds, list):
            raise _error("image_invalid_data")
        message_id = message.get("id", message.get("message_id"))

        def add(url: object, size: object = None, alternate_values: object = None) -> None:
            nonlocal declared_total
            safe = _safe_url(url)
            key = _identity(safe)
            fallbacks = _optional_fallbacks(safe, alternate_values)
            if not isinstance(message_id, str) or not message_id:
                raise _error("image_invalid_data")
            existing = by_identity.get((message_id, key))
            if existing is not None:
                merged = list(existing.get("fallback_urls", []))
                for candidate in (safe, *fallbacks):
                    if candidate != existing["url"] and candidate not in merged:
                        merged.append(candidate)
                    if len(merged) == 2:
                        break
                if merged:
                    existing["fallback_urls"] = merged[:2]
                return
            if size is not None:
                if type(size) is not int or size < 0:
                    raise _error("image_invalid_data")
                declared_total += size
                if declared_total > MAX_TOTAL_BYTES:
                    raise _error("image_limits")
            if len(result) >= MAX_IMAGES:
                raise _error("image_limits")
            item: dict[str, object] = {"message_id": message_id, "url": safe}
            if fallbacks:
                item["fallback_urls"] = fallbacks[:2]
            by_identity[(message_id, key)] = item
            result.append(item)

        for attachment in attachments:
            if not isinstance(attachment, Mapping):
                raise _error("image_invalid_data")
            if (
                _extension(attachment.get("filename")) not in _IMAGE_EXTENSIONS
                and _extension(attachment.get("url")) not in _IMAGE_EXTENSIONS
                and _media_type(attachment.get("content_type")) not in _MEDIA_SUFFIXES
            ):
                continue
            add(
                attachment.get("url"),
                attachment.get("size"),
                (
                    attachment.get("proxy_url"),
                    attachment.get("currentSrc"),
                    attachment.get("current_src"),
                ),
            )

        for embed in embeds:
            if isinstance(embed, str):
                continue
            if not isinstance(embed, Mapping):
                raise _error("image_invalid_data")
            for key in ("image", "thumbnail"):
                if key not in embed or embed[key] is None:
                    continue
                media = embed[key]
                if isinstance(media, Mapping):
                    if "url" not in media:
                        raise _error("image_invalid_url")
                    add(
                        media["url"],
                        alternate_values=(
                            media.get("proxy_url"),
                            media.get("currentSrc"),
                            media.get("current_src"),
                        ),
                    )
                elif isinstance(media, str):
                    add(media)
                else:
                    raise _error("image_invalid_data")
    return result


def _open_url(request: Request, timeout: float):
    return _OPENER.open(request, timeout=timeout)


def _http_code_error(status: object) -> ImageTransportError:
    try:
        status = int(status)
    except (TypeError, ValueError):
        return _error("image_invalid_data")
    if status in (401, 403):
        return _error("image_access_denied")
    if status in (404, 410):
        return _error("image_not_found")
    if status in (408, 504):
        return _error("image_timeout")
    if status == 429:
        return _error("image_rate_limited")
    if 500 <= status < 600:
        return _error("image_server_error")
    if 300 <= status < 400:
        return _error("image_invalid_url")
    return _error("image_invalid_data")


def _network_error(exc: BaseException) -> ImageTransportError:
    reason = exc.reason if isinstance(exc, URLError) else exc
    if isinstance(reason, (TimeoutError, socket.timeout)):
        return _error("image_timeout")
    return _error("image_network_error")


def _read_image(response, path: Path, media_type: str, budget: int, deadline: float) -> int:
    prefix = bytearray()
    written = 0
    try:
        with path.open("wb") as output:
            while True:
                if deadline - time.monotonic() <= 0:
                    raise _error("image_timeout", bytes_read=written)
                chunk_size = min(CHUNK_SIZE, budget - written + 1) if written < budget else 1
                try:
                    chunk = response.read(chunk_size)
                except (TimeoutError, socket.timeout):
                    raise _error("image_timeout", bytes_read=written) from None
                except HTTPError as exc:
                    error = _http_code_error(exc.code)
                    error.bytes_read = written
                    raise error from None
                except (URLError, ConnectionError, OSError) as exc:
                    error = _network_error(exc)
                    error.bytes_read = written
                    raise error from None
                except Exception:
                    raise _error("image_network_error", bytes_read=written) from None
                if time.monotonic() >= deadline:
                    raise _error("image_timeout", bytes_read=written)
                if not chunk:
                    break
                if not isinstance(chunk, (bytes, bytearray, memoryview)):
                    raise _error("image_invalid_data", bytes_read=written)
                chunk = bytes(chunk)
                if written + len(chunk) > budget:
                    raise _error("image_limits", bytes_read=written)
                try:
                    output.write(chunk)
                except OSError:
                    raise _error("image_local_io", bytes_read=written) from None
                written += len(chunk)
                prefix.extend(chunk[: max(0, 12 - len(prefix))])
    except ImageTransportError:
        raise
    except OSError:
        raise _error("image_local_io", bytes_read=written) from None

    valid = {
        "image/png": bytes(prefix).startswith(b"\x89PNG\r\n\x1a\n"),
        "image/jpeg": bytes(prefix).startswith(b"\xff\xd8\xff"),
        "image/jpg": bytes(prefix).startswith(b"\xff\xd8\xff"),
        "image/webp": len(prefix) >= 12
        and bytes(prefix[:4]) == b"RIFF"
        and bytes(prefix[8:12]) == b"WEBP",
    }
    if not valid[media_type]:
        raise _error("image_invalid_data", bytes_read=written)
    return written


def _unlink(path: Path | None) -> None:
    if path is None:
        return
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass


def _download_one(url: str, target: Path, index: int, budget: int, deadline: float) -> tuple[Path, int]:
    response = None
    path: Path | None = None
    try:
        if deadline - time.monotonic() <= 0:
            raise _error("image_timeout")
        request = Request(url, headers={"Accept": ",".join(_MEDIA_SUFFIXES)})
        try:
            response = _open_url(
                request,
                timeout=min(REQUEST_TIMEOUT_SECONDS, max(0.001, deadline - time.monotonic())),
            )
        except ImageTransportError:
            raise
        except HTTPError as exc:
            raise _http_code_error(exc.code) from None
        except (TimeoutError, socket.timeout, URLError, ConnectionError, OSError) as exc:
            raise _network_error(exc) from None
        except Exception:
            raise _error("image_network_error") from None

        try:
            status = int(response.status)
            headers = response.headers
            media_type = _media_type(headers.get("Content-Type"))
        except (AttributeError, TypeError, ValueError):
            raise _error("image_invalid_data") from None
        if status < 200 or status >= 300:
            raise _http_code_error(status)
        if media_type not in _MEDIA_SUFFIXES:
            raise _error("image_invalid_type")
        content_length = headers.get("Content-Length")
        if content_length is not None:
            try:
                content_length = int(str(content_length).strip())
            except (TypeError, ValueError):
                raise _error("image_invalid_data") from None
            if content_length < 0:
                raise _error("image_invalid_data")
            if content_length > budget:
                raise _error("image_limits")
        try:
            fd, raw_path = tempfile.mkstemp(
                prefix=f"image-{index:02d}-",
                suffix=_MEDIA_SUFFIXES[media_type],
                dir=str(target),
            )
            os.close(fd)
            path = Path(raw_path)
            os.chmod(path, 0o600)
        except OSError:
            if "fd" in locals():
                try:
                    os.close(fd)
                except OSError:
                    pass
            raise _error("image_local_io") from None
        written = _read_image(response, path, media_type, budget, deadline)
        return path, written
    except ImageTransportError:
        _unlink(path)
        raise
    except OSError:
        _unlink(path)
        raise _error("image_local_io") from None
    finally:
        if response is not None:
            try:
                response.close()
            except Exception:
                pass


def _source_candidates(source: Mapping[str, object]) -> tuple[str, list[str]]:
    primary = _safe_url(source.get("url"))
    values = source.get("fallback_urls", [])
    if values is None:
        values = []
    if isinstance(values, str) or not isinstance(values, Sequence) or len(values) > 2:
        raise _error("image_invalid_data")
    identity = _identity(primary)
    candidates = [primary]
    for value in values:
        candidate = _safe_url(value)
        if candidate == primary or _identity(candidate) != identity or candidate in candidates:
            raise _error("image_invalid_url")
        candidates.append(candidate)
    return primary, candidates


def download_images(sources: Sequence[Mapping[str, object]], directory: str | Path) -> list[Path]:
    """Download safe image sources to generated owner-only files."""
    if not isinstance(sources, Sequence) or isinstance(sources, (str, bytes)):
        raise _error("image_invalid_data")

    checked: list[tuple[str, list[str]]] = []
    by_identity: dict[tuple[str, str], int] = {}
    for source in sources:
        if not isinstance(source, Mapping):
            raise _error("image_invalid_data")
        message_id = source.get("message_id")
        if not isinstance(message_id, str) or not message_id:
            raise _error("image_invalid_data")
        primary, candidates = _source_candidates(source)
        key = (message_id, _identity(primary))
        if key in by_identity:
            existing = checked[by_identity[key]][1]
            for candidate in candidates:
                if candidate not in existing and len(existing) < 3:
                    existing.append(candidate)
            continue
        by_identity[key] = len(checked)
        checked.append((primary, candidates))
        if len(checked) > MAX_IMAGES:
            raise _error("image_limits")
    if not checked:
        return []

    target = Path(directory)
    try:
        target.mkdir(mode=0o700, parents=True, exist_ok=True)
    except (OSError, TypeError):
        raise _error("image_local_io") from None

    deadline = time.monotonic() + DOWNLOAD_DEADLINE_SECONDS
    paths: list[Path] = []
    total = 0
    try:
        for index, (_primary, candidates) in enumerate(checked):
            last_error: ImageTransportError | None = None
            for candidate_index, candidate in enumerate(candidates):
                budget = MAX_TOTAL_BYTES - total
                if budget <= 0:
                    raise _error("image_limits")
                try:
                    path, written = _download_one(candidate, target, index, budget, deadline)
                except ImageTransportError as error:
                    total += min(error.bytes_read, budget)
                    last_error = error
                    if candidate_index + 1 < len(candidates) and error.code in _FALLBACK_CODES:
                        continue
                    raise
                total += written
                paths.append(path)
                break
            else:
                if last_error is not None:
                    raise last_error
        return paths
    except Exception as exc:
        for path in paths:
            _unlink(path)
        if isinstance(exc, ImageTransportError):
            raise
        raise _error("image_local_io") from None


__all__ = [
    "CHUNK_SIZE",
    "DOWNLOAD_DEADLINE_SECONDS",
    "IMAGE_DIAGNOSTIC_DETAILS",
    "ImageTransportError",
    "MAX_IMAGES",
    "MAX_TOTAL_BYTES",
    "REQUEST_TIMEOUT_SECONDS",
    "attachment_identity",
    "collect_images",
    "download_images",
]
