import unittest
from unittest.mock import patch

from src.ai.digest import generate_digest_summary
from src.ai.summarizer import summarize_items


class AICallTimeoutTests(unittest.TestCase):
    def test_summarize_items_passes_timeout_and_retries_to_ai_calls(self) -> None:
        raw_items = [
            {
                "source": "rss",
                "title": "Item 1",
                "url": "https://example.com/1",
                "feed_title": "Feed",
                "content_snippet": "content",
            }
        ]
        config = {
            "app": {"language": "zh"},
            "ai": {
                "backend": "litellm",
                "max_items_per_digest": 1,
                "min_relevance_score": 5,
                "request_timeout_seconds": 17,
                "request_num_retries": 2,
            },
            "collectors": {"rss": {"max_items_per_feed": 3}},
        }

        with (
            patch("src.ai.summarizer.load_taste_examples", return_value=[]),
            patch("src.ai.summarizer.batch_select_by_titles", return_value=[0]),
            patch(
                "src.ai.summarizer.ai_dedup_across_candidates",
                side_effect=lambda items, **kwargs: items,
            ) as dedup,
            patch(
                "src.ai.summarizer.score_single_item",
                return_value=(
                    {
                        **raw_items[0],
                        "ai_score": 8,
                        "ai_summary": "summary",
                        "ai_reason": "reason",
                    },
                    None,
                ),
            ) as score,
            patch(
                "src.ai.summarizer.enforce_source_minimums",
                side_effect=lambda selected, **kwargs: selected,
            ),
            patch.dict("os.environ", {"AI_API_KEY": "dummy"}, clear=False),
        ):
            summarize_items(raw_items, config)

        self.assertEqual(dedup.call_args.kwargs["call_kwargs"]["timeout"], 17)
        self.assertEqual(dedup.call_args.kwargs["call_kwargs"]["num_retries"], 2)
        self.assertEqual(score.call_args.args[3]["timeout"], 17)
        self.assertEqual(score.call_args.args[3]["num_retries"], 2)

    def test_generate_digest_summary_passes_timeout_and_retries(self) -> None:
        news_items = [
            {
                "source": "rss",
                "title": "Item 1",
                "url": "https://example.com/1",
                "ai_score": 9,
                "ai_summary": "summary",
            }
        ]
        config = {
            "app": {"language": "zh"},
            "ai": {
                "backend": "litellm",
                "request_timeout_seconds": 23,
                "request_num_retries": 4,
            },
        }

        with patch("src.ai.digest._call_ai", return_value="digest") as call_ai:
            result = generate_digest_summary(news_items, config, focus="AI")

        self.assertEqual(result, "digest")
        self.assertEqual(call_ai.call_args.args[2]["timeout"], 23)
        self.assertEqual(call_ai.call_args.args[2]["num_retries"], 4)


if __name__ == "__main__":
    unittest.main()
