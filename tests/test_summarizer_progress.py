import unittest
from unittest.mock import patch

from src.ai.summarizer import summarize_items


class SummarizerProgressTests(unittest.TestCase):
    def test_summarize_items_emits_stage_progress_events(self) -> None:
        events = []
        raw_items = [
            {
                "source": "rss",
                "title": "Item 1",
                "url": "https://example.com/1",
                "feed_title": "Feed",
                "content_snippet": "content",
            },
            {
                "source": "rss",
                "title": "Item 2",
                "url": "https://example.com/2",
                "feed_title": "Feed",
                "content_snippet": "content",
            },
        ]
        config = {
            "app": {"language": "zh"},
            "ai": {
                "backend": "litellm",
                "max_items_per_digest": 2,
                "min_relevance_score": 5,
                "max_workers": 2,
            },
            "collectors": {"rss": {"max_items_per_feed": 3}},
        }

        with (
            patch("src.ai.summarizer.load_taste_examples", return_value=[]),
            patch("src.ai.summarizer.batch_select_by_titles", return_value=[0, 1]),
            patch(
                "src.ai.summarizer.ai_dedup_across_candidates",
                side_effect=lambda items, **kwargs: items,
            ),
            patch(
                "src.ai.summarizer.score_single_item",
                side_effect=lambda item, *args, **kwargs: (
                    {
                        **item,
                        "ai_score": 8,
                        "ai_summary": "summary",
                        "ai_reason": "reason",
                    },
                    None,
                ),
            ),
            patch(
                "src.ai.summarizer.enforce_source_minimums",
                side_effect=lambda selected, **kwargs: selected,
            ),
            patch.dict("os.environ", {"AI_API_KEY": "dummy"}, clear=False),
        ):
            result = summarize_items(
                raw_items,
                config,
                focus="AI",
                schedule_name="早间日报",
                progress_callback=events.append,
            )

        self.assertEqual(len(result), 2)
        stages = [
            event["stage"] for event in events if event["type"] == "summarizer_progress"
        ]
        self.assertIn("start", stages)
        self.assertIn("stage1_select", stages)
        self.assertIn("cross_source_dedup", stages)
        self.assertIn("stage2_score_start", stages)
        self.assertIn("stage2_score_progress", stages)
        self.assertIn("stage2_score_done", stages)
        self.assertEqual(stages[-1], "completed")


if __name__ == "__main__":
    unittest.main()
