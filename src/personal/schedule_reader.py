"""
schedule_reader.py - 读取个人日程，返回今日日程列表
"""

import logging
from datetime import date
from pathlib import Path
from zoneinfo import ZoneInfo
from typing import Optional

import yaml

logger = logging.getLogger(__name__)

WEEKDAY_MAP = {
    0: "mon", 1: "tue", 2: "wed", 3: "thu", 4: "fri", 5: "sat", 6: "sun"
}


def read_today_schedule(
    schedule_path: str,
    today: date,
    timezone: Optional[ZoneInfo] = None,
) -> list[dict]:
    """
    读取 schedule.yaml，返回今日所有日程条目（daily + 当日 weekly）。

    Returns:
        list of dict，每项包含:
            - time: str (HH:MM)
            - title: str
            - location: str (可选)
            - notes: str (可选)
    """
    path = Path(schedule_path)
    if not path.exists():
        logger.debug(f"日程文件不存在: {path}")
        return []

    try:
        with open(path, encoding="utf-8") as f:
            data = yaml.safe_load(f)
    except Exception as e:
        logger.error(f"日程文件读取失败: {e}")
        return []

    if not data:
        return []

    weekday_key = WEEKDAY_MAP[today.weekday()]
    entries = []

    # daily 条目（每天都有）
    for item in data.get("daily", []):
        entries.append(_normalize_entry(item))

    # 当日 weekly 条目
    for item in data.get("weekly", {}).get(weekday_key, []):
        entries.append(_normalize_entry(item))

    # 按时间排序
    entries.sort(key=lambda x: x.get("time", "00:00"))
    return entries


def _normalize_entry(item: dict) -> dict:
    return {
        "time": str(item.get("time", "")).strip(),
        "title": str(item.get("title", "")).strip(),
        "location": str(item.get("location", "")).strip(),
        "notes": str(item.get("notes", "")).strip(),
    }
