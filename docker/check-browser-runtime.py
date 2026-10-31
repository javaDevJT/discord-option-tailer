"""Offline build contract for the patched Node and packaged Chrome runtime."""

import re
import subprocess
from pathlib import Path


NODE_MINIMUM = (24, 22, 1)
NODE_PATH = Path("/usr/local/bin/node")
CHROME_PATH = Path("/usr/bin/google-chrome-stable")


class CheckFailure(RuntimeError):
    pass


def check_node():
    try:
        from playwright._impl._driver import compute_driver_executable
    except ImportError as exc:
        raise CheckFailure("Playwright Python package is unavailable") from exc

    driver_node, _ = compute_driver_executable()
    try:
        resolved_driver = Path(driver_node).resolve(strict=True)
        patched_node = NODE_PATH.resolve(strict=True)
    except OSError as exc:
        raise CheckFailure("Playwright driver Node executable is missing") from exc
    if resolved_driver != patched_node:
        raise CheckFailure("Playwright driver does not resolve to /usr/local/bin/node")

    try:
        result = subprocess.run(
            [driver_node, "--version"], capture_output=True, text=True, timeout=5
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise CheckFailure("patched Node did not report its version") from exc
    match = re.fullmatch(r"v?(\d+)\.(\d+)\.(\d+)\s*", result.stdout)
    if result.returncode or not match:
        raise CheckFailure("patched Node returned an invalid version")
    version = tuple(map(int, match.groups()))
    if version[0] != 24 or version < NODE_MINIMUM:
        raise CheckFailure("patched Node must be version 24.22.1 or newer within major 24")
    return ".".join(map(str, version))


def check_chrome():
    if not CHROME_PATH.is_file():
        raise CheckFailure("packaged Google Chrome executable was not found")

    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise CheckFailure("Playwright sync API is unavailable") from exc

    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(
                channel="chrome",
                headless=True,
                args=[
                    "--no-sandbox",
                    "--no-first-run",
                    "--no-default-browser-check",
                    "--disable-background-networking",
                    "--disable-sync",
                ],
                timeout=20_000,
            )
            try:
                page = browser.new_page()
                page.set_default_timeout(5_000)
                page.set_default_navigation_timeout(5_000)
                page.set_content(
                    "<!doctype html><title>relay-build-check</title>"
                    "<main id='marker'>synthetic page</main>",
                    wait_until="load",
                    timeout=5_000,
                )
                observed = page.evaluate(
                    "() => [document.title, document.querySelector('#marker').textContent]"
                )
                if observed != ["relay-build-check", "synthetic page"]:
                    raise CheckFailure("packaged Chrome returned incorrect synthetic page data")
                return browser.version
            finally:
                browser.close()
    except CheckFailure:
        raise
    except Exception as exc:
        message = "Playwright could not launch packaged Chrome and render local content"
        raise CheckFailure(message) from exc


def main():
    node_version = check_node()
    chrome_version = check_chrome()
    print(f"Browser runtime contract passed (Node {node_version}, Chrome {chrome_version})")


if __name__ == "__main__":
    main()
