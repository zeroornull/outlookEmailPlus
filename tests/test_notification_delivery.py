from __future__ import annotations

import unittest
from contextlib import ExitStack
from unittest.mock import patch

from tests._import_app import import_web_app_module


class NotificationDeliveryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = import_web_app_module().app
        from outlook_web.services import notification_delivery

        cls.delivery = notification_delivery

    def setUp(self):
        with self.delivery._gate_lock:
            self.delivery._destination_gates.clear()
        self.source = {
            "source_type": "account",
            "source_key": "account:mail@example.com",
            "email": "mail@example.com",
            "label": "Mailbox",
            "account": {
                "id": 7,
                "email": "mail@example.com",
                "telegram_push_enabled": True,
                "refresh_token": "current-secret",
                "password": "private-password",
            },
        }
        self.sources = [self.source]
        self.temp_record = None
        self.email_enabled = True
        self.telegram_runtime = {"bot_token": "secret-bot-token", "chat_id": "123"}
        self.webhook_runtime = {"url": "https://example.com/hook?secret=key", "token": "secret-header"}

    def _task(self, delivery_id=1, channel="telegram", **overrides):
        task = {
            "id": delivery_id,
            "lease_token": f"owner-{delivery_id}",
            "channel": channel,
            "source_type": self.source["source_type"],
            "source_key": self.source["source_key"],
            "message_key": f"message-{delivery_id}",
            "source": {"email": "mail@example.com", "account": {"id": 7, "email": "mail@example.com"}},
            "message": {"subject": "Subject", "html_content": "<p>Preview</p>"},
            "attempts": 1,
            "expires_at": 2000.0,
        }
        task.update(overrides)
        return task

    def _run(self, tasks, *, channel=None, now=1000.0, monotonic_now=100.0, errors=None, finish=True, owned=True):
        service = self.delivery
        mocks = {}
        with ExitStack() as stack:
            for name, value in (
                ("_is_email_channel_enabled", self.email_enabled),
                ("_get_telegram_runtime_config", self.telegram_runtime),
                ("_get_webhook_runtime_config", self.webhook_runtime),
            ):
                mocks[name] = stack.enter_context(patch.object(service.dispatch, name, return_value=value))
            mocks["load_sources"] = stack.enter_context(patch.object(service.dispatch, "list_email_notification_sources"))
            mocks["account_lookup"] = stack.enter_context(patch.object(
                service.dispatch.accounts_repo,
                "get_account_by_id",
                side_effect=lambda account_id: next((source["account"] for source in self.sources if source["account"]["id"] == account_id), None),
            ))
            mocks["temp_lookup"] = stack.enter_context(patch.object(
                service.dispatch.temp_emails_repo, "get_temp_email_by_address", return_value=self.temp_record,
            ))
            stack.enter_context(patch.object(service.dispatch.email_push, "get_saved_notification_recipient", return_value="notify@example.com"))
            for sender_channel in ("email", "telegram", "webhook"):
                mocks[sender_channel] = stack.enter_context(patch.object(service.dispatch, f"send_business_{sender_channel}_notification"))
                if errors and sender_channel in errors:
                    mocks[sender_channel].side_effect = errors[sender_channel]
            for name in ("cleanup_outbox", "claim_due_deliveries", "finish_delivery", "retry_delivery", "cancel_delivery",
                         "get_destination_delay", "defer_destination", "delivery_is_owned"):
                mocks[name] = stack.enter_context(patch.object(service.outbox_repo, name))
            mocks["get_destination_delay"].return_value = 0.0
            mocks["delivery_is_owned"].return_value = owned
            mocks["claim_due_deliveries"].side_effect = lambda *, limit, now, channel: [task for task in tasks if task["channel"] == channel][:limit]
            mocks["finish_delivery"].return_value = finish
            mocks["sleep"] = stack.enter_context(patch.object(service.time, "sleep"))
            stack.enter_context(patch.object(service.random, "uniform", return_value=1.0))
            service.run_notification_delivery_job(self.app, channel, now=now, monotonic_now=monotonic_now)
        mocks["sleep"].assert_not_called()
        mocks["load_sources"].assert_not_called()
        return mocks

    def test_current_sender_configuration_and_sanitized_source_are_used(self):
        tasks = [self._task(1, "email"), self._task(2, "telegram"), self._task(3, "webhook")]
        mocks = self._run(tasks)

        self.assertEqual(mocks["claim_due_deliveries"].call_count, 3)
        self.assertEqual(mocks["finish_delivery"].call_count, 3)
        for task in tasks:
            mocks["claim_due_deliveries"].assert_any_call(limit=1, now=1000.0, channel=task["channel"])
            mocks["finish_delivery"].assert_any_call(task["id"], task["lease_token"], now=1000.0)
        source = mocks["telegram"].call_args.args[0]
        self.assertEqual(source["account"], {"id": 7, "email": "mail@example.com"})
        self.assertNotIn("current-secret", repr(source))
        self.assertNotIn("private-password", repr(source))
        self.assertEqual(mocks["telegram"].call_args.kwargs, self.telegram_runtime)
        self.assertEqual(mocks["webhook"].call_args.kwargs, self.webhook_runtime)
        self.assertEqual(mocks["telegram"].call_args.args[1]["html_content"], "<p>Preview</p>")
        self.assertNotIn("secret-bot-token", repr(self.delivery._destination_gates))
        self.assertNotIn("secret=key", repr(self.delivery._destination_gates))

    def test_specific_channel_claims_one_and_loads_only_its_runtime(self):
        mocks = self._run([self._task(1), self._task(2, "email")], channel="telegram")
        mocks["claim_due_deliveries"].assert_called_once_with(limit=1, now=1000.0, channel="telegram")
        mocks["account_lookup"].assert_called_once_with(7)
        mocks["_is_email_channel_enabled"].assert_not_called()
        mocks["_get_webhook_runtime_config"].assert_not_called()
        mocks["email"].assert_not_called()
        mocks["telegram"].assert_called_once()

    def test_empty_queue_never_loads_current_sources(self):
        mocks = self._run([], channel="telegram")
        mocks["account_lookup"].assert_not_called()
        mocks["temp_lookup"].assert_not_called()

    def test_disabled_deleted_recreated_and_renamed_sources_are_cancelled(self):
        for case in ("deleted", "disabled", "recreated", "missing-owner", "inactive", "renamed"):
            with self.subTest(case=case):
                self.sources = [self.source]
                self.source["account"].update(id=7, email="mail@example.com", telegram_push_enabled=True, status="active")
                task = self._task()
                if case == "deleted":
                    self.sources = []
                elif case == "disabled":
                    self.source["account"]["telegram_push_enabled"] = False
                elif case == "recreated":
                    self.source["account"]["id"] = 8
                elif case == "inactive":
                    self.source["account"]["status"] = "disabled"
                elif case == "renamed":
                    self.source["account"]["email"] = "renamed@example.com"
                else:
                    task["source"] = {}
                mocks = self._run([task], channel="telegram")
                mocks["cancel_delivery"].assert_called_once_with(1, "owner-1", now=1000.0)
                mocks["telegram"].assert_not_called()
                mocks["finish_delivery"].assert_not_called()

    def test_temp_source_resolves_current_address_and_checks_status(self):
        self.temp_record = {"email": "temp@example.com", "status": "active"}
        task = self._task(1, "webhook", source_type="temp_email", source_key="temp_email:temp@example.com", source={"email": "temp@example.com"})
        mocks = self._run([task], channel="webhook")
        mocks["temp_lookup"].assert_called_once_with("temp@example.com")
        mocks["account_lookup"].assert_not_called()
        mocks["webhook"].assert_called_once()
        self.temp_record["status"] = "disabled"
        cancelled = self._run([task], channel="webhook", monotonic_now=101.0)
        cancelled["cancel_delivery"].assert_called_once()
        cancelled["webhook"].assert_not_called()

    def test_disabled_channels_and_expired_claims_are_cancelled(self):
        self.email_enabled = False
        self.telegram_runtime = None
        self.webhook_runtime = None
        mocks = self._run([self._task(1, "email"), self._task(2, "telegram"), self._task(3, "webhook")])
        self.assertEqual(mocks["cancel_delivery"].call_count, 3)
        self.email_enabled = True
        expired = self._run([self._task(4, "email", expires_at=1000.0)], channel="email")
        expired["cancel_delivery"].assert_called_once_with(4, "owner-4", now=1000.0)
        expired["email"].assert_not_called()

    def test_telegram_private_and_group_pacing_skips_claim_until_gate_opens(self):
        for chat_id, interval in (("123", 1.1), ("-100123", 3.1)):
            with self.subTest(chat_id=chat_id):
                with self.delivery._gate_lock:
                    self.delivery._destination_gates.clear()
                self.telegram_runtime["chat_id"] = chat_id
                first = self._run([self._task(1)], channel="telegram")
                first["telegram"].assert_called_once()
                second = self._run([self._task(2)], channel="telegram", now=1001.0, monotonic_now=101.0)
                second["telegram"].assert_not_called()
                second["claim_due_deliveries"].assert_not_called()
                second["retry_delivery"].assert_not_called()
                third = self._run([self._task(2)], channel="telegram", now=1000.0 + interval, monotonic_now=100.0 + interval)
                third["telegram"].assert_called_once()

    def test_retry_after_extends_shared_target_and_preserves_other_channels(self):
        from outlook_web.services.notification_transport import NotificationTransportError

        error = NotificationTransportError("must not persist secret", code="TELEGRAM_RATE_LIMITED", retry_after=60, status=429)
        tasks = [self._task(1), self._task(2, "email"), self._task(3, "webhook")]
        mocks = self._run(tasks, errors={"telegram": error})
        mocks["email"].assert_called_once()
        mocks["webhook"].assert_called_once()
        self.assertEqual(mocks["finish_delivery"].call_count, 2)
        throttled = mocks["retry_delivery"].call_args
        self.assertEqual(throttled.args, (1, "owner-1"))
        self.assertEqual(throttled.kwargs["delay_seconds"], 60.0)
        self.assertNotIn("must not persist secret", repr(throttled))
        next_job = self._run([self._task(5)], channel="telegram", now=1010.0, monotonic_now=110.0)
        target, _ = self.delivery._target_for_channel("telegram", self.telegram_runtime, None)
        mocks["defer_destination"].assert_called_once_with(target, 60.0, now=1000.0)
        next_job["telegram"].assert_not_called()
        next_job["claim_due_deliveries"].assert_not_called()
        next_job["retry_delivery"].assert_not_called()

    def test_other_errors_use_bounded_backoff_without_source_details(self):
        mocks = self._run([self._task(attempts=100)], errors={"telegram": RuntimeError("https://private/token?secret=credential")})
        retry = mocks["retry_delivery"].call_args
        self.assertEqual(retry.args, (1, "owner-1"))
        self.assertEqual(retry.kwargs["delay_seconds"], 3600.0)
        self.assertEqual(retry.kwargs["error_code"], "NOTIFICATION_SEND_FAILED")
        self.assertEqual(retry.kwargs["error_message"], "Notification send failed")
        mocks["finish_delivery"].assert_not_called()

    def test_large_channel_backlog_does_not_lease_or_defer_extra_tasks(self):
        tasks = [self._task(index) for index in range(1, 49)]
        tasks += [self._task(49, "email"), self._task(50, "webhook")]
        mocks = self._run(tasks)
        for channel in ("telegram", "email", "webhook"):
            mocks[channel].assert_called_once()
        mocks["retry_delivery"].assert_not_called()
        self.assertEqual(mocks["finish_delivery"].call_count, 3)

    def test_owner_rejected_finish_does_not_retry_sent_notification(self):
        mocks = self._run([self._task()], finish=False)
        mocks["finish_delivery"].assert_called_once_with(1, "owner-1", now=1000.0)
        mocks["retry_delivery"].assert_not_called()





    def test_rejected_lease_admission_never_sends_or_changes_delivery(self):
        mocks = self._run([self._task()], channel="telegram", owned=False)
        mocks["delivery_is_owned"].assert_called_once_with(1, "owner-1", now=1000.0)
        for name in ("telegram", "retry_delivery", "finish_delivery", "cancel_delivery"):
            mocks[name].assert_not_called()


if __name__ == "__main__":
    unittest.main()
