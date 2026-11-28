from contextlib import closing
from datetime import datetime, timedelta, timezone
import json
import sqlite3
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from tests._import_app import clear_login_attempts, import_web_app_module


class RefreshPersistenceScheduleTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.module = import_web_app_module()
        cls.app = cls.module.app
        from outlook_web.repositories import accounts
        from outlook_web.services import refresh, scheduler

        cls.accounts_repo = accounts
        cls.refresh = refresh
        cls.scheduler = scheduler

    def setUp(self):
        from outlook_web import config

        private = tempfile.TemporaryDirectory()
        self.addCleanup(private.cleanup)
        path = f"{private.name}/refresh.db"
        with closing(self.module.create_sqlite_connection()) as source:
            with closing(sqlite3.connect(path)) as destination:
                source.backup(destination)
        db_patch = patch.object(config, "get_database_path", return_value=path)
        db_patch.start()
        self.addCleanup(db_patch.stop)
        tz_patch = patch.object(self.scheduler, "_scheduler_instance", SimpleNamespace(timezone=timezone.utc))
        tz_patch.start()
        self.addCleanup(tz_patch.stop)
        with self.app.app_context():
            clear_login_attempts()
        self.conn = self.module.create_sqlite_connection()
        self.addCleanup(self.conn.close)
        self.conn.execute("UPDATE accounts SET status = 'inactive'")
        self.conn.execute("DELETE FROM distributed_locks")
        self.conn.execute("DELETE FROM refresh_runs")
        self.conn.execute("DELETE FROM account_refresh_logs")
        for key, value in {
            "enable_scheduled_refresh": "true",
            "use_cron_schedule": "false",
            "refresh_interval_days": "1",
            "refresh_delay_seconds": "0",
        }.items():
            self.conn.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", (key, value))
        self.conn.commit()

    def add_account(self, name="account", *, last_refresh_at=None, account_type="outlook", provider="outlook", status="active"):
        cursor = self.conn.execute(
            """INSERT INTO accounts
               (email, password, client_id, refresh_token, account_type, provider, status, last_refresh_at)
               VALUES (?, '', ?, ?, ?, ?, ?, ?)""",
            (f"{name}@example.com", f"client-{name}", self.module.encrypt_data(f"token-{name}"),
             account_type, provider, status, last_refresh_at),
        )
        self.conn.commit()
        return cursor.lastrowid

    def account(self, account_id):
        return self.conn.execute("SELECT * FROM accounts WHERE id = ?", (account_id,)).fetchone()

    def latest_run(self):
        return self.conn.execute("SELECT * FROM refresh_runs ORDER BY rowid DESC LIMIT 1").fetchone()

    def events(self, stream):
        return [json.loads(line[6:]) for chunk in stream for line in chunk.splitlines() if line.startswith("data: ")]

    def test_persistence_same_rotated_and_idempotent_tokens(self):
        account_id = self.add_account()
        with self.app.app_context():
            for previous, rotated, expected in (
                ("token-account", None, "token-account"),
                ("token-account", "token-account", "token-account"),
                ("token-account", "rotated-token", "rotated-token"),
                ("token-account", "rotated-token", "rotated-token"),
            ):
                with self.subTest(previous=previous, rotated=rotated):
                    effective = self.accounts_repo.persist_token_refresh(account_id, "client-account", previous, rotated)
                    self.assertEqual(effective, expected)
                    row = self.account(account_id)
                    self.assertEqual(self.module.decrypt_data(row["refresh_token"]), expected)
                    self.assertTrue(row["last_refresh_at"])

    def test_persistence_rejects_stale_credentials_changed_client_and_missing_account(self):
        account_id = self.add_account()
        with self.app.app_context():
            for identity, client_id, previous in (
                (account_id, "client-account", "stale-token"),
                (account_id, "changed-client", "token-account"),
                (account_id + 99999, "client-account", "token-account"),
            ):
                with self.subTest(identity=identity, client_id=client_id):
                    with self.assertRaises(self.accounts_repo.TokenPersistenceError):
                        self.accounts_repo.persist_token_refresh(identity, client_id, previous, "late-rotation")
        row = self.account(account_id)
        self.assertEqual(self.module.decrypt_data(row["refresh_token"]), "token-account")
        self.assertIsNone(row["last_refresh_at"])

    def test_persistence_decrypt_and_database_errors_are_safe(self):
        account_id = self.add_account()
        with self.app.app_context():
            with patch.object(self.accounts_repo, "decrypt_data", side_effect=RuntimeError("secret-token")):
                with self.assertRaises(self.accounts_repo.TokenPersistenceError) as captured:
                    self.accounts_repo.persist_token_refresh(account_id, "client-account", "token-account")
            self.assertNotIn("secret-token", str(captured.exception))
            self.conn.execute("""CREATE TRIGGER deny_refresh BEFORE UPDATE OF last_refresh_at ON accounts
                                 BEGIN SELECT RAISE(ABORT, 'private-database-details'); END""")
            self.conn.commit()
            with self.assertRaises(self.accounts_repo.TokenPersistenceError) as captured:
                self.accounts_repo.persist_token_refresh(account_id, "client-account", "token-account", "rotated")
            self.assertNotIn("private-database-details", str(captured.exception))
        row = self.account(account_id)
        self.assertIsNone(row["last_refresh_at"])
        self.assertEqual(self.module.decrypt_data(row["refresh_token"]), "token-account")

    def test_supplied_connection_does_not_commit_and_ciphertext_cas_rejects_race(self):
        account_id = self.add_account()
        self.accounts_repo.persist_token_refresh(
            account_id, "client-account", "token-account", "rotated", connection=self.conn
        )
        self.assertTrue(self.conn.in_transaction)
        self.conn.rollback()
        self.assertIsNone(self.account(account_id)["last_refresh_at"])
        self.assertEqual(self.module.decrypt_data(self.account(account_id)["refresh_token"]), "token-account")

        connection = self.conn
        replacement = self.module.encrypt_data("concurrent-token")

        class InterleavedConnection:
            def execute(self, sql, params=()):
                if "UPDATE accounts" in sql:
                    connection.execute("UPDATE accounts SET refresh_token = ? WHERE id = ?", (replacement, account_id))
                return connection.execute(sql, params)

        with self.assertRaises(self.accounts_repo.TokenPersistenceError):
            self.accounts_repo.persist_token_refresh(
                account_id, "client-account", "token-account", "late-token", connection=InterleavedConnection()
            )
        self.assertEqual(self.module.decrypt_data(self.account(account_id)["refresh_token"]), "concurrent-token")
        self.assertIsNone(self.account(account_id)["last_refresh_at"])
        self.conn.rollback()

    def test_daily_refresh_ignores_previous_batch_finishing_at_0211(self):
        account_id = self.add_account(last_refresh_at="2026-11-27 02:11:00")
        self.conn.execute("""INSERT INTO refresh_runs
                             (id, trigger_source, status, total, success_count, failed_count, finished_at)
                             VALUES ('previous', 'scheduled', 'completed', 1, 1, 0, '2026-11-27 02:11:00')""")
        self.conn.commit()
        endpoint = Mock(return_value=(True, None, "rotated"))
        with patch.object(self.scheduler, "utcnow", return_value=datetime(2026, 11, 28, 2)):
            self.scheduler.scheduled_refresh_task(self.app, endpoint)
        endpoint.assert_called_once_with("client-account", "token-account", "")
        self.assertEqual(self.latest_run()["success_count"], 1)
        self.assertEqual(self.module.decrypt_data(self.account(account_id)["refresh_token"]), "rotated")

    def test_due_dates_are_per_account_and_use_scheduler_local_calendar(self):
        local_zone = timezone(timedelta(hours=8))
        local_now = datetime(2026, 11, 28, 2, tzinfo=local_zone)
        rows = [
            {"id": 1, "last_refresh_at": "2026-11-24 18:11:00"},  # Nov 25 locally: due after 3 days
            {"id": 2, "last_refresh_at": "2026-11-25 18:11:00"},  # Nov 26 locally: not due
            {"id": 3, "last_refresh_at": None},
            {"id": 4, "last_refresh_at": "invalid"},
        ]
        with patch.object(self.scheduler, "_scheduler_instance", SimpleNamespace(timezone=local_zone)):
            due = self.refresh.select_due_refresh_accounts(rows, 3, now=local_now)
            self.assertEqual([row["id"] for row in due], [1, 3, 4])
            before_tick = self.refresh.select_due_refresh_accounts(rows[:1], 3, now=local_now - timedelta(minutes=1))
            self.assertEqual(before_tick, [])

    def test_failed_due_account_does_not_suppress_other_accounts_next_day(self):
        failed_id = self.add_account("failed", last_refresh_at="2026-11-20 02:11:00")
        healthy_id = self.add_account("healthy", last_refresh_at="2026-11-26 02:11:00")
        self.conn.execute("UPDATE settings SET value = '3' WHERE key = 'refresh_interval_days'")
        self.conn.commit()
        first = Mock(return_value=(False, "invalid_grant", None))
        with patch.object(self.scheduler, "utcnow", return_value=datetime(2026, 11, 28, 2)):
            self.scheduler.scheduled_refresh_task(self.app, first)
        first.assert_called_once_with("client-failed", "token-failed", "")
        self.assertEqual(self.latest_run()["failed_count"], 1)
        second = Mock(return_value=(True, None, None))
        with patch.object(self.scheduler, "utcnow", return_value=datetime(2026, 11, 29, 2)):
            self.scheduler.scheduled_refresh_task(self.app, second)
        self.assertEqual({call.args[0] for call in second.call_args_list}, {"client-failed", "client-healthy"})
        self.assertEqual(self.latest_run()["total"], 2)
        self.assertTrue(self.account(failed_id)["last_refresh_at"])
        self.assertTrue(self.account(healthy_id)["last_refresh_at"])

    def test_no_due_accounts_skips_and_cron_bypasses_interval_but_excludes_non_outlook(self):
        self.add_account(last_refresh_at="2026-11-28 02:01:00")
        self.add_account("imap", account_type="imap")
        self.add_account("cf", provider="cloudflare_temp_mail")
        self.add_account("disabled", status="inactive")
        endpoint = Mock(return_value=(True, None, None))
        with patch.object(self.scheduler, "utcnow", return_value=datetime(2026, 11, 28, 2)):
            self.scheduler.scheduled_refresh_task(self.app, endpoint)
        endpoint.assert_not_called()
        self.assertEqual(self.latest_run()["status"], "skipped")
        self.assertEqual(self.latest_run()["total"], 0)
        self.conn.execute("UPDATE settings SET value = 'true' WHERE key = 'use_cron_schedule'")
        self.conn.commit()
        self.scheduler.scheduled_refresh_task(self.app, endpoint)
        endpoint.assert_called_once()
        self.assertEqual(self.latest_run()["total"], 1)

    def test_manual_scheduled_refresh_uses_same_per_account_due_dates(self):
        self.add_account("due", last_refresh_at="2026-11-27 02:11:00")
        self.add_account("fresh", last_refresh_at="2026-11-28 01:00:00")
        endpoint = Mock(return_value=(True, None, None))
        with patch.object(self.refresh, "utcnow", return_value=datetime(2026, 11, 28, 2)):
            events = self.events(self.refresh.stream_trigger_scheduled_refresh(
                force=False, refresh_interval_days=1, use_cron=False, trace_id="manual-schedule",
                requested_by_ip="", requested_by_user_agent="", lock_name="refresh_all_tokens",
                test_refresh_token=endpoint,
            ))
        endpoint.assert_called_once_with("client-due", "token-due", "")
        self.assertEqual(events[-1]["success_count"], 1)
        self.assertEqual(events[-1]["total"], 1)

    def test_success_log_database_failure_rolls_back_every_refresh_variant(self):
        account_id = self.add_account()
        self.conn.execute("""CREATE TRIGGER deny_success BEFORE INSERT ON account_refresh_logs
                             WHEN NEW.status = 'success'
                             BEGIN SELECT RAISE(ABORT, 'private-database-details'); END""")
        self.conn.commit()
        client = self.app.test_client()
        self.assertTrue(client.post("/login", json={"password": "testpass123"}).get_json()["success"])
        common = dict(trace_id="db-failure", requested_by_ip="", requested_by_user_agent="",
                      lock_name="refresh_all_tokens", test_refresh_token=Mock(return_value=(True, None, "rotated")))
        for variant in ("scheduled", "manual_all", "manual_selected", "scheduled_manual", "retry_failed", "single_api"):
            with self.subTest(variant=variant):
                if variant == "scheduled":
                    self.scheduler.scheduled_refresh_task(self.app, common["test_refresh_token"])
                elif variant == "manual_all":
                    result = self.events(self.refresh.stream_refresh_all_accounts(**common))[-1]
                elif variant == "manual_selected":
                    events = self.events(self.refresh.stream_refresh_selected_accounts(account_ids=[account_id], **common))
                    result = events[-1]
                    completed = [event for event in events if event.get("result") == "failed"]
                    self.assertEqual(len(completed), 1)
                    self.assertIsNone(completed[0]["last_refresh_at"])
                elif variant == "scheduled_manual":
                    result = self.events(self.refresh.stream_trigger_scheduled_refresh(
                        force=True, refresh_interval_days=1, use_cron=False, **common
                    ))[-1]
                elif variant == "retry_failed":
                    with self.app.app_context():
                        result, status = self.refresh.refresh_failed_accounts(db=self.conn, **common)
                    self.assertEqual(status, 200)
                else:
                    with patch("outlook_web.controllers.accounts.graph_service.test_refresh_token_with_rotation",
                               return_value=(True, None, "rotated")):
                        result = client.post(f"/api/accounts/{account_id}/refresh").get_json()
                    self.assertFalse(result["success"])
                    self.assertNotIn("private-database-details", json.dumps(result))
                row = self.account(account_id)
                self.assertIsNone(row["last_refresh_at"])
                self.assertEqual(self.module.decrypt_data(row["refresh_token"]), "token-account")
                success_logs = self.conn.execute("SELECT COUNT(*) FROM account_refresh_logs WHERE status = 'success'").fetchone()[0]
                self.assertEqual(success_logs, 0)
                if variant != "single_api":
                    run = self.latest_run()
                    self.assertEqual((run["total"], run["success_count"], run["failed_count"]), (1, 0, 1))
                    if variant != "scheduled":
                        self.assertEqual((result["success_count"], result["failed_count"]), (0, 1))

    def test_single_api_late_rotation_cannot_replace_reauthorized_credentials(self):
        account_id = self.add_account()
        client = self.app.test_client()
        self.assertTrue(client.post("/login", json={"password": "testpass123"}).get_json()["success"])

        def concurrent_reauthorization(*_args):
            self.conn.execute("UPDATE accounts SET refresh_token = ? WHERE id = ?",
                              (self.module.encrypt_data("reauthorized-token"), account_id))
            self.conn.commit()
            return True, None, "late-token"

        with patch("outlook_web.controllers.accounts.graph_service.test_refresh_token_with_rotation",
                   side_effect=concurrent_reauthorization):
            payload = client.post(f"/api/accounts/{account_id}/refresh").get_json()
        self.assertFalse(payload["success"])
        self.assertEqual(self.module.decrypt_data(self.account(account_id)["refresh_token"]), "reauthorized-token")
        self.assertIsNone(self.account(account_id)["last_refresh_at"])
        self.assertEqual(self.conn.execute("SELECT status FROM account_refresh_logs ORDER BY id DESC LIMIT 1").fetchone()[0], "failed")

    def test_scheduled_and_manual_batches_reject_late_rotation(self):
        account_id = self.add_account()

        def concurrent_reauthorization(*_args):
            self.conn.execute(
                "UPDATE accounts SET refresh_token = ? WHERE id = ?",
                (self.module.encrypt_data("reauthorized-token"), account_id),
            )
            self.conn.commit()
            return True, None, "late-token"

        common = dict(
            trace_id="stale-rotation", requested_by_ip="", requested_by_user_agent="",
            lock_name="refresh_all_tokens", test_refresh_token=concurrent_reauthorization,
        )
        for variant in ("scheduled", "manual_all", "manual_selected", "scheduled_manual", "retry_failed"):
            with self.subTest(variant=variant):
                self.conn.execute(
                    "UPDATE accounts SET refresh_token = ?, last_refresh_at = NULL WHERE id = ?",
                    (self.module.encrypt_data("token-account"), account_id),
                )
                self.conn.execute(
                    "INSERT INTO account_refresh_logs (account_id, account_email, refresh_type, status) VALUES (?, ?, 'manual', 'failed')",
                    (account_id, "account@example.com"),
                )
                self.conn.commit()
                if variant == "scheduled":
                    self.scheduler.scheduled_refresh_task(self.app, concurrent_reauthorization)
                elif variant == "manual_all":
                    self.events(self.refresh.stream_refresh_all_accounts(**common))
                elif variant == "manual_selected":
                    self.events(self.refresh.stream_refresh_selected_accounts(account_ids=[account_id], **common))
                elif variant == "scheduled_manual":
                    self.events(self.refresh.stream_trigger_scheduled_refresh(
                        force=True, refresh_interval_days=1, use_cron=False, **common
                    ))
                else:
                    with self.app.app_context():
                        self.refresh.refresh_failed_accounts(db=self.conn, **common)
                self.assertEqual(self.module.decrypt_data(self.account(account_id)["refresh_token"]), "reauthorized-token")
                self.assertIsNone(self.account(account_id)["last_refresh_at"])
                self.assertEqual((self.latest_run()["success_count"], self.latest_run()["failed_count"]), (0, 1))

    def test_manual_schedule_skip_keeps_next_due_metadata(self):
        self.add_account(last_refresh_at="2026-11-28 01:00:00")
        endpoint = Mock(return_value=(True, None, None))
        with patch.object(self.refresh, "utcnow", return_value=datetime(2026, 11, 28, 2)):
            events = self.events(self.refresh.stream_trigger_scheduled_refresh(
                force=False, refresh_interval_days=3, use_cron=False, trace_id="not-due",
                requested_by_ip="", requested_by_user_agent="", lock_name="refresh_all_tokens",
                test_refresh_token=endpoint,
            ))
        endpoint.assert_not_called()
        self.assertEqual(events[-1]["type"], "skipped")
        self.assertEqual(events[-1]["next_due"], "2026-12-01T02:00:00+00:00")
        self.assertEqual((self.latest_run()["total"], self.latest_run()["status"]), (0, "skipped"))

    def scheduler_status(self, now):
        from outlook_web.controllers import scheduler as controller

        with self.app.test_request_context("/api/scheduler/status"):
            with patch.object(controller, "utcnow", return_value=now):
                return controller.api_get_scheduler_status.__wrapped__().get_json()

    def test_status_yesterday_batch_completion_does_not_postpone_today_check(self):
        self.add_account(last_refresh_at="2026-11-27 02:11:00")
        self.conn.execute("""INSERT INTO refresh_runs
                             (id, trigger_source, status, total, success_count, failed_count, finished_at)
                             VALUES ('previous', 'scheduled', 'completed', 1, 1, 0, '2026-11-27 02:11:00')""")
        self.conn.commit()
        result = self.scheduler_status(datetime(2026, 11, 28, 1))
        self.assertEqual(result["scheduler"]["next_due"], "2026-11-28T02:00:00+00:00")
        self.assertEqual(result["scheduler"]["future_runs"], [
            f"2026-{day}T02:00:00+00:00"
            for day in ("11-28", "11-29", "11-30", "12-01", "12-02")
        ])
        self.assertEqual(result["refresh"]["last_scheduled"]["finished_at"], "2026-11-27 02:11:00")
        overdue = self.scheduler_status(datetime(2026, 11, 28, 3))["scheduler"]
        self.assertEqual(overdue["next_due"], "2026-11-29T02:00:00+00:00")

    def test_status_uses_earliest_account_calendar_due_date_and_explicit_timezone(self):
        self.add_account("later", last_refresh_at="2026-11-27 18:11:00")
        self.add_account("earlier", last_refresh_at="2026-11-25 18:11:00")
        self.conn.execute("UPDATE settings SET value = '3' WHERE key = 'refresh_interval_days'")
        self.conn.commit()
        local_zone = timezone(timedelta(hours=8))
        with patch.object(self.scheduler, "_scheduler_instance", SimpleNamespace(timezone=local_zone)):
            result = self.scheduler_status(datetime(2026, 11, 27, 17))["scheduler"]
        self.assertEqual(result["future_runs"][0], "2026-11-28T02:00:00+08:00")
        self.assertEqual(result["next_due"], "2026-11-29T02:00:00+08:00")

    def test_status_disabled_and_no_eligible_accounts(self):
        self.add_account("inactive", status="inactive")
        self.add_account("imap", account_type="imap")
        self.add_account("pool", provider="cloudflare_temp_mail")
        result = self.scheduler_status(datetime(2026, 11, 28, 1))["scheduler"]
        self.assertIsNone(result["next_due"])
        self.assertEqual(len(result["future_runs"]), 5)
        self.add_account("eligible")
        self.assertEqual(self.scheduler_status(datetime(2026, 11, 28, 1))["scheduler"]["next_due"],
                         "2026-11-28T02:00:00+00:00")
        self.conn.execute("UPDATE settings SET value = 'false' WHERE key = 'enable_scheduled_refresh'")
        self.conn.commit()
        result = self.scheduler_status(datetime(2026, 11, 28, 1))["scheduler"]
        self.assertEqual(result["future_runs"], [])
        self.assertIsNone(result["next_due"])

    def test_status_cron_preview_uses_worker_timezone_and_weekday_semantics(self):
        self.conn.execute("UPDATE settings SET value = 'true' WHERE key = 'use_cron_schedule'")
        self.conn.execute("INSERT OR REPLACE INTO settings (key, value) VALUES ('refresh_cron', '15 4 * * 0')")
        self.conn.commit()
        local_zone = timezone(timedelta(hours=8))
        with patch.object(self.scheduler, "_scheduler_instance", SimpleNamespace(timezone=local_zone)):
            result = self.scheduler_status(datetime(2026, 11, 27, 17))["scheduler"]
        self.assertEqual(result["future_runs"], [
            f"2026-{day}T04:15:00+08:00"
            for day in ("11-30", "12-07", "12-14", "12-21", "12-28")
        ])
        self.assertIsNone(result["next_due"])

    def test_status_invalid_cron_previews_worker_daily_fallback(self):
        self.conn.execute("UPDATE settings SET value = 'true' WHERE key = 'use_cron_schedule'")
        self.conn.execute("INSERT OR REPLACE INTO settings (key, value) VALUES ('refresh_cron', 'invalid')")
        self.conn.commit()
        result = self.scheduler_status(datetime(2026, 11, 28, 1))["scheduler"]
        self.assertEqual(result["future_runs"][0], "2026-11-28T02:00:00+00:00")
        self.assertEqual(len(result["future_runs"]), 5)
        self.assertIsNone(result["next_due"])
