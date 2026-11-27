"""
B 层：Graph API 权限预检测试

验证 graph.py 的 scope 解析和无权限跳过逻辑。
对应 TDD 矩阵 G-01 ~ G-06。

注意：权限预检功能尚未实现，本测试先行编写（TDD 红灯阶段）。
实现时需在 graph.py 新增 has_mail_read_permission() 并修改 get_access_token_graph_result 返回 scope。
"""

import unittest
from unittest.mock import MagicMock, patch


def _make_graph_token_response(access_token: str, scope: str = "", refresh_token: str = None):
    """构造 MS Graph token endpoint 的 mock Response"""
    resp = MagicMock()
    resp.status_code = 200
    body = {
        "access_token": access_token,
        "scope": scope,
        "token_type": "Bearer",
        "expires_in": 3599,
    }
    if refresh_token:
        body["refresh_token"] = refresh_token
    resp.json.return_value = body
    return resp


def _make_error_response(status: int = 400):
    resp = MagicMock()
    resp.status_code = status
    resp.json.return_value = {"error": "invalid_grant"}
    resp.text = '{"error":"invalid_grant"}'
    resp.headers = {}
    return resp


class TestGraphPermissionPrecheck(unittest.TestCase):
    """Graph API 权限预检测试"""

    def setUp(self):
        from outlook_web.services import graph

        graph.clear_graph_token_cache()
        self.addCleanup(graph.clear_graph_token_cache)
        secret = patch.object(graph.settings_repo, "get_oauth_client_secret_for_client", return_value="")
        secret.start()
        self.addCleanup(secret.stop)

    # ------------------------------------------------------------------
    # has_mail_read_permission 工具函数测试
    # ------------------------------------------------------------------

    def test_has_mail_read_permission_true(self):
        """G-01: scope 含 Mail.Read → True"""
        from outlook_web.services.graph import has_mail_read_permission

        self.assertTrue(has_mail_read_permission("User.Read Mail.Read profile"))

    def test_has_mail_readwrite_permission_true(self):
        """G-02: scope 含 Mail.ReadWrite → True"""
        from outlook_web.services.graph import has_mail_read_permission

        self.assertTrue(has_mail_read_permission("Mail.ReadWrite User.Read"))

    def test_no_mail_permission_returns_false(self):
        """G-03: scope 无邮件权限 → False"""
        from outlook_web.services.graph import has_mail_read_permission

        self.assertFalse(has_mail_read_permission("User.Read profile openid email"))

    def test_empty_scope_returns_false(self):
        """G-04: scope 为空 → False"""
        from outlook_web.services.graph import has_mail_read_permission

        self.assertFalse(has_mail_read_permission(""))
        self.assertFalse(has_mail_read_permission(None))

    def test_scope_case_sensitive(self):
        """G-06: MS scope 区分大小写"""
        from outlook_web.services.graph import has_mail_read_permission

        self.assertFalse(has_mail_read_permission("mail.read user.read"))
        self.assertFalse(has_mail_read_permission("MAIL.READ"))

    # ------------------------------------------------------------------
    # get_access_token_graph_result 返回 scope 字段
    # ------------------------------------------------------------------

    @patch("outlook_web.services.oauth_refresh.requests.post")
    def test_token_result_includes_scope(self, mock_post):
        """token 结果应包含 scope 字段"""
        from outlook_web.services.graph import get_access_token_graph_result

        mock_post.return_value = _make_graph_token_response("at-123", scope="User.Read Mail.Read profile")

        result = get_access_token_graph_result("cid", "rt", proxy_url=None)
        self.assertTrue(result.get("success"))
        self.assertEqual(result.get("scope"), "User.Read Mail.Read profile")

    @patch("outlook_web.services.oauth_refresh.requests.post")
    def test_token_refresh_failure_returns_auth_expired(self, mock_post):
        """G-05: token 刷新失败 → 正常返回错误，不检查 scope"""
        from outlook_web.services.graph import get_access_token_graph_result

        mock_post.return_value = _make_error_response(400)

        result = get_access_token_graph_result("cid", "rt", proxy_url=None)
        self.assertFalse(result.get("success"))
        self.assertIsNone(result.get("scope"))

    # ------------------------------------------------------------------
    # get_emails_graph 跳过无权限调用
    # ------------------------------------------------------------------

    @patch("outlook_web.services.graph.requests.get")
    @patch("outlook_web.services.oauth_refresh.requests.post")
    def test_get_emails_graph_skips_api_when_no_permission(self, mock_post, mock_get):
        """无 Mail.Read 权限时不发起 Graph messages API 请求"""
        from outlook_web.services.graph import get_emails_graph

        mock_post.return_value = _make_graph_token_response("at-123", scope="User.Read profile openid email")

        result = get_emails_graph("cid", "rt", folder="inbox", skip=0, top=1)

        self.assertFalse(result.get("success"))
        self.assertTrue(result.get("no_mail_permission"))
        # Graph messages API 不应被调用
        mock_get.assert_not_called()

    @patch("outlook_web.services.graph.requests.get")
    @patch("outlook_web.services.oauth_refresh.requests.post")
    def test_get_emails_graph_calls_api_when_has_permission(self, mock_post, mock_get):
        """有 Mail.Read 权限时正常调用 Graph API"""
        from outlook_web.services.graph import get_emails_graph

        mock_post.return_value = _make_graph_token_response("at-123", scope="User.Read Mail.Read profile")
        # Graph messages API 返回
        mock_get_resp = MagicMock()
        mock_get_resp.status_code = 200
        mock_get_resp.json.return_value = {"value": []}
        mock_get.return_value = mock_get_resp

        result = get_emails_graph("cid", "rt", folder="inbox", skip=0, top=1)

        # Graph messages API 应被调用
        mock_get.assert_called_once()


