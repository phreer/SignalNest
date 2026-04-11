from __future__ import annotations

import unittest
from datetime import datetime
from zoneinfo import ZoneInfo

from src.web.app import _format_datetime_local, _format_datetime_relative


class WebDatetimeFormattingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tz = ZoneInfo("Asia/Shanghai")

    def test_format_datetime_local_converts_to_config_timezone(self) -> None:
        value = "2026-04-11T08:44:00+00:00"

        result = _format_datetime_local(value, self.tz)

        self.assertEqual(result, "2026-04-11 16:44")

    def test_format_datetime_relative_uses_human_friendly_labels(self) -> None:
        now = datetime(2026, 4, 11, 16, 44, tzinfo=self.tz)

        self.assertEqual(
            _format_datetime_relative("2026-04-11T16:39:00+08:00", self.tz, now=now),
            "5 分钟前",
        )
        self.assertEqual(
            _format_datetime_relative("2026-04-10T21:10:00+08:00", self.tz, now=now),
            "昨天 21:10",
        )
        self.assertEqual(
            _format_datetime_relative("2026-04-14T09:00:00+08:00", self.tz, now=now),
            "3 天后",
        )


if __name__ == "__main__":
    unittest.main()
