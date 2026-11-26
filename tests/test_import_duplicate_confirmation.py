"""Confirm-before-overwrite import for duplicate mailbox addresses."""

import unittest
import uuid

from tests._import_app import clear_login_attempts, import_web_app_module


class ImportDuplicateConfirmationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.module = import_web_app_module()
        cls.app = cls.module.app

    def setUp(self):
        with self.app.app_context():
            clear_login_attempts()
        self.client = self.app.test_client()
        self.assertEqual(self.client.post("/login", json={"password": "testpass123"}).status_code, 200)

    def _conn(self):
        return self.module.create_sqlite_connection()

    def _group(self, name):
        conn = self._conn()
        try:
            cur = conn.execute(
                "INSERT INTO groups (name, description, color) VALUES (?, '', '#111111')",
                (name,),
            )
            conn.commit()
            return int(cur.lastrowid)
        finally:
            conn.close()

    def _insert(self, *, email, group_id, account_type="outlook", token="old-token", imap_password="old-imap", status="disabled"):
        conn = self._conn()
        try:
            cur = conn.execute(
                """
                INSERT INTO accounts (
                    email, password, client_id, refresh_token, account_type, provider,
                    imap_host, imap_port, imap_password, group_id, remark, status
                ) VALUES (?, ?, 'old-cid', ?, ?, ?, 'old.imap', 143, ?, ?, 'keep-remark', ?)
                """,
                (
                    email,
                    self.module.encrypt_data("old-password"),
                    self.module.encrypt_data(token),
                    account_type,
                    "outlook" if account_type == "outlook" else "custom",
                    self.module.encrypt_data(imap_password),
                    group_id,
                    status,
                ),
            )
            account_id = int(cur.lastrowid)
            conn.execute("INSERT INTO tags (name, color) VALUES (?, '#111111')", (f"tag-{account_id}",))
            tag_id = conn.execute("SELECT id FROM tags WHERE name = ?", (f"tag-{account_id}",)).fetchone()["id"]
            conn.execute("INSERT INTO account_tags (account_id, tag_id) VALUES (?, ?)", (account_id, tag_id))
            conn.execute(
                "INSERT INTO account_refresh_logs (account_id, account_email, refresh_type, status, error_message) VALUES (?, ?, 'auto', 'failed', 'old failure')",
                (account_id, email),
            )
            conn.commit()
            return account_id
        finally:
            conn.close()

    def _row(self, account_id):
        conn = self._conn()
        try:
            return conn.execute("SELECT * FROM accounts WHERE id = ?", (account_id,)).fetchone()
        finally:
            conn.close()

    def _counts(self):
        conn = self._conn()
        try:
            accounts = conn.execute("SELECT COUNT(*) AS n FROM accounts").fetchone()["n"]
            groups = conn.execute("SELECT COUNT(*) AS n FROM groups").fetchone()["n"]
            logs = conn.execute("SELECT COUNT(*) AS n FROM audit_logs").fetchone()["n"]
            return accounts, groups, logs
        finally:
            conn.close()

    def test_outlook_confirm_conflict_writes_nothing_including_mixed_batch(self):
        kept = self._group("kept-dup")
        email = f"Mix_{uuid.uuid4().hex}@dupconfirm.test"
        account_id = self._insert(email=email, group_id=kept)
        before = self._counts()
        text = f"{email}----newpass----newcid----newtoken\nfresh_{uuid.uuid4().hex}@dupconfirm.test----p----c----t"
        resp = self.client.post(
            "/api/accounts",
            json={"account_string": text, "provider": "outlook", "group_id": kept, "duplicate_strategy": "confirm"},
        )
        self.assertEqual(resp.status_code, 409)
        body = resp.get_json()
        self.assertEqual(body["error"]["code"], "ACCOUNT_IMPORT_DUPLICATES")
        self.assertEqual(body["duplicates"], [email])
        self.assertEqual(self._counts(), before)
        self.assertEqual(self.module.decrypt_data(self._row(account_id)["refresh_token"]), "old-token")

    def test_confirm_overwrite_keeps_identity_metadata_and_pending_token(self):
        kept = self._group("kept-ow")
        other = self._group("other-ow")
        email = f"Case_{uuid.uuid4().hex}@dupconfirm.test"
        account_id = self._insert(email=email, group_id=kept, status="disabled")
        resp = self.client.post(
            "/api/accounts",
            json={
                "account_string": f"{email.upper()}----newpass----newcid----newtoken",
                "provider": "outlook",
                "group_id": other,
                "duplicate_strategy": "overwrite",
                "preserve_existing_group": True,
            },
        )
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.get_json()["success"])
        self.assertEqual(resp.get_json()["summary"]["imported"], 1)
        row = self._row(account_id)
        self.assertEqual(row["email"], email)
        self.assertEqual(row["group_id"], kept)
        self.assertEqual(row["remark"], "keep-remark")
        self.assertEqual(row["status"], "disabled")
        self.assertEqual(self.module.decrypt_data(row["refresh_token"]), "newtoken")
        self.assertNotEqual(row["refresh_token"], "newtoken")
        conn = self._conn()
        try:
            tags = conn.execute("SELECT COUNT(*) AS n FROM account_tags WHERE account_id = ?", (account_id,)).fetchone()
            latest = conn.execute(
                "SELECT status, error_message FROM account_refresh_logs WHERE account_id = ? ORDER BY id DESC LIMIT 1",
                (account_id,),
            ).fetchone()
            old = conn.execute(
                "SELECT error_message FROM account_refresh_logs WHERE account_id = ? ORDER BY id ASC LIMIT 1",
                (account_id,),
            ).fetchone()
        finally:
            conn.close()
        self.assertEqual(tags["n"], 1)
        self.assertEqual(latest["status"], "pending")
        self.assertIsNone(latest["error_message"])
        self.assertEqual(old["error_message"], "old failure")

    def test_custom_imap_overwrite_preserves_metadata(self):
        kept = self._group("kept-imap")
        other = self._group("other-imap")
        email = f"imap_{uuid.uuid4().hex}@dupconfirm.test"
        account_id = self._insert(email=email, group_id=kept, account_type="imap")
        resp = self.client.post(
            "/api/accounts",
            json={
                "account_string": f"{email}----newimap----custom----mail.example.test----993",
                "provider": "custom",
                "group_id": other,
                "imap_host": "ignored.example",
                "imap_port": 993,
                "duplicate_strategy": "overwrite",
                "preserve_existing_group": True,
            },
        )
        self.assertEqual(resp.status_code, 200)
        row = self._row(account_id)
        self.assertEqual(row["provider"], "custom")
        self.assertEqual(row["group_id"], kept)
        self.assertEqual(row["remark"], "keep-remark")
        self.assertEqual(row["status"], "disabled")
        self.assertEqual(self.module.decrypt_data(row["imap_password"]), "newimap")
        self.assertEqual(row["refresh_token"], "")

    def test_auto_confirm_does_not_create_groups_and_preserve_keeps_group(self):
        kept = self._group("kept-auto")
        email = f"auto_{uuid.uuid4().hex}@dupconfirm.test"
        account_id = self._insert(email=email, group_id=kept)
        before_groups = self._counts()[1]
        text = f"{email}----pwd----cid----tokenpart\ncontinuation"
        resp = self.client.post(
            "/api/accounts",
            json={"account_string": text, "provider": "auto", "duplicate_strategy": "confirm"},
        )
        self.assertEqual(resp.status_code, 409)
        self.assertEqual(resp.get_json()["duplicates"], [email])
        self.assertEqual(self._counts()[1], before_groups)
        resp = self.client.post(
            "/api/accounts",
            json={
                "account_string": text,
                "provider": "auto",
                "duplicate_strategy": "overwrite",
                "preserve_existing_group": True,
            },
        )
        self.assertEqual(resp.status_code, 200)
        row = self._row(account_id)
        self.assertEqual(row["group_id"], kept)
        self.assertEqual(self.module.decrypt_data(row["refresh_token"]), "tokenpartcontinuation")
        self.assertEqual(self._counts()[1], before_groups)

    def test_invalid_replacement_keeps_old_credentials_and_default_skip(self):
        kept = self._group("kept-invalid")
        email = f"bad_{uuid.uuid4().hex}@dupconfirm.test"
        account_id = self._insert(email=email, group_id=kept)
        resp = self.client.post(
            "/api/accounts",
            json={
                "account_string": f"{email}----onlypassword",
                "provider": "outlook",
                "group_id": kept,
                "duplicate_strategy": "overwrite",
            },
        )
        self.assertFalse(resp.get_json()["success"])
        self.assertEqual(self.module.decrypt_data(self._row(account_id)["refresh_token"]), "old-token")
        fresh = f"skip_{uuid.uuid4().hex}@dupconfirm.test"
        first = self.client.post(
            "/api/accounts",
            json={"account_string": f"{fresh}----p----c----t", "provider": "auto", "duplicate_strategy": "skip"},
        )
        self.assertTrue(first.get_json()["success"])
        second = self.client.post(
            "/api/accounts",
            json={"account_string": f"{fresh}----p2----c2----t2", "provider": "auto", "duplicate_strategy": "skip"},
        )
        body = second.get_json()
        self.assertEqual(body["summary"]["skipped"], 1)
        self.assertEqual(body["summary"]["imported"], 0)
        conn = self._conn()
        try:
            row = conn.execute("SELECT refresh_token FROM accounts WHERE email = ?", (fresh,)).fetchone()
        finally:
            conn.close()
        self.assertEqual(self.module.decrypt_data(row["refresh_token"]), "t")
