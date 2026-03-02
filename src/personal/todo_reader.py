"""
todo_reader.py - 读取 todos.yaml，返回到期/逾期/即将到期的 TODO 项目
"""

import logging
from datetime import date, timedelta
from pathlib import Path
from typing import Optional

import yaml

logger = logging.getLogger(__name__)


def read_due_todos(
    todos_path: str,
    today: date,
    lookahead_days: int = 3,
) -> list[dict]:
    """
    读取 todos.yaml，按优先级返回需要提醒的 TODO：
      - 逾期未完成（due < today）
      - 今日到期（due == today）
      - 即将到期（today < due <= today + lookahead_days）

    Returns:
        list of dict，每项包含:
            - id, title, due, priority, notes, done
            - status: "overdue" | "today" | "upcoming"
    """
    path = Path(todos_path)
    if not path.exists():
        logger.debug(f"TODO 文件不存在: {path}")
        return []

    try:
        with open(path, encoding="utf-8") as f:
            data = yaml.safe_load(f)
    except Exception as e:
        logger.error(f"TODO 文件读取失败: {e}")
        return []

    if not data:
        return []

    settings = data.get("settings", {})
    show_overdue = settings.get("show_overdue", True)
    lookahead = settings.get("lookahead_days", lookahead_days)
    cutoff = today + timedelta(days=lookahead)

    results = []
    priority_order = {"high": 0, "medium": 1, "low": 2}

    for item in data.get("todos", []):
        if item.get("done", False):
            continue

        due_raw = item.get("due")
        if not due_raw:
            continue

        try:
            due_date = date.fromisoformat(str(due_raw))
        except ValueError:
            logger.warning(f"无效日期格式: {due_raw}")
            continue

        if due_date < today:
            if not show_overdue:
                continue
            status = "overdue"
        elif due_date == today:
            status = "today"
        elif due_date <= cutoff:
            status = "upcoming"
        else:
            continue

        results.append({
            "id": item.get("id", ""),
            "title": item.get("title", ""),
            "due": str(due_date),
            "priority": item.get("priority", "medium"),
            "notes": item.get("notes", ""),
            "done": False,
            "status": status,
            "days_until": (due_date - today).days,
        })

    # 排序：逾期 > 今日 > 即将，同状态内按优先级
    status_order = {"overdue": 0, "today": 1, "upcoming": 2}
    results.sort(key=lambda x: (
        status_order.get(x["status"], 9),
        priority_order.get(x["priority"], 9),
    ))

    return results
