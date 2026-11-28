from __future__ import annotations

import tempfile
import threading
import time
import unittest
from email.message import EmailMessage
from pathlib import Path
from unittest.mock import Mock, patch

from cryptography.fernet import Fernet
from flask import Flask, g, has_app_context

from outlook_web.db import get_db, init_db, register_db
from outlook_web.repositories import notification_outbox as outbox
from outlook_web.repositories import notification_state
from outlook_web.services import notification_dispatch as dispatch
from outlook_web.services import telegram_push
from outlook_web.services.notification_transport import NotificationTransportError


def _imap_server(folders):
    """Deterministic LIST/SELECT/UID fixture with no network or wall clock."""
    conn = Mock()
    conn.list.return_value = ("OK", [b'(\\HasNoChildren) "." "' + name.encode() + b'"' for name in folders])
    conn.response.return_value = ("UIDVALIDITY", [b"77"])

    def select(name, readonly=True):
        return ("OK", []) if name.strip('"') in folders else ("NO", [b"No such mailbox"])

    def uid(command, ids, fields):
        messages = folders[conn.select.call_args.args[0].strip('"')]
        if command == "search":
            return "OK", [b" ".join(messages)]
        requested = ids.split(b",")
        if "INTERNALDATE" in fields:
            return "OK", [b'1 (UID ' + key + b' INTERNALDATE "02-Mar-2026 10:00:00 +0000")' for key in requested]
        return "OK", [(b"1 (UID " + key + b" BODY[])", messages[key]) for key in requested]

    conn.select.side_effect = select
    conn.uid.side_effect = uid
    return conn


