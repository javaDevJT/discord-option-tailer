import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.error import URLError

from relay.images import (
    IMAGE_DIAGNOSTIC_DETAILS,
    MAX_IMAGES,
    MAX_TOTAL_BYTES,
    ImageTransportError,
    attachment_identity,
    collect_images,
    download_images,
)


PNG = b"\x89PNG\r\n\x1a\nfixture"
JPEG = b"\xff\xd8\xfffixture"
WEBP = b"RIFFxxxxWEBPfixture"
URL = "https://cdn.discordapp.com/attachments/123/456/chart.png?ex=signed"
PROXY_URL = "https://media.discordapp.net/attachments/123/456/chart.png?ex=proxy"


class Response:
    def __init__(self, body=b"", content_type="image/png", status=200, content_length=None, read_error=None):
        self.body = body
        self.headers = {"Content-Type": content_type}
        if content_length is not None:
            self.headers["Content-Length"] = str(content_length)
        self.status = status
        self.closed = False
        self.read_error = read_error

    def read(self, size=-1):
        if self.read_error is not None:
            error, self.read_error = self.read_error, None
            raise error
        if size < 0:
            size = len(self.body)
        body, self.body = self.body[:size], self.body[size:]
        return body

    def close(self):
        self.closed = True


class ImageTests(unittest.TestCase):
    def test_download_identifies_application_without_account_credentials(self):
        with tempfile.TemporaryDirectory() as directory, patch("relay.images._open_url", return_value=Response(PNG)) as opening:
            download_images([{"message_id": "123", "url": URL}], directory)
        request = opening.call_args.args[0]
        self.assertEqual(request.get_header("User-agent"),
                         "DiscordOptionTailer/0.1 (+https://github.com/javaDevJT/discord-option-tailer)")
        self.assertFalse(request.has_header("Authorization"))
        self.assertFalse(request.has_header("Cookie"))

    def assert_transport(self, context, code, *, retryable=None):
        with self.assertRaises(ImageTransportError) as raised:
            context()
        error = raised.exception
        self.assertEqual(error.code, code)
        self.assertEqual(str(error), IMAGE_DIAGNOSTIC_DETAILS[code])
        if retryable is not None:
            self.assertEqual(error.retryable, retryable)
        return error

    def test_collects_supported_sources_and_deduplicates_same_attachment_path(self):
        first = {
            "id": "1000000000000000001",
            "attachments": [
                {"filename": "chart.png", "url": URL, "proxy_url": PROXY_URL},
                {"filename": "same.jpg", "url": PROXY_URL},
                {"filename": "notes.pdf", "url": "https://evil.example/notes.pdf"},
                {
                    "filename": "photo",
                    "content_type": "image/webp",
                    "url": URL.replace("chart.png", "photo.webp"),
                },
            ],
            "embeds": [{"thumbnail": {"url": URL.replace("chart.png", "thumb.webp")}}],
        }
        second = {
            "id": "1000000000000000002",
            "attachments": [{"filename": "again.png", "url": URL}],
        }

        result = collect_images([first, second])

        self.assertEqual(
            result,
            [
                {
                    "message_id": first["id"],
                    "url": URL,
                    "fallback_urls": [PROXY_URL],
                },
                {"message_id": first["id"], "url": URL.replace("chart.png", "photo.webp")},
                {"message_id": first["id"], "url": URL.replace("chart.png", "thumb.webp")},
                {"message_id": second["id"], "url": URL},
            ],
        )
        self.assertEqual(attachment_identity(URL), "/attachments/123/456/chart.png")
        self.assertEqual(attachment_identity(PROXY_URL), attachment_identity(URL))

    def test_collect_rejects_unsafe_urls_and_preserves_limits(self):
        cases = [
            URL.replace("cdn.discordapp.com", "127.0.0.1"),
            URL.replace("cdn.discordapp.com", "cdn.discordapp.com.evil"),
            URL.replace("cdn.discordapp.com", "user:pass@cdn.discordapp.com"),
            URL.replace("cdn.discordapp.com", "cdn.discordapp.com:8443"),
            URL + "#fragment",
            URL.replace("/attachments/123/456", "/avatars/123"),
        ]
        for unsafe in cases:
            with self.subTest(unsafe=unsafe):
                error = self.assert_transport(
                    lambda unsafe=unsafe: collect_images(
                        [{"id": "1000000000000000001", "attachments": [{"filename": "x.png", "url": unsafe}]}]
                    ),
                    "image_invalid_url",
                    retryable=False,
                )
                self.assertNotIn(unsafe, str(error))

        many = {
            "id": "1000000000000000001",
            "attachments": [
                {"filename": f"{index}.png", "url": URL.replace("chart.png", f"{index}.png")}
                for index in range(MAX_IMAGES + 1)
            ],
        }
        self.assert_transport(lambda: collect_images([many]), "image_limits", retryable=False)
        self.assert_transport(
            lambda: collect_images(
                [{"id": "1000000000000000001", "attachments": [{"filename": "x.png", "url": URL, "size": MAX_TOTAL_BYTES + 1}]}]
            ),
            "image_limits",
            retryable=False,
        )

    def test_collect_ignores_invalid_optional_fallbacks(self):
        result = collect_images(
            [
                {
                    "id": "1000000000000000001",
                    "attachments": [
                        {
                            "filename": "chart.png",
                            "url": URL,
                            "proxy_url": "https://evil.example/attachments/123/456/chart.png",
                            "currentSrc": URL.replace("/123/456/", "/123/999/"),
                        }
                    ],
                }
            ]
        )
        self.assertEqual(result, [{"message_id": "1000000000000000001", "url": URL}])

    def test_pdf_is_not_recognized_and_recognized_missing_url_fails_closed(self):
        self.assertEqual(
            collect_images(
                [{"id": "1000000000000000001", "attachments": [{"filename": "file.pdf"}]}]
            ),
            [],
        )
        self.assert_transport(
            lambda: collect_images(
                [{"id": "1000000000000000001", "attachments": [{"filename": "chart.png"}]}]
            ),
            "image_invalid_url",
        )
        self.assert_transport(
            lambda: collect_images(
                [{"id": "1000000000000000001", "embeds": [{"image": {}}]}]
            ),
            "image_invalid_url",
        )

    def test_download_writes_owner_only_files_and_preserves_source_order(self):
        sources = [
            {"message_id": "1000000000000000001", "url": URL},
            {"message_id": "1000000000000000002", "url": URL.replace("chart.png", "two.jpg")},
        ]
        responses = [Response(PNG), Response(JPEG, "image/jpeg")]
        seen = []

        def open_url(request, timeout):
            self.assertLessEqual(timeout, 5.0)
            seen.append(request.full_url)
            self.assertNotIn("Authorization", request.headers)
            self.assertNotIn("Cookie", request.headers)
            return responses[len(seen) - 1]

        with tempfile.TemporaryDirectory() as directory, patch("relay.images._open_url", side_effect=open_url):
            paths = download_images(sources, directory)
            self.assertEqual(seen, [source["url"] for source in sources])
            self.assertEqual([path.read_bytes() for path in paths], [PNG, JPEG])
            self.assertEqual([path.suffix for path in paths], [".png", ".jpg"])
            self.assertTrue(
                all(stat.S_IMODE(path.stat().st_mode) == stat.S_IRUSR | stat.S_IWUSR for path in paths)
            )

    def test_download_classifies_http_statuses_and_does_not_leak_url(self):
        cases = (
            (403, "image_access_denied", False),
            (404, "image_not_found", False),
            (429, "image_rate_limited", True),
            (500, "image_server_error", True),
        )
        for status, code, retryable in cases:
            with self.subTest(status=status), tempfile.TemporaryDirectory() as directory:
                with patch("relay.images._open_url", return_value=Response(status=status)):
                    error = self.assert_transport(
                        lambda: download_images([{"message_id": "1", "url": URL}], directory),
                        code,
                        retryable=retryable,
                    )
                self.assertNotIn("signed", str(error))

    def test_download_classifies_network_and_timeout(self):
        for raised, code in ((TimeoutError("secret URL"), "image_timeout"), (URLError("private URL"), "image_network_error")):
            with self.subTest(code=code), tempfile.TemporaryDirectory() as directory:
                with patch("relay.images._open_url", side_effect=raised):
                    error = self.assert_transport(
                        lambda: download_images([{"message_id": "1", "url": URL}], directory),
                        code,
                        retryable=True,
                    )
                self.assertNotIn("URL", str(error))

    def test_download_uses_distinct_same_path_proxy_after_http_failure(self):
        seen = []

        def open_url(request, timeout):
            seen.append(request.full_url)
            return Response(status=403) if len(seen) == 1 else Response(PNG)

        source = {"message_id": "1", "url": URL, "fallback_urls": [PROXY_URL]}
        with tempfile.TemporaryDirectory() as directory, patch("relay.images._open_url", side_effect=open_url):
            paths = download_images([source], directory)
            self.assertEqual(seen, [URL, PROXY_URL])
            self.assertEqual(paths[0].read_bytes(), PNG)

    def test_download_preserves_same_path_for_different_messages(self):
        sources = [
            {"message_id": "first", "url": URL, "fallback_urls": [PROXY_URL]},
            {"message_id": "second", "url": URL},
        ]
        responses = [Response(PNG), Response(PNG)]
        seen = []

        def open_url(request, timeout):
            seen.append(request.full_url)
            return responses[len(seen) - 1]

        with tempfile.TemporaryDirectory() as directory, patch("relay.images._open_url", side_effect=open_url):
            paths = download_images(sources, directory)
            self.assertEqual(seen, [URL, URL])
            self.assertEqual(len(paths), 2)
            self.assertEqual([path.read_bytes() for path in paths], [PNG, PNG])

    def test_download_merges_same_message_primary_variants_as_fallbacks(self):
        sources = [
            {"message_id": "same", "url": URL},
            {"message_id": "same", "url": PROXY_URL},
        ]
        seen = []

        def open_url(request, timeout):
            seen.append(request.full_url)
            return Response(status=403) if len(seen) == 1 else Response(PNG)

        with tempfile.TemporaryDirectory() as directory, patch("relay.images._open_url", side_effect=open_url):
            paths = download_images(sources, directory)
            self.assertEqual(seen, [URL, PROXY_URL])
            self.assertEqual(len(paths), 1)
            self.assertEqual(paths[0].read_bytes(), PNG)

    def test_download_rejects_invalid_fallback_before_opening(self):
        with tempfile.TemporaryDirectory() as directory, patch("relay.images._open_url") as open_url:
            error = self.assert_transport(
                lambda: download_images(
                    [{"message_id": "1", "url": URL, "fallback_urls": ["https://127.0.0.1/attachments/123/456/chart.png"]}],
                    directory,
                ),
                "image_invalid_url",
            )
            self.assertNotIn("127.0.0.1", str(error))
            open_url.assert_not_called()

    def test_download_rejects_type_magic_and_size_without_leaking_data(self):
        bad_responses = [
            (Response(PNG, status=302), "image_invalid_url"),
            (Response(PNG, "application/octet-stream"), "image_invalid_type"),
            (Response(PNG, "image/gif"), "image_invalid_type"),
            (Response(JPEG, "image/png"), "image_invalid_data"),
            (Response(b"x" * 10, "image/png", content_length=MAX_TOTAL_BYTES + 1), "image_limits"),
            (Response(b"x" * (MAX_TOTAL_BYTES + 1), "image/png"), "image_limits"),
        ]
        for response, code in bad_responses:
            with self.subTest(response=response.headers), tempfile.TemporaryDirectory() as directory:
                with patch("relay.images._open_url", return_value=response):
                    self.assert_transport(
                        lambda: download_images([{"message_id": "1", "url": URL}], directory),
                        code,
                        retryable=False,
                    )
                self.assertEqual(list(Path(directory).iterdir()), [])

    def test_download_rejects_unsafe_source_before_opening(self):
        with tempfile.TemporaryDirectory() as directory, patch("relay.images._open_url") as open_url:
            self.assert_transport(
                lambda: download_images(
                    [{"message_id": "1", "url": "https://127.0.0.1/attachments/1/2/x.png"}],
                    directory,
                ),
                "image_invalid_url",
            )
            open_url.assert_not_called()

    def test_download_reports_local_io(self):
        with tempfile.NamedTemporaryFile() as target:
            self.assert_transport(
                lambda: download_images([{"message_id": "1", "url": URL}], target.name),
                "image_local_io",
                retryable=False,
            )


if __name__ == "__main__":
    unittest.main()
