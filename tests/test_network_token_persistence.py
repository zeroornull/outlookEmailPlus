"""Token rotation must survive mailbox failures and never bypass account persistence."""
from contextlib import closing
import sqlite3
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from tests._import_app import import_web_app_module


class NetworkTokenPersistenceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.module = import_web_app_module()
        cls.app = cls.module.app

    def setUp(self):
        from outlook_web import config
        from outlook_web.services import graph, imap
        self.graph, self.imap = graph, imap
        graph.clear_graph_token_cache()
        imap.clear_imap_token_cache()
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        path = directory.name + '/network.db'
        source = self.module.create_sqlite_connection()
        try:
            with closing(sqlite3.connect(path)) as destination:
                source.backup(destination)
        finally:
            source.close()
        database = patch.object(config, 'get_database_path', return_value=path)
        database.start()
        self.addCleanup(database.stop)
        context = self.app.app_context()
        context.push()
        self.addCleanup(context.pop)
        from outlook_web.db import get_db
        from outlook_web.security.crypto import encrypt_data
        self.db = get_db()
        self.account_id = self.db.execute(
            "INSERT INTO accounts (email,password,client_id,refresh_token,account_type,provider,status) "
            "VALUES ('network-rotation@outlook.com','unused','client',?,'outlook','outlook','active')",
            (encrypt_data('old'),),
        ).lastrowid
        self.db.commit()
        secret = patch('outlook_web.repositories.settings.get_oauth_client_secret_for_client', return_value='')
        secret.start()
        self.addCleanup(secret.stop)

    def response(self, rotated='new'):
        result = MagicMock()
        result.status_code = 200
        result.json.return_value = {'access_token': 'access', 'refresh_token': rotated,
                                    'expires_in': 3600, 'scope': 'Mail.Read'}
        return result

    def saved_token(self):
        from outlook_web.security.crypto import decrypt_data
        return decrypt_data(self.db.execute('SELECT refresh_token FROM accounts WHERE id=?', (self.account_id,)).fetchone()[0])

    def test_imap_rotation_survives_mailbox_failure(self):
        with patch('outlook_web.services.imap.post_refresh_token', return_value=self.response()), \
             patch('outlook_web.services.imap.imaplib.IMAP4_SSL', side_effect=OSError('mailbox unavailable')):
            result = self.imap.get_emails_imap('network-rotation@outlook.com', 'client', 'old', account_id=self.account_id)
        self.assertFalse(result['success'])
        self.assertEqual(self.saved_token(), 'new')

    def test_graph_http_failure_still_persists_rotation(self):
        failed = MagicMock(status_code=503)
        failed.json.return_value = {'error': 'mailbox unavailable'}
        with patch('outlook_web.services.graph.post_refresh_token', return_value=self.response()), \
             patch('outlook_web.services.graph.requests.get', return_value=failed):
            result = self.graph.get_emails_graph('client', 'old', account_id=self.account_id)
        self.assertFalse(result['success'])
        self.assertEqual(self.saved_token(), 'new')

    def test_detail_and_raw_persist_before_mailbox(self):
        for method in (self.graph.get_email_detail_graph, self.graph.get_email_raw_graph):
            with self.subTest(method=method.__name__):
                self.graph.clear_graph_token_cache()
                previous = self.saved_token()
                rotated = previous + '-next'
                with patch('outlook_web.services.graph.post_refresh_token', return_value=self.response(rotated)), \
                     patch('outlook_web.services.graph.requests.get', side_effect=OSError('mailbox unavailable')):
                    self.assertIsNone(method('client', previous, 'message', account_id=self.account_id))
                self.assertEqual(self.saved_token(), rotated)

    def test_idless_cache_does_not_bypass_account_persistence(self):
        for service, getter in ((self.graph, self.graph.get_access_token_graph_result),
                                (self.imap, self.imap.get_access_token_imap_result)):
            with self.subTest(service=service.__name__):
                with patch(service.__name__ + '.post_refresh_token', return_value=self.response()) as endpoint:
                    self.assertTrue(getter('client', 'old')['success'])
                    self.assertTrue(getter('client', 'old', account_id=self.account_id)['success'])
                    self.assertEqual(endpoint.call_count, 2)
                self.assertEqual(self.saved_token(), 'new')

    def test_save_failure_does_not_cache_or_use_access_token(self):
        from outlook_web.repositories.accounts import TokenPersistenceError
        for service, getter in ((self.graph, self.graph.get_access_token_graph_result),
                                (self.imap, self.imap.get_access_token_imap_result)):
            with self.subTest(service=service.__name__):
                with patch(service.__name__ + '.post_refresh_token', return_value=self.response()) as endpoint, \
                     patch('outlook_web.repositories.accounts.persist_token_refresh', side_effect=TokenPersistenceError('save failed')):
                    first = getter('client', 'old', account_id=self.account_id)
                    second = getter('client', 'old', account_id=self.account_id)
                self.assertFalse(first['success'])
                self.assertFalse(second['success'])
                self.assertEqual(first['error']['code'], 'TOKEN_PERSISTENCE_FAILED')
                self.assertNotIn('access_token', first)
                self.assertEqual(endpoint.call_count, 2)

    def test_late_stale_endpoint_response_cannot_overwrite_new_credentials(self):
        from outlook_web.repositories import accounts
        def delayed_response(*args, **kwargs):
            # Another request commits while this token exchange is in flight.
            accounts.persist_token_refresh(self.account_id, 'client', 'old', 'winner')
            return self.response('stale-response')
        for service, getter in ((self.graph, self.graph.get_access_token_graph_result),
                                (self.imap, self.imap.get_access_token_imap_result)):
            with self.subTest(service=service.__name__):
                current = self.saved_token()
                accounts.persist_token_refresh(self.account_id, 'client', current, 'old')
                with patch(service.__name__ + '.post_refresh_token', side_effect=delayed_response):
                    result = getter('client', 'old', account_id=self.account_id)
                self.assertFalse(result['success'])
                self.assertEqual(self.saved_token(), 'winner')
