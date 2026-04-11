from __future__ import annotations

import argparse
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from src.ai.title_translator import translate_item_titles
from src.config_loader import load_config
from src.web.store import AppStateStore


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

    pending_items = store.list_raw_items_missing_translation(limit=max(1, args.limit))
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
