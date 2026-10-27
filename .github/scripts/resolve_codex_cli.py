#!/usr/bin/env python3
"""Resolve the current stable @openai/codex release from the npm registry."""

from __future__ import annotations

import json
import os
import re
import sys
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, Request, build_opener


PACKAGE_NAME = "@openai/codex"
REGISTRY_URL = "https://registry.npmjs.org/@openai%2Fcodex/latest"
REGISTRY_HOST = "registry.npmjs.org"
MAX_METADATA_BYTES = 1024 * 1024
REQUEST_TIMEOUT_SECONDS = 20

# SemVer core numbers cannot have leading zeroes. Build metadata is allowed,
# while prerelease versions are intentionally excluded from the stable channel.
STABLE_VERSION = re.compile(
    r"(?:0|[1-9][0-9]*)\."
    r"(?:0|[1-9][0-9]*)\."
    r"(?:0|[1-9][0-9]*)"
    r"(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?"
)


class ResolverError(Exception):
    """Raised when official npm metadata cannot safely resolve a stable release."""


class _RejectRedirects(HTTPRedirectHandler):
    """Do not allow the fixed official-registry request to follow other URLs."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def fetch_latest_metadata() -> dict:
    """Fetch bounded JSON from the one hard-coded official npm registry URL."""
    request = Request(
        REGISTRY_URL,
        headers={
            "Accept": "application/json",
            "User-Agent": "codex-cli-version-resolver/1.0",
        },
    )
    opener = build_opener(_RejectRedirects())
    try:
        with opener.open(request, timeout=REQUEST_TIMEOUT_SECONDS) as response:
            if response.geturl() != REGISTRY_URL:
                raise ResolverError("npm registry returned an unexpected metadata URL")
            payload = response.read(MAX_METADATA_BYTES + 1)
    except ResolverError:
        raise
    except (HTTPError, URLError, TimeoutError, OSError):
        raise ResolverError("official npm registry request failed") from None

    if len(payload) > MAX_METADATA_BYTES:
        raise ResolverError("npm registry metadata exceeded the size limit")
    try:
        metadata = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise ResolverError("npm registry returned invalid JSON metadata") from None
    if not isinstance(metadata, dict):
        raise ResolverError("npm registry metadata must be an object")
    return metadata


def validate_metadata(metadata: object) -> str:
    """Return a strict stable version only for the expected npm package."""
    if not isinstance(metadata, dict):
        raise ResolverError("npm registry metadata must be an object")
    if metadata.get("name") != PACKAGE_NAME:
        raise ResolverError("npm registry metadata names an unexpected package")
    version = metadata.get("version")
    if not isinstance(version, str) or STABLE_VERSION.fullmatch(version) is None:
        raise ResolverError("npm registry metadata has an invalid stable version")
    return version


def resolve_latest(fetch_metadata=fetch_latest_metadata) -> str:
    """Resolve once per invocation, making the changing latest tag explicit."""
    return validate_metadata(fetch_metadata())


def write_github_output(version: str, output_path: str | None = None) -> None:
    """Append a validated version output for GitHub Actions."""
    if not isinstance(version, str) or STABLE_VERSION.fullmatch(version) is None:
        raise ResolverError("refusing to write an invalid stable version")
    output_path = output_path or os.environ.get("GITHUB_OUTPUT")
    if not output_path:
        raise ResolverError("GITHUB_OUTPUT is not set")
    try:
        with open(output_path, "a", encoding="utf-8", newline="\n") as output:
            output.write(f"version={version}\n")
    except OSError:
        raise ResolverError("could not write the resolved version to GITHUB_OUTPUT") from None


def main() -> int:
    try:
        version = resolve_latest()
        write_github_output(version)
    except ResolverError as error:
        print(f"Codex CLI version resolution failed: {error}", file=sys.stderr)
        return 1

    # Print only fixed registry/package identifiers and the validated version.
    print(json.dumps(
        {"registry": REGISTRY_HOST, "package": PACKAGE_NAME, "version": version},
        sort_keys=True,
        separators=(",", ":"),
    ))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
