from __future__ import annotations

import unittest

from outlook_web.services.notification_transport import NotificationTransportError, retry_after_seconds


class NotificationRetryTimingTests(unittest.TestCase):
    def test_http_date_uses_remaining_wait_instead_of_epoch(self):
        self.assertEqual(retry_after_seconds("Wed, 21 Oct 2015 07:28:00 GMT", now=1445412470), 10)
        self.assertEqual(retry_after_seconds("Wed, 21 Oct 2015 07:28:00 GMT", now=1445412490), 0)

    def test_invalid_delays_cannot_disable_or_poison_scheduler(self):
        for value in (None, "", "NaN", "inf", "-5", "not a date", True):
            with self.subTest(value=value):
                self.assertIsNone(retry_after_seconds(value))

    def test_upstream_delay_survives_error_propagation(self):
        error = NotificationTransportError("Throttled", code="UPSTREAM_THROTTLED", retry_after="12", status=429)
        self.assertEqual(error.retry_after, 12)
        self.assertEqual(error.status, 429)
        self.assertEqual(error.code, "UPSTREAM_THROTTLED")
        self.assertEqual(str(error), "Throttled")


if __name__ == "__main__":
    unittest.main()
