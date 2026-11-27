from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from flask import Flask

from outlook_web.db import get_db, init_db, register_db
from outlook_web.repositories import accounts, notification_outbox, notification_state


class NotificationQueueResetTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="notification-reset-")
        self.addCleanup(self.directory.cleanup)
        database_path = str(Path(self.directory.name) / "test.db")
        self.environment = patch.dict(
            os.environ,
            {
                "DATABASE_PATH": database_path,
                "SECRET_KEY": "test-secret-key-32bytes-minimum-0000000000000000",
                "LOGIN_PASSWORD": "testpass123",
            },
        )
        self.environment.start()
        self.addCleanup(self.environment.stop)
        init_db(database_path)
        self.app = Flask(__name__)
        register_db(self.app)
        context = self.app.app_context()
        context.push()
        self.addCleanup(context.pop)
        db = get_db()
        row = db.execute(
            """INSERT INTO accounts
               (email, password, client_id, refresh_token, status, account_type, provider, telegram_push_enabled)
               VALUES ('queue-reset@example.com', '', '', '', 'active', 'outlook', 'outlook', 1)"""
        )
        self.account_id = int(row.lastrowid)
        db.commit()
        self.source = {
            "source_type": "account",
            "source_key": "account:queue-reset@example.com",
            "email": "queue-reset@example.com",
            "label": "queue-reset@example.com",
            "account": {"id": self.account_id, "email": "queue-reset@example.com"},
        }
        self.message = {
            "message_id": "reset-message",
            "subject": "Pending notification",
            "received_at": "2026-01-01T00:00:01",
            "content": "private body",
        }

    def enqueue(self, channels=("email", "webhook")):
        cursors = {channel: "2026-01-01T00:00:00" for channel in channels}
        for channel, cursor in cursors.items():
            notification_state.upsert_cursor(channel, "account", self.source["source_key"], cursor)
        return notification_outbox.enqueue_messages(
            self.source, [self.message], cursors, sync_cursor=self.message["received_at"]
        )

    def test_channel_reset_invalidates_old_lease_without_cancelling_other_channel(self):
        self.enqueue()
        jobs = {job["channel"]: job for job in notification_outbox.claim_due_deliveries()}
        notification_state.reset_channel_cursor(
            "email", "account", self.source["source_key"], "2026-01-02T00:00:00"
        )
        email_job = jobs["email"]
        webhook_job = jobs["webhook"]
        self.assertFalse(notification_outbox.finish_delivery(email_job["id"], email_job["lease_token"]))
        self.assertTrue(notification_outbox.finish_delivery(webhook_job["id"], webhook_job["lease_token"]))

    def test_account_disable_cancels_all_pending_channels(self):
        self.enqueue()
        self.assertTrue(accounts.toggle_telegram_push(self.account_id, False))
        self.assertEqual(notification_outbox.claim_due_deliveries(), [])

    def test_reenable_resets_webhook_baseline_and_rejects_stale_fetch(self):
        self.enqueue()
        accounts.toggle_telegram_push(self.account_id, False)
        accounts.toggle_telegram_push(self.account_id, True)
        webhook_cursor = notification_state.get_cursor("webhook", "account", self.source["source_key"])
        telegram_cursor = notification_state.get_cursor("telegram", "account", self.source["source_key"])
        self.assertEqual(webhook_cursor, telegram_cursor)
        self.assertGreater(webhook_cursor, self.message["received_at"])
        added = notification_outbox.enqueue_messages(
            self.source,
            [self.message],
            {"webhook": "2026-01-01T00:00:00"},
            sync_cursor=self.message["received_at"],
        )
        self.assertEqual(added, 0)
        self.assertEqual(notification_outbox.claim_due_deliveries(), [])

    def test_repeated_enable_preserves_pending_work(self):
        self.enqueue(channels=("email",))
        accounts.toggle_telegram_push(self.account_id, True)
        jobs = notification_outbox.claim_due_deliveries()
        self.assertEqual([job["channel"] for job in jobs], ["email"])


if __name__ == "__main__":
    unittest.main()
