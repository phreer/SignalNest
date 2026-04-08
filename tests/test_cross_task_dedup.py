from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.ai.dedup import (
    ai_dedup_across_candidates,
    ai_dedup_against_history,
    fallback_dedup_against_history,
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


def test_fallback_dedup_against_history_drops_same_youtube_video_across_tasks() -> None:
    history_records = [
        {
            "source": "youtube",
            "title": "Morning title",
            "url": "https://www.youtube.com/watch?v=abc123XYZ&feature=share",
            "video_id": "abc123XYZ",
            "schedule_name": "早间日报",
        }
    ]
    items = [
        {
            "source": "youtube",
            "title": "Evening title changed",
            "url": "https://youtu.be/abc123XYZ",
            "video_id": "abc123XYZ",
        },
        {
            "source": "youtube",
            "title": "Different video",
            "url": "https://www.youtube.com/watch?v=zzz999888",
            "video_id": "zzz999888",
        },
    ]

    assert fallback_dedup_against_history(items, history_records) == [1]


def test_fallback_dedup_against_history_drops_same_github_repo_across_tasks() -> None:
    history_records = [
        {
            "source": "github",
            "title": "NVIDIA/personaplex",
            "url": "https://github.com/NVIDIA/personaplex",
            "repo_full_name": "NVIDIA/personaplex",
            "schedule_name": "早间日报",
        }
    ]
    items = [
        {
            "source": "github",
            "title": "nvidia/personaplex",
            "url": "https://github.com/NVIDIA/personaplex?ref=trending",
        },
        {
            "source": "github",
            "title": "openai/openai-python",
            "url": "https://github.com/openai/openai-python",
        },
    ]

    assert fallback_dedup_against_history(items, history_records) == [1]


def test_ai_dedup_against_history_prefers_programmatic_result(monkeypatch) -> None:
    history_records = [
        {
            "source": "youtube",
            "title": "Morning title",
            "url": "https://www.youtube.com/watch?v=abc123XYZ",
            "video_id": "abc123XYZ",
        }
    ]
    items = [
        {
            "source": "youtube",
            "title": "Evening title changed",
            "url": "https://youtu.be/abc123XYZ",
        },
        {
            "source": "youtube",
            "title": "Different video",
            "url": "https://www.youtube.com/watch?v=zzz999888",
        },
    ]

    called = {"value": False}

    def _never_call_ai(*args, **kwargs):
        called["value"] = True
        raise AssertionError(
            "AI should not be called when programmatic history dedup already found duplicates"
        )

    monkeypatch.setattr("src.ai.dedup._call_ai", _never_call_ai)

    kept = ai_dedup_against_history(
        items,
        history_records,
        call_kwargs={"model": "test", "api_key": "test"},
        language="zh",
        backend="litellm",
    )

    assert kept == [1]
    assert called["value"] is False


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
