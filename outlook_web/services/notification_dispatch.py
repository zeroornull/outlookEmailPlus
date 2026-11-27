from __future__ import annotations

import logging
import re
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from datetime import datetime, timezone
from typing import Any, Callable

from outlook_web.repositories import accounts as accounts_repo
from outlook_web.repositories import notification_state as notification_state_repo
from outlook_web.repositories import notification_outbox as outbox_repo
from outlook_web.repositories import settings as settings_repo
from outlook_web.repositories import temp_emails as temp_emails_repo
from outlook_web.services import email_push, webhook_push
from outlook_web.services.temp_mail_service import TempMailService

logger = logging.getLogger(__name__)

CHANNEL_EMAIL = "email"
CHANNEL_TELEGRAM = "telegram"
CHANNEL_WEBHOOK = "webhook"
SOURCE_ACCOUNT = "account"
SOURCE_TEMP_EMAIL = "temp_email"
DEFAULT_EMAIL_JOB_INTERVAL_SECONDS = 60
ACCOUNT_INCLUDED_FOLDERS = ("inbox", "junkemail")
MAX_EMAIL_BODY_LENGTH = 4000
MAX_TEMP_EMAIL_PREVIEW_LENGTH = 200


class NotificationDispatchError(Exception):
    def __init__(self, code: str, message: str, *, message_en: str, status: int = 502) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.message_en = message_en
        self.status = status


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")




def _html_to_plain(html_text: str) -> str:
    if not html_text:
        return ""
    text = re.sub(r"<[^>]+>", " ", html_text)
    return re.sub(r"\s+", " ", text).strip()


def build_source_key(source_type: str, raw_key: str) -> str:
    return f"{source_type}:{(raw_key or '').strip().lower()}"


def _normalize_account_source(account: dict[str, Any]) -> dict[str, Any]:
    return {
        "source_type": SOURCE_ACCOUNT,
        "source_key": build_source_key(SOURCE_ACCOUNT, account.get("email", "")),
        "email": account.get("email", ""),
        "label": account.get("email", ""),
        "account": account,
    }


def _normalize_temp_email_source(temp_email: dict[str, Any]) -> dict[str, Any]:
    address = temp_email.get("email", "")
    return {
        "source_type": SOURCE_TEMP_EMAIL,
        "source_key": build_source_key(SOURCE_TEMP_EMAIL, address),
        "email": address,
        "label": address,
        "temp_email": temp_email,
    }


def list_email_notification_sources() -> list[dict[str, Any]]:
    accounts = [acc for acc in accounts_repo.load_accounts() if (acc.get("status") or "active") == "active"]
    temp_emails = [item for item in temp_emails_repo.load_temp_emails() if (item.get("status") or "active") == "active"]
    return [_normalize_account_source(acc) for acc in accounts] + [_normalize_temp_email_source(item) for item in temp_emails]


def _is_account_notification_participant(account: dict[str, Any]) -> bool:
    """账号级通知参与开关。

    兼容旧模型：底层仍复用 telegram_push_enabled 存储该账号是否参与任意通知渠道。
    """
    return bool(account.get("telegram_push_enabled"))


def _is_source_notification_enabled(source: dict[str, Any]) -> bool:
    if source["source_type"] != SOURCE_ACCOUNT:
        return True

    account = source.get("account") or {}
    return _is_account_notification_participant(account)


def bootstrap_channel_cursors(channel: str, *, cursor_value: str | None = None) -> None:
    target_cursor = cursor_value or utc_now_iso()
    for source in list_email_notification_sources():
        notification_state_repo.reset_channel_cursor(
            channel,
            source["source_type"],
            source["source_key"],
            target_cursor,
        )


def _extract_message_timestamp(raw_value: Any) -> str:
    if raw_value is None or raw_value == "":
        return ""
    fraction = ""
    if isinstance(raw_value, (int, float)):
        value = datetime.fromtimestamp(float(raw_value), timezone.utc)
        fraction = f".{value.microsecond:06d}".rstrip("0").rstrip(".")
    else:
        text = str(raw_value).strip()
        value = datetime.fromisoformat(text.replace("Z", "+00:00"))
        match = re.search(r"T\d{2}:\d{2}:\d{2}(\.\d+)", text)
        if match:
            fraction = match.group(1).rstrip("0").rstrip(".")
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).replace(tzinfo=None).isoformat(timespec="seconds") + fraction


def _message_sort_key(message: dict[str, Any]) -> tuple[str, str]:
    return (
        str(message.get("received_at") or ""),
        str(message.get("message_id") or ""),
    )


