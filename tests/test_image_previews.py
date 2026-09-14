"""Image cards and authenticated-gateway routes, using synthetic image bytes only."""
import base64
import http.client
import json
from pathlib import Path
import sqlite3
import unittest
from unittest.mock import patch
from urllib.error import HTTPError

from tests import test_dashboard as dashboard_fixtures

PNG = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jNxoAAAAASUVORK5CYII=")
URL = "https://cdn.discordapp.com/attachments/123/456/chart.png?ex=private-fixture"
MESSAGE_ID = "1545700000000000001"


class ImagePreviewTests(unittest.TestCase):
    def setUp(self):
        self.fixture = dashboard_fixtures.DashboardTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)
        self.set_image(URL)

    def set_image(self, url):
        with sqlite3.connect(self.fixture.database) as connection:
            row = connection.execute("SELECT body FROM messages WHERE id=?", (MESSAGE_ID,)).fetchone()
            body = json.loads(row[0])
            body["attachments"] = [{"url": url, "filename": "chart.png", "content_type": "image/png"}]
            connection.execute("UPDATE messages SET body=? WHERE id=?", (json.dumps(body), MESSAGE_ID))

    def get(self, path):
        client = http.client.HTTPConnection("127.0.0.1", self.fixture.server.server_address[1], timeout=5)
        try:
            client.request("GET", path)
            response = client.getresponse()
            return response.status, dict((key.lower(), value) for key, value in response.getheaders()), response.read()
        finally:
            client.close()

    def image_url(self):
        status, _, body = self.get("/api/messages")
        self.assertEqual(status, 200)
        self.assertNotIn("private-fixture", body.decode())
        return next(row for row in json.loads(body)["items"] if row["id"] == MESSAGE_ID)["images"][0]["url"]

    @staticmethod
    def download(sources, directory):
        path = Path(directory) / "fixture.png"
        path.write_bytes(PNG)
        return [path]

    def test_preview_is_same_origin_bounded_lookup_and_binary_response(self):
        path = self.image_url()
        self.assertTrue(path.startswith("/api/message-image?"))
        with patch("relay.dashboard.download_images", side_effect=self.download) as download:
            status, headers, body = self.get(path)
        self.assertEqual(status, 200)
        self.assertEqual(body, PNG)
        self.assertEqual(headers["content-type"], "image/png")
        self.assertEqual(headers["cache-control"], "private, max-age=300")
        self.assertEqual(headers["x-content-type-options"], "nosniff")
        self.assertFalse(Path(download.call_args.args[1]).exists())
        with patch("relay.dashboard.download_images") as download:
            self.assertEqual(self.get(path.replace("rev-1", "unknown"))[0], 404)
            self.assertEqual(self.get(path.replace("index=0", "index=4"))[0], 400)
            self.assertEqual(self.get(path + "&url=https://evil.example")[0], 400)
            download.assert_not_called()

    def test_download_error_is_visible_without_signed_url_or_provider_text(self):
        with patch("relay.images._open_url", side_effect=HTTPError(URL, 403, "private provider text", {}, None)):
            status, _, body = self.get(self.image_url())
        self.assertEqual(status, 502)
        error = json.loads(body)
        self.assertTrue(error["code"].startswith("image_"))
        self.assertNotIn("private", body.decode())
        self.assertNotIn("https://", body.decode())
        self.set_image("https://evil.example/chart.png")
        with patch("relay.dashboard.download_images") as download:
            status, _, body = self.get("/api/message-image?message_id=" + MESSAGE_ID + "&revision=rev-1&index=0")
            self.assertEqual(status, 502)
            download.assert_not_called()

    def test_card_loads_preview_only_when_expanded(self):
        from playwright.sync_api import sync_playwright, expect
        origin = "http://127.0.0.1:" + str(self.fixture.server.server_address[1])
        with patch("relay.dashboard.download_images", side_effect=self.download) as download, sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            try:
                page = browser.new_page()
                page.route("**/*", lambda route: route.continue_() if route.request.url.startswith(origin + "/") else route.abort())
                page.goto(origin, wait_until="domcontentloaded")
                card = page.locator(".message-card").filter(has=page.locator(".message-content", has_text="entry one"))
                expect(card.locator("summary")).to_have_text("1 image")
                self.assertEqual(download.call_count, 0)
                card.locator("summary").click()
                expect(card.locator("img.message-image")).to_be_visible()
                expect(card.locator("img.message-image")).to_have_js_property("naturalWidth", 1)
                self.assertTrue(card.locator("a.message-image-link").get_attribute("href").startswith("/api/message-image?"))
                page.locator("#refresh-button").click()
                expect(card.locator("details.message-images")).to_have_attribute("open", "")
                expect(card.locator("img.message-image")).to_be_visible()
            finally:
                browser.close()
