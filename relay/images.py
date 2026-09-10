"""Bounded, anonymous transport for Discord-hosted option images."""

from __future__ import annotations

import os
import re
import tempfile
import time
from collections.abc import Mapping, Sequence
from pathlib import Path
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener


MAX_IMAGES = 4
MAX_TOTAL_BYTES = 16 * 1024 * 1024
DOWNLOAD_DEADLINE_SECONDS = 15.0
REQUEST_TIMEOUT_SECONDS = 5.0
CHUNK_SIZE = 64 * 1024
_ALLOWED_HOSTS = {"cdn.discordapp.com", "media.discordapp.net"}
_ATTACHMENT_PATH = re.compile(r"^/attachments/[0-9]+/[0-9]+(?:/[^/]*)+$")
_IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp"}
_MEDIA_SUFFIXES = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/jpg": ".jpg",
    "image/webp": ".webp",
}


class ImageTransportError(ValueError):
    """A sanitized image collection or download failure."""


class _NoRedirectHandler(HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, msg, headers, new_url):
        raise ImageTransportError("Image redirect rejected")


_OPENER = build_opener(_NoRedirectHandler(), ProxyHandler({}))


def _safe_url(value: object) -> str:
    if not isinstance(value, str) or not value or len(value) > 4096:
        raise ImageTransportError("Image URL is not permitted")
    if any(ord(char) < 0x20 or ord(char) == 0x7F for char in value):
        raise ImageTransportError("Image URL is not permitted")
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError:
        raise ImageTransportError("Image URL is not permitted") from None
    if (
        parsed.scheme != "https"
        or parsed.hostname not in _ALLOWED_HOSTS
        or parsed.username is not None
        or parsed.password is not None
        or port not in (None, 443)
        or "#" in value
        or not _ATTACHMENT_PATH.fullmatch(parsed.path)
    ):
        raise ImageTransportError("Image URL is not permitted")
    return value


def _media_type(value: object) -> str:
    return value.split(";", 1)[0].strip().lower() if isinstance(value, str) else ""


def _extension(value: object) -> str:
    return Path(value.split("?", 1)[0]).suffix.lower() if isinstance(value, str) else ""


def collect_images(messages: Sequence[Mapping[str, object]]) -> list[dict[str, str]]:
    """Collect safe image URLs in message order, bounded to four sources."""
    if not isinstance(messages, Sequence) or isinstance(messages, (str, bytes)):
        raise ImageTransportError("Image messages are invalid")
    result: list[dict[str, str]] = []
    declared_total = 0
    for message in messages:
        if not isinstance(message, Mapping):
            raise ImageTransportError("Image message is invalid")
        attachments = message.get("attachments") or []
        embeds = message.get("embeds") or []
        if not isinstance(attachments, list) or not isinstance(embeds, list):
            raise ImageTransportError("Image metadata is invalid")
        message_id = message.get("id", message.get("message_id"))
        seen: set[str] = set()

        def add(url: object, size: object = 0) -> None:
            nonlocal declared_total
            safe = _safe_url(url)
            if safe in seen:
                return
            if not isinstance(message_id, str) or not message_id:
                raise ImageTransportError("Image message ID is missing")
            seen.add(safe)
            result.append({"message_id": message_id, "url": safe})
            if size is not None:
                if type(size) is not int or size < 0:
                    raise ImageTransportError("Image size metadata is invalid")
                declared_total += size
                if declared_total > MAX_TOTAL_BYTES:
                    raise ImageTransportError("Image payload exceeds size limit")
            if len(result) > MAX_IMAGES:
                raise ImageTransportError("Too many images")

        for attachment in attachments:
            if not isinstance(attachment, Mapping):
                raise ImageTransportError("Image attachment is invalid")
            if (
                _extension(attachment.get("filename")) not in _IMAGE_EXTENSIONS
                and _extension(attachment.get("url")) not in _IMAGE_EXTENSIONS
                and _media_type(attachment.get("content_type")) not in _MEDIA_SUFFIXES
            ):
                continue
            add(attachment.get("url"), attachment.get("size"))
        for embed in embeds:
            if isinstance(embed, str):
                continue
            if not isinstance(embed, Mapping):
                raise ImageTransportError("Image embed is invalid")
            for key in ("image", "thumbnail"):
                if key not in embed or embed[key] is None:
                    continue
                media = embed[key]
                if isinstance(media, Mapping):
                    if "url" not in media:
                        raise ImageTransportError("Image URL is missing")
                    add(media["url"])
                elif isinstance(media, str):
                    add(media)
                else:
                    raise ImageTransportError("Image metadata is invalid")
    return result


def _open_url(request: Request, timeout: float):
    return _OPENER.open(request, timeout=timeout)