class NotificationSyncTests(unittest.TestCase):
    BASELINE = "2026-03-01T00:00:00"
    SCAN_START = "2026-03-02T12:00:00"

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        database = str(Path(temporary.name) / "sync.db")
        for target, value in (
            ("outlook_web.config.get_database_path", database),
            ("outlook_web.config.require_secret_key", "notification-sync-test-secret"),
            ("outlook_web.security.crypto.get_cipher", Fernet(Fernet.generate_key())),
            ("outlook_web.services.notification_dispatch.utc_now_iso", self.SCAN_START),
            ("outlook_web.services.notification_dispatch._is_email_channel_enabled", True),
            ("outlook_web.services.notification_dispatch._get_telegram_runtime_config", {"bot_token": "test", "chat_id": "123"}),
            ("outlook_web.services.notification_dispatch._get_webhook_runtime_config", None),
        ):
            mocked = patch(target, return_value=value)
            mocked.start()
            self.addCleanup(mocked.stop)
        init_db(database)
        self.app = Flask(__name__)
        register_db(self.app)
        self.context = self.app.app_context()
        self.context.push()
        self.addCleanup(self.context.pop)
        self.add_source("mail@example.com")

    def add_source(self, email):
        get_db().execute(
            """INSERT INTO accounts(email, client_id, refresh_token, provider, account_type, telegram_push_enabled, telegram_last_checked_at)
                VALUES (?, 'sync-test-client', 'sync-test-refresh', 'imap', 'imap', 1, ?)""", (email, self.BASELINE)
        )
        get_db().commit()
        for channel in ("email", "telegram"):
            notification_state.upsert_cursor(channel, "account", "account:" + email, self.BASELINE)

    def message(self, key="one", received_at="2026-03-02T10:00:00"):
        return {"message_id": key, "received_at": received_at, "content": "body", "html_content": "<p>body</p>"}

    def run_sync(self):
        dispatch.run_notification_dispatch_job(self.app)

    def test_sync_enqueues_without_sending_and_shares_one_fetch_for_divergent_channels(self):
        notification_state.reset_channel_cursor("telegram", "account", "account:mail@example.com", "2026-03-02T00:00:00")
        old = self.message("old", "2026-03-01T10:00:00")
        fresh = self.message()
        with patch.object(dispatch, "fetch_source_messages", return_value=[old, fresh]) as fetch, patch.object(
            dispatch, "send_business_email_notification"
        ) as email_send, patch.object(dispatch, "send_business_telegram_notification") as telegram_send, patch.object(
            dispatch, "list_email_notification_sources", wraps=dispatch.list_email_notification_sources
        ) as list_sources:
            self.run_sync()
        fetch.assert_called_once()
        list_sources.assert_called_once()
        self.assertEqual(fetch.call_args.args[1], self.BASELINE)
        email_send.assert_not_called()
        telegram_send.assert_not_called()
        tasks = outbox.claim_due_deliveries()
        self.assertEqual({(task["channel"], task["message"]["message_id"]) for task in tasks},
                         {("email", "old"), ("email", "one"), ("telegram", "one")})
        self.assertEqual(outbox.get_sync_cursor("account", "account:mail@example.com"), self.SCAN_START)

    def test_initial_channels_only_set_baseline_without_fetching_history(self):
        get_db().execute("DELETE FROM notification_cursor_states")
        get_db().execute("UPDATE accounts SET telegram_last_checked_at = NULL")
        get_db().commit()
        with patch.object(dispatch, "fetch_source_messages") as fetch:
            self.run_sync()
        fetch.assert_not_called()
        for channel in ("email", "telegram"):
            self.assertEqual(notification_state.get_cursor(channel, "account", "account:mail@example.com"), self.SCAN_START)
        self.assertEqual(outbox.claim_due_deliveries(), [])

    def test_new_channel_does_not_backfill_while_existing_channel_catches_up(self):
        get_db().execute("DELETE FROM notification_cursor_states WHERE channel = 'email'")
        get_db().commit()
        with patch.object(dispatch, "fetch_source_messages", return_value=[self.message()]):
            self.run_sync()
        self.assertEqual([task["channel"] for task in outbox.claim_due_deliveries()], ["telegram"])

    def test_new_channel_queues_every_post_baseline_message_during_existing_channel_fetch(self):
        get_db().execute("DELETE FROM notification_cursor_states WHERE channel = 'email'")
        get_db().commit()
        older = self.message("older")
        first = self.message("first", "2026-03-02T12:01:00")
        second = self.message("second", "2026-03-02T12:02:00")

        def fetch(source, since):
            self.assertEqual(since, self.BASELINE)
            self.assertEqual(notification_state.get_cursor("email", "account", source["source_key"]), self.SCAN_START)
            return [older, first, second]

        with patch.object(dispatch, "fetch_source_messages", side_effect=fetch):
            self.run_sync()
        tasks = outbox.claim_due_deliveries()
        self.assertEqual({task["message"]["message_id"] for task in tasks if task["channel"] == "email"}, {"first", "second"})
        self.assertEqual({task["message"]["message_id"] for task in tasks if task["channel"] == "telegram"}, {"older", "first", "second"})
        self.assertEqual(outbox.get_sync_cursor("account", "account:mail@example.com"), second["received_at"])
        with patch.object(dispatch, "fetch_source_messages", return_value=[second]) as replay:
            self.run_sync()
        self.assertEqual(replay.call_args.args[1], second["received_at"])
        self.assertEqual(outbox.claim_due_deliveries(), [])

    def test_existing_sync_cursor_is_authoritative_and_empty_complete_scan_advances(self):
        source = dispatch.list_email_notification_sources()[0]
        outbox.enqueue_messages(source, [], {"email": self.BASELINE}, sync_cursor="2026-03-02T01:00:00")
        with patch.object(dispatch, "fetch_source_messages", return_value=[]) as fetch:
            self.run_sync()
        self.assertEqual(fetch.call_args.args[1], "2026-03-02T01:00:00")
        self.assertEqual(outbox.get_sync_cursor("account", "account:mail@example.com"), self.SCAN_START)

    def test_reset_channel_baselines_clamp_stale_independent_sync_cursor(self):
        source = dispatch.list_email_notification_sources()[0]
        outbox.enqueue_messages(source, [], {"email": self.BASELINE}, sync_cursor="2026-03-01T01:00:00")
        notification_state.reset_channel_cursor("email", "account", source["source_key"], "2026-03-02T01:00:00")
        notification_state.reset_channel_cursor("telegram", "account", source["source_key"], "2026-03-02T02:00:00")
        with patch.object(dispatch, "fetch_source_messages", return_value=[]) as fetch:
            self.run_sync()
        self.assertEqual(fetch.call_args.args[1], "2026-03-02T01:00:00")
        self.assertEqual(outbox.get_sync_cursor("account", source["source_key"]), self.SCAN_START)

    def test_source_429_cooldown_persists_and_does_not_advance_progress(self):
        with patch.object(dispatch, "fetch_source_messages", side_effect=NotificationTransportError(
            "Rate limited", code="GRAPH_RATE_LIMITED", retry_after=120, status=429
        )) as fetch:
            self.run_sync()
            self.run_sync()
        fetch.assert_called_once()
        self.assertFalse(outbox.source_is_due("account", "account:mail@example.com"))
        self.assertIsNone(outbox.get_sync_cursor("account", "account:mail@example.com"))
        self.assertEqual(outbox.claim_due_deliveries(), [])

    def test_partial_second_folder_failure_never_enqueues_first_folder_or_advances(self):
        with patch.object(telegram_push, "_fetch_new_emails_imap", side_effect=[[self.message()], RuntimeError("partial")]):
            self.run_sync()
        self.assertIsNone(outbox.get_sync_cursor("account", "account:mail@example.com"))
        self.assertEqual(outbox.claim_due_deliveries(), [])

    def test_inbox_only_imap_server_enqueues_shared_notifications_and_advances(self):
        raw = b"Message-ID: <inbox>\r\nSubject: Inbox\r\n\r\nbody"
        conn = _imap_server({"INBOX": {b"1": raw}})
        with patch("imaplib.IMAP4_SSL", return_value=conn), patch.object(
            dispatch, "fetch_source_messages", wraps=dispatch.fetch_source_messages
        ) as fetch:
            self.run_sync()
        fetch.assert_called_once()
        self.assertEqual([call.args[0] for call in conn.select.call_args_list], ["INBOX"])
        conn.list.assert_called_once()
        self.assertEqual({(task["channel"], task["message"]["message_id"]) for task in outbox.claim_due_deliveries()},
                         {("email", "<inbox>"), ("telegram", "<inbox>")})
        self.assertEqual(outbox.get_sync_cursor("account", "account:mail@example.com"), self.SCAN_START)

    def test_listed_junk_select_failure_keeps_existing_progress_and_discards_inbox(self):
        source = dispatch.list_email_notification_sources()[0]
        outbox.enqueue_messages(source, [], {"email": self.BASELINE}, sync_cursor=self.BASELINE)
        conn = _imap_server({"INBOX": {b"1": b"Message-ID: <inbox>\r\n\r\nbody"}, "Junk": {}})
        conn.select.side_effect = lambda name, readonly=True: ("OK", []) if name == "INBOX" else ("NO", [b"Server unavailable"])
        with patch("imaplib.IMAP4_SSL", return_value=conn):
            self.run_sync()
        self.assertEqual(outbox.get_sync_cursor("account", source["source_key"]), self.BASELINE)
        self.assertEqual(outbox.claim_due_deliveries(), [])
        for channel in ("email", "telegram"):
            self.assertEqual(notification_state.get_cursor(channel, "account", source["source_key"]), self.BASELINE)

    def test_special_use_junk_failure_cannot_fall_back_to_another_listed_spam_folder(self):
        conn = _imap_server({"INBOX": {b"1": b"Message-ID: <inbox>\r\n\r\nbody"}, "Junk": {}, "Spam": {}})
        conn.list.return_value = ("OK", [b'(\\Junk) "." "Junk"', b'() "." "Spam"', b'() "." "INBOX"'])
        conn.select.side_effect = lambda name, readonly=True: ("NO", [b"Unavailable"]) if name == "Junk" else ("OK", [])
        with patch("imaplib.IMAP4_SSL", return_value=conn):
            self.run_sync()
        self.assertIsNone(outbox.get_sync_cursor("account", "account:mail@example.com"))
        self.assertEqual(outbox.claim_due_deliveries(), [])
        self.assertEqual([call.args[0] for call in conn.select.call_args_list], ["INBOX", "Junk"])

    def test_unknown_and_malformed_charsets_do_not_block_normal_messages_or_progress(self):
        messages = {}
        for index, charset in enumerate((b"x-unknown", b"invalid charset"), start=1):
            messages[str(index).encode()] = (
                b"Message-ID: <bad-" + str(index).encode() + b">\r\n"
                b"Subject: =?" + (b"x-unknown" if index == 1 else b"utf-8;broken") + b"?b?5rWL6K+V?=\r\n"
                b'Content-Type: text/plain; charset="' + charset + b'"\r\n'
                b"Content-Transfer-Encoding: 8bit\r\n\r\nusable \xe2\x82\xac \xff"
            )
        messages[b"3"] = b"Message-ID: <normal>\r\nSubject: Normal\r\n\r\nnormal body"
        conn = _imap_server({"INBOX": messages})
        with patch("imaplib.IMAP4_SSL", return_value=conn):
            self.run_sync()
        tasks = outbox.claim_due_deliveries()
        self.assertEqual(len(tasks), 6)
        for channel in ("email", "telegram"):
            by_id = {task["message"]["message_id"]: task["message"] for task in tasks if task["channel"] == channel}
            self.assertEqual(set(by_id), {"<bad-1>", "<bad-2>", "<normal>"})
            for key in ("<bad-1>", "<bad-2>"):
                self.assertEqual(by_id[key]["subject"], "测试")
                self.assertEqual(by_id[key]["content"], "usable € �")
            self.assertEqual(by_id["<normal>"]["content"], "normal body")
        self.assertEqual(outbox.get_sync_cursor("account", "account:mail@example.com"), self.SCAN_START)

    def test_queue_capacity_failure_retains_progress_for_retry(self):
        with patch.object(dispatch, "fetch_source_messages", return_value=[self.message()]), patch.object(
            outbox, "enqueue_messages", side_effect=outbox.OutboxFullError("full")
        ):
            self.run_sync()
        self.assertIsNone(outbox.get_sync_cursor("account", "account:mail@example.com"))
        self.assertFalse(outbox.source_is_due("account", "account:mail@example.com"))

    def test_disable_during_fetch_drops_batch_without_progress(self):
        def fetch(source, since):
            get_db().execute("UPDATE accounts SET telegram_push_enabled = 0")
            get_db().commit()
            return [self.message()]
        with patch.object(dispatch, "fetch_source_messages", side_effect=fetch):
            self.run_sync()
        self.assertIsNone(outbox.get_sync_cursor("account", "account:mail@example.com"))
        self.assertEqual(outbox.claim_due_deliveries(), [])

    def test_reset_during_fetch_uses_current_channel_cutoff(self):
        def fetch(source, since):
            notification_state.reset_channel_cursor("email", "account", source["source_key"], self.SCAN_START)
            return [self.message()]
        with patch.object(dispatch, "fetch_source_messages", side_effect=fetch):
            self.run_sync()
        self.assertEqual([task["channel"] for task in outbox.claim_due_deliveries()], ["telegram"])

    def test_same_timestamp_boundary_is_replayed_and_deduplicated(self):
        message = self.message(received_at=self.SCAN_START)
        with patch.object(dispatch, "fetch_source_messages", return_value=[message]):
            self.run_sync()
            self.run_sync()
        tasks = outbox.claim_due_deliveries()
        self.assertEqual(len(tasks), 2)
        self.assertEqual({task["channel"] for task in tasks}, {"email", "telegram"})

    def test_four_source_contexts_and_fast_enqueue_do_not_wait_for_slow_first_source(self):
        for index in range(5):
            self.add_source(f"extra{index}@example.com")
        lock = threading.Lock()
        barrier = threading.Barrier(4)
        fast_enqueued = threading.Event()
        contexts = []
        active = peak = calls = 0
        real_enqueue = outbox.enqueue_messages

        def enqueue(source, *args, **kwargs):
            result = real_enqueue(source, *args, **kwargs)
            if source["email"] != "mail@example.com":
                fast_enqueued.set()
            return result

        def fetch(source, since):
            nonlocal active, peak, calls
            self.assertTrue(has_app_context())
            with lock:
                contexts.append(g._get_current_object())
                active += 1
                calls += 1
                ordinal = calls
                peak = max(peak, active)
            try:
                if ordinal <= 4:
                    barrier.wait(timeout=3)
                if source["email"] == "mail@example.com":
                    self.assertTrue(fast_enqueued.wait(timeout=3))
                return [self.message(source["email"])]
            finally:
                with lock:
                    active -= 1
        with patch.object(dispatch, "fetch_source_messages", side_effect=fetch), patch.object(outbox, "enqueue_messages", side_effect=enqueue):
            self.run_sync()
        self.assertEqual(calls, 6)
        self.assertEqual(peak, 4)
        self.assertEqual(len({id(context) for context in contexts}), 6)
        self.assertEqual(len(outbox.claim_due_deliveries()), 12)

    def test_timestamp_normalization_preserves_subsecond_precision_and_timezone(self):
        self.assertEqual(dispatch._extract_message_timestamp("2026-03-02T18:00:00.1234567+08:00"), "2026-03-02T10:00:00.1234567")
        self.assertEqual(dispatch._extract_message_timestamp("2026-03-02T10:00:00.1200000Z"), "2026-03-02T10:00:00.12")


