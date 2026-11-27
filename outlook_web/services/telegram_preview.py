"""Seven-day bearer links to encrypted, single-message snapshots."""
from __future__ import annotations

import json
import os
import secrets
import time
from urllib.parse import urlsplit, urlunsplit

from itsdangerous import BadData, URLSafeTimedSerializer

from outlook_web import config
from outlook_web.db import create_sqlite_connection
from outlook_web.security.crypto import decrypt_data, encrypt_data

PREVIEW_MAX_AGE = 7 * 24 * 60 * 60
_SIGNING_SALT = "telegram-single-email-preview-v1"


def _serializer() -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(config.require_secret_key(), salt=_SIGNING_SALT)


def _public_root() -> str | None:
    raw = os.environ.get("TELEGRAM_PUBLIC_URL", "").strip()
    if not raw:
        return None
    try:
        parsed = urlsplit(raw)
        if (parsed.scheme != "https" or not parsed.hostname or parsed.username is not None
                or parsed.password is not None or parsed.query or parsed.fragment
                or any(char.isspace() or ord(char) < 32 for char in raw)
                or "\\" in raw):
            return None
        # Validate the port as well as the host before persisting anything.
        parsed.port
        return urlunsplit((parsed.scheme, parsed.netloc, parsed.path.rstrip("/"), "", ""))
    except ValueError:
        return None


def create_telegram_preview_url(account_email: str, email: dict) -> str | None:
    """Create a snapshot only when a valid public HTTPS root is configured."""
    root = _public_root()
    if root is None:
        return None
    payload = {key: str(email.get(key) or "") for key in
               ("subject", "sender", "received_at", "content", "html_content")}
    payload["recipient"] = str(email.get("recipient") or account_email or "")
    encrypted = encrypt_data(json.dumps(payload, ensure_ascii=False))
    snapshot_id = secrets.token_urlsafe(32)
    token = _serializer().dumps(snapshot_id)
    now = time.time()
    conn = create_sqlite_connection()
    try:
        with conn:
            conn.execute("DELETE FROM telegram_preview_snapshots WHERE expires_at <= ?", (now,))
            conn.execute(
                "INSERT INTO telegram_preview_snapshots (id, encrypted_payload, expires_at) VALUES (?, ?, ?)",
                (snapshot_id, encrypted, now + PREVIEW_MAX_AGE),
            )
    finally:
        conn.close()
    return f"{root}/telegram/preview/{token}"


def load_telegram_preview(token: str) -> dict | None:
    """Require both the signed timestamp and the stored expiration to be valid."""
    try:
        snapshot_id = _serializer().loads(token, max_age=PREVIEW_MAX_AGE)
    except BadData:
        return None
    if not isinstance(snapshot_id, str):
        return None
    conn = create_sqlite_connection()
    try:
        row = conn.execute(
            "SELECT encrypted_payload FROM telegram_preview_snapshots WHERE id = ? AND expires_at > ?",
            (snapshot_id, time.time()),
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        return None
    try:
        payload = json.loads(decrypt_data(row["encrypted_payload"]))
    except (ValueError, RuntimeError, TypeError):
        return None
    if not isinstance(payload, dict) or not all(isinstance(value, str) for value in payload.values()):
        return None
    return payload
