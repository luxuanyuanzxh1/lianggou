from __future__ import annotations

import unittest
from datetime import datetime, time

from scripts.data_scheduler import is_due, update_command


class SchedulerTests(unittest.TestCase):
    def test_due_once_on_weekday_after_close(self) -> None:
        now = datetime(2026, 8, 5, 18, 10)
        self.assertTrue(is_due(now, time(18, 10), {}))
        self.assertFalse(is_due(now, time(18, 10), {"last_success_date": "2026-08-05"}))

    def test_not_due_before_close_or_on_weekend(self) -> None:
        self.assertFalse(is_due(datetime(2026, 8, 5, 18, 9), time(18, 10), {}))
        self.assertFalse(is_due(datetime(2026, 8, 8, 19, 0), time(18, 10), {}))

    def test_market_only_update_disables_inline_precompute(self) -> None:
        self.assertNotIn("--no-precompute", update_command())
        self.assertEqual(update_command(update_precomputed=False)[-1], "--no-precompute")


if __name__ == "__main__":
    unittest.main()
