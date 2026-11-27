from __future__ import annotations

import json
import math
import re
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from threading import Lock
from weakref import WeakKeyDictionary

from flask import current_app

from outlook_web.db import get_db
from outlook_web.errors import sanitize_error_details
from outlook_web.repositories.notification_state import build_stable_message_key
from outlook_web.security.crypto import decrypt_data, encrypt_data

PAYLOAD_TTL_SECONDS = 86400
MAX_PENDING_BYTES = 32 * 1024 * 1024
MAX_PENDING_EVENTS = 10000
LEASE_SECONDS = 300
TERMINAL_RETENTION_SECONDS = 86400
DELIVERY_LOG_RETENTION_SECONDS = 14 * 86400
_MESSAGE_FIELDS = ("message_id", "subject", "sender", "received_at", "preview", "content", "html_content", "folder")
_RETENTION_INTERVAL_SECONDS = 60
_retention_sweeps = WeakKeyDictionary()
_retention_lock = Lock()


class OutboxFullError(RuntimeError):
    """The entire fetched batch must be retried without advancing its cursor."""


def _now(value):
    value = time.time() if value is None else float(value)
    if not math.isfinite(value):
        raise ValueError("Notification time must be finite")
    return value


def _delay(value):
    value = float(value)
    if not math.isfinite(value):
        raise ValueError("Notification delay must be finite")
    return max(0.0, value)


def _date(value):
    parsed = datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
    return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed.astimezone(timezone.utc)