def _persist_channel_cursor(channel: str, source: dict[str, Any], cursor_value: str) -> None:
    notification_state_repo.upsert_cursor(channel, source["source_type"], source["source_key"], cursor_value)

    if channel == CHANNEL_TELEGRAM and source["source_type"] == SOURCE_ACCOUNT:
        from outlook_web.repositories.accounts import update_telegram_cursor

        account_id = source.get("account", {}).get("id")
        if account_id:
            update_telegram_cursor(int(account_id), cursor_value)


def _get_initial_cursor_value(channel: str, source: dict[str, Any], job_start: str) -> str:
    if channel == CHANNEL_TELEGRAM and source["source_type"] == SOURCE_ACCOUNT:
        legacy_cursor = str(source.get("account", {}).get("telegram_last_checked_at") or "").strip()
        if legacy_cursor:
            return legacy_cursor
    return job_start


def _ensure_channel_cursor(channel: str, source: dict[str, Any], job_start: str) -> tuple[str, bool]:
    cursor = notification_state_repo.get_cursor(channel, source["source_type"], source["source_key"])
    if cursor:
        return _extract_message_timestamp(cursor), False
    initial_cursor = _extract_message_timestamp(_get_initial_cursor_value(channel, source, job_start))
    _persist_channel_cursor(channel, source, initial_cursor)
    return initial_cursor, initial_cursor == job_start


def _fetch_account_messages(source: dict[str, Any], since: str) -> list[dict[str, Any]]:
    from outlook_web.services import telegram_push

    account = source["account"]
    emails: list[dict[str, Any]] = []
    for folder in ACCOUNT_INCLUDED_FOLDERS:
        try:
            if telegram_push._should_fetch_account_via_graph(account):
                fetched = telegram_push._fetch_new_emails_graph(account, since, folder=folder)
            else:
                fetched = telegram_push._fetch_new_emails_imap(account, since, folder=folder)
            for item in fetched:
                enriched = dict(item)
                enriched["folder"] = folder
                emails.append(enriched)
        except Exception as exc:
            logger.warning(
                "[notification_dispatch] account fetch failed source=%s folder=%s err=%s",
                source["label"],
                folder,
                exc,
            )
            raise
    return emails


def _fetch_temp_email_messages(source: dict[str, Any], since: str) -> list[dict[str, Any]]:
    address = source["email"]

    # A failed remote sync is not an empty mailbox: retain the source cursor.
    TempMailService().list_messages(address, sync_remote=True)
    messages = temp_emails_repo.get_temp_email_messages(address)
    results: list[dict[str, Any]] = []
    for item in messages:
        received_at = _extract_message_timestamp(item.get("timestamp") or item.get("created_at"))
        if received_at and received_at < since:
            continue
        plain_content = (item.get("content", "") or "").strip()
        if not plain_content and item.get("has_html"):
            plain_content = _html_to_plain(item.get("html_content", "") or "")
        preview = plain_content[:MAX_TEMP_EMAIL_PREVIEW_LENGTH]
        results.append(
            {
                "message_id": item.get("message_id", ""),
                "subject": item.get("subject", "") or "无主题",
                "sender": item.get("from_address", "") or "unknown",
                "received_at": received_at,
                "preview": preview,
                "content": plain_content,
                "folder": "inbox",
            }
        )
    return results


def fetch_source_messages(source: dict[str, Any], since: str) -> list[dict[str, Any]]:
    if source["source_type"] == SOURCE_ACCOUNT:
        return _fetch_account_messages(source, since)
    if source["source_type"] == SOURCE_TEMP_EMAIL:
        return _fetch_temp_email_messages(source, since)
    return []




def send_business_email_notification(source: dict[str, Any], message: dict[str, Any]) -> None:
    recipient = email_push.get_saved_notification_recipient()
    subject = f"[Outlook Email Plus] {source['label']} 收到新邮件"
    received_at = message.get("received_at") or "-"
    folder = message.get("folder") or "inbox"
    body_text = (message.get("content") or message.get("preview") or "").strip()
    if len(body_text) > MAX_EMAIL_BODY_LENGTH:
        body_text = body_text[:MAX_EMAIL_BODY_LENGTH].rstrip() + "\n\n...[truncated]"
    if not body_text:
        body_text = "(正文为空)"
    text_body = (
        f"邮箱来源: {source['label']}\n"
        f"来源类型: {'普通邮箱' if source['source_type'] == SOURCE_ACCOUNT else '临时邮箱'}\n"
        f"目录: {folder}\n"
        f"发件人: {message.get('sender') or '-'}\n"
        f"主题: {message.get('subject') or '无主题'}\n"
        f"时间: {received_at}\n\n"
        f"正文:\n{body_text}"
    )
    html_body = (
        f"<p><strong>邮箱来源:</strong> {source['label']}</p>"
        f"<p><strong>来源类型:</strong> {'普通邮箱' if source['source_type'] == SOURCE_ACCOUNT else '临时邮箱'}</p>"
        f"<p><strong>目录:</strong> {folder}</p>"
        f"<p><strong>发件人:</strong> {message.get('sender') or '-'}</p>"
        f"<p><strong>主题:</strong> {message.get('subject') or '无主题'}</p>"
        f"<p><strong>时间:</strong> {received_at}</p>"
        f"<p><strong>正文:</strong><br>{body_text.replace(chr(10), '<br>')}</p>"
    )
    email_push.send_email_message(
        recipient=recipient,
        subject=subject,
        text_body=text_body,
        html_body=html_body,
    )


