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
_MAX_BATCH_SIZE = 10


def _looks_like_chinese(text: str) -> bool:
    sample = str(text or "").strip()
    if not sample:
        return False
    if re.search(r"[\u3040-\u30ff\u31f0-\u31ff]", sample):
        return False
    cjk_chars = re.findall(r"[\u4e00-\u9fff]", sample)
    if not cjk_chars:
        return False
    meaningful_chars = re.findall(r"[A-Za-z0-9\u4e00-\u9fff]", sample)
    if not meaningful_chars:
        return False
    return len(cjk_chars) / len(meaningful_chars) >= 0.6


def _extract_json_payload(raw_text: str) -> Any:
    text = str(raw_text or "").strip()
    if not text:
        return None

    candidates = [
        text,
        *(match.group() for match in re.finditer(r"\{[\s\S]*?\}", text)),
        *(match.group() for match in re.finditer(r"\[[\s\S]*?\]", text)),
    ]
    for candidate in candidates:
        try:
            return json.loads(candidate)
        except json.JSONDecodeError:
            continue
    return None


def _collect_translations_from_list(items: list[Any], total: int) -> dict[int, str]:
    result: dict[int, str] = {}
    for list_idx, entry in enumerate(items):
        if isinstance(entry, dict):
            idx = entry.get("index", entry.get("idx", entry.get("id", list_idx)))
            translated_title = str(
                entry.get("translated_title")
                or entry.get("title_zh")
                or entry.get("translation")
                or entry.get("title")
                or ""
            ).strip()
        else:
            idx = list_idx
            translated_title = str(entry or "").strip()
        if not isinstance(idx, int) or not (0 <= idx < total) or not translated_title:
            continue
        result[idx] = translated_title
    return result


def _parse_translations(raw_text: str, total: int) -> dict[int, str]:
    parsed = _extract_json_payload(raw_text)
    if parsed is None:
        return {}

    if isinstance(parsed, list):
        return _collect_translations_from_list(parsed, total)

    if not isinstance(parsed, dict):
        return {}

    for key in ("translations", "items", "results"):
        items = parsed.get(key)
        if isinstance(items, list):
            return _collect_translations_from_list(items, total)

    result: dict[int, str] = {}
    for key, value in parsed.items():
        if isinstance(key, str) and key.isdigit():
            idx = int(key)
            translated_title = str(value or "").strip()
            if 0 <= idx < total and translated_title:
                result[idx] = translated_title
    return result


def _build_translation_prompt(batch: list[tuple[int, dict[str, Any]]]) -> str:
    payload_lines = [
        f"[{idx}] source={str(item.get('source', '')).strip().lower()} | title={str(item.get('title') or '').strip()}"
        for idx, item in batch
    ]
    return (
        "请把下面这些内容标题翻译成自然、简洁、准确的中文标题。\n"
        "要求：\n"
        "- 仅翻译标题，不补充解释\n"
        "- 保留产品名、项目名、人名、机构名等专有名词\n"
        "- 如果原始标题已经是中文，可直接原样返回\n"
        "- 遇到 GitHub 仓库名（如 owner/repo）时，可补足成简洁自然的中文标题\n"
        "- 按输入 index 返回\n"
        "- 严格输出 JSON，不要包含任何额外文字\n\n"
        "输出格式：\n"
        '{"translations": [{"index": 0, "translated_title": "中文标题"}]}\n\n'
        "待翻译标题：\n" + "\n".join(payload_lines)
    )


def _translate_batch(
    batch: list[tuple[int, dict[str, Any]]], backend: str, call_kwargs: dict[str, Any]
) -> dict[int, str]:
    if not batch:
        return {}

    prompt = _build_translation_prompt(batch)
    messages = [
        {"role": "system", "content": "你是科技资讯编辑。只返回 JSON。"},
        {"role": "user", "content": prompt},
    ]

    try:
        raw_text = _call_ai(messages, backend, call_kwargs)
    except Exception as exc:
        logger.warning("标题翻译失败，batch=%d: %s", len(batch), exc)
        raw_text = ""

    translated = _parse_translations(raw_text, max(idx for idx, _ in batch) + 1)
    if translated:
        return translated

    if len(batch) == 1:
        logger.warning(
            "标题翻译未解析到结果，batch=%d raw=%r",
            len(batch),
            str(raw_text)[:400],
        )
        return {}

    midpoint = len(batch) // 2
    left = _translate_batch(batch[:midpoint], backend, call_kwargs)
    right = _translate_batch(batch[midpoint:], backend, call_kwargs)
    return {**left, **right}


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
        "max_tokens": max(2000, min(int(ai_cfg.get("max_tokens", 1200)), 8000)),
    }
    if api_base:
        call_kwargs["api_base"] = api_base

    pending_batch: list[tuple[int, dict[str, Any]]] = []
    for idx, item in enumerate(items):
        source = str(item.get("source") or "").strip().lower()
        title = str(item.get("title") or "").strip()
        translated_title = str(item.get("translated_title") or "").strip()
        if not title:
            continue
        if source == "github":
            continue
        if translated_title:
            continue
        if _looks_like_chinese(title):
            item["translated_title"] = title
            continue
        pending_batch.append((idx, item))

    if not pending_batch:
        return items

    translated: dict[int, str] = {}
    for start in range(0, len(pending_batch), _MAX_BATCH_SIZE):
        batch = pending_batch[start : start + _MAX_BATCH_SIZE]
        translated.update(_translate_batch(batch, backend, call_kwargs))

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

    logger.info("标题翻译完成: pending=%d translated=%d", len(pending_batch), applied)
    return items
