from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from src.ai.title_translator import translate_item_titles
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
        "_personal_dir": str(Path(data_dir) / "personal"),
    }


class RawItemTranslationPersistenceTests(unittest.TestCase):
    def test_upsert_raw_items_preserves_existing_translation_on_empty_update(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = AppStateStore.from_config(_sample_config(tmp))
            store.init_db()

            first_ids = store.upsert_raw_items(
                [
                    {
                        "source": "rss",
                        "url": "https://example.com/item",
                        "title": "Original Title",
                        "translated_title": "原始标题",
                    }
                ]
            )
            second_ids = store.upsert_raw_items(
                [
                    {
                        "source": "rss",
                        "url": "https://example.com/item",
                        "title": "Original Title",
                    }
                ]
            )

            self.assertEqual(first_ids, second_ids)

            conn = store._connect()
            try:
                row = conn.execute(
                    "SELECT translated_title, seen_count FROM raw_items WHERE id=?",
                    (first_ids[0],),
                ).fetchone()
            finally:
                conn.close()

            self.assertIsNotNone(row)
            self.assertEqual(row["translated_title"], "原始标题")
            self.assertEqual(row["seen_count"], 2)


class TitleTranslatorRegressionTests(unittest.TestCase):
    def test_japanese_title_with_kanji_still_goes_through_translation(self) -> None:
        items = [
            {
                "source": "rss",
                "title": "新しいAIモデル発表",
                "url": "https://example.com/jp-item",
            }
        ]

        with patch.dict(
            "os.environ",
            {"AI_BACKEND": "claude-cli", "AI_MODEL": "test-model"},
            clear=False,
        ):
            with patch(
                "src.ai.title_translator._call_ai",
                return_value='{"translations": [{"index": 0, "translated_title": "新 AI 模型发布"}]}',
            ) as call_ai:
                result = translate_item_titles(items, _sample_config("/tmp"))

        self.assertEqual(result[0]["translated_title"], "新 AI 模型发布")
        call_ai.assert_called_once()

    def test_existing_translated_title_skips_ai_call(self) -> None:
        items = [
            {
                "source": "rss",
                "title": "Already translated",
                "translated_title": "已翻译标题",
                "url": "https://example.com/already-translated",
            }
        ]

        with patch.dict(
            "os.environ",
            {"AI_BACKEND": "claude-cli", "AI_MODEL": "test-model"},
            clear=False,
        ):
            with patch(
                "src.ai.title_translator._call_ai",
                side_effect=AssertionError(
                    "AI should not be called for pre-translated items"
                ),
            ):
                result = translate_item_titles(items, _sample_config("/tmp"))

        self.assertEqual(result[0]["translated_title"], "已翻译标题")
