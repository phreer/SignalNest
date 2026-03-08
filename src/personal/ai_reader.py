"""
ai_reader.py - 用 AI 读取 schedule.md 和 projects.md

schedule.md 和 projects.md 支持任意格式（表格、自然语言、Markdown 等），
由 LLM 负责理解和提取结构化数据。
"""

import json
import logging
import os
import re
from datetime import date, timedelta
from pathlib import Path
from typing import Optional

from src.ai.cli_backend import _call_ai

logger = logging.getLogger(__name__)

WEEKDAY_ZH = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]


def read_today_schedule(
    schedule_path: str,
    today: date,
    config: dict,
) -> list[dict]:
    """
    用 AI 从 schedule.md 中提取今日日程。
    支持任意 Markdown 格式，包括课程表（按周次自动过滤）。

    Returns:
        list of dict: {time, title, location, notes}
    """
    content = _read_file(schedule_path)
    if content is None:
        return []

    weekday_zh = WEEKDAY_ZH[today.weekday()]
    system_prompt = f"""你是日程解析助手，只输出 JSON，不输出任何其他内容。

根据用户提供的日程文件，提取今日需要显示的日程条目。
今天是 {weekday_zh}，日期 {today.isoformat()}。

提取规则：
- 包含每日固定事项（每天都有的）
- 包含今日对应星期的事项
- 如果有课程表，根据文件中的学期开始日期计算当前周次，只提取周次范围内的课程
- 按时间升序排列

输出格式（严格 JSON，不含注释或 markdown 代码块）：
{{"entries": [{{"time": "HH:MM", "title": "事项名称", "location": "地点或空字符串", "notes": "备注或空字符串"}}]}}

time 字段格式为 HH:MM（24小时制），必填。其余字段若无内容填空字符串。"""

    raw = _call_llm(system_prompt, content, config)
    if raw is None:
        return []

    try:
        m = re.search(r'\{[\s\S]*\}', raw)
        if not m:
            logger.warning("schedule AI 返回内容中未找到 JSON")
            return []
        parsed = json.loads(m.group())
        entries = parsed.get("entries", [])
        return [_normalize_entry(e) for e in entries if isinstance(e, dict) and e.get("time")]
    except (json.JSONDecodeError, ValueError) as e:
        logger.warning(f"schedule AI 返回 JSON 解析失败: {e}")
        return []


def read_active_projects(
    projects_path: str,
    today: date,
    config: dict,
    lookahead_days: int = 7,
) -> list[dict]:
    """
    用 AI 从 projects.md 中提取活跃项目及子任务。
    支持任意 Markdown 格式（checkbox、自然语言等）。

    Returns:
        list of dict: {title, due, due_status, tasks}
    """
    content = _read_file(projects_path)
    if content is None:
        return []

    system_prompt = f"""你是任务解析助手，只输出 JSON，不输出任何其他内容。

根据用户提供的项目文件，提取所有有待办子任务的活跃项目。
今天是 {today.isoformat()}。

提取规则：
- 只返回有未完成子任务的项目
- 已完成的子任务不包含在内
- 对每个子任务，按以下逻辑处理截止日期：
  1. 若用户已明确写出日期（如注释 <!-- 2026-03-20 -->、"截止"、"due" 等任意格式），提取该日期，due_source 设为 "user"，due_reason 为 null
  2. 若未写日期，根据任务描述关键词（如"紧急"、"本周"、"下周前"、"尽快"、"明天"）和项目整体上下文，推断一个合理的建议完成日期（格式 YYYY-MM-DD），due_source 设为 "ai"，due_reason 填写≤15字的判断理由
  3. 若任务明显无时效性（长期规划、随时可做、无任何紧迫信号），due 和 due_source 均为 null

输出格式（严格 JSON，不含注释或 markdown 代码块）：
{{"projects": [{{"title": "项目名称", "due": "YYYY-MM-DD 或 null", "tasks": [{{"title": "子任务名称", "due": "YYYY-MM-DD 或 null", "due_source": "user 或 ai 或 null", "due_reason": "理由或 null"}}]}}]}}"""

    raw = _call_llm(system_prompt, content, config)
    if raw is None:
        return []

    try:
        m = re.search(r'\{[\s\S]*\}', raw)
        if not m:
            logger.warning("projects AI 返回内容中未找到 JSON")
            return []
        parsed = json.loads(m.group())
        projects_raw = parsed.get("projects", [])
    except (json.JSONDecodeError, ValueError) as e:
        logger.warning(f"projects AI 返回 JSON 解析失败: {e}")
        return []

    cutoff = today + timedelta(days=lookahead_days)
    result = []
    for proj in projects_raw:
        if not isinstance(proj, dict):
            continue
        tasks = [
            _enrich_task(t, today, cutoff)
            for t in proj.get("tasks", [])
            if isinstance(t, dict)
        ]
        if not tasks:
            continue
        due = proj.get("due") or None
        result.append({
            "title":           str(proj.get("title", "")).strip(),
            "due":        due,
            "due_status": _due_status(due, today, cutoff) if due else None,
            "tasks":           tasks,
        })

    return result


