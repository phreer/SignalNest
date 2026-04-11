from pathlib import Path
import sqlite3
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.web.store import AppStateStore


def test_get_selected_dedup_keys_returns_legacy_and_canonical_keys(
    tmp_path: Path,
) -> None:
    store = AppStateStore(tmp_path / "app.db")
    store.init_db()

    conn = sqlite3.connect(store.db_path)
    try:
        conn.execute(
            """
            INSERT INTO raw_items (
                source, url, title, translated_title, dedup_key, external_id, author, feed_title,
                language, published_at, first_seen_at, last_seen_at, seen_count, raw_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "rss",
                "https://example.com/post?a=1&utm_source=x",
                "Example",
                None,
                "rss:https://example.com/post?a=1&utm_source=x",
                None,
                None,
                None,
                None,
                None,
                "2026-01-01T00:00:00+00:00",
                "2026-01-01T00:00:00+00:00",
                1,
                "{}",
            ),
        )
        raw_item_id = conn.execute("SELECT id FROM raw_items").fetchone()[0]
        conn.execute(
            """
            INSERT INTO item_annotations (
                raw_item_id, job_run_id, digest_id, selected_for_digest, ai_score, ai_summary, ai_reason, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (raw_item_id, 1, None, 1, 8, "", "", "2026-01-01T00:00:00+00:00"),
        )
        conn.commit()
    finally:
        conn.close()

    keys = store.get_selected_dedup_keys()

    assert "rss:https://example.com/post?a=1&utm_source=x" in keys
    assert "https://example.com/post?a=1" in keys
