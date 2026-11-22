from __future__ import annotations

from dataclasses import FrozenInstanceError
import hashlib
import hmac
import io
import json
import socket
import unittest
from urllib.error import HTTPError, URLError
from unittest.mock import Mock, patch

from relay import t3_webhook
from relay.t3_webhook import DeliveryError, T3WebhookConfig, deliver, load_config


URL = (
    "https://relay.t3.codes/v1/hooks/staging/scheduled-task%3A"
    "123e4567-e89b-12d3-a456-426614174000/fixture_opaque-token-123456"
)
SECRET = "unit-test-signing-key-not-a-real-secret"
ENV = {
    t3_webhook.URL_ENV: URL,
    t3_webhook.SECRET_ENV: SECRET,
}


class _Response:
    def __init__(self, status=204):
        self.status = status
        self.closed = False
        self.read_calls = 0

    def getcode(self):
        return self.status

    def read(self, *args, **kwargs):
        self.read_calls += 1
        raise AssertionError("the transport must not read response bodies")

    def close(self):
        self.closed = True


class T3WebhookTests(unittest.TestCase):
    def test_load_config_treats_both_absent_or_empty_settings_as_disabled(self):
        self.assertIsNone(load_config({}))
        self.assertIsNone(load_config({t3_webhook.URL_ENV: None, t3_webhook.SECRET_ENV: None}))
        self.assertIsNone(load_config({t3_webhook.URL_ENV: "", t3_webhook.SECRET_ENV: ""}))

        for environ in (
            {t3_webhook.URL_ENV: URL},
            {t3_webhook.SECRET_ENV: SECRET},
            {t3_webhook.URL_ENV: "", t3_webhook.SECRET_ENV: SECRET},
            {t3_webhook.URL_ENV: URL, t3_webhook.SECRET_ENV: ""},
            {t3_webhook.URL_ENV: None, t3_webhook.SECRET_ENV: SECRET},
            {t3_webhook.URL_ENV: URL, t3_webhook.SECRET_ENV: None},
        ):
            with self.subTest(environ=tuple(environ)):
                with self.assertRaisesRegex(ValueError, "invalid T3 webhook configuration") as caught:
                    load_config(environ)
                self.assertNotIn(URL, str(caught.exception))
                self.assertNotIn(SECRET, str(caught.exception))

    def test_config_is_immutable_and_repr_hides_endpoint_credentials(self):
        config = load_config(ENV)
        self.assertIsInstance(config, T3WebhookConfig)
        self.assertNotIn(URL, repr(config))
        self.assertNotIn(SECRET, repr(config))
        self.assertNotIn(URL, str(config))
        self.assertNotIn(SECRET, str(config))
        with self.assertRaises(FrozenInstanceError):
            config._url = "https://attacker.invalid/secret"

    def test_url_validation_allows_only_the_exact_https_route_shape(self):
        self.assertIsNotNone(load_config(ENV))
        rejected = (
            URL.replace("https://", "http://", 1),
            URL.replace("relay.t3.codes", "relay.t3.codes.evil.invalid"),
            URL.replace("relay.t3.codes", "user:pass@relay.t3.codes"),
            URL.replace("relay.t3.codes", "relay.t3.codes:443"),
            URL + "?debug=1",
            URL + "#fragment",
            URL.replace("/staging/", "/Staging/"),
            URL.replace("scheduled-task%3A", "scheduled-task%3a"),
            URL.replace("123e4567-e89b-12d3-a456-426614174000", "not-a-uuid"),
            URL.replace("fixture_opaque-token-123456", "../token"),
            URL[:-1] + "%2F",
            URL + ("a" * (t3_webhook.MAX_URL_BYTES + 1)),
        )
        for value in rejected:
            with self.subTest(value=value[:96]):
                with self.assertRaisesRegex(ValueError, "invalid T3 webhook configuration") as caught:
                    load_config({t3_webhook.URL_ENV: value, t3_webhook.SECRET_ENV: SECRET})
                self.assertNotIn(value, str(caught.exception))

    def test_signing_secret_accepts_any_nonempty_utf8_value_up_to_the_limit(self):
        self.assertIsNotNone(load_config({t3_webhook.URL_ENV: URL, t3_webhook.SECRET_ENV: "x"}))
        self.assertIsNotNone(load_config({t3_webhook.URL_ENV: URL, t3_webhook.SECRET_ENV: "🔑"}))
        value = "s" * (t3_webhook.MAX_SECRET_BYTES + 1)
        with self.subTest(length=len(value)):
            with self.assertRaisesRegex(ValueError, "invalid T3 webhook configuration"):
                load_config({t3_webhook.URL_ENV: URL, t3_webhook.SECRET_ENV: value})

    def test_posts_exact_json_bytes_with_sha256_hmac_and_bounded_timeout(self):
        unicode_secret = "🔑"
        config = load_config({**ENV, t3_webhook.SECRET_ENV: unicode_secret})
        payload = {"z": "café 🐕", "a": {"value": 3}}
        expected_body = json.dumps(
            payload,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
            allow_nan=False,
        ).encode("utf-8")
        expected_signature = hmac.new(unicode_secret.encode("utf-8"), expected_body, hashlib.sha256).hexdigest()
        response = _Response(204)
        opener = Mock()
        opener.open.return_value = response

        with patch.object(t3_webhook, "build_opener", return_value=opener) as build:
            deliver(config, payload)

        self.assertEqual(build.call_count, 1)
        handlers = build.call_args.args
        self.assertEqual(len(handlers), 2)
        self.assertIsInstance(handlers[0], t3_webhook.ProxyHandler)
        self.assertEqual(handlers[0].proxies, {})
        self.assertIsInstance(handlers[1], t3_webhook._NoRedirect)

        request = opener.open.call_args.args[0]
        self.assertEqual(request.full_url, URL)
        self.assertEqual(request.get_method(), "POST")
        self.assertEqual(request.data, expected_body)
        request_headers = {key.lower(): value for key, value in request.header_items()}
        self.assertEqual(request_headers["content-type"], "application/json")
        self.assertEqual(request_headers["x-hub-signature-256"], f"sha256={expected_signature}")
        self.assertEqual(opener.open.call_args.kwargs, {"timeout": t3_webhook.HTTP_TIMEOUT_SECONDS})
        self.assertTrue(response.closed)
        self.assertEqual(response.read_calls, 0)

    def test_accepts_success_responses_and_closes_without_reading_body(self):
        for status in (200, 201, 202, 204, 299):
            with self.subTest(status=status):
                response = _Response(status)
                opener = Mock()
                opener.open.return_value = response
                with patch.object(t3_webhook, "build_opener", return_value=opener):
                    self.assertIsNone(deliver(load_config(ENV), {"event": "ok"}))
                self.assertTrue(response.closed)
                self.assertEqual(response.read_calls, 0)
                self.assertEqual(opener.open.call_count, 1)

    def test_http_error_exposes_only_safe_code_and_status(self):
        response_body = f"body={SECRET}; endpoint={URL}".encode("utf-8")
        http_error = HTTPError(URL, 503, response_body.decode(), {}, io.BytesIO(response_body))
        opener = Mock()
        opener.open.side_effect = http_error
        with patch.object(t3_webhook, "build_opener", return_value=opener):
            with self.assertRaises(DeliveryError) as caught:
                deliver(load_config(ENV), {"event": "error"})

        error = caught.exception
        self.assertEqual(error.code, "http_error")
        self.assertEqual(error.status, 503)
        self._assert_error_is_sanitized(error)
        self.assertEqual(opener.open.call_count, 1)

    def test_redirect_status_is_not_followed(self):
        redirect = HTTPError(URL, 302, "redirect", {"Location": "https://attacker.invalid/"}, io.BytesIO())
        opener = Mock()
        opener.open.side_effect = redirect
        with patch.object(t3_webhook, "build_opener", return_value=opener):
            with self.assertRaises(DeliveryError) as caught:
                deliver(load_config(ENV), {"event": "redirect"})
        self.assertEqual((caught.exception.code, caught.exception.status), ("http_error", 302))
        self.assertEqual(opener.open.call_count, 1)
        self._assert_error_is_sanitized(caught.exception)

    def test_network_and_timeout_errors_do_not_echo_url_or_exception_text(self):
        failures = (
            (URLError(f"private reason {URL} {SECRET}"), "network_error"),
            (socket.timeout(f"private timeout {URL} {SECRET}"), "timeout"),
            (TimeoutError(f"private timeout {URL} {SECRET}"), "timeout"),
        )
        for failure, expected_code in failures:
            with self.subTest(expected_code=expected_code):
                opener = Mock()
                opener.open.side_effect = failure
                with patch.object(t3_webhook, "build_opener", return_value=opener):
                    with self.assertRaises(DeliveryError) as caught:
                        deliver(load_config(ENV), {"event": "network"})
                self.assertEqual(caught.exception.code, expected_code)
                self.assertIsNone(caught.exception.status)
                self._assert_error_is_sanitized(caught.exception)

    def test_non_success_response_and_missing_status_are_safe_failures(self):
        for status, expected_status in ((400, 400), (429, 429), (599, 599), (None, None)):
            with self.subTest(status=status):
                response = _Response(status)
                opener = Mock()
                opener.open.return_value = response
                with patch.object(t3_webhook, "build_opener", return_value=opener):
                    with self.assertRaises(DeliveryError) as caught:
                        deliver(load_config(ENV), {"event": "response"})
                self.assertEqual(caught.exception.code, "http_error" if status is not None else "invalid_response")
                self.assertEqual(caught.exception.status, expected_status)
                self.assertTrue(response.closed)
                self._assert_error_is_sanitized(caught.exception)

    def test_invalid_or_oversized_payload_fails_before_network_access(self):
        for payload, expected_code in (
            ([], "invalid_payload"),
            ({"value": object()}, "invalid_payload"),
            ({"value": float("nan")}, "invalid_payload"),
            ({"value": "x" * t3_webhook.MAX_PAYLOAD_BYTES}, "payload_too_large"),
        ):
            with self.subTest(expected_code=expected_code):
                with patch.object(t3_webhook, "build_opener") as build:
                    with self.assertRaises(DeliveryError) as caught:
                        deliver(load_config(ENV), payload)
                self.assertEqual(caught.exception.code, expected_code)
                build.assert_not_called()
                self._assert_error_is_sanitized(caught.exception)

    def test_invalid_config_object_fails_safely(self):
        with patch.object(t3_webhook, "build_opener") as build:
            with self.assertRaises(DeliveryError) as caught:
                deliver(None, {"event": "bad config"})
        self.assertEqual(caught.exception.code, "invalid_configuration")
        build.assert_not_called()
        self._assert_error_is_sanitized(caught.exception)

    def _assert_error_is_sanitized(self, error):
        rendered = f"{error!s} {error!r}"
        self.assertIsNone(error.__context__)
        self.assertNotIn(URL, rendered)
        self.assertNotIn(SECRET, rendered)
        self.assertNotIn("private", rendered)
        self.assertNotIn("fixture_opaque-token", rendered)


if __name__ == "__main__":
    unittest.main()