def send_business_telegram_notification(
    source: dict[str, Any],
    message: dict[str, Any],
    *,
    bot_token: str,
    chat_id: str,
) -> None:
    from outlook_web.services.telegram_push import (
        _build_telegram_message,
        _send_telegram_message,
        _telegram_reply_markup,
    )

    account_email = source.get("account", {}).get("email", source.get("label", ""))
    payload = _build_telegram_message(account_email, message)
    markup = _telegram_reply_markup(account_email, message, chat_id)
    if not _send_telegram_message(bot_token, chat_id, payload, reply_markup=markup):
        raise NotificationDispatchError(
            "TELEGRAM_SEND_FAILED",
            "Telegram 发送失败",
            message_en="Failed to send Telegram notification",
        )


def send_business_webhook_notification(
    source: dict[str, Any],
    message: dict[str, Any],
    *,
    url: str,
    token: str,
) -> None:
    webhook_push.send_business_webhook_notification(
        source,
        message,
        url=url,
        token=token,
    )






def _get_telegram_runtime_config() -> dict[str, str] | None:
    from outlook_web.security.crypto import decrypt_data, is_encrypted

    bot_token_raw = settings_repo.get_setting("telegram_bot_token", "")
    chat_id = settings_repo.get_setting("telegram_chat_id", "").strip()
    if not bot_token_raw or not chat_id:
        return None

    bot_token = decrypt_data(bot_token_raw) if is_encrypted(bot_token_raw) else bot_token_raw
    bot_token = str(bot_token or "").strip()
    if not bot_token:
        return None
    return {"bot_token": bot_token, "chat_id": chat_id}


def _is_email_channel_enabled() -> bool:
    enabled = settings_repo.get_setting("email_notification_enabled", "false").lower() == "true"
    return enabled and email_push.is_email_notification_ready()


def _is_telegram_channel_enabled(telegram_runtime: dict[str, str] | None) -> bool:
    return telegram_runtime is not None


def _get_webhook_runtime_config() -> dict[str, str] | None:
    if not settings_repo.get_webhook_notification_enabled():
        return None

    url = settings_repo.get_webhook_notification_url()
    token = settings_repo.get_webhook_notification_token()
    if not url:
        return None

    try:
        url = webhook_push.validate_webhook_url(url)
    except webhook_push.WebhookPushError as exc:
        logger.warning(
            "[notification_dispatch] webhook config invalid code=%s details=%s",
            exc.code,
            exc.details,
        )
        return None

    return {
        "url": url,
        "token": token,
    }


def _is_webhook_channel_enabled(webhook_runtime: dict[str, str] | None) -> bool:
    return webhook_runtime is not None


def _build_active_channels_for_source(
    source: dict[str, Any],
    *,
    email_enabled: bool,
    telegram_runtime: dict[str, str] | None,
    webhook_runtime: dict[str, str] | None = None,
) -> list[tuple[str, Callable[[dict[str, Any], dict[str, Any]], None]]]:
    # 账号级 telegram_push_enabled 现在表示“是否参与任意通知渠道”；
    # Email/Telegram 只是通道层，避免后续维护时把它误改回 Telegram 专属开关。
    if not _is_source_notification_enabled(source):
        return []

    active_channels: list[tuple[str, Callable[[dict[str, Any], dict[str, Any]], None]]] = []

    if email_enabled:
        active_channels.append(
            (
                CHANNEL_EMAIL,
                send_business_email_notification,
            )
        )

    if telegram_runtime is not None and source["source_type"] == SOURCE_ACCOUNT:
        active_channels.append(
            (
                CHANNEL_TELEGRAM,
                lambda current_source, message, bot_token=telegram_runtime["bot_token"], chat_id=telegram_runtime[
                    "chat_id"
                ]: send_business_telegram_notification(  # noqa: E731
                    current_source,
                    message,
                    bot_token=bot_token,
                    chat_id=chat_id,
                ),
            )
        )

    if webhook_runtime is not None:
        active_channels.append(
            (
                CHANNEL_WEBHOOK,
                lambda current_source, message, url=webhook_runtime["url"], token=webhook_runtime[
                    "token"
                ]: send_business_webhook_notification(  # noqa: E731
                    current_source,
                    message,
                    url=url,
                    token=token,
                ),
            )
        )

    return active_channels


