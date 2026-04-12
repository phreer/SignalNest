import unittest
from datetime import datetime, timezone
from unittest.mock import patch

from src.agent.tools import ToolRuntime, _tool_summarize_news


class AgentSummarizeCandidateTests(unittest.TestCase):
    def test_summarize_news_prefers_candidate_raw_items(self) -> None:
        events = []
        rt = ToolRuntime(
            config={"ai": {"max_items_per_digest": 5}},
            state={
                "raw_items": [
                    {
                        "source": "rss",
                        "title": "full-1",
                        "url": "https://example.com/full-1",
                    },
                    {
                        "source": "rss",
                        "title": "full-2",
                        "url": "https://example.com/full-2",
                    },
                ],
                "candidate_raw_items": [
                    {
                        "source": "rss",
                        "title": "candidate",
                        "url": "https://example.com/candidate",
                    }
                ],
            },
            dry_run=True,
            now=datetime.now(timezone.utc),
            progress_callback=events.append,
        )

        with (
            patch(
                "src.agent.tools.translate_item_titles",
                side_effect=lambda items, config: items,
            ),
            patch(
                "src.agent.tools.summarize_items",
                return_value=[
                    {
                        "source": "rss",
                        "title": "candidate",
                        "url": "https://example.com/candidate",
                        "ai_score": 8,
                        "ai_summary": "summary",
                        "ai_reason": "reason",
                    }
                ],
            ) as summarize,
            patch("src.agent.tools.generate_digest_summary", return_value="digest"),
        ):
            result = _tool_summarize_news({}, rt)

        self.assertEqual(result["news_count"], 1)
        self.assertEqual(summarize.call_args.args[0][0]["title"], "candidate")
        self.assertEqual(len(summarize.call_args.args[0]), 1)
        progress_callback = summarize.call_args.kwargs["progress_callback"]
        self.assertTrue(callable(progress_callback))


if __name__ == "__main__":
    unittest.main()
