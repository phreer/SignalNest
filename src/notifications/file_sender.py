"""
file_sender.py - 将日报写入本地文件，作为一种无外部依赖的通知渠道。
"""

from __future__ import annotations

import json
import logging
from datetime import date, datetime
from pathlib import Path

from src.notifications.email_sender import _render_html

logger = logging.getLogger(__name__)


def _sanitize_schedule_name(name: str) -> str:
    safe = "".join(ch.lower() if ch.isalnum() else "_" for ch in (name or "").strip())
    safe = "_".join(part for part in safe.split("_") if part)
    return safe or "digest"


def _json_default(value):
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    return str(value)


def send_file(payload: dict, config: dict) -> bool:
    """
    将 payload 以 JSON 和 HTML 两种格式写到 data 目录下。
    返回 True 表示至少成功写出主文件。
    """
    file_cfg = config.get("notifications", {}).get("file", {})
    data_dir = Path(config.get("storage", {}).get("data_dir", "data"))
    output_dir = data_dir / str(file_cfg.get("output_dir", "outputs"))
    archive_enabled = bool(file_cfg.get("archive", True))

    output_dir.mkdir(parents=True, exist_ok=True)

    html = _render_html(payload, config)
    json_text = json.dumps(payload, ensure_ascii=False, indent=2, default=_json_default)

    latest_json = output_dir / "latest.json"
    latest_html = output_dir / "latest.html"
    latest_json.write_text(json_text, encoding="utf-8")
    latest_html.write_text(html, encoding="utf-8")

    written_paths = [latest_json, latest_html]

    if archive_enabled:
        run_dt = payload.get("datetime")
        if isinstance(run_dt, str):
            run_dt = datetime.fromisoformat(run_dt)
        if not isinstance(run_dt, datetime):
            run_dt = datetime.now()

        timestamp = run_dt.strftime("%Y%m%d_%H%M%S_%f")
        slug = _sanitize_schedule_name(str(payload.get("schedule_name", "")))
        archive_json = output_dir / f"digest_{timestamp}_{slug}.json"
        archive_html = output_dir / f"digest_{timestamp}_{slug}.html"
        archive_json.write_text(json_text, encoding="utf-8")
        archive_html.write_text(html, encoding="utf-8")
        written_paths.extend([archive_json, archive_html])

    logger.info("文件通知已写出: %s", ", ".join(str(p) for p in written_paths))
    return True