def _read_image(response, path: Path, media_type: str, budget: int, deadline: float) -> int:
    prefix = bytearray()
    written = 0
    with path.open("wb") as output:
        while True:
            if deadline - time.monotonic() <= 0:
                raise ImageTransportError("Image download deadline exceeded")
            chunk_size = min(CHUNK_SIZE, budget - written + 1) if written < budget else 1
            chunk = response.read(chunk_size)
            if time.monotonic() >= deadline:
                raise ImageTransportError("Image download deadline exceeded")
            if not chunk:
                break
            if not isinstance(chunk, (bytes, bytearray, memoryview)):
                raise ImageTransportError("Image response body rejected")
            chunk = bytes(chunk)
            if written + len(chunk) > budget:
                raise ImageTransportError("Image payload exceeds size limit")
            prefix.extend(chunk[: max(0, 12 - len(prefix))])
            output.write(chunk)
            written += len(chunk)
            if time.monotonic() >= deadline:
                raise ImageTransportError("Image download deadline exceeded")
    valid = {
        "image/png": bytes(prefix).startswith(b"\x89PNG\r\n\x1a\n"),
        "image/jpeg": bytes(prefix).startswith(b"\xff\xd8\xff"),
        "image/jpg": bytes(prefix).startswith(b"\xff\xd8\xff"),
        "image/webp": bytes(prefix[:4]) == b"RIFF" and bytes(prefix[8:12]) == b"WEBP",
    }
    if not valid[media_type]:
        raise ImageTransportError("Image response format rejected")
    return written


def download_images(sources: Sequence[Mapping[str, object]], directory: str | Path) -> list[Path]:
    """Download safe image sources to generated owner-only files."""
    if not isinstance(sources, Sequence) or isinstance(sources, (str, bytes)):
        raise ImageTransportError("Image sources are invalid")
    if len(sources) > MAX_IMAGES:
        raise ImageTransportError("Too many images")
    checked: list[tuple[str, str]] = []
    for source in sources:
        if not isinstance(source, Mapping):
            raise ImageTransportError("Image source is invalid")
        message_id = source.get("message_id")
        if not isinstance(message_id, str) or not message_id:
            raise ImageTransportError("Image message ID is missing")
        checked.append((message_id, _safe_url(source.get("url"))))
    if not checked:
        return []
    target = Path(directory)
    try:
        target.mkdir(mode=0o700, parents=True, exist_ok=True)
    except OSError:
        raise ImageTransportError("Image directory is unavailable") from None
    deadline = time.monotonic() + DOWNLOAD_DEADLINE_SECONDS
    paths: list[Path] = []
    total = 0
    path: Path | None = None
    try:
        for index, (_message_id, url) in enumerate(checked):
            path = None
            if deadline - time.monotonic() <= 0:
                raise ImageTransportError("Image download deadline exceeded")
            response = None
            try:
                response = _open_url(Request(url, headers={"Accept": ",".join(_MEDIA_SUFFIXES)}),
                                     timeout=min(REQUEST_TIMEOUT_SECONDS, deadline - time.monotonic()))
                if response.status < 200 or response.status >= 300:
                    raise ImageTransportError("Image response status rejected")
                media_type = _media_type(response.headers.get("Content-Type"))
                if media_type not in _MEDIA_SUFFIXES:
                    raise ImageTransportError("Image response media type rejected")
                content_length = response.headers.get("Content-Length")
                if content_length is not None:
                    try:
                        content_length = int(str(content_length).strip())
                    except (TypeError, ValueError):
                        raise ImageTransportError("Image response size rejected") from None
                    if content_length < 0:
                        raise ImageTransportError("Image response size rejected")
                budget = MAX_TOTAL_BYTES - total
                if budget <= 0 or (content_length is not None and content_length > budget):
                    raise ImageTransportError("Image payload exceeds size limit")
                fd, raw_path = tempfile.mkstemp(
                    prefix=f"image-{index:02d}-", suffix=_MEDIA_SUFFIXES[media_type], dir=str(target)
                )
                path = Path(raw_path)
                try:
                    os.chmod(path, 0o600)
                finally:
                    os.close(fd)
                total += _read_image(response, path, media_type, budget, deadline)
                paths.append(path)
            finally:
                if response is not None:
                    try:
                        response.close()
                    except Exception:
                        pass
    except Exception as exc:
        for candidate in {*paths, path} - {None}:
            try:
                candidate.unlink(missing_ok=True)
            except OSError:
                pass
        if isinstance(exc, ImageTransportError):
            raise
        raise ImageTransportError("Image download failed") from None
    return paths


__all__ = [
    "CHUNK_SIZE",
    "DOWNLOAD_DEADLINE_SECONDS",
    "ImageTransportError",
    "MAX_IMAGES",
    "MAX_TOTAL_BYTES",
    "REQUEST_TIMEOUT_SECONDS",
    "collect_images",
    "download_images",
]
