import threading
import unittest
from concurrent.futures import Future, ThreadPoolExecutor
from email.utils import formatdate
from unittest.mock import MagicMock, patch

from outlook_web.services import graph


def token_response(token="access-one", *, expires_in=100, refresh_token=None):
    response = MagicMock(status_code=200, headers={})
    payload = {"access_token": token, "scope": "Mail.Read"}
    if expires_in is not None:
        payload["expires_in"] = expires_in
    if refresh_token is not None:
        payload["refresh_token"] = refresh_token
    response.json.return_value = payload
    return response


def error_response(status=429, retry_after="10"):
    response = MagicMock(status_code=status, headers={})
    if retry_after is not None:
        response.headers["Retry-After"] = retry_after
    response.json.return_value = {"error": "temporarily_unavailable"}
    response.text = '{"error":"temporarily_unavailable"}'
    return response


class GraphTokenCacheTests(unittest.TestCase):
    def setUp(self):
        graph.clear_graph_token_cache()
        self.addCleanup(graph.clear_graph_token_cache)
        self.secret = patch.object(graph.settings_repo, "get_oauth_client_secret_for_client", return_value="secret")
        self.secret_mock = self.secret.start()
        self.addCleanup(self.secret.stop)
        self.clock = patch.object(graph.time, "monotonic", return_value=100.0)
        self.now = self.clock.start()
        self.addCleanup(self.clock.stop)
        self.transport = patch("outlook_web.services.oauth_refresh.requests.post", return_value=token_response())
        self.post = self.transport.start()
        self.addCleanup(self.transport.stop)

    def fetch(self, client="client", refresh="refresh", proxy=None):
        return graph.get_access_token_graph_result(client, refresh, proxy)

    def test_reuses_token_and_scope_until_server_expiry_with_margin(self):
        first = self.fetch()
        self.assertTrue(first["success"])
        self.assertEqual(first["expires_in"], 100)
        self.assertEqual(first["scope"], "Mail.Read")
        first["access_token"] = "caller-mutation"
        self.now.return_value = 189.9
        self.assertEqual(graph.get_access_token_graph("client", "refresh"), "access-one")
        self.post.assert_called_once()
        self.now.return_value = 190.0
        self.post.return_value = token_response("access-two")
        self.assertEqual(self.fetch()["access_token"], "access-two")
        self.assertEqual(self.post.call_count, 2)

    def test_missing_invalid_or_nonpositive_expiry_does_not_cache_success(self):
        for expiry in (None, 0, -1, "invalid", float("inf"), float("nan"), True, MagicMock()):
            with self.subTest(expiry=expiry):
                graph.clear_graph_token_cache()
                self.post.reset_mock()
                self.post.return_value = token_response(expires_in=expiry)
                self.assertTrue(self.fetch()["success"])
                self.assertTrue(self.fetch()["success"])
                self.assertEqual(self.post.call_count, 2)

    def test_short_expiry_and_request_time_are_not_extended(self):
        self.post.return_value = token_response(expires_in="2")
        self.fetch()
        self.now.return_value = 101.7
        self.fetch()
        self.post.assert_called_once()
        self.now.return_value = 101.8
        self.fetch()
        self.assertEqual(self.post.call_count, 2)

        graph.clear_graph_token_cache()
        self.now.return_value = 200.0

        def slow_refresh(*args, **kwargs):
            self.now.return_value = 202.0
            return token_response(expires_in=1)

        self.post.side_effect = slow_refresh
        self.fetch()
        self.post.side_effect = None
        self.fetch()
        self.assertEqual(self.post.call_count, 4)

    def test_client_refresh_proxy_and_secret_each_separate_keys(self):
        variants = [("client", "refresh", None), ("other", "refresh", None),
                    ("client", "different", None), ("client", "refresh", "http://proxy")]
        for args in variants:
            self.assertTrue(self.fetch(*args)["success"])
        self.assertEqual(self.post.call_count, 4)
        for args in variants:
            self.fetch(*args)
        self.assertEqual(self.post.call_count, 4)
        self.secret_mock.return_value = "changed-secret"
        self.fetch()
        self.assertEqual(self.post.call_count, 5)
        self.assertEqual(self.post.call_args.kwargs["data"]["client_secret"], "changed-secret")
        self.assertTrue(all(len(key) == 64 for key in graph._graph_token_cache))
        self.assertNotIn("changed-secret", repr(tuple(graph._graph_token_cache)))

    def test_rotation_alias_preserves_result_and_expires_with_original(self):
        self.post.return_value = token_response(refresh_token="rotated")
        original = self.fetch()
        aliased = self.fetch(refresh="rotated")
        self.assertEqual(original, aliased)
        self.assertEqual(aliased["new_refresh_token"], "rotated")
        self.assertEqual(aliased["refresh_token"], "rotated")
        self.post.assert_called_once()
        self.now.return_value = 190.0
        self.post.return_value = token_response("access-two", refresh_token="rotated-again")
        self.assertEqual(self.fetch(refresh="rotated")["new_refresh_token"], "rotated-again")
        self.assertEqual(self.post.call_args.kwargs["data"]["refresh_token"], "rotated")
        self.assertEqual(self.post.call_count, 2)
        self.assertEqual(self.fetch(refresh="rotated-again")["access_token"], "access-two")
        self.assertEqual(self.post.call_count, 2)

    def test_invalidation_removes_every_rotation_alias(self):
        self.post.return_value = token_response(refresh_token="rotated")
        self.fetch()
        graph.invalidate_graph_access_token("client", "rotated")
        self.assertEqual(len(graph._graph_token_cache), 0)
        self.fetch()
        self.assertEqual(self.post.call_count, 2)
        graph.clear_graph_token_cache()
        self.fetch(refresh="rotated")
        self.assertEqual(self.post.call_count, 3)

    def test_cache_size_bounds_aliases_and_evicts_least_recent_entry(self):
        with patch.object(graph, "GRAPH_TOKEN_CACHE_MAX_ENTRIES", 3):
            self.fetch(refresh="one")
            self.fetch(refresh="two")
            self.fetch(refresh="three")
            self.fetch(refresh="one")
            self.fetch(refresh="four")
            self.assertEqual(len(graph._graph_token_cache), 3)
            self.fetch(refresh="two")
            self.assertEqual(self.post.call_count, 5)
            graph.clear_graph_token_cache()
            self.post.return_value = token_response(refresh_token="alias")
            self.fetch(refresh="original")
            self.post.return_value = token_response()
            self.fetch(refresh="two")
            self.fetch(refresh="three")
            self.assertLessEqual(len(graph._graph_token_cache), 3)
            self.fetch(refresh="alias")
            self.assertEqual(self.post.call_count, 9)

    def test_retry_after_enforces_per_key_cooldown_without_sleep(self):
        self.post.return_value = error_response()
        with patch.object(graph.time, "sleep") as sleep:
            first = self.fetch()
            self.assertFalse(first["success"])
            self.assertEqual(first["retry_after"], 10.0)
            self.now.return_value = 104.0
            cached = self.fetch()
            self.assertEqual(cached["retry_after"], 6.0)
            self.post.assert_called_once()
            self.post.return_value = token_response()
            self.assertTrue(self.fetch(refresh="other")["success"])
            self.assertEqual(self.post.call_count, 2)
            self.now.return_value = 110.0
            self.assertTrue(self.fetch()["success"])
            self.assertEqual(self.post.call_count, 3)
            sleep.assert_not_called()

    def test_http_date_retry_after_and_non_429_cooldown(self):
        self.post.return_value = error_response(status=503, retry_after=formatdate(1060, usegmt=True))
        with patch.object(graph.time, "time", return_value=1000):
            self.assertEqual(self.fetch()["retry_after"], 60.0)
        self.now.return_value = 115.0
        self.assertEqual(self.fetch()["retry_after"], 45.0)
        self.post.assert_called_once()

    def test_429_invalid_header_gets_finite_default_and_other_errors_are_not_cached(self):
        for value in (None, "invalid", "-1", "nan"):
            with self.subTest(retry_after=value):
                graph.clear_graph_token_cache()
                self.post.return_value = error_response(retry_after=value)
                self.assertEqual(self.fetch()["retry_after"], graph.GRAPH_TOKEN_DEFAULT_RETRY_SECONDS)
        graph.clear_graph_token_cache()
        self.post.reset_mock()
        self.post.return_value = error_response(status=400, retry_after=None)
        self.assertIsNone(self.fetch()["retry_after"])
        self.assertIsNone(self.fetch()["retry_after"])
        self.assertEqual(self.post.call_count, 2)

    def test_concurrent_same_key_refresh_issues_one_post_and_shares_rotation(self):
        workers = 6
        barrier = threading.Barrier(workers)
        release = threading.Event()
        all_waiting = threading.Event()
        wait_lock = threading.Lock()
        waiting = 0

        class ObservedFuture(Future):
            def result(self, timeout=None):
                nonlocal waiting
                with wait_lock:
                    waiting += 1
                    if waiting == workers - 1:
                        all_waiting.set()
                return super().result(timeout=timeout)

        def blocked_post(*args, **kwargs):
            if not release.wait(5):
                raise TimeoutError("test did not release refresh")
            return token_response(refresh_token="rotated")

        def fetch_together():
            barrier.wait(timeout=5)
            return self.fetch()

        self.post.side_effect = blocked_post
        with patch.object(graph, "Future", ObservedFuture), ThreadPoolExecutor(max_workers=workers) as executor:
            results = [executor.submit(fetch_together) for _ in range(workers)]
            try:
                self.assertTrue(all_waiting.wait(5), "same-key callers did not join the active refresh")
            finally:
                release.set()
            values = [future.result(timeout=5) for future in results]
        self.post.assert_called_once()
        self.assertTrue(all(value == values[0] for value in values))
        self.assertEqual(values[0]["new_refresh_token"], "rotated")
        self.assertEqual(self.fetch(refresh="rotated"), values[0])
        self.post.assert_called_once()

    def test_different_keys_refresh_concurrently_and_clear_discards_inflight_cache_write(self):
        first_started = threading.Event()
        second_started = threading.Event()
        release = threading.Event()

        def blocked_post(*args, **kwargs):
            refresh = kwargs["data"]["refresh_token"]
            (first_started if refresh == "first" else second_started).set()
            if not release.wait(5):
                raise TimeoutError("test did not release refresh")
            return token_response()

        self.post.side_effect = blocked_post
        with ThreadPoolExecutor(max_workers=2) as executor:
            first = executor.submit(self.fetch, refresh="first")
            second = executor.submit(self.fetch, refresh="second")
            try:
                self.assertTrue(first_started.wait(5))
                self.assertTrue(second_started.wait(5), "one credential blocked a different credential")
                graph.clear_graph_token_cache()
            finally:
                release.set()
            self.assertTrue(first.result(timeout=5)["success"])
            self.assertTrue(second.result(timeout=5)["success"])
        self.assertEqual(len(graph._graph_token_cache), 0)
        self.assertEqual(len(graph._graph_token_inflight), 0)
        self.assertEqual(self.post.call_count, 2)

    def test_graph_401_invalidates_cached_token_before_next_page_request(self):
        self.fetch()
        with patch.object(graph.requests, "get", return_value=error_response(status=401, retry_after=None)):
            self.assertTrue(graph.get_emails_graph("client", "refresh")["auth_expired"])
        self.post.return_value = token_response("access-two")
        self.assertEqual(self.fetch()["access_token"], "access-two")
        self.assertEqual(self.post.call_count, 2)

    def test_detail_raw_and_delete_401_remove_cached_tokens(self):
        calls = (
            ("get", lambda: graph.get_email_detail_graph("client", "refresh", "message"), error_response(status=401)),
            ("get", lambda: graph.get_email_raw_graph("client", "refresh", "message"), error_response(status=401)),
            ("post", lambda: graph.delete_emails_graph("client", "refresh", ["message"]), error_response(status=401)),
        )
        batch_response = MagicMock(status_code=200, headers={})
        batch_response.json.return_value = {"responses": [{"id": "0", "status": 401}]}
        calls += (("post", lambda: graph.delete_emails_graph("client", "refresh", ["message"]), batch_response),)
        for method, request, response in calls:
            with self.subTest(method=method, request=request):
                graph.clear_graph_token_cache()
                self.fetch()
                with patch.object(graph.requests, method, return_value=response):
                    request()
                self.assertEqual(len(graph._graph_token_cache), 0)


if __name__ == "__main__":
    unittest.main()
