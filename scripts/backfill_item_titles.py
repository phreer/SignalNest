from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path
from typing import Any

from src.ai.title_translator import translate_item_titles
from src.config_loader import load_config
from src.web.store import AppStateStore


def _load_pending_items(store: AppStateStore, limit: int) -> list[dict[str, Any]]:
    conn = sqlite3.connect(store.db_path)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            """
            SELECT *
            FROM raw_items
            WHERE source != 'github'
              AND COALESCE(translated_title, '') = ''
            ORDER BY id ASC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
    finally:
        conn.close()

    items: list[dict[str, Any]] = []
    for row in rows:
        raw = {}
        if row["raw_json"]:
            try:
                raw = json.loads(row["raw_json"])
            except json.JSONDecodeError:
                raw = {}
        item = dict(raw) if isinstance(raw, dict) else {}
        item.setdefault("source", row["source"])
        item.setdefault("url", row["url"])
        item.setdefault("title", row["title"])
        item.setdefault("translated_title", row["translated_title"] or "")
        item.setdefault("external_id", row["external_id"] or "")
        item.setdefault("author", row["author"] or "")
        item.setdefault("feed_title", row["feed_title"] or "")
        item.setdefault("language", row["language"] or "")
        item.setdefault("published_at", row["published_at"] or "")
        items.append(item)
    return items


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Backfill translated_title for incomplete raw_items"
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=100,
        help="Maximum number of raw_items to backfill in one run",
    )
    args = parser.parse_args()

    config = load_config()
    store = AppStateStore.from_config(config)
    store.init_db()

    pending_items = _load_pending_items(store, limit=max(1, args.limit))
    print(f"[backfill] pending={len(pending_items)}")
    if not pending_items:
        return

    translated_items = translate_item_titles(pending_items, config)
    updated = [
        item
        for item in translated_items
        if str(item.get("translated_title") or "").strip()
    ]
    print(f"[backfill] translated={len(updated)}")
    if not updated:
        return

    store.upsert_raw_items(updated)
    print("[backfill] done")


if __name__ == "__main__":
    main()
