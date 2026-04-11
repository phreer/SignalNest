from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.ai.dedup import (
    ai_dedup_across_candidates,
    dedup_key_for_item,
    stable_history_key,
)


def test_stable_history_key_uses_youtube_video_id() -> None:
    item = {
        "source": "youtube",
        "title": "Same video, different title",
        "url": "https://www.youtube.com/watch?v=abc123XYZ",
    }

    assert stable_history_key(item) == "youtube::abc123XYZ"


def test_stable_history_key_uses_github_repo_name() -> None:
    item = {
        "source": "github",
        "title": "owner/repo",
        "url": "https://github.com/owner/repo",
    }

    assert stable_history_key(item) == "github::owner/repo"


def test_dedup_key_for_item_normalizes_generic_urls() -> None:
    item = {
        "source": "rss",
        "title": "Example",
        "url": "https://example.com/post?a=1&utm_source=x",
    }

    assert dedup_key_for_item(item) == "https://example.com/post?a=1"


def test_ai_dedup_across_candidates_prefers_programmatic_result(monkeypatch) -> None:
    candidates = [
        {
            "source": "rss",
            "title": "Same article title with enough length for strict duplicate",
            "url": "https://example.com/post?a=1&utm_source=x",
        },
        {
            "source": "rss",
            "title": "Same article title with enough length for strict duplicate",
            "url": "https://example.com/post?a=1",
        },
        {
            "source": "rss",
            "title": "Another clearly different article title",
            "url": "https://example.com/post-b",
        },
    ]

    called = {"value": False}

    def _never_call_ai(*args, **kwargs):
        called["value"] = True
        raise AssertionError(
            "AI should not be called when programmatic candidate dedup already found duplicates"
        )

    monkeypatch.setattr("src.ai.dedup._call_ai", _never_call_ai)

    kept = ai_dedup_across_candidates(
        candidates,
        focus="",
        call_kwargs={"model": "test", "api_key": "test"},
        language="zh",
        backend="litellm",
    )

    assert len(kept) == 2
    assert any(
        item["url"] == "https://example.com/post?a=1&utm_source=x" for item in kept
    )
    assert any(item["url"] == "https://example.com/post-b" for item in kept)
    assert called["value"] is False
