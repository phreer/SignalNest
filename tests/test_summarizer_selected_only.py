from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.ai.summarizer import summarize_items


def test_summarize_items_skips_history_based_dedup(monkeypatch) -> None:
    config = {
        "app": {"language": "zh"},
        "collectors": {"rss": {"max_items_per_feed": 3}},
        "ai": {
            "backend": "litellm",
            "model": "test-model",
            "max_items_per_digest": 5,
            "min_relevance_score": 5,
            "max_workers": 1,
        },
        "storage": {"data_dir": "data"},
    }
    raw_items = [
        {
            "source": "rss",
            "title": "Fresh item",
            "url": "https://example.com/fresh",
            "description": "short desc",
            "content_snippet": "short desc",
        }
    ]

    monkeypatch.setenv("AI_API_KEY", "test-key")

    def _fail_load_history(*args, **kwargs):
        raise AssertionError("history records should not be loaded")

    def _fail_history_dedup(*args, **kwargs):
        raise AssertionError("history dedup should not be called")

    monkeypatch.setattr(
        "src.ai.summarizer.load_recent_history_records",
        _fail_load_history,
        raising=False,
    )
    monkeypatch.setattr(
        "src.ai.summarizer.ai_dedup_against_history", _fail_history_dedup, raising=False
    )
    monkeypatch.setattr(
        "src.ai.summarizer.load_taste_examples", lambda *args, **kwargs: []
    )
    monkeypatch.setattr(
        "src.ai.summarizer.batch_select_by_titles", lambda items, *args, **kwargs: [0]
    )
    monkeypatch.setattr(
        "src.ai.summarizer.ai_dedup_across_candidates",
        lambda candidates, **kwargs: candidates,
    )
    monkeypatch.setattr(
        "src.ai.summarizer.score_single_item",
        lambda item, *args, **kwargs: (
            {
                **item,
                "ai_score": 9,
                "ai_summary": "summary",
                "ai_reason": "reason",
            },
            None,
        ),
    )

    result = summarize_items(raw_items, config, already_selected_keys=set())

    assert len(result) == 1
    assert result[0]["title"] == "Fresh item"