def _iso(value):
    return datetime.fromtimestamp(value, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")


@contextmanager
def _transaction():
    db = get_db()
    db.execute("BEGIN IMMEDIATE")
    try:
        yield db
        db.commit()
    except BaseException:
        db.rollback()
        raise


def _stored_bytes(db):
    # Include encrypted bytes and reference metadata, reserving fixed space for leases/numbers.
    return db.execute(
        """SELECT
            COALESCE((SELECT SUM(payload_bytes + length(CAST(source_type AS BLOB))
                + length(CAST(source_key AS BLOB)) + length(CAST(message_key AS BLOB)) + 64)
                FROM notification_outbox_payloads), 0)
            + COALESCE((SELECT SUM(length(CAST(channel AS BLOB)) + length(CAST(source_type AS BLOB))
                + length(CAST(source_key AS BLOB)) + length(CAST(message_key AS BLOB))
                + length(CAST(error_code AS BLOB)) + length(CAST(error_message AS BLOB)) + 256)
                FROM notification_outbox), 0)"""
    ).fetchone()[0]


def _bounded_error(value, max_bytes):
    # Upstream HTTP exceptions can embed bot tokens, proxy credentials or webhook query secrets in URLs.
    message = re.sub(r"https?://[^\s<>\"']+", '[url]', str(value), flags=re.IGNORECASE)
    return sanitize_error_details(message).encode('utf-8')[:max(0, max_bytes)].decode('utf-8', errors='ignore')


def _remove_unused_payloads(db):
    return db.execute(
        """DELETE FROM notification_outbox_payloads WHERE NOT EXISTS (
            SELECT 1 FROM notification_outbox o
            WHERE o.payload_id = notification_outbox_payloads.id
              AND o.status IN ('pending', 'processing'))"""
    ).rowcount


def _record_terminal_logs(db, status, now, where, params):
    db.execute(
        """INSERT INTO notification_delivery_logs
            (channel, source_type, source_key, message_id, status, error_code, error_message, delivered_at)
            SELECT channel, source_type, source_key, message_key, ?, '', '', ?
            FROM notification_outbox WHERE """ + where + """
            ON CONFLICT(channel, source_type, source_key, message_id) DO UPDATE SET
            status = excluded.status, error_code = '', error_message = '', delivered_at = excluded.delivered_at
            WHERE notification_delivery_logs.status != 'sent'""",
        (status, _iso(now), *params),
    )


def _retention_due(force):
    app = current_app._get_current_object()
    tick = time.monotonic()
    with _retention_lock:
        previous = _retention_sweeps.get(app)
        if not force and previous is not None and tick - previous < _RETENTION_INTERVAL_SECONDS:
            return False
        _retention_sweeps[app] = tick
        return True


def _cleanup_retention(db, now):
    terminal = db.execute(
        """DELETE FROM notification_outbox WHERE status NOT IN ('pending', 'processing')
            AND updated_at <= ?""", (now - TERMINAL_RETENTION_SECONDS,)
    ).rowcount
    logs = db.execute(
        "DELETE FROM notification_delivery_logs WHERE julianday(delivered_at) < julianday(?)",
        (_iso(now - DELIVERY_LOG_RETENTION_SECONDS),),
    ).rowcount
    db.execute(
        """UPDATE notification_delivery_logs SET error_code = '', error_message = ''
            WHERE status = 'sent' AND (error_code != '' OR error_message != '')"""
    )
    return terminal, logs


def _cleanup(db, now, *, force_retention=False):
    db.execute("DELETE FROM notification_destination_cooldowns WHERE next_allowed_at <= ?", (now,))
    _record_terminal_logs(db, 'expired', now, "status IN ('pending', 'processing') AND expires_at <= ?", (now,))
    expired = db.execute(
        """UPDATE notification_outbox SET status = 'expired', payload_id = NULL,
            lease_token = NULL, leased_at = NULL, error_code = '', error_message = '', updated_at = ?
            WHERE status IN ('pending', 'processing') AND expires_at <= ?""", (now, now)
    ).rowcount
    payloads = _remove_unused_payloads(db)
    terminal, logs = _cleanup_retention(db, now) if _retention_due(force_retention) else (0, 0)
    return {"expired": expired, "payloads_removed": payloads, "terminal_removed": terminal, "logs_removed": logs}


def get_sync_cursor(source_type: str, source_key: str) -> str | None:
    row = get_db().execute(
        "SELECT sync_cursor FROM notification_sync_states WHERE source_type = ? AND source_key = ?",
        (source_type, source_key),
    ).fetchone()
    return row["sync_cursor"] or None if row else None


def source_is_due(source_type, source_key, *, now: float | None = None) -> bool:
    row = get_db().execute(
        "SELECT next_fetch_at FROM notification_sync_states WHERE source_type = ? AND source_key = ?",
        (source_type, source_key),
    ).fetchone()
    return row is None or row["next_fetch_at"] <= _now(now)


def defer_source(source_type, source_key, delay_seconds: float, *, error: str = '', now=None) -> None:
    now = _now(now)
    with _transaction() as db:
        db.execute(
            """INSERT INTO notification_sync_states
                (source_type, source_key, next_fetch_at, last_error, updated_at) VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(source_type, source_key) DO UPDATE SET
                next_fetch_at = MAX(notification_sync_states.next_fetch_at, excluded.next_fetch_at),
                last_error = excluded.last_error, updated_at = excluded.updated_at""",
            (source_type, source_key, now + _delay(delay_seconds), _bounded_error(error, 512), now),
        )


def get_destination_delay(target: str, *, now=None) -> float:
    now = _now(now)
    row = get_db().execute(
        "SELECT next_allowed_at FROM notification_destination_cooldowns WHERE target = ?", (target,),
    ).fetchone()
    # Waiting is read-only: repeated scheduler ticks must not extend the deadline.
    return max(0.0, row["next_allowed_at"] - now) if row else 0.0


def defer_destination(target: str, delay_seconds: float, *, now=None) -> None:
    if not isinstance(target, str) or re.fullmatch(r"[0-9a-f]{64}", target) is None:
        raise ValueError("Notification destination must be a SHA-256 key")
    now = _now(now)
    deadline = _now(now + _delay(delay_seconds))
    with _transaction() as db:
        db.execute("DELETE FROM notification_destination_cooldowns WHERE next_allowed_at <= ?", (now,))
        if deadline > now:
            db.execute(
                """INSERT INTO notification_destination_cooldowns(target, next_allowed_at) VALUES (?, ?)
                    ON CONFLICT(target) DO UPDATE SET
                    next_allowed_at = MAX(notification_destination_cooldowns.next_allowed_at, excluded.next_allowed_at)""",
                (target, deadline),
            )


def _source_snapshot(source):
    snapshot = {key: str(source.get(key) or '') for key in ("source_type", "source_key", "email", "label")}
    account = source.get("account") or {}
    snapshot["account"] = {"id": account.get("id"), "email": str(account.get("email") or '')}
    return snapshot


def _source_is_active(db, source):
    if source["source_type"] == 'account':
        account = source["account"]
        return db.execute(
            """SELECT 1 FROM accounts WHERE id = ? AND lower(trim(email)) = ?
                AND lower(trim(email)) = ? AND COALESCE(NULLIF(status, ''), 'active') = 'active'
                AND telegram_push_enabled = 1""",
            (account["id"], account["email"].strip().lower(), source["email"].strip().lower()),
        ).fetchone() is not None
    if source["source_type"] == 'temp_email':
        return db.execute(
            """SELECT 1 FROM temp_emails WHERE lower(trim(email)) = ?
                AND COALESCE(NULLIF(status, ''), 'active') = 'active'""",
            (source["email"].strip().lower(),),
        ).fetchone() is not None
    return False


def enqueue_messages(source: dict, messages: list[dict], channel_cursors: dict[str, str], *, sync_cursor: str,
                     now: float | None = None) -> int:
    now = _now(now)
    snapshot = _source_snapshot(source)
    source_type, source_key = snapshot["source_type"], snapshot["source_key"]
    if not source_type or not source_key:
        raise ValueError("Notification source requires a type and key")
    added = 0
    with _transaction() as db:
        _cleanup(db, now)
        if not _source_is_active(db, snapshot):
            return 0
        active_count = db.execute(
            "SELECT COUNT(*) FROM notification_outbox WHERE status IN ('pending', 'processing')"
        ).fetchone()[0]
        payload_bytes = db.execute(
            "SELECT COALESCE(SUM(payload_bytes), 0) FROM notification_outbox_payloads"
        ).fetchone()[0]
        boundaries = {}
        for channel, cursor in channel_cursors.items():
            row = db.execute(
                """SELECT last_cursor_value FROM notification_cursor_states
                    WHERE channel = ? AND source_type = ? AND source_key = ?""",
                (channel, source_type, source_key),
            ).fetchone()
            boundary = _date(cursor) if cursor else None
            if row and row["last_cursor_value"]:
                current = _date(row["last_cursor_value"])
                boundary = max(boundary, current) if boundary else current
            boundaries[channel] = boundary
        for message in messages:
            if not message.get("received_at"):
                raise ValueError("Notification message requires received_at")
            received_at = _date(message["received_at"])
            message_key = build_stable_message_key(
                source_type=source_type, source_key=source_key,
                **{key: message.get(key) for key in ("message_id", "subject", "sender", "received_at", "preview", "content")},
            )
            channels = []
            for channel, boundary in boundaries.items():
                if boundary is not None and received_at < boundary:
                    continue
                if db.execute(
                    """SELECT 1 FROM notification_outbox
                        WHERE channel = ? AND source_type = ? AND source_key = ? AND message_key = ?
                        UNION ALL SELECT 1 FROM notification_delivery_logs
                        WHERE channel = ? AND source_type = ? AND source_key = ? AND message_id = ?
                            AND status IN ('sent', 'expired', 'cancelled')
                        LIMIT 1""",
                    (channel, source_type, source_key, message_key) * 2,
                ).fetchone():
                    continue
                channels.append(channel)
            if not channels:
                continue
            active_count += len(channels)
            if active_count > MAX_PENDING_EVENTS:
                raise OutboxFullError("Notification outbox event capacity exceeded")
            payload = db.execute(
                """SELECT id, expires_at FROM notification_outbox_payloads
                    WHERE source_type = ? AND source_key = ? AND message_key = ?""",
                (source_type, source_key, message_key),
            ).fetchone()
            if payload is None:
                # Explicit fields exclude credentials, attachments and raw protocol responses.
                visible_message = {key: str(message[key] or '') for key in _MESSAGE_FIELDS if key in message}
                encrypted = encrypt_data(json.dumps(
                    {"source": snapshot, "message": visible_message}, ensure_ascii=False, separators=(',', ':'),
                ))
                payload_bytes += len(encrypted.encode('utf-8'))
                if payload_bytes > MAX_PENDING_BYTES:
                    raise OutboxFullError("Notification outbox byte capacity exceeded")
                payload_id = db.execute(
                    """INSERT INTO notification_outbox_payloads
                        (source_type, source_key, message_key, encrypted_payload, payload_bytes, created_at, expires_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?)""",
                    (source_type, source_key, message_key, encrypted, len(encrypted.encode('utf-8')), now,
                     now + PAYLOAD_TTL_SECONDS),
                ).lastrowid
                expires_at = now + PAYLOAD_TTL_SECONDS
            else:
                payload_id, expires_at = payload["id"], payload["expires_at"]
            for channel in channels:
                db.execute(
                    """INSERT INTO notification_outbox
                        (payload_id, channel, source_type, source_key, message_key,
                         next_attempt_at, expires_at, created_at, updated_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (payload_id, channel, source_type, source_key, message_key, now, expires_at, now, now),
                )
                added += 1
        count = db.execute("SELECT COUNT(*) FROM notification_outbox").fetchone()[0]
        if count > MAX_PENDING_EVENTS:
            # Terminal tombstones carry no payload; shed oldest ones before rejecting active work.
            db.execute(
                """DELETE FROM notification_outbox WHERE id IN (
                    SELECT id FROM notification_outbox WHERE status NOT IN ('pending', 'processing')
                    ORDER BY updated_at, id LIMIT ?)""", (count - MAX_PENDING_EVENTS,)
            )
        count = db.execute("SELECT COUNT(*) FROM notification_outbox").fetchone()[0]
        size = _stored_bytes(db)
        if size > MAX_PENDING_BYTES:
            db.execute("DELETE FROM notification_outbox WHERE status NOT IN ('pending', 'processing')")
            size = _stored_bytes(db)
        if count > MAX_PENDING_EVENTS or size > MAX_PENDING_BYTES:
            raise OutboxFullError("Notification outbox capacity exceeded")
        row = db.execute(
            "SELECT sync_cursor FROM notification_sync_states WHERE source_type = ? AND source_key = ?",
            (source_type, source_key),
        ).fetchone()
        previous = row["sync_cursor"] if row else ''
        cursor = sync_cursor or previous
        if previous and (not cursor or _date(previous) > _date(cursor)):
            cursor = previous
        if cursor:
            _date(cursor)
        db.execute(
            """INSERT INTO notification_sync_states
                (source_type, source_key, sync_cursor, next_fetch_at, last_error, updated_at)
                VALUES (?, ?, ?, 0, '', ?) ON CONFLICT(source_type, source_key) DO UPDATE SET
                sync_cursor = excluded.sync_cursor, next_fetch_at = 0, last_error = '', updated_at = excluded.updated_at""",
            (source_type, source_key, cursor, now),
        )
    return added


def claim_due_deliveries(*, limit: int = 50, now: float | None = None, channel: str | None = None) -> list[dict]:
    now = _now(now)
    limit = max(0, min(int(limit), MAX_PENDING_EVENTS))
    claimed = []
    with _transaction() as db:
        _cleanup(db, now)
        if channel is not None:
            rows = db.execute(
                """WITH selected AS (
                    SELECT id, next_attempt_at FROM notification_outbox
                    WHERE channel = ? AND ((status = 'pending' AND next_attempt_at <= ?)
                        OR (status = 'processing' AND leased_at <= ?))
                    ORDER BY next_attempt_at, id LIMIT ?
                ) SELECT o.*, p.encrypted_payload FROM selected s
                    JOIN notification_outbox o ON o.id = s.id
                    JOIN notification_outbox_payloads p ON p.id = o.payload_id
                    ORDER BY s.next_attempt_at, s.id""",
                (channel, now, now - LEASE_SECONDS, limit),
            ).fetchall()
        else:
            rows = db.execute(
                """WITH ranked AS (
                    SELECT id, ROW_NUMBER() OVER (
                        PARTITION BY channel ORDER BY next_attempt_at, id) AS channel_rank
                    FROM notification_outbox
                    WHERE (status = 'pending' AND next_attempt_at <= ?)
                        OR (status = 'processing' AND leased_at <= ?)
                ), selected AS (
                    SELECT id, channel_rank FROM ranked ORDER BY channel_rank, id LIMIT ?
                ) SELECT o.*, p.encrypted_payload FROM selected s
                    JOIN notification_outbox o ON o.id = s.id
                    JOIN notification_outbox_payloads p ON p.id = o.payload_id
                    ORDER BY s.channel_rank, s.id""",
                (now, now - LEASE_SECONDS, limit),
            ).fetchall()
        for row in rows:
            payload = json.loads(decrypt_data(row["encrypted_payload"]))
            token = uuid.uuid4().hex
            db.execute(
                """UPDATE notification_outbox SET status = 'processing', lease_token = ?, leased_at = ?,
                    attempts = attempts + 1, updated_at = ? WHERE id = ?""", (token, now, now, row["id"])
            )
            claimed.append({
                "id": row["id"], "lease_token": token, "channel": row["channel"],
                "source_type": row["source_type"], "source_key": row["source_key"],
                "message_key": row["message_key"], "source": payload["source"], "message": payload["message"],
                "attempts": row["attempts"] + 1, "expires_at": row["expires_at"],
            })
    return claimed


def _owned(db, delivery_id, lease_token, now):
    return db.execute(
        """SELECT * FROM notification_outbox WHERE id = ? AND lease_token = ? AND status = 'processing'
            AND leased_at > ? AND expires_at > ?""", (delivery_id, lease_token, now - LEASE_SECONDS, now)
    ).fetchone()


def delivery_is_owned(delivery_id: int, lease_token: str, *, now=None) -> bool:
    """Admit a send only while its current, unexpired lease remains active."""
    return _owned(get_db(), delivery_id, lease_token, _now(now)) is not None


def _terminal(db, delivery_id, status, now):
    _record_terminal_logs(db, status, now, 'id = ?', (delivery_id,))
    db.execute(
        """UPDATE notification_outbox SET status = ?, payload_id = NULL, lease_token = NULL,
            leased_at = NULL, error_code = '', error_message = '', updated_at = ? WHERE id = ?""",
        (status, now, delivery_id),
    )
    _remove_unused_payloads(db)


def finish_delivery(delivery_id: int, lease_token: str, *, now=None) -> bool:
    now = _now(now)
    with _transaction() as db:
        row = _owned(db, delivery_id, lease_token, now)
        if row is None:
            return False
        _terminal(db, delivery_id, 'sent', now)
    return True


def retry_delivery(delivery_id, lease_token, *, delay_seconds: float, error_code: str,
                   error_message: str, now=None) -> bool:
    now = _now(now)
    next_attempt = now + _delay(delay_seconds)
    with _transaction() as db:
        row = _owned(db, delivery_id, lease_token, now)
        if row is None:
            return False
        if next_attempt >= row["expires_at"]:
            _terminal(db, delivery_id, 'expired', now)
        else:
            available = max(0, MAX_PENDING_BYTES - _stored_bytes(db)
                            + len(row["error_code"].encode('utf-8')) + len(row["error_message"].encode('utf-8')))
            code = _bounded_error(error_code, min(96, available))
            message = _bounded_error(error_message, min(512, available - len(code.encode('utf-8'))))
            db.execute(
                """UPDATE notification_outbox SET status = 'pending', lease_token = NULL, leased_at = NULL,
                    next_attempt_at = ?, error_code = ?, error_message = ?, updated_at = ? WHERE id = ?""",
                (next_attempt, code, message, now, delivery_id),
            )
    return True


def cancel_delivery(delivery_id, lease_token, *, now=None) -> bool:
    now = _now(now)
    with _transaction() as db:
        if _owned(db, delivery_id, lease_token, now) is None:
            return False
        _terminal(db, delivery_id, 'cancelled', now)
    return True


def cancel_source_deliveries(source_type, source_key, channel: str | None = None, *, commit: bool = True) -> None:
    def cancel(db):
        sql = """UPDATE notification_outbox SET status = 'cancelled', payload_id = NULL, lease_token = NULL,
            leased_at = NULL, error_code = '', error_message = '', updated_at = ?
            WHERE source_type = ? AND source_key = ? AND status IN ('pending', 'processing')"""
        params = [time.time(), source_type, source_key]
        if channel is not None:
            sql += " AND channel = ?"
            params.append(channel)
        _record_terminal_logs(db, 'cancelled', params[0],
                              "source_type = ? AND source_key = ? AND status IN ('pending', 'processing')"
                              + (" AND channel = ?" if channel is not None else ''), params[1:])
        db.execute(sql, params)
        _remove_unused_payloads(db)
    if commit:
        with _transaction() as db:
            cancel(db)
    else:
        cancel(get_db())


def cleanup_outbox(*, now=None, force_retention: bool = True) -> dict:
    with _transaction() as db:
        return _cleanup(db, _now(now), force_retention=force_retention)
