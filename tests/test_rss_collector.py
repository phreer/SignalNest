from pathlib import Path
import unittest
from unittest.mock import Mock, patch

from src.collectors.rss_collector import _fetch_feed, _fetch_feed_with_diagnostics


class RssCollectorTests(unittest.TestCase):
    def test_fetch_feed_parses_mit_feed_example(self) -> None:
        feed_bytes = Path(__file__).with_name("mit-feed-example.xml").read_bytes()
        response = Mock()
        response.content = feed_bytes
        response.status_code = 200
        response.raise_for_status.return_value = None

        with patch(
            "src.collectors.rss_collector.requests.get", return_value=response
        ) as get:
            items = _fetch_feed(
                "https://www.technologyreview.com/feed/",
                days_back=3650,
                max_items=2,
            )

        get.assert_called_once()
        self.assertEqual(len(items), 2)

        first = items[0]
        self.assertEqual(first["source"], "rss")
        self.assertEqual(first["feed_title"], "MIT Technology Review")
        self.assertEqual(
            first["title"],
            "What’s in a name? Moderna’s “vaccine” vs. “therapy” dilemma",
        )
        self.assertEqual(
            first["url"],
            "https://www.technologyreview.com/2026/04/10/1135631/whats-in-a-name-modernas-vaccine-vs-therapy-dilemma/",
        )
        self.assertEqual(first["published_at"], "2026-04-10T14:04:20+00:00")
        self.assertIn(
            "That’s the Trump-era vocabulary paradox facing Moderna",
            first["content_snippet"],
        )
        self.assertNotIn("<p>", first["content_snippet"])
        self.assertTrue(first["description"])
        self.assertLessEqual(len(first["description"]), 500)

    def test_fetch_feed_reports_old_entries_in_diagnostics(self) -> None:
        feed_bytes = Path(__file__).with_name("mit-feed-example.xml").read_bytes()
        response = Mock()
        response.content = feed_bytes
        response.status_code = 200
        response.raise_for_status.return_value = None

        with patch("src.collectors.rss_collector.requests.get", return_value=response):
            items, diagnostics = _fetch_feed_with_diagnostics(
                "https://www.technologyreview.com/feed/",
                days_back=0,
                max_items=10,
            )

        self.assertEqual(items, [])
        self.assertEqual(diagnostics.feed_title, "MIT Technology Review")
        self.assertEqual(diagnostics.kept_count, 0)
        self.assertGreater(diagnostics.entry_count, 0)
        self.assertEqual(diagnostics.old_count, diagnostics.entry_count)
        self.assertEqual(diagnostics.failure_reason, "all_entries_outside_lookback")
        self.assertTrue(diagnostics.cutoff_at)
        self.assertTrue(diagnostics.newest_published_at)


if __name__ == "__main__":
    unittest.main()