# ── Internal helpers ──────────────────────────────────────────────────────────

def _read_file(path_str: str) -> Optional[str]:
    path = Path(path_str)
    if not path.exists():
        logger.debug(f"文件不存在: {path}")
        return None
    try:
        return path.read_text(encoding="utf-8")
    except Exception as e:
        logger.error(f"文件读取失败 {path}: {e}")
        return None


def _build_call_kwargs(config: dict) -> dict:
    ai_cfg   = config.get("ai", {})
    model    = os.environ.get("AI_MODEL")    or ai_cfg.get("model", "openai/gpt-4o-mini")
    api_key  = os.environ.get("AI_API_KEY", "")
    api_base = os.environ.get("AI_API_BASE") or ai_cfg.get("api_base") or None
    kwargs = dict(model=model, api_key=api_key, max_tokens=1024)
    if api_base:
        kwargs["api_base"] = api_base
    return kwargs


def _call_llm(system_prompt: str, user_content: str, config: dict) -> Optional[str]:
    try:
        ai_cfg = config.get("ai", {})
        backend = os.environ.get("AI_BACKEND") or ai_cfg.get("backend", "litellm")
        call_kwargs = _build_call_kwargs(config)
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user",   "content": user_content},
        ]
        return _call_ai(messages, backend, call_kwargs)
    except Exception as e:
        logger.warning(f"AI 读取调用失败: {e}")
        return None


def _normalize_entry(e: dict) -> dict:
    return {
        "time":     str(e.get("time", "")).strip(),
        "title":    str(e.get("title", "")).strip(),
        "location": str(e.get("location", "")).strip(),
        "notes":    str(e.get("notes", "")).strip(),
    }


def _enrich_task(task: dict, today: date, cutoff: date) -> dict:
    due_str = task.get("due") or None
    source = task.get("due_source") or None
    reason = task.get("due_reason") or None
    status    = None
    days_until = None
    if due_str:
        try:
            due_date   = date.fromisoformat(str(due_str))
            days_until = (due_date - today).days
            status     = _due_status_from_date(due_date, today, cutoff)
        except ValueError:
            pass
    return {
        "title":           str(task.get("title", "")).strip(),
        "due":        due_str,
        "due_source": source,
        "due_reason": reason,
        "status":          status,
        "days_until":      days_until,
    }


def _due_status(due_str: str, today: date, cutoff: date) -> Optional[str]:
    try:
        return _due_status_from_date(date.fromisoformat(due_str), today, cutoff)
    except ValueError:
        return None


def _due_status_from_date(due_date: date, today: date, cutoff: date) -> Optional[str]:
    if due_date < today:
        return "overdue"
    elif due_date == today:
        return "today"
    elif due_date <= cutoff:
        return "upcoming"
    return None
