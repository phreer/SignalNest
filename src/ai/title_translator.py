"""
title_translator.py - 批量生成条目中文标题
"""

from __future__ import annotations

import json
import logging
import os
import re
from typing import Any

from src.ai.cli_backend import _call_ai

logger = logging.getLogger(__name__)


def _parse_translations(raw_text: str, total: int) -> dict[int, str]:
    json_match = re.search(r"\{[\s\S]*\}", raw_text or "")
    if not json_match:
        return {}

    try:
        parsed = json.loads(json_match.group())
    except json.JSONDecodeError:
        return {}

    if not isinstance(parsed, dict):
        return {}

    items = parsed.get("translations")
    if not isinstance(items, list):
        return {}

    result: dict[int, str] = {}
    for entry in items:
        if not isinstance(entry, dict):
            continue
        idx = entry.get("index")
        translated_title = str(entry.get("translated_title") or "").strip()
        if not isinstance(idx, int) or not (0 <= idx < total) or not translated_title:
            continue
        result[idx] = translated_title
    return result


def translate_item_titles(
    items: list[dict[str, Any]], config: dict
) -> list[dict[str, Any]]:
    if not items:
        return items

    ai_cfg = config.get("ai", {})
    backend = os.environ.get("AI_BACKEND") or ai_cfg.get("backend", "litellm")
    model = os.environ.get("AI_MODEL") or ai_cfg.get("model", "openai/gpt-4o-mini")
    api_key = os.environ.get("AI_API_KEY", "")
    api_base = os.environ.get("AI_API_BASE") or ai_cfg.get("api_base") or None

    if backend == "litellm" and not api_key and not api_base:
        logger.warning("标题翻译跳过：backend=litellm 且未配置 AI_API_KEY")
        return items

    call_kwargs: dict[str, Any] = {
        "model": model,
        "api_key": api_key,
        "max_tokens": min(int(ai_cfg.get("max_tokens", 1200)), 1600),
    }
    if api_base:
        call_kwargs["api_base"] = api_base

    payload_lines: list[str] = []
    pending_indices: list[int] = []
    for idx, item in enumerate(items):
        title = str(item.get("title") or "").strip()
        translated_title = str(item.get("translated_title") or "").strip()
        if not title:
            continue
        if translated_title:
            continue
        pending_indices.append(idx)
        payload_lines.append(
            f"[{idx}] source={str(item.get('source', '')).strip().lower()} | title={title}"
        )

    if not pending_indices:
        return items

    prompt = (
        "请把下面这些内容标题翻译成自然、简洁、准确的中文标题。\n"
        "要求：\n"
        "- 仅翻译标题，不补充解释\n"
        "- 保留产品名、项目名、人名、机构名等专有名词\n"
        "- 如果原始标题已经是中文，可直接原样返回\n"
        "- 按输入 index 返回\n"
        "- 严格输出 JSON，不要包含任何额外文字\n\n"
        "输出格式：\n"
        '{"translations": [{"index": 0, "translated_title": "中文标题"}]}\n\n'
        "待翻译标题：\n" + "\n".join(payload_lines)
    )

    try:
        messages = [
            {"role": "system", "content": "你是科技资讯编辑。只返回 JSON。"},
            {"role": "user", "content": prompt},
        ]
        raw_text = _call_ai(messages, backend, call_kwargs)
        translated = _parse_translations(raw_text, len(items))
    except Exception as exc:
        logger.warning("标题翻译失败，跳过: %s", exc)
        return items

    applied = 0
    for idx, title in translated.items():
        original = str(items[idx].get("title") or "").strip()
        if not original:
            continue
        if title == original:
            items[idx]["translated_title"] = original
        else:
            items[idx]["translated_title"] = title
        applied += 1

    logger.info("标题翻译完成: pending=%d translated=%d", len(pending_indices), applied)
    return items
