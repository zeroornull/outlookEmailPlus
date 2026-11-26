import os
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import MagicMock, patch

import requests
from cryptography.fernet import Fernet
from flask import Flask

from outlook_web.db import close_db
from outlook_web.repositories import settings as settings_repo
from outlook_web.security import crypto
from outlook_web.services import graph, imap


class OAuthClientCredentialTests(unittest.TestCase):
    def setUp(self):
        temporary_directory = tempfile.TemporaryDirectory(prefix="oauth-credentials-")
        self.addCleanup(temporary_directory.cleanup)
        self.database_path = Path(temporary_directory.name) / "credentials.db"
        environment = patch.dict(
            os.environ,
            {
                "DATABASE_PATH": str(self.database_path),
                "OAUTH_CLIENT_ID": "environment-client",
                "OAUTH_CLIENT_SECRET": "environment-secret",
            },
        )
        environment.start()
        self.addCleanup(environment.stop)
        with closing(sqlite3.connect(self.database_path)) as db, db:
            db.execute("CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT)")
        self.app = Flask(__name__)
        self.app.teardown_appcontext(close_db)
        imap.clear_imap_token_cache()
        self.addCleanup(imap.clear_imap_token_cache)

    def _set_settings(self, client_id, secret):
        with closing(sqlite3.connect(self.database_path)) as db, db:
            db.executemany(
                "INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)",
                [("oauth_tool_client_id", client_id), ("oauth_tool_client_secret", secret)],
            )

    def test_matching_environment_credentials_take_precedence_without_database_access(self):
        self._set_settings("environment-client", "old-settings-secret")
        with patch.object(settings_repo, "get_setting") as get_setting:
            self.assertEqual(
                settings_repo.get_oauth_client_secret_for_client("environment-client"),
                "environment-secret",
            )
        get_setting.assert_not_called()

    def test_empty_or_different_database_rows_do_not_hide_environment_credentials(self):
        for stored_client_id in ("", "environment-client", "different-client"):
            with self.subTest(stored_client_id=stored_client_id):
                self._set_settings(stored_client_id, "")
                self.assertEqual(
                    settings_repo.get_oauth_client_secret_for_client("environment-client"),
                    "environment-secret",
                )

    def test_plaintext_settings_secret_is_bound_to_its_own_client(self):
        self._set_settings("stored-client", "stored-secret")
        self.assertEqual(settings_repo.get_oauth_client_secret_for_client("stored-client"), "stored-secret")
        for client_id in ("other-client", "Stored-client", "", "stored-client "):
            with self.subTest(client_id=client_id):
                self.assertEqual(settings_repo.get_oauth_client_secret_for_client(client_id), "")

    def test_encrypted_settings_secret_is_decrypted_only_for_matching_client(self):
        with patch.object(crypto, "get_cipher", return_value=Fernet(Fernet.generate_key())):
            encrypted = crypto.encrypt_data("encrypted-settings-secret")
            self._set_settings("stored-client", encrypted)
            self.assertEqual(
                settings_repo.get_oauth_client_secret_for_client("stored-client"),
                "encrypted-settings-secret",
            )
            with patch.object(settings_repo, "decrypt_data") as decrypt:
                self.assertEqual(settings_repo.get_oauth_client_secret_for_client("other-client"), "")
            decrypt.assert_not_called()

    def test_environment_secret_never_follows_a_different_settings_client(self):
        self._set_settings("stored-client", "")
        self.assertEqual(settings_repo.get_oauth_client_secret_for_client("stored-client"), "")
        self.assertEqual(settings_repo.get_oauth_tool_client_secret(), "")

    def test_unbound_legacy_secret_never_borrows_environment_client_id(self):
        self._set_settings("", "unbound-secret")
        with patch.dict(os.environ, {"OAUTH_CLIENT_SECRET": ""}):
            self.assertEqual(settings_repo.get_oauth_client_secret_for_client("environment-client"), "")
            self.assertEqual(settings_repo.get_oauth_tool_client_secret(), "")

    def test_empty_environment_secret_can_use_settings_bound_to_same_client(self):
        self._set_settings("environment-client", "stored-secret")
        with patch.dict(os.environ, {"OAUTH_CLIENT_SECRET": ""}):
            self.assertEqual(settings_repo.get_oauth_client_secret_for_client("environment-client"), "stored-secret")

    def test_tool_secret_uses_current_tool_client_id(self):
        self.assertEqual(settings_repo.get_oauth_tool_client_secret(), "environment-secret")
        self._set_settings("stored-client", "stored-secret")
        self.assertEqual(settings_repo.get_oauth_tool_client_secret(), "stored-secret")

    def test_public_client_does_not_create_missing_database_without_flask_context(self):
        missing_path = self.database_path.parent / "missing-directory" / "missing.db"
        with patch.dict(os.environ, {"DATABASE_PATH": str(missing_path)}):
            self.assertEqual(settings_repo.get_oauth_client_secret_for_client("public-client"), "")
        self.assertFalse(missing_path.exists())

    def test_public_client_supports_database_without_settings_table(self):
        with closing(sqlite3.connect(self.database_path)) as db, db:
            db.execute("DROP TABLE settings")
        self.assertEqual(settings_repo.get_oauth_client_secret_for_client("public-client"), "")
        with self.app.app_context():
            self.assertEqual(settings_repo.get_oauth_client_secret_for_client("public-client"), "")

    def test_database_failures_are_not_treated_as_missing_credentials(self):
        with patch.object(settings_repo, "get_setting", side_effect=sqlite3.OperationalError("database is locked")):
            with self.assertRaisesRegex(sqlite3.OperationalError, "database is locked"):
                settings_repo.get_oauth_client_secret_for_client("public-client")

    @staticmethod
    def _response():
        response = MagicMock()
        response.status_code = 200
        response.json.return_value = {
            "access_token": "issued-access-token",
            "refresh_token": "rotated-refresh-token",
            "scope": "Mail.Read",
            "expires_in": 3600,
        }
        return response

    def _assert_refresh_payloads(self, client_id, expected_secret):
        calls = (
            ("graph", lambda: graph.get_access_token_graph_result(client_id, "refresh-token"), graph.TOKEN_URL_GRAPH),
            (
                "save-validation",
                lambda: graph.test_refresh_token_with_rotation(
                    client_id,
                    "refresh-token",
                    tenant="consumers",
                    scope="offline_access https://outlook.office.com/IMAP.AccessAsUser.All",
                    max_retries=0,
                ),
                graph.build_token_url("consumers"),
            ),
            ("imap", lambda: imap.get_access_token_imap_result(client_id, "refresh-token"), imap.TOKEN_URL_IMAP),
        )
        for name, refresh, expected_url in calls:
            with self.subTest(refresh=name):
                imap.clear_imap_token_cache()
                with patch.object(requests, "post", return_value=self._response()) as post:
                    result = refresh()
                post.assert_called_once()
                self.assertEqual(post.call_args.args, (expected_url,))
                payload = post.call_args.kwargs["data"]
                self.assertEqual(payload["client_id"], client_id)
                self.assertEqual(payload["grant_type"], "refresh_token")
                self.assertEqual(payload["refresh_token"], "refresh-token")
                if expected_secret:
                    self.assertEqual(payload.get("client_secret"), expected_secret)
                    self.assertNotIn(expected_secret, repr(result))
                else:
                    self.assertNotIn("client_secret", payload)
                if name == "save-validation":
                    self.assertEqual(result, (True, None, "rotated-refresh-token"))
                    self.assertEqual(payload["scope"], "offline_access https://outlook.office.com/IMAP.AccessAsUser.All")
                else:
                    self.assertTrue(result["success"])
                    self.assertEqual(result["access_token"], "issued-access-token")

    def test_all_refresh_payloads_use_matching_environment_secret(self):
        self._set_settings("different-client", "")
        self._assert_refresh_payloads("environment-client", "environment-secret")

    def test_all_refresh_payloads_use_matching_encrypted_settings_secret(self):
        with patch.object(crypto, "get_cipher", return_value=Fernet(Fernet.generate_key())):
            self._set_settings("stored-client", crypto.encrypt_data("encrypted-settings-secret"))
            self._assert_refresh_payloads("stored-client", "encrypted-settings-secret")

    def test_all_refresh_payloads_omit_secret_for_unrelated_public_client(self):
        self._set_settings("stored-client", "stored-secret")
        self._assert_refresh_payloads("public-client", "")

    def test_all_refresh_payloads_omit_secret_for_settings_client_without_secret(self):
        self._set_settings("stored-client", "")
        self._assert_refresh_payloads("stored-client", "")

    def test_all_refresh_payloads_allow_public_client_with_uninitialized_database(self):
        with closing(sqlite3.connect(self.database_path)) as db, db:
            db.execute("DROP TABLE settings")
        self._assert_refresh_payloads("public-client", "")


if __name__ == "__main__":
    unittest.main()
