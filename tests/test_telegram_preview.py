import tempfile
import time
import unittest
from html.parser import HTMLParser
from pathlib import Path
from unittest.mock import patch
from urllib.parse import urlsplit

from cryptography.fernet import Fernet
from flask import Flask

from outlook_web.db import create_sqlite_connection, init_db
from outlook_web.routes.telegram_preview import create_blueprint
from outlook_web.services import telegram_preview as preview


class _Frames(HTMLParser):
    def __init__(self):
        super().__init__()
        self.frames = []
        self.scripts = 0

    def handle_starttag(self, tag, attrs):
        if tag == "iframe":
            self.frames.append(dict(attrs))
        if tag == "script":
            self.scripts += 1


class TelegramPreviewTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.database = str(Path(self.temp.name) / "preview.db")
        for target, value in (
            ("outlook_web.config.get_database_path", self.database),
            ("outlook_web.config.require_secret_key", "preview-test-secret"),
            ("outlook_web.security.crypto.get_cipher", Fernet(Fernet.generate_key())),
        ):
            mock = patch(target, return_value=value)
            mock.start()
            self.addCleanup(mock.stop)
        self.environment = patch.dict("os.environ", {"TELEGRAM_PUBLIC_URL": "https://mail.example/prefix"})
        self.environment.start()
        self.addCleanup(self.environment.stop)
        init_db(self.database)
        app = Flask(__name__, template_folder=str(Path(__file__).resolve().parents[1] / "templates"))
        app.register_blueprint(create_blueprint())
        self.client = app.test_client()
        self.email = {
            "subject": "Private subject", "sender": "sender@example.com", "received_at": "2026-11-27",
            "content": "private body\nsecond line", "html_content": "",
        }

    def create(self, email=None):
        url = preview.create_telegram_preview_url("recipient@example.com", email or self.email)
        return url.rsplit("/", 1)[1]

    def get(self, token):
        return self.client.get("/telegram/preview/" + token)

    def test_public_token_authorizes_only_its_snapshot_and_encrypts_storage(self):
        token = self.create()
        other = self.create(dict(self.email, content="another message"))
        self.assertNotEqual(token, other)
        response = self.get(token)
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"private body", response.data)
        self.assertNotIn(b"another message", response.data)
        self.assertIn(b"recipient@example.com", response.data)
        self.assertNotIn("recipient", token)
        conn = create_sqlite_connection()
        try:
            stored = conn.execute("SELECT encrypted_payload FROM telegram_preview_snapshots").fetchone()[0]
            self.assertTrue(stored.startswith("enc:"))
            self.assertNotIn("Private subject", stored)
            self.assertNotIn("private body", stored)
        finally:
            conn.close()
        self.assertIn("no-store", response.headers["Cache-Control"])
        self.assertEqual(response.headers["Referrer-Policy"], "no-referrer")
        self.assertNotIn("X-Frame-Options", response.headers)
        self.assertNotIn("frame-ancestors", response.headers["Content-Security-Policy"])
        self.assertNotIn("Set-Cookie", response.headers)

    def test_invalid_tampered_missing_and_wrong_salt_tokens_disclose_nothing(self):
        token = self.create()
        snapshot = preview._serializer().loads(token)
        from itsdangerous import URLSafeTimedSerializer
        wrong_salt = URLSafeTimedSerializer("preview-test-secret", salt="another-purpose").dumps(snapshot)
        for invalid in ("invalid", "x" + token, preview._serializer().dumps("missing"), wrong_salt):
            with self.subTest(token=invalid):
                response = self.get(invalid)
                self.assertEqual(response.status_code, 404)
                self.assertIn("邮件链接已失效", response.get_data(as_text=True))
                self.assertNotIn(b"private body", response.data)
                self.assertIn("no-store", response.headers["Cache-Control"])

    def test_signed_age_expires_even_when_database_expiration_is_in_future(self):
        with patch("itsdangerous.timed.TimestampSigner.get_timestamp", return_value=int(time.time()) - preview.PREVIEW_MAX_AGE - 1):
            token = self.create()
        self.assertEqual(self.get(token).status_code, 404)

    def test_database_expiration_and_creation_cleanup(self):
        token = self.create()
        conn = create_sqlite_connection()
        try:
            with conn:
                conn.execute("UPDATE telegram_preview_snapshots SET expires_at = ?", (time.time() - 1,))
            self.assertEqual(self.get(token).status_code, 404)
            self.create()
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM telegram_preview_snapshots").fetchone()[0], 1)
        finally:
            conn.close()

    def test_html_is_confined_to_opaque_sandbox_and_metadata_is_escaped(self):
        malicious = '<script>parent.document.body.innerHTML="bad"</script><form action="https://evil.example"><input></form><a target="_top" href="https://evil.example">go</a>'
        token = self.create(dict(self.email, subject='<script>bad()</script>', html_content=malicious))
        response = self.get(token)
        parser = _Frames()
        parser.feed(response.get_data(as_text=True))
        self.assertEqual(parser.scripts, 0)
        self.assertEqual(len(parser.frames), 1)
        frame = parser.frames[0]
        self.assertEqual(frame["sandbox"], "")
        self.assertEqual(frame["referrerpolicy"], "no-referrer")
        self.assertIn(malicious, frame["srcdoc"])
        self.assertIn("Content-Security-Policy", frame["srcdoc"])
        self.assertIn("frame-src", frame["srcdoc"])
        self.assertIn("script-src 'none'", response.headers["Content-Security-Policy"])
        self.assertIn("form-action 'none'", response.headers["Content-Security-Policy"])

    def test_https_and_data_images_allowed_with_other_remote_resources_blocked(self):
        response = self.get(self.create(dict(self.email, html_content='<img src="https://images.example/logo.png">')))
        parser = _Frames()
        parser.feed(response.get_data(as_text=True))
        self.assertIn("img-src https: data:", response.headers["Content-Security-Policy"])
        self.assertIn("img-src https: data:", parser.frames[0]["srcdoc"])
        self.assertIn("default-src 'none'", response.headers["Content-Security-Policy"])
        self.assertIn("style-src 'unsafe-inline'", response.headers["Content-Security-Policy"])
        self.assertEqual(parser.frames[0]["sandbox"], "")

    def test_plain_text_is_escaped_and_preserves_newlines(self):
        token = self.create(dict(self.email, content='<img src=x onerror="bad()">\nnext'))
        text = self.get(token).get_data(as_text=True)
        self.assertIn('&lt;img src=x onerror=&#34;bad()&#34;&gt;\nnext', text)
        self.assertIn("white-space: pre-wrap", text)
        self.assertNotIn("<iframe", text)

    def test_invalid_or_missing_public_url_does_not_create_snapshot(self):
        for root in ("", "http://mail.example", "https://user:pass@mail.example", "https://mail.example?secret=1", "https://mail.example/#x", "https://mail.example:bad", "https://mail.example\\evil"):
            with self.subTest(root=root), patch.dict("os.environ", {"TELEGRAM_PUBLIC_URL": root}):
                self.assertIsNone(preview.create_telegram_preview_url("recipient@example.com", self.email))
        conn = create_sqlite_connection()
        try:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM telegram_preview_snapshots").fetchone()[0], 0)
        finally:
            conn.close()
        url = preview.create_telegram_preview_url("recipient@example.com", self.email)
        self.assertTrue(url.startswith("https://mail.example/prefix/telegram/preview/"))
        self.assertNotIn("private body", urlsplit(url).path)


if __name__ == "__main__":
    unittest.main()
