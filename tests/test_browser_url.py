"""Browser login URLs must reach the guarded WebSocket proxy with noVNC 1.6."""
import re
import unittest
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import parse_qs, urljoin, urlsplit

from relay.setup import PUBLIC_BROWSER_URL


class BrowserLinks(HTMLParser):
    def __init__(self):
        super().__init__()
        self.urls = []

    def handle_starttag(self, tag, attrs):
        for name, value in attrs:
            if name in {"href", "src"} and value and "/browser/vnc.html?" in value:
                self.urls.append(value)


class BrowserURLTests(unittest.TestCase):
    def test_every_browser_login_link_resolves_to_guarded_websocket_route(self):
        static = Path(__file__).resolve().parents[1] / "relay" / "static"
        links = BrowserLinks()
        links.feed((static / "index.html").read_text())
        self.assertEqual(len(links.urls), 3)
        match = re.search(r'const BROWSER_LOGIN_URL = "([^"]+)";', (static / "app.js").read_text())
        self.assertIsNotNone(match)
        for url in [PUBLIC_BROWSER_URL, match.group(1), *links.urls]:
            with self.subTest(url=url):
                viewer = urljoin("http://relay.invalid/", url)
                self.assertEqual(urlsplit(viewer).netloc, "relay.invalid")
                self.assertEqual(urlsplit(viewer).path, "/browser/vnc.html")
                path = parse_qs(urlsplit(viewer).query)["path"][0]
                # The current noVNC client resolves this setting relative to vnc.html.
                websocket = urljoin(viewer, path)
                self.assertEqual(urlsplit(websocket).netloc, "relay.invalid")
                self.assertEqual(urlsplit(websocket).path, "/browser/websockify")
