from __future__ import annotations

import math
import time
from datetime import timezone
from email.utils import parsedate_to_datetime


class NotificationTransportError(Exception):
    """Preserve upstream throttling so jobs can reschedule without sleeping."""

    def __init__(
        self,
        message: str,
        *,
        code: str = "NOTIFICATION_UPSTREAM_FAILED",
        retry_after: float | None = None,
        status: int = 502,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.retry_after = retry_after_seconds(retry_after)
        self.status = status
        self.message_en = "Notification upstream request failed"


def retry_after_seconds(value: object, *, now: float | None = None) -> float | None:
    """Read Retry-After delta-seconds or an HTTP date; reject invalid delays."""
    if value is None or isinstance(value, bool):
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        seconds = float(text)
    except (TypeError, ValueError):
        try:
            deadline = parsedate_to_datetime(text)
            if deadline.tzinfo is None:
                deadline = deadline.replace(tzinfo=timezone.utc)
            seconds = deadline.timestamp() - (time.time() if now is None else now)
        except (TypeError, ValueError, OverflowError):
            return None
        seconds = max(0.0, seconds)
    return seconds if math.isfinite(seconds) and seconds >= 0 else None
