from __future__ import annotations

import hashlib
import logging
import random
import threading
import time
from collections import OrderedDict
from typing import Any

from outlook_web.repositories import notification_outbox as outbox_repo
from outlook_web.services import notification_dispatch as dispatch
from outlook_web.services.notification_transport import NotificationTransportError, retry_after_seconds

logger = logging.getLogger(__name__)

DELIVERY_CHANNELS = (dispatch.CHANNEL_EMAIL, dispatch.CHANNEL_TELEGRAM, dispatch.CHANNEL_WEBHOOK)
MAX_CLAIMED_DELIVERIES = 1
PRIVATE_CHAT_INTERVAL = 1.1
GROUP_CHAT_INTERVAL = 3.1
OTHER_TARGET_INTERVAL = 0.2
MAX_DESTINATION_GATES = 1024
MAX_RETRY_DELAY = 3600.0

_gate_lock = threading.Lock()
_destination_gates: OrderedDict[str, float] = OrderedDict()


def _destination_key(channel: str, *parts: str) -> str:
    # Runtime credentials and endpoint URLs never become queue fields or log keys.
    digest = hashlib.sha256()
    for part in (channel, *parts):
        encoded = str(part).encode("utf-8")
        digest.update(len(encoded).to_bytes(8, "big"))
        digest.update(encoded)
    return digest.hexdigest()


def _target_for_channel(
    channel: str,
    telegram_runtime: dict[str, str] | None,
    webhook_runtime: dict[str, str] | None,
) -> tuple[str, float]:
    if channel == dispatch.CHANNEL_TELEGRAM:
        runtime = telegram_runtime or {}
        chat_id = runtime.get("chat_id", "").strip()
        interval = GROUP_CHAT_INTERVAL if chat_id.startswith("-") else PRIVATE_CHAT_INTERVAL
        return _destination_key(channel, runtime.get("bot_token", ""), chat_id), interval
    if channel == dispatch.CHANNEL_WEBHOOK:
        return _destination_key(channel, (webhook_runtime or {}).get("url", "")), OTHER_TARGET_INTERVAL
    return _destination_key(channel, dispatch.email_push.get_saved_notification_recipient()), OTHER_TARGET_INTERVAL


def _reserve_target(target: str, interval: float, now: float, *, reserve: bool = True) -> float:
    with _gate_lock:
        next_allowed = _destination_gates.get(target, 0.0)
        if target in _destination_gates:
            _destination_gates.move_to_end(target)
        if next_allowed > now:
            return next_allowed - now
        if reserve:
            _destination_gates[target] = now + interval
            while len(_destination_gates) > MAX_DESTINATION_GATES:
                _destination_gates.popitem(last=False)
        return 0.0


def _extend_target_cooldown(target: str, now: float, delay: float) -> None:
    with _gate_lock:
        _destination_gates[target] = max(_destination_gates.get(target, 0.0), now + delay)
        _destination_gates.move_to_end(target)
        while len(_destination_gates) > MAX_DESTINATION_GATES:
            _destination_gates.popitem(last=False)


def _retry_delay(attempts: int) -> float:
    # Bounded jitter avoids aligning retries across independent deployments.
    base = min(MAX_RETRY_DELAY, 2.0 ** min(max(1, attempts), 12))
    return min(MAX_RETRY_DELAY, base * random.uniform(1.0, 1.25))


def _sender_source(source: dict[str, Any]) -> dict[str, Any]:
    result = {key: source.get(key, "") for key in ("source_type", "source_key", "email", "label")}
    if source.get("source_type") == dispatch.SOURCE_ACCOUNT:
        account = source.get("account") or {}
        result["account"] = {"id": account.get("id"), "email": account.get("email", source.get("email", ""))}
    return result


def _matches_source(delivery: dict[str, Any], source: dict[str, Any] | None) -> bool:
    if source is None:
        return False
    if delivery["source_type"] != dispatch.SOURCE_ACCOUNT:
        return True
    queued_id = ((delivery.get("source") or {}).get("account") or {}).get("id")
    current_id = (source.get("account") or {}).get("id")
    # An email address reused after account deletion is a different source owner.
    return queued_id is not None and current_id is not None and str(queued_id) == str(current_id)


def _resolve_current_source(delivery: dict[str, Any]) -> dict[str, Any] | None:
    snapshot = delivery.get("source") or {}
    if delivery["source_type"] == dispatch.SOURCE_ACCOUNT:
        account_id = (snapshot.get("account") or {}).get("id")
        try:
            account_id = int(account_id)
        except (TypeError, ValueError):
            return None
        record = dispatch.accounts_repo.get_account_by_id(account_id)
        if not record or (record.get("status") or "active") != "active":
            return None
        source = dispatch._normalize_account_source(record)
    elif delivery["source_type"] == dispatch.SOURCE_TEMP_EMAIL:
        address = str(snapshot.get("email") or "").strip()
        if not address:
            return None
        record = dispatch.temp_emails_repo.get_temp_email_by_address(address)
        if not record or (record.get("status") or "active") != "active":
            return None
        source = dispatch._normalize_temp_email_source(record)
    else:
        return None
    if source["source_key"] != delivery["source_key"] or not _matches_source(delivery, source):
        return None
    return source


