from __future__ import annotations

from dataclasses import FrozenInstanceError
import hashlib
import hmac
import json
import os
import subprocess
import unittest
from unittest.mock import patch

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

CURL_ARGS = (
    "curl",
    "--disable",
    "--config",
    "-",
    "--request",
    "POST",
    "--proto",
    "=https",
    "--noproxy",
    "*",
    "--silent",
    "--output",
    os.devnull,
    "--connect-timeout",
    "8",
    "--max-time",
    "10",
    "--write-out",
    "%{http_code}\n%header{cf-ray}\n",
)
CF_RAY = "1234567890abcdef-DFW"


def _curl_result(stdout=b"202\n", returncode=0):
    return subprocess.CompletedProcess(CURL_ARGS, returncode, stdout=stdout)


def _expected_request(payload, secret):
    body = json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
        allow_nan=False,
    ).encode("utf-8")
    signature = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    config = "".join(
        f"{key} = {json.dumps(value, ensure_ascii=False)}\n"
        for key, value in (
            ("url", URL),
            ("header", "Content-Type: application/json"),
            ("header", f"X-Hub-Signature-256: sha256={signature}"),
            ("data-binary", body.decode("utf-8")),
        )
    ).encode("utf-8")
    return body, signature, config


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
        payload = {
            "z": 'café 🐕 newline\n tab\t quote" slash\\',
            "a": {"value": 3, "control": "carriage\rreturn"},
        }
        expected_body, expected_signature, expected_config = _expected_request(payload, unicode_secret)
        with patch.object(
            t3_webhook.subprocess,
            "run",
            return_value=_curl_result(f"202\n{CF_RAY}\n".encode("ascii")),
        ) as run:
            deliver(config, payload)

        self.assertEqual(run.call_count, 1)
        argv = run.call_args.args[0]
        self.assertEqual(argv, CURL_ARGS)
        self.assertNotIn(URL, argv)
        self.assertNotIn(unicode_secret, argv)
        self.assertNotIn(expected_signature, argv)
        self.assertNotIn(expected_body.decode("utf-8"), argv)
        self.assertIn("café".encode("utf-8"), run.call_args.kwargs["input"])
        self.assertEqual(run.call_args.kwargs, {
            "input": expected_config,
            "stdout": subprocess.PIPE,
            "stderr": subprocess.DEVNULL,
            "timeout": 12,
            "check": False,
            "env": {"PATH": os.environ.get("PATH", os.defpath)},
        })

        # Unicode stays UTF-8 and JSON control/quote/backslash escapes survive
        # both serialization layers byte-for-byte.
        self.assertIn("café 🐕".encode("utf-8"), expected_body)
        self.assertIn(b"\\n", expected_body)
        self.assertIn(b"\\t", expected_body)
        self.assertIn(b"\\r", expected_body)
        self.assertIn(b'\\"', expected_body)
        self.assertIn(b"\\\\", expected_body)
        self.assertIn(
            json.dumps(expected_body.decode("utf-8"), ensure_ascii=False).encode("utf-8"),
            expected_config,
        )

    def test_accepts_success_responses(self):
        for status in (200, 201, 202, 204, 299):
            with self.subTest(status=status):
                with patch.object(
                    t3_webhook.subprocess,
                    "run",
                    return_value=_curl_result(f"{status}\n\n".encode("ascii")),
                ) as run:
                    self.assertIsNone(deliver(load_config(ENV), {"event": "ok"}))
                self.assertEqual(run.call_count, 1)

    def test_http_error_exposes_only_safe_code_and_status(self):
        with patch.object(
            t3_webhook.subprocess,
            "run",
            return_value=_curl_result(f"503\n{CF_RAY}\n".encode("ascii")),
        ) as run:
            with self.assertRaises(DeliveryError) as caught:
                deliver(load_config(ENV), {"event": "error"})

        error = caught.exception
        self.assertEqual(error.code, "http_error")
        self.assertEqual(error.status, 503)
        self.assertEqual(error.cf_ray, CF_RAY)
        self._assert_error_is_sanitized(error)

    def test_redirect_status_is_reported_without_following_or_retrying(self):
        with patch.object(
            t3_webhook.subprocess,
            "run",
            return_value=_curl_result(b"307\n\n", returncode=47),
        ) as run:
            with self.assertRaises(DeliveryError) as caught:
                deliver(load_config(ENV), {"event": "redirect"})
        self.assertEqual((caught.exception.code, caught.exception.status), ("http_error", 307))
        self.assertEqual(run.call_count, 1)
        self.assertNotIn("--location", run.call_args.args[0])
        self.assertNotIn("--retry", run.call_args.args[0])
        self._assert_error_is_sanitized(caught.exception)

    def test_network_and_timeout_errors_do_not_echo_url_or_exception_text(self):
        failures = (
            (OSError(f"private OS error {URL} {SECRET}"), "network_error"),
            (subprocess.SubprocessError(f"private subprocess error {URL} {SECRET}"), "network_error"),
            (subprocess.TimeoutExpired(CURL_ARGS, 12, output=f"private output {URL} {SECRET}"), "timeout"),
        )
        for failure, expected_code in failures:
            with self.subTest(expected_code=expected_code):
                with patch.object(t3_webhook.subprocess, "run", side_effect=failure) as run:
                    with self.assertRaises(DeliveryError) as caught:
                        deliver(load_config(ENV), {"event": "network"})
                self.assertEqual(caught.exception.code, expected_code)
                self.assertIsNone(caught.exception.status)
                self.assertEqual(run.call_count, 1)
                self._assert_error_is_sanitized(caught.exception)

    def test_curl_exit_codes_and_missing_status_are_safe_failures(self):
        for returncode, stdout, expected_code, expected_status in (
            (0, b"", "invalid_response", None),
            (0, b"000\n", "invalid_response", None),
            (0, b"not-status\n", "invalid_response", None),
            (28, b"000\n", "timeout", None),
            (7, b"000\n", "network_error", None),
            (56, b"503\n\n", "http_error", 503),
        ):
            with self.subTest(returncode=returncode, stdout=stdout):
                with patch.object(
                    t3_webhook.subprocess,
                    "run",
                    return_value=_curl_result(stdout, returncode),
                ) as run:
                    with self.assertRaises(DeliveryError) as caught:
                        deliver(load_config(ENV), {"event": "response"})
                self.assertEqual(caught.exception.code, expected_code)
                self.assertEqual(caught.exception.status, expected_status)
                self.assertEqual(run.call_count, 1)
                self._assert_error_is_sanitized(caught.exception)

    def test_invalid_or_oversized_payload_fails_before_network_access(self):
        for payload, expected_code in (
            ([], "invalid_payload"),
            ({"value": object()}, "invalid_payload"),
            ({"value": float("nan")}, "invalid_payload"),
            ({"value": "x" * t3_webhook.MAX_PAYLOAD_BYTES}, "payload_too_large"),
        ):
            with self.subTest(expected_code=expected_code):
                with patch.object(t3_webhook.subprocess, "run") as run:
                    with self.assertRaises(DeliveryError) as caught:
                        deliver(load_config(ENV), payload)
                self.assertEqual(caught.exception.code, expected_code)
                run.assert_not_called()
                self._assert_error_is_sanitized(caught.exception)

    def test_invalid_config_object_fails_safely(self):
        with patch.object(t3_webhook.subprocess, "run") as run:
            with self.assertRaises(DeliveryError) as caught:
                deliver(None, {"event": "bad config"})
        self.assertEqual(caught.exception.code, "invalid_configuration")
        run.assert_not_called()
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
