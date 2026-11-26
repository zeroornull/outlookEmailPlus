"""Issue #49: 失效 Token 治理端点与判定逻辑的集成测试。

覆盖:
  - _classify_refresh_failure helper 分类逻辑
  - GET /api/accounts/invalid-token-candidates 候选查询
  - POST /api/accounts/batch-update-status 批量状态更新
  - 两者闭环联动（候选 → 批量停用）
"""

from contextlib import closing
import json
import sqlite3
import tempfile
import unittest
import uuid
from unittest.mock import patch

from tests._import_app import clear_login_attempts, import_web_app_module


class InvalidTokenGovernanceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.module = import_web_app_module()
        cls.app = cls.module.app

    def setUp(self):
        from outlook_web import config

        private_dir = tempfile.TemporaryDirectory()
        self.addCleanup(private_dir.cleanup)
        private_path = f"{private_dir.name}/governance.db"
        source = self.module.create_sqlite_connection()
        try:
            with closing(sqlite3.connect(private_path)) as destination:
                source.backup(destination)
        finally:
            source.close()
        database_path = patch.object(config, "get_database_path", return_value=private_path)
        database_path.start()
        self.addCleanup(database_path.stop)
        with self.app.app_context():
            clear_login_attempts()

    def test_import_only_log_sets_last_refresh_time(self):
        client = self.app.test_client()
        self._login(client)
        with self.app.app_context():
            from outlook_web.db import get_db

            account_id, email = self._insert_account_direct()
            db = get_db()
            timestamp = db.execute(
                "SELECT datetime(MAX(COALESCE((SELECT MAX(created_at) FROM account_refresh_logs), "
                "datetime('now')), datetime('now')), '+1 second')"
            ).fetchone()[0]
            db.execute(
                """INSERT INTO account_refresh_logs
                   (account_id, account_email, status, refresh_type, created_at)
                   VALUES (?, ?, 'success', 'oauth_import', ?)""",
                (account_id, email, timestamp),
            )
            db.commit()
        stats = client.get("/api/accounts/refresh-stats").get_json()["stats"]
        self.assertEqual(stats["last_refresh_time"], timestamp)

    # ---------- helpers ----------

    def _login(self, client, password: str = "testpass123"):
        resp = client.post("/login", json={"password": password})
        self.assertEqual(resp.status_code, 200)
        return resp

    def _insert_account_direct(self, email=None):
        """直接向 DB 插入一个 Outlook 账号，返回 (account_id, email)。"""
        from outlook_web.db import get_db
        from outlook_web.security.crypto import encrypt_data

        suffix = uuid.uuid4().hex[:8]
        email = email or f"govtest_{suffix}@outlook.com"
        db = get_db()
        cursor = db.execute(
            """
            INSERT INTO accounts (email, password, refresh_token, client_id, account_type, provider, status)
            VALUES (?, ?, ?, ?, 'outlook', 'outlook', 'active')
            """,
            (email, "unused", encrypt_data(f"rt_{suffix}"), "test-client-id"),
        )
        db.commit()
        self.addCleanup(self._delete_account_fixture, cursor.lastrowid)
        return cursor.lastrowid, email

    def _insert_refresh_log(self, account_id, account_email, status, error_message, refresh_type="manual"):
        """直接向 account_refresh_logs 插入一条记录。"""
        from outlook_web.db import get_db

        db = get_db()
        db.execute(
            """
            INSERT INTO account_refresh_logs
                (account_id, account_email, refresh_type, status, error_message)
            VALUES (?, ?, ?, ?, ?)
            """,
            (account_id, account_email, refresh_type, status, error_message),
        )
        db.commit()

    def _delete_account_fixture(self, account_id):
        from outlook_web.db import get_db

        with self.app.app_context():
            db = get_db()
            db.execute("DELETE FROM account_refresh_logs WHERE account_id = ?", (account_id,))
            db.execute("DELETE FROM accounts WHERE id = ?", (account_id,))
            db.commit()

    def test_equal_timestamp_import_recovery_updates_all_log_views(self):
        client = self.app.test_client()
        self._login(client)
        baseline = client.get("/api/accounts/refresh-stats").get_json()["stats"]
        with self.app.app_context():
            from outlook_web.db import get_db

            account_id, email = self._insert_account_direct()
            db = get_db()
            timestamp = db.execute("SELECT datetime('now')").fetchone()[0]
            for status, refresh_type, error in (
                ("failed", "manual", "invalid_grant"),
                ("success", "oauth_import", None),
            ):
                db.execute(
                    """INSERT INTO account_refresh_logs
                       (account_id, account_email, status, refresh_type, error_message, created_at)
                       VALUES (?, ?, ?, ?, ?, ?)""",
                    (account_id, email, status, refresh_type, error, timestamp),
                )
            db.commit()

        for endpoint, key in (
            ("refresh-logs/failed", "logs"),
            ("invalid-token-candidates", "candidates"),
        ):
            rows = client.get(f"/api/accounts/{endpoint}").get_json()[key]
            self.assertNotIn(account_id, [row["account_id"] for row in rows])
        stats = client.get("/api/accounts/refresh-stats").get_json()["stats"]
        self.assertEqual(stats["failed_count"], baseline["failed_count"])
        self.assertEqual(stats["success_count"], baseline["success_count"] + 1)
        self.assertGreaterEqual(stats["last_refresh_time"], timestamp)
        for endpoint in ("refresh-logs", f"{account_id}/refresh-logs"):
            rows = client.get(f"/api/accounts/{endpoint}").get_json()["logs"]
            own = [row for row in rows if row["account_id"] == account_id]
            self.assertEqual([row["status"] for row in own], ["success", "failed"])
            self.assertEqual(own[0]["refresh_type"], "oauth_import")
            self.assertGreater(own[0]["id"], own[1]["id"])

    def test_equal_timestamp_failure_after_import_remains_failed(self):
        client = self.app.test_client()
        self._login(client)
        baseline = client.get("/api/accounts/refresh-stats").get_json()["stats"]
        with self.app.app_context():
            from outlook_web.db import get_db

            account_id, email = self._insert_account_direct()
            db = get_db()
            timestamp = db.execute("SELECT datetime('now')").fetchone()[0]
            for status, refresh_type, error in (
                ("success", "oauth_import", None),
                ("failed", "manual", "invalid_grant"),
            ):
                db.execute(
                    """INSERT INTO account_refresh_logs
                       (account_id, account_email, status, refresh_type, error_message, created_at)
                       VALUES (?, ?, ?, ?, ?, ?)""",
                    (account_id, email, status, refresh_type, error, timestamp),
                )
            db.commit()
        for endpoint, key in (
            ("refresh-logs/failed", "logs"),
            ("invalid-token-candidates", "candidates"),
        ):
            rows = client.get(f"/api/accounts/{endpoint}").get_json()[key]
            self.assertEqual(len([row for row in rows if row["account_id"] == account_id]), 1)
        stats = client.get("/api/accounts/refresh-stats").get_json()["stats"]
        self.assertEqual(stats["failed_count"], baseline["failed_count"] + 1)
        self.assertEqual(stats["success_count"], baseline["success_count"])

    def test_pending_credentials_clear_current_failure_without_claiming_refresh_success(self):
        client = self.app.test_client()
        self._login(client)
        baseline = client.get("/api/accounts/refresh-stats").get_json()["stats"]
        with self.app.app_context():
            from outlook_web.db import get_db

            account_id, email = self._insert_account_direct()
            self._insert_account_direct()
            db = get_db()
            timestamp = db.execute("SELECT datetime('now')").fetchone()[0]
            for status, refresh_type, error in (
                ("failed", "manual", "invalid_grant"),
                ("pending", "credentials_updated", None),
            ):
                db.execute(
                    """INSERT INTO account_refresh_logs
                       (account_id, account_email, status, refresh_type, error_message, created_at)
                       VALUES (?, ?, ?, ?, ?, ?)""",
                    (account_id, email, status, refresh_type, error, timestamp),
                )
            db.commit()

        stats = client.get("/api/accounts/refresh-stats").get_json()["stats"]
        self.assertEqual(stats["total"], baseline["total"] + 2)
        self.assertEqual(stats["pending_count"], baseline["pending_count"] + 2)
        self.assertEqual(stats["success_count"], baseline["success_count"])
        self.assertEqual(stats["failed_count"], baseline["failed_count"])
        for endpoint, key in (
            ("refresh-logs/failed", "logs"),
            ("invalid-token-candidates", "candidates"),
        ):
            rows = client.get(f"/api/accounts/{endpoint}").get_json()[key]
            self.assertNotIn(account_id, [row["account_id"] for row in rows])
        accounts = client.get("/api/accounts").get_json()["accounts"]
        listed = next(account for account in accounts if account["id"] == account_id)
        detail = client.get(f"/api/accounts/{account_id}").get_json()["account"]
        for account in (listed, detail):
            self.assertEqual(account["last_refresh_status"], "pending")
            self.assertIsNone(account["last_refresh_error"])
            self.assertFalse(account["last_refresh_at"])
        for endpoint in ("refresh-logs", f"{account_id}/refresh-logs"):
            rows = client.get(f"/api/accounts/{endpoint}").get_json()["logs"]
            own = [row for row in rows if row["account_id"] == account_id]
            self.assertEqual([row["status"] for row in own], ["pending", "failed"])
            self.assertEqual(own[0]["refresh_type"], "credentials_updated")
            self.assertIsNone(own[0]["error_message"])
            self.assertGreater(own[0]["id"], own[1]["id"])

        with self.app.app_context():
            from outlook_web.db import get_db

            db = get_db()
            future = db.execute("SELECT datetime('now', '+10 years')").fetchone()[0]
            db.execute(
                "UPDATE account_refresh_logs SET created_at = ? WHERE account_id = ? AND status = 'pending'",
                (future, account_id),
            )
            db.commit()
        self.assertEqual(client.get("/api/accounts/refresh-stats").get_json()["stats"]["last_refresh_time"],
                         stats["last_refresh_time"])
        with self.app.app_context():
            self._insert_refresh_log(account_id, email, "success", None, "oauth_import")
        recovered = client.get("/api/accounts/refresh-stats").get_json()["stats"]
        self.assertEqual(recovered["pending_count"], baseline["pending_count"] + 1)
        self.assertEqual(recovered["success_count"], baseline["success_count"] + 1)
        for endpoint in ("refresh-logs", f"{account_id}/refresh-logs"):
            rows = client.get(f"/api/accounts/{endpoint}").get_json()["logs"]
            own = [row for row in rows if row["account_id"] == account_id]
            self.assertEqual([row["status"] for row in own], ["success", "pending", "failed"])
        detail = client.get(f"/api/accounts/{account_id}").get_json()["account"]
        self.assertEqual(detail["last_refresh_status"], "success")
        self.assertIsNone(detail["last_refresh_error"])
        with self.app.app_context():
            self._insert_refresh_log(account_id, email, "failed", "invalid_grant")
        for endpoint, key in (
            ("refresh-logs/failed", "logs"),
            ("invalid-token-candidates", "candidates"),
        ):
            rows = client.get(f"/api/accounts/{endpoint}").get_json()[key]
            self.assertEqual(len([row for row in rows if row["account_id"] == account_id]), 1)
        failed = client.get("/api/accounts/refresh-stats").get_json()["stats"]
        self.assertEqual(failed["pending_count"], baseline["pending_count"] + 1)
        self.assertEqual(failed["failed_count"], baseline["failed_count"] + 1)
        self.assertEqual(failed["success_count"], baseline["success_count"])

    # ---------- _classify_refresh_failure unit tests ----------

    def test_classify_invalid_grant_error(self):
        """invalid_grant 错误应被分类为 invalid_token。"""
        from outlook_web.services.refresh import _classify_refresh_failure

        result = _classify_refresh_failure("AADSTS70000: invalid_grant - token expired")
        self.assertTrue(result.get("is_invalid_token"))
        self.assertEqual(result.get("reason_code"), "INVALID_GRANT_OR_AADSTS70000")

    def test_classify_aadsts70000_error(self):
        """AADSTS70000 错误应被分类为 invalid_token。"""
        from outlook_web.services.refresh import _classify_refresh_failure

        result = _classify_refresh_failure("AADSTS70000: The token has been revoked")
        self.assertTrue(result.get("is_invalid_token"))
        self.assertEqual(result.get("reason_code"), "INVALID_GRANT_OR_AADSTS70000")

    def test_classify_normal_error_not_invalid_token(self):
        """普通网络错误不应被分类为 invalid_token。"""
        from outlook_web.services.refresh import _classify_refresh_failure

        result = _classify_refresh_failure("ConnectionTimeout: failed to connect to graph.microsoft.com")
        self.assertFalse(result.get("is_invalid_token"))

    def test_classify_none_error(self):
        """None error_message 应返回非 invalid_token。"""
        from outlook_web.services.refresh import _classify_refresh_failure

        result = _classify_refresh_failure(None)
        self.assertFalse(result.get("is_invalid_token"))

    def test_classify_empty_error(self):
        """空字符串应返回非 invalid_token。"""
        from outlook_web.services.refresh import _classify_refresh_failure

        result = _classify_refresh_failure("")
        self.assertFalse(result.get("is_invalid_token"))

    # ---------- invalid-token-candidates endpoint ----------

    def test_get_invalid_token_candidates_returns_empty_when_no_logs(self):
        """没有 invalid_grant 日志时应返回空的候选项（结构正确）。"""
        client = self.app.test_client()
        self._login(client)

        resp = client.get("/api/accounts/invalid-token-candidates")
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertTrue(data.get("success"))
        self.assertIsInstance(data.get("candidates"), list)
        # 注：测试间共享 DB，可能有前序测试残留的候选，
        # 此处只检验结构完整性，不严格断言数量为 0
        self.assertIn("total", data)

    def test_get_invalid_token_candidates_returns_classified_account(self):
        """有 invalid_grant 日志时应返回对应候选。"""
        client = self.app.test_client()
        self._login(client)

        with self.app.app_context():
            account_id, email = self._insert_account_direct()
            self._insert_refresh_log(account_id, email, "failed", "AADSTS70000: invalid_grant - token expired")

        resp = client.get("/api/accounts/invalid-token-candidates")
        data = resp.get_json()
        self.assertTrue(data.get("success"))
        candidates = data.get("candidates", [])
        found = [c for c in candidates if c["account_id"] == account_id]
        self.assertTrue(
            len(found) > 0, f"应至少有1条候选包含 account_id={account_id}，实际: {[c['account_id'] for c in candidates]}"
        )
        self.assertTrue(found[0].get("is_invalid_token"))
        self.assertEqual(found[0].get("reason_code"), "INVALID_GRANT_OR_AADSTS70000")

    def test_get_invalid_token_candidates_excludes_normal_error(self):
        """普通失败的账号不应出现在候选列表。"""
        client = self.app.test_client()
        self._login(client)

        with self.app.app_context():
            account_id, email = self._insert_account_direct()
            self._insert_refresh_log(account_id, email, "failed", "NetworkError: timeout")

        resp = client.get("/api/accounts/invalid-token-candidates")
        data = resp.get_json()
        self.assertTrue(data.get("success"))
        candidates = data.get("candidates", [])
        found = [c for c in candidates if c["account_id"] == account_id]
        self.assertEqual(len(found), 0, "普通网络错误不应出现在 invalid token 候选中")

    # ---------- batch-update-status endpoint ----------

    def test_batch_update_status_to_inactive(self):
        """批量将账号状态置为 inactive。"""
        client = self.app.test_client()
        self._login(client)

        with self.app.app_context():
            account_id_1, _ = self._insert_account_direct()
            account_id_2, _ = self._insert_account_direct()

        resp = client.post(
            "/api/accounts/batch-update-status",
            json={"account_ids": [account_id_1, account_id_2], "status": "inactive"},
        )
        data = resp.get_json()
        self.assertTrue(data.get("success"), f"批量停用应成功: {data}")
        self.assertEqual(data.get("updated_count"), 2)

        # 验证状态确实变了
        for aid in [account_id_1, account_id_2]:
            detail_resp = client.get(f"/api/accounts/{aid}")
            detail = detail_resp.get_json()
            self.assertEqual(detail["account"]["status"], "inactive")

    def test_batch_update_status_rejects_empty_ids(self):
        """空 account_ids 应被拒绝。"""
        client = self.app.test_client()
        self._login(client)

        resp = client.post(
            "/api/accounts/batch-update-status",
            json={"account_ids": [], "status": "inactive"},
        )
        data = resp.get_json()
        self.assertFalse(data.get("success"))
        self.assertIn("ACCOUNT_IDS_REQUIRED", data.get("error", {}).get("code", ""))

    def test_batch_update_status_rejects_invalid_status(self):
        """无效的 status 值应被拒绝。"""
        client = self.app.test_client()
        self._login(client)

        with self.app.app_context():
            account_id, _ = self._insert_account_direct()

        resp = client.post(
            "/api/accounts/batch-update-status",
            json={"account_ids": [account_id], "status": "bogus_status"},
        )
        data = resp.get_json()
        self.assertFalse(data.get("success"))

    # ---------- governance loop: candidates → batch inactive ----------

    def test_governance_loop_candidates_then_batch_inactive(self):
        """端到端闭环：插入 invalid_grant 日志 → 查到候选 → 批量停用。"""
        client = self.app.test_client()
        self._login(client)

        # 1) 创建账号
        with self.app.app_context():
            account_id, email = self._insert_account_direct()
            self._insert_refresh_log(account_id, email, "failed", "AADSTS70000: The token has expired due to inactivity")

        # 2) 查询候选
        resp = client.get("/api/accounts/invalid-token-candidates")
        data = resp.get_json()
        self.assertTrue(data.get("success"))
        candidates = data.get("candidates", [])
        found = [c for c in candidates if c["account_id"] == account_id]
        self.assertTrue(len(found) > 0, f"应能查到候选 account_id={account_id}")
        self.assertTrue(found[0].get("is_invalid_token"))

        # 3) 批量停用
        resp = client.post(
            "/api/accounts/batch-update-status",
            json={"account_ids": [account_id], "status": "inactive"},
        )
        data = resp.get_json()
        self.assertTrue(data.get("success"), f"批量停用应成功: {data}")

        # 4) 验证状态
        detail_resp = client.get(f"/api/accounts/{account_id}")
        detail = detail_resp.get_json()
        self.assertEqual(detail["account"]["status"], "inactive")


if __name__ == "__main__":
    unittest.main()