class GraphNotificationFetchTests(unittest.TestCase):
    def setUp(self):
        mocked = patch("outlook_web.services.graph.get_access_token_graph_result", return_value={"success": True, "access_token": "access"})
        self.token = mocked.start()
        self.addCleanup(mocked.stop)
        self.account = {"id": 10, "client_id": "client", "refresh_token": "refresh", "proxy_url": "http://proxy.example:8888"}

    def response(self, value=None, *, status=200, next_link=None, retry_after=None):
        response = Mock(ok=status < 400, status_code=status, headers={})
        data = {"value": [] if value is None else value}
        if next_link is not None:
            data["@odata.nextLink"] = next_link
        if retry_after is not None:
            response.headers["Retry-After"] = str(retry_after)
        response.json.return_value = data
        return response

    def item(self, key):
        return {"id": key, "receivedDateTime": "2026-03-02T10:00:00.1234567Z", "body": {"contentType": "html", "content": "<p>Original</p>"}}

    def fetch(self):
        return telegram_push._fetch_new_emails_graph(self.account, "2026-03-01T00:00:00")

    def test_full_pagination_keeps_proxy_and_precision_and_does_not_repeat_query_params(self):
        next_link = "https://graph.microsoft.com/v1.0/me/messages?$skiptoken=next"
        with patch.object(telegram_push.requests, "get", side_effect=[self.response([self.item("one")], next_link=next_link), self.response([self.item("two")])]) as get:
            messages = self.fetch()
        self.assertEqual([message["message_id"] for message in messages], ["one", "two"])
        self.assertEqual(messages[0]["received_at"], "2026-03-02T10:00:00.1234567")
        self.assertEqual(messages[0]["html_content"], "<p>Original</p>")
        self.assertIn(" ge ", get.call_args_list[0].kwargs["params"]["$filter"])
        self.assertIsNone(get.call_args_list[1].kwargs["params"])
        self.assertEqual(get.call_args_list[0].kwargs["proxies"], get.call_args_list[1].kwargs["proxies"])

    def test_cross_origin_next_link_is_rejected_before_bearer_can_be_sent(self):
        with patch.object(telegram_push.requests, "get", return_value=self.response(next_link="https://evil.example/messages")) as get:
            with self.assertRaises(NotificationTransportError):
                self.fetch()
        get.assert_called_once()

    def test_page_failure_discards_partial_result_and_exposes_retry_after(self):
        with patch.object(telegram_push.requests, "get", side_effect=[self.response([self.item("one")], next_link="https://graph.microsoft.com/v1.0/me/messages?page=2"), self.response(status=429, retry_after=37)]) as get:
            with self.assertRaises(NotificationTransportError) as raised:
                self.fetch()
        self.assertEqual(raised.exception.status, 429)
        self.assertEqual(raised.exception.retry_after, 37)
        self.assertEqual(get.call_count, 2)

    def test_401_invalidates_cached_access_token(self):
        with patch.object(telegram_push.requests, "get", return_value=self.response(status=401)), patch(
            "outlook_web.services.graph.invalidate_graph_access_token"
        ) as invalidate, self.assertRaises(NotificationTransportError):
            self.fetch()
        invalidate.assert_called_once_with("client", "refresh", self.account["proxy_url"], account_id=10)

    def test_token_failure_is_not_an_empty_mailbox(self):
        self.token.return_value = {"success": False, "error": {"code": "RATE_LIMITED", "status": 429}, "retry_after": 20}
        with patch.object(telegram_push.requests, "get") as get, self.assertRaises(NotificationTransportError) as raised:
            self.fetch()
        get.assert_not_called()
        self.assertEqual(raised.exception.retry_after, 20)

    def test_rotated_refresh_token_is_persisted_and_reused_for_next_folder(self):
        self.account["id"] = 10
        self.token.return_value = {"success": True, "access_token": "access", "new_refresh_token": "rotated"}
        with patch.object(telegram_push.requests, "get", return_value=self.response()):
            self.fetch()
            telegram_push._fetch_new_emails_graph(self.account, "2026-03-01T00:00:00", folder="junkemail")
        self.assertEqual(self.token.call_args.kwargs["account_id"], 10)
        self.assertEqual(self.token.call_args.args[1], "rotated")