def _current_channel_cursors(source: dict[str, Any], channel_cursors: dict[str, str]) -> dict[str, str]:
    """Recheck settings and reset cutoffs after the potentially slow fetch."""
    if source["source_type"] == SOURCE_ACCOUNT:
        account_id = source.get("account", {}).get("id")
        account = accounts_repo.get_account_by_id(int(account_id)) if account_id else None
        if not account or (account.get("status") or "active") != "active":
            return {}
        current_source = _normalize_account_source(account)
    elif source["source_type"] == SOURCE_TEMP_EMAIL:
        mailbox = temp_emails_repo.get_temp_email_by_address(source["email"])
        if not mailbox or (mailbox.get("status") or "active") != "active":
            return {}
        current_source = _normalize_temp_email_source(mailbox)
    else:
        return {}
    if current_source["source_key"] != source["source_key"]:
        return {}
    active = _build_active_channels_for_source(
        current_source,
        email_enabled=_is_email_channel_enabled(),
        telegram_runtime=_get_telegram_runtime_config(),
        webhook_runtime=_get_webhook_runtime_config(),
    )
    enabled = {channel for channel, _ in active}
    result = {}
    for channel, cursor in channel_cursors.items():
        current_cursor = notification_state_repo.get_cursor(channel, source["source_type"], source["source_key"])
        if channel in enabled and current_cursor:
            result[channel] = max(cursor, _extract_message_timestamp(current_cursor))
    return result


def _sync_source(app, source: dict[str, Any], channel_cursors: dict[str, str]) -> int:
    with app.app_context():
        source_type, source_key = source["source_type"], source["source_key"]
        try:
            scan_start = utc_now_iso()
            since = max(outbox_repo.get_sync_cursor(source_type, source_key) or "", min(channel_cursors.values()))
            messages = sorted(fetch_source_messages(source, since), key=_message_sort_key)
            current_cursors = _current_channel_cursors(source, channel_cursors)
            if not current_cursors:
                return 0
            next_cursor = max(since, scan_start)
            for message in messages:
                received_at = _extract_message_timestamp(message.get("received_at"))
                if not received_at:
                    raise ValueError("Notification message has no received timestamp")
                message["received_at"] = received_at
                next_cursor = max(next_cursor, received_at)
            return outbox_repo.enqueue_messages(source, messages, current_cursors, sync_cursor=next_cursor)
        except Exception as exc:
            delay = getattr(exc, "retry_after", None)
            outbox_repo.defer_source(source_type, source_key, delay if delay is not None else 60, error=getattr(exc, "code", type(exc).__name__))
            logger.warning("[notification_dispatch] source sync failed source=%s code=%s", source_key, getattr(exc, "code", type(exc).__name__))
            return 0


def run_notification_dispatch_job(app) -> None:
    """Fetch each source once and enqueue; the delivery job owns all sending."""
    plans = []
    with app.app_context():
        email_enabled = _is_email_channel_enabled()
        telegram_runtime = _get_telegram_runtime_config()
        webhook_runtime = _get_webhook_runtime_config()
        job_start = utc_now_iso()
        for source in list_email_notification_sources():
            active_channels = _build_active_channels_for_source(
                source, email_enabled=email_enabled, telegram_runtime=telegram_runtime, webhook_runtime=webhook_runtime,
            )
            channel_cursors = {}
            needs_fetch = False
            for channel, _ in active_channels:
                cursor, initialized = _ensure_channel_cursor(channel, source, job_start)
                channel_cursors[channel] = cursor
                needs_fetch = needs_fetch or not initialized
            if needs_fetch and outbox_repo.source_is_due(source["source_type"], source["source_key"]):
                plans.append((source, channel_cursors))

    # Keep only four outstanding futures, and refill when any source completes.
    with ThreadPoolExecutor(max_workers=4, thread_name_prefix="notification-sync") as executor:
        pending = set()
        remaining = iter(plans)
        for _ in range(min(4, len(plans))):
            source, cursors = next(remaining)
            pending.add(executor.submit(_sync_source, app, source, cursors))
        while pending:
            completed, pending = wait(pending, return_when=FIRST_COMPLETED)
            for future in completed:
                future.result()
                plan = next(remaining, None)
                if plan is not None:
                    pending.add(executor.submit(_sync_source, app, *plan))


