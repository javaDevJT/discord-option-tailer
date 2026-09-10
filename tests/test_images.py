import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from relay.images import (
    MAX_TOTAL_BYTES,
    ImageTransportError,
    collect_images,
    download_images,
)


PNG = b"\x89PNG\r\n\x1a\nfixture"
JPEG = b"\xff\xd8\xfffixture"
WEBP = b"RIFFxxxxWEBPfixture"
URL = "https://cdn.discordapp.com/attachments/123/456/chart.png?ex=signed"


class Response:
    def __init__(self, body, content_type="image/png", status=200, content_length=None):
        self.body = body
        self.headers = {"Content-Type": content_type}
        if content_length is not None:
            self.headers["Content-Length"] = str(content_length)
        self.status = status
        self.closed = False

    def read(self, size=-1):
        if size < 0:
            size = len(self.body)
        body, self.body = self.body[:size], self.body[size:]
        return body

    def close(self):
        self.closed = True


class ImageTests(unittest.TestCase):
    def test_collects_supported_sources_in_stable_order_and_deduplicates_per_message(self):
        first = {
            "id": "1000000000000000001",
            "attachments": [
                {"filename": "chart.png", "url": URL},
                {"filename": "same.jpg", "url": URL},
                {"filename": "notes.pdf", "url": "https://evil.example/notes.pdf"},
                {"filename": "photo", "content_type": "image/webp", "url": URL.replace("chart.png", "photo")},
            ],
            "embeds": [{"thumbnail": {"url": URL.replace("chart.png", "thumb.webp")}}],
        }
        second = {
            "id": "1000000000000000002",
            "attachments": [{"filename": "again.png", "url": URL}],
        }

        self.assertEqual(
            collect_images([first, second]),
            [
                {"message_id": first["id"], "url": URL},
                {"message_id": first["id"], "url": URL.replace("chart.png", "photo")},
                {"message_id": first["id"], "url": URL.replace("chart.png", "thumb.webp")},
                {"message_id": second["id"], "url": URL},
            ],
        )

    def test_pdf_is_not_recognized_and_recognized_missing_url_fails_closed(self):
        self.assertEqual(
            collect_images(
                [{"id": "1000000000000000001", "attachments": [{"filename": "file.pdf"}]}]
            ),
            [],
        )
        with self.assertRaisesRegex(ImageTransportError, "URL"):
            collect_images(
                [{"id": "1000000000000000001", "attachments": [{"filename": "chart.png"}]}]
            )
        with self.assertRaisesRegex(ImageTransportError, "URL"):
            collect_images(
                [{"id": "1000000000000000001", "embeds": [{"image": {}}]}]
            )

    def test_collect_rejects_ssrf_urls_and_bounds_count_and_declared_size(self):
        cases = [
            URL.replace("cdn.discordapp.com", "127.0.0.1"),
            URL.replace("cdn.discordapp.com", "cdn.discordapp.com.evil"),
            URL.replace("cdn.discordapp.com", "user:pass@cdn.discordapp.com"),
            URL.replace("cdn.discordapp.com", "cdn.discordapp.com:8443"),
            URL + "#fragment",
            URL.replace("/attachments/123/456", "/avatars/123"),
        ]
        for unsafe in cases:
            with self.subTest(unsafe=unsafe), self.assertRaises(ImageTransportError):
                collect_images(
                    [{"id": "1000000000000000001", "attachments": [{"filename": "x.png", "url": unsafe}]}]
                )
        many = {
            "id": "1000000000000000001",
            "embeds": [
                {"image": {"url": URL.replace("chart.png", f"{index}.png")}}
                for index in range(5)
            ],
        }
        with self.assertRaisesRegex(ImageTransportError, "many|Too many"):
            collect_images([many])
        with self.assertRaises(ImageTransportError):
            collect_images(
                [
                    {
                        "id": "1000000000000000001",
                        "attachments": [{"filename": "x.png", "url": URL, "size": MAX_TOTAL_BYTES + 1}],
                    }
                ]
            )

    def test_download_writes_owner_only_files_and_preserves_source_order(self):
        sources = [
            {"message_id": "1000000000000000001", "url": URL},
            {"message_id": "1000000000000000002", "url": URL.replace("chart.png", "two.jpg")},
        ]
        responses = [Response(PNG), Response(JPEG, "image/jpeg")]

        def open_url(request, timeout):
            self.assertLessEqual(timeout, 5.0)
            self.assertEqual(request.full_url, sources[len(seen)]["url"])
            seen.append(request.full_url)
            return responses[len(seen) - 1]

        seen = []
        with tempfile.TemporaryDirectory() as directory, patch("relay.images._open_url", side_effect=open_url):
            paths = download_images(sources, directory)
            self.assertEqual(seen, [source["url"] for source in sources])
            self.assertEqual([path.read_bytes() for path in paths], [PNG, JPEG])
            self.assertEqual([path.suffix for path in paths], [".png", ".jpg"])
            self.assertTrue(all(stat.S_IMODE(path.stat().st_mode) == stat.S_IRUSR | stat.S_IWUSR for path in paths))

    def test_download_rejects_redirect_type_magic_and_size_without_leaking_data(self):
        bad_responses = [
            Response(PNG, status=302),
            Response(PNG, "application/pdf"),
            Response(JPEG, "image/png"),
            Response(b"x" * 10, "image/png", content_length=MAX_TOTAL_BYTES + 1),
            Response(b"x" * (MAX_TOTAL_BYTES + 1), "image/png"),
        ]
        for response in bad_responses:
            with self.subTest(response=response.headers), tempfile.TemporaryDirectory() as directory:
                with patch("relay.images._open_url", return_value=response):
                    with self.assertRaises(ImageTransportError) as raised:
                        download_images([{"message_id": "1000000000000000001", "url": URL}], directory)
                self.assertNotIn("signed", str(raised.exception))
                self.assertEqual(list(Path(directory).iterdir()), [])

    def test_download_rejects_unsafe_source_before_opening(self):
        with tempfile.TemporaryDirectory() as directory, patch("relay.images._open_url") as open_url:
            with self.assertRaises(ImageTransportError):
                download_images(
                    [{"message_id": "1000000000000000001", "url": "https://127.0.0.1/attachments/1/2/x.png"}],
                    directory,
                )
            open_url.assert_not_called()


if __name__ == "__main__":
    unittest.main()
