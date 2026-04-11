import unittest
from datetime import datetime, timezone
from unittest.mock import patch

from src.agent.tools import ToolRuntime, build_agent_tools


class AgentToolTests(unittest.TestCase):
    def test_collect_rss_result_exposes_effective_days_back(self) -> None:
        tool = build_agent_tools()["collect_rss"]
        rt = ToolRuntime(
            config={"collectors": {"rss": {"days_lookback": 99999}}},
            state={},
            dry_run=True,
            now=datetime.now(timezone.utc),
        )

        with patch(
            "src.agent.tools.collect_rss",
            return_value=(
                [{"title": "item", "url": "https://example.com", "source": "rss"}],
                [{"feed_url": "https://example.com/feed", "kept_count": 1}],
            ),
        ):
            result = tool.handler({}, rt)

        self.assertEqual(result["effective_days_back"], 99999)
        self.assertEqual(result["fetched_count"], 1)
        self.assertEqual(result["feed_diagnostics"][0]["kept_count"], 1)


if __name__ == "__main__":
    unittest.main()