class TestGraphGrantedScopeRecovery(unittest.TestCase):
    def setUp(self):
        from outlook_web.services.graph import clear_graph_token_cache

        clear_graph_token_cache()
        self.addCleanup(clear_graph_token_cache)
        secret = patch("outlook_web.services.graph.settings_repo.get_oauth_client_secret_for_client", return_value="secret")
        secret.start()
        self.addCleanup(secret.stop)

    @staticmethod
    def rejection(status=400, error="invalid_request", description="AADSTS90023: No applicable permissions found.", codes=None):
        response = _make_error_response(status)
        response.json.return_value = {
            "error": error,
            "error_description": description,
            "error_codes": [90023] if codes is None else codes,
        }
        return response

    @patch("outlook_web.services.oauth_refresh.requests.post")
    def test_recovers_existing_mail_grant_and_preserves_request_and_rotation(self, post):
        from outlook_web.services import graph

        for codes, description in (([90023], "No applicable permissions found."),
                                   (["90023"], "No applicable permissions found."),
                                   ([], "AADSTS90023: No applicable permissions found.")):
            with self.subTest(codes=codes, description=description):
                graph.clear_graph_token_cache()
                post.reset_mock()
                post.side_effect = [self.rejection(codes=codes, description=description),
                                    _make_graph_token_response("mail-token", "Mail.Read", "rotated")]
                result = graph.get_access_token_graph_result("cid", "rt", "http://proxy")
                self.assertEqual(result, {"success": True, "access_token": "mail-token",
                                          "refresh_token": "rotated", "new_refresh_token": "rotated", "scope": "Mail.Read",
                                          "expires_in": 3599})
                self.assertEqual(post.call_count, 2)
                first, second = post.call_args_list
                self.assertEqual(first.args, (graph.TOKEN_URL_GRAPH,))
                self.assertEqual(second.args, first.args)
                self.assertEqual(first.kwargs["data"], {"client_id": "cid", "grant_type": "refresh_token",
                                                       "refresh_token": "rt", "scope": graph.DEFAULT_GRAPH_SCOPE,
                                                       "client_secret": "secret"})
                self.assertIsNot(first.kwargs["data"], second.kwargs["data"])
                self.assertEqual(second.kwargs["data"], dict(first.kwargs["data"], scope="https://graph.microsoft.com/Mail.Read"))
                for request in (first, second):
                    self.assertEqual(request.kwargs["timeout"], 30)
                    self.assertEqual(request.kwargs["proxies"], {"http": "http://proxy", "https": "http://proxy"})

    @patch("outlook_web.services.oauth_refresh.requests.post")
    def test_other_failures_do_not_request_mail_scope(self, post):
        from outlook_web.services.graph import get_access_token_graph_result

        failures = [self.rejection(status=401), self.rejection(status=429),
                    self.rejection(error="invalid_grant", description="AADSTS70000: Refresh token expired."),
                    self.rejection(error="invalid_client"),
                    self.rejection(description="AADSTS90023: Other public client error."),
                    self.rejection(codes=[70000], description="No applicable permissions found."),
                    self.rejection(description="AADSTS9002331: No applicable permissions found.", codes=[])]
        for response in failures:
            with self.subTest(payload=response.json.return_value, status=response.status_code):
                from outlook_web.services.graph import clear_graph_token_cache

                clear_graph_token_cache()
                post.reset_mock()
                post.side_effect = None
                post.return_value = response
                result = get_access_token_graph_result("cid", "rt")
                self.assertFalse(result["success"])
                post.assert_called_once()

    @patch("outlook_web.services.oauth_refresh.requests.post")
    def test_fallback_failure_reports_final_response_without_another_scope_retry(self, post):
        from outlook_web.services.graph import get_access_token_graph_result

        final = self.rejection(error="invalid_grant", description="AADSTS70000: Explicit grant expired.")
        post.side_effect = [self.rejection(), final]
        result = get_access_token_graph_result("cid", "rt")
        self.assertFalse(result["success"])
        self.assertIn("AADSTS70000: Explicit grant expired.", str(result["error"]))
        self.assertNotIn("No applicable permissions", str(result["error"]))
        self.assertEqual(post.call_count, 2)

    @patch("outlook_web.services.graph.requests.get")
    @patch("outlook_web.services.oauth_refresh.requests.post")
    def test_recovered_grant_reads_messages_and_returns_rotation(self, post, get):
        from outlook_web.services.graph import get_emails_graph

        post.side_effect = [self.rejection(), _make_graph_token_response("mail-token", "Mail.Read", "rotated")]
        get.return_value.status_code = 200
        get.return_value.json.return_value = {"value": [{"id": "message"}]}
        result = get_emails_graph("cid", "rt")
        self.assertEqual(result, {"success": True, "emails": [{"id": "message"}], "new_refresh_token": "rotated"})
        self.assertEqual(get.call_args.kwargs["headers"]["Authorization"], "Bearer mail-token")

    @patch("outlook_web.services.oauth_refresh.requests.post")
    def test_explicit_scope_keeps_public_client_secret_correction(self, post):
        from outlook_web.services.graph import get_access_token_graph_result

        public_error = self.rejection(description="AADSTS90023: Public clients can't send a client secret.")
        post.side_effect = [self.rejection(), public_error, _make_graph_token_response("mail-token", "Mail.Read")]
        self.assertTrue(get_access_token_graph_result("cid", "rt")["success"])
        self.assertEqual(post.call_count, 3)
        explicit, public = post.call_args_list[1:]
        self.assertEqual(explicit.kwargs["data"]["scope"], "https://graph.microsoft.com/Mail.Read")
        self.assertEqual(public.kwargs["data"], {key: value for key, value in explicit.kwargs["data"].items() if key != "client_secret"})


if __name__ == "__main__":
    unittest.main()
