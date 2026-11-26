import json
import unittest
from unittest.mock import patch

import requests

from outlook_web.services.oauth_refresh import post_refresh_token


class OAuthRefreshTransportTests(unittest.TestCase):
    url = "https://login.microsoftonline.com/common/oauth2/v2.0/token"

    def setUp(self):
        self.data = {
            "client_id": "test-client",
            "client_secret": "test-secret",
            "refresh_token": "test-refresh",
            "grant_type": "refresh_token",
            "scope": "https://graph.microsoft.com/.default",
        }
        self.kwargs = {"timeout": 17, "proxies": {"https": "http://proxy.invalid"}}

    @staticmethod
    def response(status, payload):
        response = requests.Response()
        response.status_code = status
        response._content = json.dumps(payload).encode("utf-8")
        return response

    def public_rejection(self, **overrides):
        payload = {
            "error": "invalid_request",
            "error_codes": [90023],
            "error_description": "AADSTS90023: Public clients can't send a client secret.",
        }
        payload.update(overrides)
        return self.response(400, payload)

    def test_public_rejection_retries_without_mutating_first_payload_or_kwargs(self):
        rejected = self.public_rejection()
        success = self.response(200, {"access_token": "test-access"})
        original = self.data.copy()
        with patch("outlook_web.services.oauth_refresh.requests.post", side_effect=[rejected, success]) as post:
            result = post_refresh_token(self.url, data=self.data, **self.kwargs)
        self.assertIs(result, success)
        self.assertEqual(post.call_count, 2)
        first, second = post.call_args_list
        self.assertIs(first.kwargs["data"], self.data)
        self.assertEqual(first.kwargs["data"], original)
        self.assertEqual(self.data, original)
        self.assertIsNot(second.kwargs["data"], self.data)
        self.assertEqual(second.kwargs["data"], {key: value for key, value in original.items() if key != "client_secret"})
        for call in (first, second):
            self.assertEqual(call.args, (self.url,))
            self.assertEqual({key: value for key, value in call.kwargs.items() if key != "data"}, self.kwargs)
            self.assertIs(call.kwargs["proxies"], self.kwargs["proxies"])

    def test_explicit_description_code_and_case_insensitive_phrase_are_supported(self):
        rejected = self.public_rejection(
            error_codes=None,
            error_description="AADSTS90023: PUBLIC CLIENTS CAN'T SEND A CLIENT SECRET.",
        )
        success = self.response(200, {"access_token": "test-access"})
        with patch("outlook_web.services.oauth_refresh.requests.post", side_effect=[rejected, success]) as post:
            self.assertIs(post_refresh_token(self.url, data=self.data), success)
        self.assertEqual(post.call_count, 2)
        self.assertEqual(set(post.call_args.kwargs), {"data"})

    def test_unrelated_errors_preserve_secret_and_do_not_retry(self):
        errors = [
            self.public_rejection(error_description="AADSTS90023: No applicable permissions found."),
            self.public_rejection(error="invalid_client", error_description="AADSTS70002: Client secret required."),
            self.public_rejection(error="invalid_grant", error_codes=[70000], error_description="Grant expired; sign in again."),
            self.public_rejection(error_codes=[123], error_description="Public clients can't send a client secret."),
            self.public_rejection(error_description=None),
            self.response(400, []),
            self.response(429, {"error": "invalid_request", "error_codes": [90023], "error_description": "Public clients can't send a client secret."}),
        ]
        for response in errors:
            with self.subTest(status=response.status_code, body=response.content):
                with patch("outlook_web.services.oauth_refresh.requests.post", return_value=response) as post:
                    self.assertIs(post_refresh_token(self.url, data=self.data), response)
                post.assert_called_once_with(self.url, data=self.data)
                self.assertEqual(post.call_args.kwargs["data"]["client_secret"], "test-secret")

    def test_no_secret_does_not_retry_even_on_public_rejection(self):
        for secret in (None, ""):
            data = self.data.copy()
            if secret is None:
                del data["client_secret"]
            else:
                data["client_secret"] = secret
            with self.subTest(secret=secret):
                rejected = self.public_rejection()
                with patch("outlook_web.services.oauth_refresh.requests.post", return_value=rejected) as post:
                    self.assertIs(post_refresh_token(self.url, data=data), rejected)
                post.assert_called_once_with(self.url, data=data)

    def test_second_failure_is_returned_without_another_retry(self):
        first = self.public_rejection()
        second = self.public_rejection()
        with patch("outlook_web.services.oauth_refresh.requests.post", side_effect=[first, second]) as post:
            self.assertIs(post_refresh_token(self.url, data=self.data), second)
        self.assertEqual(post.call_count, 2)
        self.assertNotIn("client_secret", post.call_args.kwargs["data"])

    def test_invalid_json_returns_original_response(self):
        response = requests.Response()
        response.status_code = 400
        response._content = b"invalid JSON"
        with patch("outlook_web.services.oauth_refresh.requests.post", return_value=response) as post:
            self.assertIs(post_refresh_token(self.url, data=self.data), response)
        post.assert_called_once_with(self.url, data=self.data)
        self.assertEqual(response.content, b"invalid JSON")

    def test_confidential_success_keeps_secret(self):
        response = self.response(200, {"access_token": "test-access"})
        with patch("outlook_web.services.oauth_refresh.requests.post", return_value=response) as post:
            self.assertIs(post_refresh_token(self.url, data=self.data, **self.kwargs), response)
        post.assert_called_once_with(self.url, data=self.data, **self.kwargs)
        self.assertEqual(post.call_args.kwargs["data"]["client_secret"], "test-secret")


if __name__ == "__main__":
    unittest.main()