class ImapNotificationFetchTests(unittest.TestCase):
    def connection(self, count=1, *, partial=False):
        message = EmailMessage()
        message["Subject"] = "Subject"
        message["Date"] = "Mon, 01 Jan 2001 00:00:00 +0000"
        message.set_content("body")
        conn = Mock()
        conn.select.return_value = ("OK", [])
        conn.response.return_value = ("UIDVALIDITY", [b"77"])
        fetched_batches = []

        def uid(command, ids, fields):
            if command == "search":
                return "OK", [b" ".join(str(index).encode() for index in range(count, 0, -1))]
            requested = ids.split(b",")
            if "INTERNALDATE" in fields:
                return "OK", [b'1 (UID ' + key + b' INTERNALDATE "02-Mar-2026 18:00:00 +0800")' for key in requested]
            fetched_batches.append(requested)
            if partial:
                requested = requested[:-1]
            return "OK", [(b"1 (UID " + key + b" BODY[])", message.as_bytes()) for key in requested]
        conn.uid.side_effect = uid
        return conn, fetched_batches

    def fetch(self, conn, folder="inbox"):
        with patch("imaplib.IMAP4_SSL", return_value=conn):
            return telegram_push._fetch_new_emails_imap({"email": "mail@example.com", "imap_host": "imap.example.com"}, "2026-03-02T10:00:00", folder=folder)

    def test_earliest_uids_are_drained_in_batches_past_old_fifty_limit(self):
        conn, batches = self.connection(120)
        messages = self.fetch(conn)
        self.assertEqual(len(messages), 120)
        self.assertEqual([len(batch) for batch in batches], [50, 50, 20])
        self.assertEqual(batches[0][0], b"1")
        self.assertEqual(batches[-1][-1], b"120")
        self.assertEqual(messages[0]["received_at"], "2026-03-02T10:00:00")
        self.assertTrue(all(message["message_id"].startswith("imap:mail@example.com:inbox:77:") for message in messages))
        conn.logout.assert_called_once()

    def test_incomplete_batch_raises_instead_of_skipping_progress(self):
        conn, _ = self.connection(3, partial=True)
        with self.assertRaises(NotificationTransportError):
            self.fetch(conn)
        conn.logout.assert_called_once()

    def test_uidvalidity_changes_fallback_identity(self):
        conn, _ = self.connection()
        first = self.fetch(conn)[0]
        conn.response.return_value = ("UIDVALIDITY", [b"78"])
        second = self.fetch(conn)[0]
        self.assertNotEqual(first["message_id"], second["message_id"])

    def test_list_discovers_delimited_spam_folder(self):
        conn = _imap_server({"INBOX": {}, "INBOX.Spam": {b"1": b"Message-ID: <spam>\r\n\r\nspam body"}})
        messages = self.fetch(conn, "junkemail")
        self.assertEqual([message["message_id"] for message in messages], ["<spam>"])
        self.assertEqual(messages[0]["folder"], "junkemail")
        conn.select.assert_called_once_with("INBOX.Spam", readonly=True)

    def test_special_use_junk_discovers_unusual_quoted_name(self):
        conn = _imap_server({"INBOX": {}, "Unwanted Mail": {b"1": b"Message-ID: <spam>\r\n\r\nspam body"}})
        conn.list.return_value = ("OK", [b'(\\Inbox) "/" "INBOX"', b'(\\Junk) "/" "Unwanted Mail"'])
        self.assertEqual(self.fetch(conn, "junkemail")[0]["message_id"], "<spam>")
        conn.select.assert_called_once_with('"Unwanted Mail"', readonly=True)

    def test_failed_or_malformed_list_never_establishes_optional_absence(self):
        for response in (("NO", [b"Server unavailable"]), ("OK", [b"unparseable"]),
                         ("OK", [b'(\\HasNoChildren) "." "INBOX"', b"unparseable"]), ("OK", None)):
            with self.subTest(response=response):
                conn = _imap_server({"INBOX": {}})
                conn.list.return_value = response
                with self.assertRaises(NotificationTransportError):
                    self.fetch(conn, "junkemail")
                conn.select.assert_not_called()
                conn.logout.assert_called_once()

    def test_optional_junk_select_protocol_and_transport_failures_propagate(self):
        import imaplib

        for error in (imaplib.IMAP4.error("SELECT failed"), imaplib.IMAP4.abort("connection lost"), OSError("transport failed")):
            with self.subTest(error=error):
                conn = _imap_server({"INBOX": {}, "Junk": {}})
                conn.select.side_effect = error
                with self.assertRaises(type(error)):
                    self.fetch(conn, "junkemail")
                conn.logout.assert_called_once()

    def test_inbox_select_failure_never_becomes_optional_empty_result(self):
        conn = _imap_server({"INBOX": {}})
        conn.select.side_effect = None
        conn.select.return_value = ("NO", [b"Unavailable"])
        with self.assertRaises(NotificationTransportError):
            self.fetch(conn)
        conn.list.assert_not_called()

    def test_authentication_and_listing_transport_errors_propagate(self):
        import imaplib

        for operation in ("login", "list"):
            with self.subTest(operation=operation):
                conn = _imap_server({"INBOX": {}})
                getattr(conn, operation).side_effect = imaplib.IMAP4.abort("connection lost")
                with self.assertRaises(imaplib.IMAP4.abort):
                    self.fetch(conn, "junkemail")
                conn.select.assert_not_called()
                conn.logout.assert_called_once()


