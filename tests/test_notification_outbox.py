from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from cryptography.fernet import Fernet
from flask import Flask

from outlook_web.db import get_db, init_db, register_db
from outlook_web.repositories import notification_outbox as outbox
from outlook_web.repositories import notification_state
from outlook_web.security.crypto import decrypt_data


class NotificationOutboxTests(unittest.TestCase):
    NOW = 1800000000.0
    BASELINE = '2026-03-01T00:00:00'
    CURSOR = '2026-03-01T00:00:01'

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.database = str(Path(temporary.name) / 'outbox.db')
        for target, value in (
            ('outlook_web.config.get_database_path', self.database),
            ('outlook_web.config.require_secret_key', 'notification-outbox-test-secret'),
            ('outlook_web.security.crypto.get_cipher', Fernet(Fernet.generate_key())),
        ):
            mocked = patch(target, return_value=value)
            mocked.start()
            self.addCleanup(mocked.stop)
        init_db(self.database)
        self.app = Flask(__name__)
        register_db(self.app)
        self.context = self.app.app_context()
        self.context.push()
        self.addCleanup(lambda: self.context.pop())
        account_id = get_db().execute(
            """INSERT INTO accounts(email, client_id, refresh_token, telegram_push_enabled)
                VALUES ('mail@example.com', 'client-secret', 'refresh-secret', 1)"""
        ).lastrowid
        get_db().commit()
        self.source = {
            'source_type': 'account', 'source_key': 'account:mail@example.com',
            'email': 'mail@example.com', 'label': 'Mail',
            'account': {'id': account_id, 'email': 'mail@example.com', 'refresh_token': 'refresh-secret',
                        'password': 'password-secret', 'proxy_url': 'http://user:proxy-secret@host'},
            'bot_token': 'bot-secret', 'webhook_token': 'webhook-secret',
        }
        self.message = {
            'message_id': 'message-1', 'received_at': self.CURSOR, 'subject': 'Private subject',
            'sender': 'sender@example.com', 'content': 'Private body', 'preview': 'Private preview',
            'html_content': '<p>Private body</p>', 'folder': 'inbox',
            'access_token': 'access-secret', 'attachments': [{'content': 'attachment-secret'}],
            'raw_mime': 'mime-secret',
        }

    def enqueue(self, messages=None, channels=None, cursor=None, now=None):
        return outbox.enqueue_messages(
            self.source, [self.message] if messages is None else messages,
            {'telegram': self.BASELINE} if channels is None else channels,
            sync_cursor=self.CURSOR if cursor is None else cursor,
            now=self.NOW if now is None else now,
        )

    def claim(self, now=None, **kwargs):
        return outbox.claim_due_deliveries(now=self.NOW if now is None else now, **kwargs)

    def count(self, table):
        return get_db().execute('SELECT COUNT(*) FROM ' + table).fetchone()[0]

    def test_schema_v27_migration_is_idempotent(self):
        self.enqueue()
        get_db().execute("UPDATE settings SET value = '26' WHERE key = 'db_schema_version'")
        get_db().commit()
        init_db(self.database)
        init_db(self.database)
        self.assertEqual(get_db().execute("SELECT value FROM settings WHERE key = 'db_schema_version'").fetchone()[0], '27')
        self.assertEqual(self.count('notification_outbox'), 1)
        self.assertEqual(self.count('notification_outbox_payloads'), 1)
        self.assertEqual(self.count('notification_destination_cooldowns'), 0)

    def test_shared_payload_is_encrypted_and_strips_credentials_and_raw_data(self):
        self.assertEqual(self.enqueue(channels={'telegram': self.BASELINE, 'email': self.BASELINE}), 2)
        self.assertEqual(self.count('notification_outbox_payloads'), 1)
        encrypted = get_db().execute('SELECT encrypted_payload FROM notification_outbox_payloads').fetchone()[0]
        self.assertTrue(encrypted.startswith('enc:'))
        self.assertNotIn('Private body', encrypted)
        payload = json.loads(decrypt_data(encrypted))
        self.assertEqual(set(payload['source']), {'source_type', 'source_key', 'email', 'label', 'account'})
        self.assertEqual(payload['source']['account'], {'id': self.source['account']['id'], 'email': 'mail@example.com'})
        for secret in ('refresh-secret', 'password-secret', 'proxy-secret', 'bot-secret', 'webhook-secret',
                       'access-secret', 'attachment-secret', 'mime-secret'):
            self.assertNotIn(secret, json.dumps(payload))
        self.assertEqual(payload['message']['html_content'], '<p>Private body</p>')
        self.assertEqual(len(self.claim()), 2)

    def test_large_notification_visible_body_is_preserved(self):
        message = dict(self.message, content='正文' * 40000, html_content='<div>' + '邮件' * 50000 + '</div>')
        self.enqueue([message])
        claimed = self.claim()[0]
        self.assertEqual(claimed['message']['content'], message['content'])
        self.assertEqual(claimed['message']['html_content'], message['html_content'])

    def test_restart_retains_progress_and_reclaims_abandoned_lease(self):
        self.enqueue()
        first = self.claim()[0]
        self.context.pop()
        self.context = self.app.app_context()
        self.context.push()
        self.assertEqual(outbox.get_sync_cursor('account', self.source['source_key']), self.CURSOR)
        self.assertEqual(self.claim(now=self.NOW + 299), [])
        recovered = self.claim(now=self.NOW + 300)[0]
        self.assertEqual(recovered['id'], first['id'])
        self.assertEqual(recovered['attempts'], 2)
        self.assertNotEqual(recovered['lease_token'], first['lease_token'])
        self.assertEqual(recovered['message']['subject'], 'Private subject')

    def test_capacity_failure_rolls_back_entire_batch_and_sync_progress(self):
        self.enqueue([], cursor=self.BASELINE)
        with patch.object(outbox, 'MAX_PENDING_EVENTS', 1), self.assertRaises(outbox.OutboxFullError):
            self.enqueue([self.message, dict(self.message, message_id='message-2')])
        self.assertEqual(self.count('notification_outbox'), 0)
        self.assertEqual(self.count('notification_outbox_payloads'), 0)
        self.assertEqual(outbox.get_sync_cursor('account', self.source['source_key']), self.BASELINE)
        self.enqueue()
        self.assertEqual(self.count('notification_outbox'), 1)

    def test_byte_capacity_failure_preserves_preexisting_payload_and_cursor(self):
        self.enqueue()
        with patch.object(outbox, 'MAX_PENDING_BYTES', 1), self.assertRaises(outbox.OutboxFullError):
            self.enqueue([dict(self.message, message_id='message-2')], cursor='2026-03-01T00:00:02')
        self.assertEqual(self.count('notification_outbox_payloads'), 1)
        self.assertEqual(outbox.get_sync_cursor('account', self.source['source_key']), self.CURSOR)

    def test_stale_worker_cannot_complete_retry_or_cancel_new_lease(self):
        self.enqueue()
        old = self.claim()[0]
        current = self.claim(now=self.NOW + 301)[0]
        arguments = (old['id'], old['lease_token'])
        self.assertFalse(outbox.finish_delivery(*arguments, now=self.NOW + 302))
        self.assertFalse(outbox.cancel_delivery(*arguments, now=self.NOW + 302))
        self.assertFalse(outbox.retry_delivery(*arguments, delay_seconds=1, error_code='X', error_message='', now=self.NOW + 302))
        self.assertEqual(self.count('notification_delivery_logs'), 0)
        self.assertTrue(outbox.finish_delivery(current['id'], current['lease_token'], now=self.NOW + 302))
        self.assertEqual(self.count('notification_delivery_logs'), 1)
        self.assertEqual(self.count('notification_outbox_payloads'), 0)

    def test_expired_lease_cannot_finish_even_before_reclaim(self):
        self.enqueue()
        claim = self.claim()[0]
        self.assertFalse(outbox.finish_delivery(claim['id'], claim['lease_token'], now=self.NOW + 300))
        self.assertEqual(self.count('notification_delivery_logs'), 0)

    def test_duplicate_batch_replay_and_legacy_sent_log_are_skipped(self):
        notification_state.upsert_delivery_log(
            'email', 'account', self.source['source_key'], self.message['message_id'],
            status='sent', delivered_at=outbox._iso(self.NOW),
        )
        channels = {'telegram': self.BASELINE, 'email': self.BASELINE}
        self.assertEqual(self.enqueue([self.message, self.message], channels=channels), 1)
        self.assertEqual(self.enqueue(channels=channels), 0)
        claim = self.claim()[0]
        self.assertTrue(outbox.finish_delivery(claim['id'], claim['lease_token'], now=self.NOW + 1))
        outbox.cleanup_outbox(now=self.NOW + outbox.TERMINAL_RETENTION_SECONDS + 1)
        self.assertEqual(self.enqueue(channels=channels, now=self.NOW + outbox.TERMINAL_RETENTION_SECONDS + 2), 0)

    def test_missing_message_id_uses_stable_fallback_dedup(self):
        message = dict(self.message)
        message.pop('message_id')
        self.assertEqual(self.enqueue([message, message]), 1)
        self.assertTrue(self.claim()[0]['message_key'].startswith('fallback:'))

    def test_inclusive_boundary_preserves_fractional_order_and_timezone(self):
        messages = [dict(self.message, message_id='older', received_at='2026-03-01T00:00:00.099999Z'),
                    dict(self.message, message_id='equal', received_at='2026-03-01T01:00:00.100000+01:00'),
                    dict(self.message, message_id='newer', received_at='2026-03-01T00:00:00.100001Z')]
        self.assertEqual(self.enqueue(messages, channels={'email': '2026-03-01T00:00:00.100000Z'}), 2)
        self.assertEqual({row['message_key'] for row in self.claim()}, {'equal', 'newer'})

    def test_destination_cooldown_max_upsert_and_wait_reads_survive_restart(self):
        target = 'a' * 64
        outbox.defer_destination(target, 60, now=self.NOW)
        outbox.defer_destination(target, 10, now=self.NOW + 1)
        outbox.defer_destination(target, 90, now=self.NOW + 2)
        self.context.pop()
        self.context = self.app.app_context()
        self.context.push()
        db = get_db()
        changes = db.total_changes
        self.assertEqual(outbox.get_destination_delay(target, now=self.NOW + 10), 82)
        self.assertEqual(outbox.get_destination_delay(target, now=self.NOW + 20), 72)
        self.assertEqual(db.total_changes, changes)
        self.assertEqual(db.execute('SELECT next_allowed_at FROM notification_destination_cooldowns').fetchone()[0], self.NOW + 92)
        self.assertEqual(self.count('notification_sync_states'), 0)

    def test_destination_cooldown_expiry_cleanup_and_input_validation(self):
        for delay in (float('nan'), float('inf'), -float('inf')):
            with self.subTest(delay=delay), self.assertRaises(ValueError):
                outbox.defer_destination('a' * 64, delay, now=self.NOW)
        with self.assertRaises(ValueError):
            outbox.defer_destination('https://private/token?secret=value', 60, now=self.NOW)
        outbox.defer_destination('a' * 64, 10, now=self.NOW)
        outbox.defer_destination('b' * 64, 20, now=self.NOW)
        self.assertEqual(outbox.get_destination_delay('a' * 64, now=self.NOW + 10), 0)
        outbox.cleanup_outbox(now=self.NOW + 10, force_retention=False)
        self.assertEqual(self.count('notification_destination_cooldowns'), 1)
        outbox.defer_destination('c' * 64, 30, now=self.NOW + 20)
        self.assertEqual([row[0] for row in get_db().execute('SELECT target FROM notification_destination_cooldowns')], ['c' * 64])
        outbox.defer_destination('d' * 64, -1, now=self.NOW + 20)
        self.assertEqual(self.count('notification_destination_cooldowns'), 1)

    def test_delivery_admission_requires_current_unexpired_uncancelled_lease(self):
        self.enqueue()
        first = self.claim()[0]
        self.assertTrue(outbox.delivery_is_owned(first['id'], first['lease_token'], now=self.NOW))
        self.assertFalse(outbox.delivery_is_owned(first['id'], 'wrong-owner', now=self.NOW))
        self.assertFalse(outbox.delivery_is_owned(first['id'], first['lease_token'], now=self.NOW + 300))
        current = self.claim(now=self.NOW + 300)[0]
        self.assertFalse(outbox.delivery_is_owned(first['id'], first['lease_token'], now=self.NOW + 300))
        self.assertTrue(outbox.delivery_is_owned(current['id'], current['lease_token'], now=self.NOW + 300))
        outbox.cancel_source_deliveries('account', self.source['source_key'])
        self.assertFalse(outbox.delivery_is_owned(current['id'], current['lease_token'], now=self.NOW + 300))

    def _delivery_mocks(self, service, sender, *, build_channels=None):
        from contextlib import ExitStack

        stack = ExitStack()
        self.addCleanup(stack.close)
        runtime = {'bot_token': 'private-bot-token', 'chat_id': '123'}
        stack.enter_context(patch.object(service.dispatch, '_get_telegram_runtime_config', return_value=runtime))
        stack.enter_context(patch.object(service, '_resolve_current_source', return_value=self.source))
        stack.enter_context(patch.object(service.dispatch, '_build_active_channels_for_source',
                                        side_effect=build_channels or (lambda *args, **kwargs: [('telegram', sender)])))
        stack.enter_context(patch.object(service.random, 'uniform', return_value=1.0))
        with service._gate_lock:
            service._destination_gates.clear()
        self.addCleanup(service._destination_gates.clear)
        return runtime

    def test_retry_after_blocks_second_real_task_after_runtime_and_database_restart(self):
        from unittest.mock import Mock

        from outlook_web.services import notification_delivery as service
        from outlook_web.services.notification_transport import NotificationTransportError

        self.enqueue([self.message, dict(self.message, message_id='second')])
        sender = Mock(side_effect=NotificationTransportError('private detail', status=429, retry_after=60))
        runtime = self._delivery_mocks(service, sender)
        service.run_notification_delivery_job(self.app, 'telegram', now=self.NOW, monotonic_now=100)
        sender.assert_called_once()
        with service._gate_lock:
            service._destination_gates.clear()
        self.context.pop()
        self.context = self.app.app_context()
        self.context.push()
        target, _ = service._target_for_channel('telegram', runtime, None)
        self.assertEqual(outbox.get_destination_delay(target, now=self.NOW + 59), 1)
        persisted = get_db().execute('SELECT target FROM notification_destination_cooldowns').fetchone()[0]
        self.assertEqual(persisted, target)
        self.assertNotIn('private-bot-token', persisted)
        sender.reset_mock(side_effect=True)
        with patch.object(outbox, 'claim_due_deliveries', wraps=outbox.claim_due_deliveries) as claim:
            for elapsed in (1, 30, 59):
                service.run_notification_delivery_job(self.app, 'telegram', now=self.NOW + elapsed, monotonic_now=elapsed)
            claim.assert_not_called()
        sender.assert_not_called()
        rows = get_db().execute('SELECT message_key, attempts, next_attempt_at FROM notification_outbox ORDER BY id').fetchall()
        self.assertEqual([(row['message_key'], row['attempts']) for row in rows], [('message-1', 1), ('second', 0)])
        self.assertEqual(rows[0]['next_attempt_at'], self.NOW + 60)
        self.assertEqual(outbox.get_destination_delay(target, now=self.NOW + 59), 1)
        service.run_notification_delivery_job(self.app, 'telegram', now=self.NOW + 60, monotonic_now=60)
        sender.assert_called_once()
        self.assertEqual(sender.call_args.args[1]['message_id'], 'second')

    def test_reset_after_claim_before_sender_rejects_cached_payload(self):
        from unittest.mock import Mock

        from outlook_web.services import notification_delivery as service

        self.enqueue()
        sender = Mock()

        def reset_then_build(*args, **kwargs):
            row = get_db().execute('SELECT status FROM notification_outbox').fetchone()
            self.assertEqual(row['status'], 'processing')
            notification_state.reset_channel_cursor('telegram', 'account', self.source['source_key'], self.CURSOR)
            return [('telegram', sender)]

        self._delivery_mocks(service, sender, build_channels=reset_then_build)
        with patch.object(outbox, 'retry_delivery', wraps=outbox.retry_delivery) as retry, patch.object(
            outbox, 'finish_delivery', wraps=outbox.finish_delivery,
        ) as finish:
            service.run_notification_delivery_job(self.app, 'telegram', now=self.NOW, monotonic_now=100)
        sender.assert_not_called()
        retry.assert_not_called()
        finish.assert_not_called()
        self.assertEqual(get_db().execute('SELECT status FROM notification_outbox').fetchone()[0], 'cancelled')
        self.assertEqual(self.count('notification_outbox_payloads'), 0)


    def test_current_reset_cutoff_overrides_fetch_snapshot_and_sync_never_regresses(self):
        notification_state.upsert_cursor('email', 'account', self.source['source_key'], '2026-03-01T00:00:02')
        self.assertEqual(self.enqueue(channels={'email': self.BASELINE}, cursor='2026-03-01T00:00:03'), 0)
        self.enqueue([], cursor=self.BASELINE)
        self.assertEqual(outbox.get_sync_cursor('account', self.source['source_key']), '2026-03-01T00:00:03')
        self.assertEqual(notification_state.get_cursor('email', 'account', self.source['source_key']), '2026-03-01T00:00:02')

    def test_source_deferral_survives_restart_and_does_not_advance_cursor(self):
        outbox.defer_source('account', self.source['source_key'], 60, error='access_token=secret', now=self.NOW)
        self.assertIsNone(outbox.get_sync_cursor('account', self.source['source_key']))
        self.context.pop()
        self.context = self.app.app_context()
        self.context.push()
        self.assertFalse(outbox.source_is_due('account', self.source['source_key'], now=self.NOW + 59))
        self.assertTrue(outbox.source_is_due('account', self.source['source_key'], now=self.NOW + 60))
        stored = get_db().execute('SELECT last_error FROM notification_sync_states').fetchone()[0]
        self.assertNotIn('secret', stored)
        self.enqueue([], now=self.NOW + 61)
        self.assertTrue(outbox.source_is_due('account', self.source['source_key'], now=self.NOW + 61))

    def test_retry_is_persistent_bounded_and_scheduled_without_new_lease(self):
        self.enqueue()
        claimed = self.claim()[0]
        self.assertTrue(outbox.retry_delivery(
            claimed['id'], claimed['lease_token'], delay_seconds=60, error_code='HTTP_429',
            error_message='access_token=secret ' + '错' * 1000, now=self.NOW + 1,
        ))
        stored = get_db().execute('SELECT * FROM notification_outbox').fetchone()
        self.assertLessEqual(len(stored['error_message'].encode('utf-8')), 512)
        self.assertNotIn('secret', stored['error_message'])
        self.assertIsNone(stored['lease_token'])
        self.assertEqual(self.claim(now=self.NOW + 60), [])
        retried = self.claim(now=self.NOW + 61)[0]
        self.assertEqual(retried['attempts'], 2)
        self.assertNotEqual(retried['lease_token'], claimed['lease_token'])

    def test_retry_beyond_ttl_expires_and_cleans_payload_immediately(self):
        self.enqueue()
        claimed = self.claim()[0]
        self.assertTrue(outbox.retry_delivery(
            claimed['id'], claimed['lease_token'], delay_seconds=outbox.PAYLOAD_TTL_SECONDS,
            error_code='HTTP_429', error_message='rate limited', now=self.NOW + 1,
        ))
        self.assertEqual(self.count('notification_outbox_payloads'), 0)
        self.assertEqual(self.claim(now=self.NOW + 2), [])
        self.assertEqual(get_db().execute('SELECT status FROM notification_outbox').fetchone()[0], 'expired')

    def test_cleanup_expires_processing_and_pending_and_removes_orphan_payload(self):
        self.enqueue(channels={'telegram': self.BASELINE, 'email': self.BASELINE})
        claim = self.claim(limit=1)[0]
        result = outbox.cleanup_outbox(now=self.NOW + outbox.PAYLOAD_TTL_SECONDS)
        self.assertEqual(result['expired'], 2)
        self.assertEqual(self.count('notification_outbox_payloads'), 0)
        self.assertFalse(outbox.finish_delivery(claim['id'], claim['lease_token'], now=self.NOW + outbox.PAYLOAD_TTL_SECONDS))
        self.assertEqual({row[0] for row in get_db().execute('SELECT status FROM notification_delivery_logs')}, {'expired'})

    def test_finishing_shared_refs_removes_payload_only_after_last_active_channel(self):
        self.enqueue(channels={'telegram': self.BASELINE, 'email': self.BASELINE})
        first, second = self.claim()
        self.assertTrue(outbox.finish_delivery(first['id'], first['lease_token'], now=self.NOW + 1))
        self.assertEqual(self.count('notification_outbox_payloads'), 1)
        self.assertTrue(outbox.cancel_delivery(second['id'], second['lease_token'], now=self.NOW + 1))
        self.assertEqual(self.count('notification_outbox_payloads'), 0)
        self.assertEqual({row[0] for row in get_db().execute('SELECT status FROM notification_delivery_logs')}, {'sent', 'cancelled'})

    def test_source_cancel_can_join_and_rollback_caller_transaction(self):
        self.enqueue(channels={'telegram': self.BASELINE, 'email': self.BASELINE})
        db = get_db()
        db.execute('BEGIN IMMEDIATE')
        outbox.cancel_source_deliveries('account', self.source['source_key'], commit=False)
        self.assertEqual(self.count('notification_outbox_payloads'), 0)
        db.rollback()
        self.assertEqual(self.count('notification_outbox_payloads'), 1)
        outbox.cancel_source_deliveries('account', self.source['source_key'], 'telegram')
        self.assertEqual(self.count('notification_outbox_payloads'), 1)
        remaining = self.claim()
        self.assertEqual([row['channel'] for row in remaining], ['email'])
        outbox.cancel_source_deliveries('account', self.source['source_key'])
        self.assertEqual(self.count('notification_outbox_payloads'), 0)

    def test_channel_round_robin_claim_does_not_starve_email_and_webhook(self):
        self.enqueue([dict(self.message, message_id=str(number)) for number in range(60)])
        self.enqueue(channels={'email': self.BASELINE, 'webhook': self.BASELINE})
        claims = self.claim(limit=3)
        self.assertEqual({row['channel'] for row in claims}, {'telegram', 'email', 'webhook'})

    def test_inactive_or_replaced_account_cannot_enqueue_or_advance_progress(self):
        db = get_db()
        for field, value in (('status', 'disabled'), ('telegram_push_enabled', 0), ('id', self.source['account']['id'] + 100)):
            with self.subTest(field=field):
                db.execute('SAVEPOINT source_change')
                db.execute('UPDATE accounts SET ' + field + ' = ?', (value,))
                db.execute('RELEASE SAVEPOINT source_change')
                db.commit()
                self.assertEqual(self.enqueue(), 0)
                self.assertIsNone(outbox.get_sync_cursor('account', self.source['source_key']))
                db.execute("UPDATE accounts SET status = 'active', telegram_push_enabled = 1, id = ?",
                           (self.source['account']['id'],))
                db.commit()
        self.assertEqual(self.count('notification_outbox_payloads'), 0)

    def test_active_temp_source_queues_without_persisting_temp_configuration(self):
        get_db().execute("INSERT INTO temp_emails(email) VALUES ('temp@example.com')")
        get_db().commit()
        self.source = {'source_type': 'temp_email', 'source_key': 'temp_email:temp@example.com',
                       'email': 'temp@example.com', 'label': 'Temporary',
                       'temp_email': {'email': 'temp@example.com', 'api_key': 'temp-api-secret'}}
        self.assertEqual(self.enqueue(), 1)
        self.assertNotIn('temp_email', self.claim()[0]['source'])
        get_db().execute("UPDATE temp_emails SET status = 'disabled'")
        get_db().commit()
        self.assertEqual(self.enqueue([dict(self.message, message_id='another')], cursor='2026-03-01T00:00:02'), 0)
        self.assertEqual(outbox.get_sync_cursor('temp_email', self.source['source_key']), self.CURSOR)

    def test_cleanup_keeps_only_slim_sent_logs_for_fourteen_days(self):
        for message_id, age in (('old', 15 * 86400), ('recent', 13 * 86400)):
            notification_state.upsert_delivery_log(
                'email', 'account', self.source['source_key'], message_id, status='sent',
                error_code='previous-failure', error_message='previous-detail', delivered_at=outbox._iso(self.NOW - age),
            )
        result = outbox.cleanup_outbox(now=self.NOW)
        self.assertEqual(result['logs_removed'], 1)
        log = get_db().execute('SELECT * FROM notification_delivery_logs').fetchone()
        self.assertEqual(log['message_id'], 'recent')
        self.assertEqual(log['error_code'], '')
        self.assertEqual(log['error_message'], '')

    def test_terminal_tombstones_are_trimmed_before_rejecting_new_events(self):
        with patch.object(outbox, 'MAX_PENDING_EVENTS', 1):
            self.enqueue()
            claimed = self.claim()[0]
            outbox.finish_delivery(claimed['id'], claimed['lease_token'], now=self.NOW + 1)
            self.assertEqual(self.enqueue([dict(self.message, message_id='new')], now=self.NOW + 2), 1)
            self.assertEqual(self.count('notification_outbox'), 1)

    def test_filtered_claim_leaves_other_channels_unclaimed(self):
        self.enqueue(channels={'telegram': self.BASELINE, 'email': self.BASELINE, 'webhook': self.BASELINE})
        claims = self.claim(channel='email', limit=1)
        self.assertEqual([row['channel'] for row in claims], ['email'])
        self.assertEqual({row['channel'] for row in self.claim()}, {'telegram', 'webhook'})

    def test_orphan_payload_is_removed_without_waiting_for_ttl(self):
        self.enqueue()
        get_db().execute('DELETE FROM notification_outbox')
        get_db().commit()
        self.assertEqual(self.count('notification_outbox_payloads'), 1)
        result = outbox.cleanup_outbox(now=self.NOW + 1)
        self.assertEqual(result['payloads_removed'], 1)
        self.assertEqual(self.count('notification_outbox_payloads'), 0)

    def test_new_channel_ref_keeps_original_shared_payload_expiry(self):
        self.enqueue()
        self.enqueue(channels={'email': self.BASELINE}, now=self.NOW + 1000)
        claimed = self.claim(now=self.NOW + 1000)
        self.assertEqual({row['expires_at'] for row in claimed}, {self.NOW + outbox.PAYLOAD_TTL_SECONDS})

    def test_retry_diagnostics_do_not_exceed_full_storage_byte_budget(self):
        self.enqueue()
        claimed = self.claim()[0]
        size = outbox._stored_bytes(get_db())
        with patch.object(outbox, 'MAX_PENDING_BYTES', size):
            self.assertTrue(outbox.retry_delivery(
                claimed['id'], claimed['lease_token'], delay_seconds=2, error_code='HTTP_429',
                error_message='details', now=self.NOW + 1,
            ))
            self.assertLessEqual(outbox._stored_bytes(get_db()), size)
        self.assertEqual(len(self.claim(now=self.NOW + 3)), 1)

    def test_persisted_errors_redact_credentials_embedded_in_upstream_urls(self):
        outbox.defer_source('account', self.source['source_key'], 1,
                            error='Failed https://api.telegram.org/botBOTSECRET/sendMessage?token=QUERYSECRET', now=self.NOW)
        error = get_db().execute('SELECT last_error FROM notification_sync_states').fetchone()[0]
        self.assertNotIn('BOTSECRET', error)
        self.assertNotIn('QUERYSECRET', error)

    def test_expired_and_cancelled_dedup_survives_terminal_reference_cleanup(self):
        self.enqueue(channels={'telegram': self.BASELINE, 'email': self.BASELINE})
        claim = self.claim(channel='email')[0]
        outbox.cancel_delivery(claim['id'], claim['lease_token'], now=self.NOW + 1)
        outbox.cleanup_outbox(now=self.NOW + outbox.PAYLOAD_TTL_SECONDS)
        outbox.cleanup_outbox(now=self.NOW + 2 * outbox.PAYLOAD_TTL_SECONDS + 1)
        self.assertEqual(self.count('notification_outbox'), 0)
        self.assertEqual(self.enqueue(channels={'telegram': self.BASELINE, 'email': self.BASELINE},
                                      now=self.NOW + 2 * outbox.PAYLOAD_TTL_SECONDS + 2), 0)
        self.assertEqual(self.count('notification_outbox_payloads'), 0)
        self.assertFalse(notification_state.was_delivered('email', 'account', self.source['source_key'], self.message['message_id']))

    def test_automatic_retention_is_throttled_while_expiry_remains_immediate(self):
        with patch.object(outbox.time, 'monotonic', return_value=0) as clock, patch.object(
            outbox, '_cleanup_retention', wraps=outbox._cleanup_retention,
        ) as retention:
            self.enqueue()
            self.assertEqual(retention.call_count, 1)
            clock.return_value = 1
            claimed = self.claim(channel='telegram')[0]
            clock.return_value = 2
            self.claim(channel='email')
            outbox.cleanup_outbox(now=self.NOW + 1, force_retention=False)
            self.assertEqual(retention.call_count, 1)
            # Wall-clock TTL expiry must run even though the monotonic retention interval has not elapsed.
            self.assertEqual(self.claim(now=self.NOW + outbox.PAYLOAD_TTL_SECONDS), [])
            self.assertEqual(self.count('notification_outbox_payloads'), 0)
            self.assertEqual(retention.call_count, 1)
            self.assertFalse(outbox.finish_delivery(claimed['id'], claimed['lease_token'],
                                                    now=self.NOW + outbox.PAYLOAD_TTL_SECONDS))
            clock.return_value = 60
            self.claim(now=self.NOW + outbox.PAYLOAD_TTL_SECONDS + 1)
            self.assertEqual(retention.call_count, 2)
            outbox.cleanup_outbox(now=self.NOW + outbox.PAYLOAD_TTL_SECONDS + 2)
            self.assertEqual(retention.call_count, 3)

    def test_filtered_claim_orders_due_tasks_and_decodes_only_selected_payloads(self):
        self.enqueue([dict(self.message, message_id='first'), dict(self.message, message_id='second')],
                     channels={'email': self.BASELINE, 'telegram': self.BASELINE})
        with patch.object(outbox, 'decrypt_data', wraps=outbox.decrypt_data) as decrypt:
            claims = self.claim(channel='email', limit=1)
            self.assertEqual([(row['channel'], row['message_key']) for row in claims], [('email', 'first')])
            self.assertEqual(decrypt.call_count, 1)
        self.assertEqual([row['message_key'] for row in self.claim(channel='email', limit=1)], ['second'])

    def test_fresh_v27_schema_does_not_create_obsolete_telegram_push_log(self):
        self.assertIsNone(get_db().execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'telegram_push_log'"
        ).fetchone())

    def test_v26_legacy_telegram_sent_keys_migrate_exactly_then_old_table_is_dropped(self):
        db = get_db()
        db.execute("""CREATE TABLE telegram_push_log (
            id INTEGER PRIMARY KEY, account_id INTEGER NOT NULL, message_id TEXT NOT NULL,
            pushed_at TEXT NOT NULL, UNIQUE(account_id, message_id))""")
        fallback_key = notification_state.build_stable_message_key(
            source_type='account', source_key=self.source['source_key'],
            subject='fallback subject', sender='sender@example.com', received_at=self.CURSOR,
        )
        recent = outbox._iso(self.NOW - 60)
        keys = [self.message['message_id'], fallback_key, 'failed-now-sent', 'confirmed']
        for key in keys:
            db.execute('INSERT INTO telegram_push_log(account_id, message_id, pushed_at) VALUES (?, ?, ?)',
                       (self.source['account']['id'], key, recent))
        db.execute('INSERT INTO telegram_push_log(account_id, message_id, pushed_at) VALUES (?, ?, ?)',
                   (self.source['account']['id'], 'expired-legacy-key', outbox._iso(self.NOW - 15 * 86400)))
        db.execute('INSERT INTO telegram_push_log(account_id, message_id, pushed_at) VALUES (?, ?, ?)',
                   (999999, 'deleted-account-key', recent))
        db.execute("UPDATE accounts SET email = ' Mail@Example.COM '")
        db.execute("UPDATE settings SET value = '26' WHERE key = 'db_schema_version'")
        db.commit()
        for key, status in (('failed-now-sent', 'failed'), ('confirmed', 'sent')):
            notification_state.upsert_delivery_log(
                'telegram', 'account', self.source['source_key'], key, status=status,
                error_code='previous-error', error_message='previous-detail', delivered_at=outbox._iso(self.NOW - 30),
            )
        with patch('outlook_web.db.time.time', return_value=self.NOW):
            init_db(self.database)
            init_db(self.database)
        self.assertIsNone(db.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'telegram_push_log'"
        ).fetchone())
        rows = db.execute("SELECT * FROM notification_delivery_logs WHERE channel = 'telegram'").fetchall()
        self.assertEqual({row['message_id'] for row in rows}, set(keys))
        self.assertEqual({row['source_key'] for row in rows}, {self.source['source_key']})
        self.assertEqual({row['status'] for row in rows}, {'sent'})
        promoted = next(row for row in rows if row['message_id'] == 'failed-now-sent')
        self.assertEqual(promoted['error_code'], '')
        self.assertEqual(promoted['error_message'], '')
        self.assertEqual(promoted['delivered_at'], recent)
        confirmed = next(row for row in rows if row['message_id'] == 'confirmed')
        self.assertEqual(confirmed['delivered_at'], outbox._iso(self.NOW - 30))
        self.assertEqual(self.enqueue(), 0)
