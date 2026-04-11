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

    def test_parser_accepts_items_key_payload(self) -> None:
        items = [
            {
                "source": "rss",
                "title": "France to ditch Windows for Linux to reduce reliance on US tech",
                "url": "https://example.com/france-linux",
            }
        ]

        with patch.dict(
            "os.environ",
            {"AI_BACKEND": "claude-cli", "AI_MODEL": "test-model"},
            clear=False,
        ):
            with patch(
                "src.ai.title_translator._call_ai",
                return_value='{"items": [{"index": 0, "translation": "法国将弃用 Windows 转向 Linux，以减少对美国科技的依赖"}]}',
            ):
                result = translate_item_titles(items, _sample_config("/tmp"))

        self.assertEqual(
            result[0]["translated_title"],
            "法国将弃用 Windows 转向 Linux，以减少对美国科技的依赖",
        )

    def test_chinese_title_is_filled_without_ai_call(self) -> None:
        items = [
            {
                "source": "rss",
                "title": "阿里发布新一代开源模型",
                "url": "https://example.com/zh-item",
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
                    "AI should not be called for Chinese titles"
                ),
            ):
                result = translate_item_titles(items, _sample_config("/tmp"))

        self.assertEqual(result[0]["translated_title"], "阿里发布新一代开源模型")

    def test_empty_batch_response_falls_back_to_split_batches(self) -> None:
        items = [
            {
                "source": "rss",
                "title": "France to ditch Windows for Linux to reduce reliance on US tech",
                "url": "https://example.com/france-linux",
            },
            {
                "source": "rss",
                "title": "FAA says gamers are the answer to its air traffic controller shortage",
                "url": "https://example.com/faa-gamers",
            },
        ]

        responses = iter(
            [
                "",
                '{"translations": [{"index": 0, "translated_title": "法国拟弃用 Windows 转用 Linux，以减少对美技术依赖"}]}',
                '{"translations": [{"index": 1, "translated_title": "FAA 称玩家可能成为缓解空管短缺的潜在人选"}]}',
            ]
        )

        with patch.dict(
            "os.environ",
            {"AI_BACKEND": "claude-cli", "AI_MODEL": "test-model"},
            clear=False,
        ):
            with patch(
                "src.ai.title_translator._call_ai",
                side_effect=lambda *args, **kwargs: next(responses),
            ):
                result = translate_item_titles(items, _sample_config("/tmp"))

        self.assertEqual(
            result[0]["translated_title"],
            "法国拟弃用 Windows 转用 Linux，以减少对美技术依赖",
        )
        self.assertEqual(
            result[1]["translated_title"],
            "FAA 称玩家可能成为缓解空管短缺的潜在人选",
        )

    def test_github_items_are_not_translated(self) -> None:
        items = [
            {
                "source": "github",
                "title": "microsoft/markitdown",
                "url": "https://example.com/markitdown",
            }
        ]

        with patch.dict(
            "os.environ",
            {"AI_BACKEND": "claude-cli", "AI_MODEL": "test-model"},
            clear=False,
        ):
            with patch(
                "src.ai.title_translator._call_ai",
                side_effect=AssertionError("AI should not be called for GitHub items"),
            ):
                result = translate_item_titles(items, _sample_config("/tmp"))

        self.assertNotIn("translated_title", result[0])

    def test_batches_are_limited_to_ten_items(self) -> None:
        items = [
            {
                "source": "rss",
                "title": f"English title {i}",
                "url": f"https://example.com/item-{i}",
            }
            for i in range(12)
        ]
        prompts: list[str] = []

        def fake_call_ai(messages, *args, **kwargs):
            prompts.append(messages[-1]["content"])
            if len(prompts) == 1:
                return json_payload(0, 10)
            return json_payload(10, 12)

        def json_payload(start: int, end: int) -> str:
            translations = [
                {"index": i, "translated_title": f"中文标题 {i}"}
                for i in range(start, end)
            ]
            return '{"translations": ' + str(translations).replace("'", '"') + "}"

        with patch.dict(
            "os.environ",
            {"AI_BACKEND": "claude-cli", "AI_MODEL": "test-model"},
            clear=False,
        ):
            with patch(
                "src.ai.title_translator._call_ai",
                side_effect=fake_call_ai,
            ):
                result = translate_item_titles(items, _sample_config("/tmp"))

        self.assertEqual(len(prompts), 2)
        self.assertEqual(prompts[0].count("source=rss | title="), 10)
        self.assertEqual(prompts[1].count("source=rss | title="), 2)
        self.assertEqual(result[11]["translated_title"], "中文标题 11")

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