class TelegramRateLimitTests(unittest.TestCase):
    def test_429_propagates_retry_after_without_fast_retry(self):
        response = Mock(ok=False, status_code=429, headers={})
        response.json.return_value = {"parameters": {"retry_after": 42}}
        with patch("outlook_web.repositories.settings.get_telegram_proxy_url", return_value=""), patch.object(
            telegram_push.requests, "post", return_value=response
        ) as post, self.assertRaises(NotificationTransportError) as raised:
            telegram_push._send_telegram_message("test", "123", "body")
        self.assertEqual(raised.exception.retry_after, 42)
        self.assertEqual(raised.exception.status, 429)
        post.assert_called_once()

    def test_button_fallback_429_is_propagated(self):
        rejected = Mock(ok=False, status_code=400)
        limited = Mock(ok=False, status_code=429, headers={"Retry-After": "12"})
        limited.json.return_value = {}
        with patch("outlook_web.repositories.settings.get_telegram_proxy_url", return_value=""), patch.object(
            telegram_push.requests, "post", side_effect=[rejected, limited]
        ), self.assertRaises(NotificationTransportError) as raised:
            telegram_push._send_telegram_message("test", "123", "body", {"inline_keyboard": [[{"text": "button"}]]})
        self.assertEqual(raised.exception.retry_after, 12)