def run_notification_delivery_job(
    app, channel: str | None = None, *, now: float | None = None, monotonic_now: float | None = None
) -> None:
    """Send at most one queued message for one channel, without pacing sleeps.

    Production schedules each channel independently so a slow transport cannot
    hold up another channel's next tick. The all-channel entry is for manual
    invocation. Runtime pacing gates assume the default single scheduler process;
    upstream Retry-After deadlines also survive process restarts in the outbox DB.
    """
    if channel is None:
        for current_channel in DELIVERY_CHANNELS:
            run_notification_delivery_job(app, current_channel, now=now, monotonic_now=monotonic_now)
        return
    if channel not in DELIVERY_CHANNELS:
        raise ValueError("Unsupported notification delivery channel")

    def wall_time() -> float:
        return time.time() if now is None else now

    def monotonic_time() -> float:
        return time.monotonic() if monotonic_now is None else monotonic_now

    with app.app_context():
        outbox_repo.cleanup_outbox(now=wall_time(), force_retention=False)
        email_enabled = channel == dispatch.CHANNEL_EMAIL and dispatch._is_email_channel_enabled()
        telegram_runtime = dispatch._get_telegram_runtime_config() if channel == dispatch.CHANNEL_TELEGRAM else None
        webhook_runtime = dispatch._get_webhook_runtime_config() if channel == dispatch.CHANNEL_WEBHOOK else None
        channel_enabled = email_enabled or telegram_runtime is not None or webhook_runtime is not None
        target, interval = _target_for_channel(channel, telegram_runtime, webhook_runtime)
        # Do not lease tasks merely to wait for an existing destination gate;
        # pacing must not inflate persistent delivery attempt counters.
        if channel_enabled and (
            outbox_repo.get_destination_delay(target, now=wall_time()) > 0
            or _reserve_target(target, interval, monotonic_time(), reserve=False) > 0
        ):
            return
        deliveries = outbox_repo.claim_due_deliveries(limit=MAX_CLAIMED_DELIVERIES, now=wall_time(), channel=channel)
        for delivery in deliveries:
            delivery_id = delivery["id"]
            lease_token = delivery["lease_token"]
            source = _resolve_current_source(delivery)
            senders = {} if source is None else {
                current_channel: sender
                for current_channel, sender in dispatch._build_active_channels_for_source(
                    source,
                    email_enabled=email_enabled,
                    telegram_runtime=telegram_runtime,
                    webhook_runtime=webhook_runtime,
                )
            }
            sender = senders.get(channel)
            if not _matches_source(delivery, source) or sender is None or delivery["expires_at"] <= wall_time():
                outbox_repo.cancel_delivery(delivery_id, lease_token, now=wall_time())
                continue

            delay = _reserve_target(target, interval, monotonic_time())
            if delay > 0:
                outbox_repo.retry_delivery(
                    delivery_id,
                    lease_token,
                    delay_seconds=delay,
                    error_code="NOTIFICATION_DESTINATION_COOLDOWN",
                    error_message="Destination pacing deferred delivery",
                    now=wall_time(),
                )
                continue

            sender_source = _sender_source(source)
            # A reset can cancel a claimed payload after source resolution. Admit
            # immediately before sending, without holding a DB transaction over
            # network I/O. A send already admitted/in flight cannot be undone.
            if not outbox_repo.delivery_is_owned(delivery_id, lease_token, now=wall_time()):
                continue

            try:
                sender(sender_source, delivery["message"])
            except Exception as exc:
                delay = _retry_delay(delivery["attempts"])
                retry_after = retry_after_seconds(getattr(exc, "retry_after", None), now=wall_time())
                if isinstance(exc, NotificationTransportError) and exc.status == 429 and retry_after is None:
                    retry_after = 60.0
                if retry_after is not None:
                    delay = max(delay, retry_after)
                    _extend_target_cooldown(target, monotonic_time(), delay)
                    outbox_repo.defer_destination(target, delay, now=wall_time())
                code = str(getattr(exc, "code", "NOTIFICATION_SEND_FAILED"))
                # Never persist an arbitrary exception string: network errors can
                # contain credentials, URLs, source details or message content.
                message = "Notification destination rate limited" if retry_after is not None else "Notification send failed"
                outbox_repo.retry_delivery(
                    delivery_id,
                    lease_token,
                    delay_seconds=delay,
                    error_code=code,
                    error_message=message,
                    now=wall_time(),
                )
                logger.warning("[notification_delivery] send deferred id=%s channel=%s", delivery_id, channel)
                continue

            outbox_repo.finish_delivery(delivery_id, lease_token, now=wall_time())
