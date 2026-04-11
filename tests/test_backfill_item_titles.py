from __future__ import annotations

import tempfile
import unittest

from src.web.store import AppStateStore


def _sample_config(data_dir: str) -> dict:
    return {
        "app": {"timezone": "Asia/Shanghai", "language": "zh"},
        "schedules": [],
        "collectors": {},
        "ai": {"backend": "claude-cli", "model": "test-model"},
        "agent": {
            "max_steps": 6,
            "schedule_max_steps": 8,
            "max_steps_hard_limit": 20,
            "schedule_allow_side_effects": True,
            "require_dispatch_tool_call": False,
            "fallback_response_max_tokens": 800,
            "session_title_template": "Scheduled Push | {schedule_name}",
            "recent_turns_context_limit": 6,
            "policy": {
                "allow_tools": [],
                "deny_tools": [],
                "allow_side_effects": False,
            },
        },
        "notifications": {},
        "storage": {"data_dir": data_dir},
        "_personal_dir": data_dir,
    }


class BackfillItemTitlesTests(unittest.TestCase):
    def test_list_raw_items_missing_translation_skips_github_and_existing_translations(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = AppStateStore.from_config(_sample_config(tmp))
            store.init_db()
            store.upsert_raw_items(
                [
                    {
                        "source": "rss",
                        "url": "https://example.com/a",
                        "title": "A",
                    },
                    {
                        "source": "rss",
                        "url": "https://example.com/b",
                        "title": "B",
                        "translated_title": "B 中文",
                    },
                    {
                        "source": "github",
                        "url": "https://example.com/c",
                        "title": "owner/repo",
                    },
                ]
            )

            pending = store.list_raw_items_missing_translation(limit=10)

        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0]["title"], "A")
        self.assertEqual(pending[0]["source"], "rss")


if __name__ == "__main__":
    unittest.main()
