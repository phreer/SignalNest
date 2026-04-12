"""
summarizer.py - AI 摘要与品味过滤引擎（主流程编排）

子模块职责：
  - dedup.py   : URL/标题归一化、内容身份键、跨源去重
  - filter.py  : 批量标题筛选、来源保底逻辑
  - scorer.py  : 单条内容 AI 评分与摘要
  - digest.py  : 生成「今日要点」整体总结
"""

from __future__ import annotations

import logging
import os
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Callable, Optional

from src.ai.dedup import (
    ai_dedup_across_candidates,
    item_key,
)
from src.ai.digest import generate_digest_summary
from src.ai.feedback import load_taste_examples
from src.ai.filter import (
    ai_pick_fill_candidates,
    batch_select_by_titles,
    ensure_source_candidates,
    enforce_source_minimums,
    normalize_source_minimums,
)
from src.ai.scorer import build_scoring_system_prompt, score_single_item

# Re-export generate_digest_summary so existing callers don't need to change imports
__all__ = ["summarize_items", "generate_digest_summary"]

logger = logging.getLogger(__name__)

SummarizerProgress = Callable[[dict], None]


def _safe_positive_int(value, default: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed > 0 else default


def _build_call_kwargs(ai_cfg: dict) -> dict:
    model = os.environ.get("AI_MODEL") or ai_cfg.get("model", "openai/gpt-4o-mini")
    api_base = os.environ.get("AI_API_BASE") or ai_cfg.get("api_base") or None
    api_key = os.environ.get("AI_API_KEY", "")

    call_kwargs: dict = {
        "model": model,
        "api_key": api_key,
        "max_tokens": ai_cfg.get("max_tokens", 512),
        "timeout": _safe_positive_int(ai_cfg.get("request_timeout_seconds", 90), 90),
        "num_retries": _safe_positive_int(ai_cfg.get("request_num_retries", 1), 1),
    }
    if api_base:
        call_kwargs["api_base"] = api_base
    return call_kwargs


def _emit_progress(progress_callback: SummarizerProgress | None, event: dict) -> None:
    if progress_callback is None:
        return
    try:
        progress_callback(event)
    except Exception:
        logger.debug("summarizer progress callback failed", exc_info=True)


def summarize_items(
    raw_items: list[dict],
    config: dict,
    min_score: Optional[int] = None,
    max_output: Optional[int] = None,
    focus: str = "",
    schedule_name: str = "",
    already_selected_keys: set[str] | None = None,
    progress_callback: SummarizerProgress | None = None,
) -> list[dict]:
    """
    两阶段处理：
      阶段 A : 过滤历史已入选内容
      阶段 1 : AI 仅看标题+简介，一次调用批量筛选入围条目
      阶段 B : 跨源去重（精读前）
      阶段 2 : 仅对入围条目逐条调用 AI，生成完整评分+摘要

    Args:
        raw_items: 来自各 collector 的原始数据列表
        config:    AppConfig dict
        min_score: 低于此分数的条目被过滤（默认读 config）
        max_output: 最多返回条目数（默认读 config）
        focus:     本次筛选方向（来自 schedule.focus），传给 AI 做相关度评分
        schedule_name: 当前任务名，用于日志和归档上下文

    Returns:
        过滤并排序后的列表，每项新增：
            - ai_score: int (1-10)
            - ai_summary: str
            - ai_reason: str
    """
    if not raw_items:
        return []

    ai_cfg = config.get("ai", {})
    rss_cfg = config.get("collectors", {}).get("rss", {})

    min_score_default = _safe_positive_int(ai_cfg.get("min_relevance_score", 5), 5)
    min_score = (
        _safe_positive_int(min_score, min_score_default)
        if min_score is not None
        else min_score_default
    )

    default_cap = 15
    raw_config_cap = ai_cfg.get("max_items_per_digest", default_cap)
    config_cap = _safe_positive_int(raw_config_cap, default_cap)
    if config_cap != raw_config_cap:
        logger.warning(
            f"ai.max_items_per_digest 非法({raw_config_cap!r})，回退默认值 {default_cap}"
        )

    if max_output is None:
        requested_max = config_cap
    else:
        requested_max = _safe_positive_int(max_output, config_cap)
    effective_max_output = min(requested_max, config_cap)

    logger.info(
        f"  新闻筛选开始: raw_count={len(raw_items)} "
        f"requested_max={requested_max} config_cap={config_cap} "
        f"effective_cap={effective_max_output} min_score={min_score} "
        f"schedule={schedule_name or '(unknown)'}"
    )
    _emit_progress(
        progress_callback,
        {
            "type": "summarizer_progress",
            "stage": "start",
            "raw_count": len(raw_items),
            "requested_max": requested_max,
            "config_cap": config_cap,
            "effective_cap": effective_max_output,
            "min_score": min_score,
            "schedule_name": schedule_name,
        },
    )

    rss_per_feed_limit = _safe_positive_int(rss_cfg.get("max_items_per_feed", 3), 3)
    backend = os.environ.get("AI_BACKEND") or ai_cfg.get("backend", "litellm")
    api_key = os.environ.get("AI_API_KEY", "")

    if backend == "litellm" and not api_key:
        logger.error("AI_API_KEY 未配置，跳过 AI 摘要（backend=litellm 需要 API key）")
        return raw_items[:effective_max_output]

    call_kwargs = _build_call_kwargs(ai_cfg)

    taste_limit = ai_cfg.get("taste_examples_limit", 8)
    source_minimums = normalize_source_minimums(ai_cfg.get("min_items_per_source"))
    language = config.get("app", {}).get("language", "zh")
    max_workers = ai_cfg.get("max_workers", 5)

    taste_examples = load_taste_examples(config, limit=taste_limit)
    items_after_history = list(raw_items)

    # ── 阶段 A2：跳过曾入选过 digest 的 items ────────────────────────────────
    if already_selected_keys:
        pre_count = len(items_after_history)
        t0 = time.monotonic()
        items_after_history = [
            item
            for item in items_after_history
            if item_key(item) not in already_selected_keys
        ]
        skipped = pre_count - len(items_after_history)
        if skipped:
            logger.info(f"  skipped_already_selected={skipped}")
        _emit_progress(
            progress_callback,
            {
                "type": "summarizer_progress",
                "stage": "filter_history",
                "before_count": pre_count,
                "after_count": len(items_after_history),
                "skipped_count": skipped,
                "duration_ms": round((time.monotonic() - t0) * 1000),
            },
        )
        if not items_after_history:
            logger.info(f"  新闻筛选完成: final_count=0 (all items already selected)")
            _emit_progress(
                progress_callback,
                {
                    "type": "summarizer_progress",
                    "stage": "completed",
                    "final_count": 0,
                    "reason": "all_items_already_selected",
                },
            )
            return []

    # ── 阶段 1：标题批量筛选 ──────────────────────────────────────────────────
    max_keep = min(effective_max_output * 2, len(items_after_history))
    t0 = time.monotonic()
    selected_indices = batch_select_by_titles(
        items_after_history,
        focus,
        taste_examples,
        call_kwargs,
        language,
        max_keep,
        backend=backend,
    )
    selected_indices = ensure_source_candidates(
        items_after_history, selected_indices, source_minimums, max_keep
    )
    candidates = [items_after_history[i] for i in selected_indices]
    _emit_progress(
        progress_callback,
        {
            "type": "summarizer_progress",
            "stage": "stage1_select",
            "input_count": len(items_after_history),
            "selected_count": len(candidates),
            "max_keep": max_keep,
            "duration_ms": round((time.monotonic() - t0) * 1000),
        },
    )
    logger.info(f"  after_stage1_select_count={len(candidates)}")
    if not candidates:
        logger.info(
            f"  新闻筛选完成: final_count=0 effective_cap={effective_max_output}"
        )
        _emit_progress(
            progress_callback,
            {
                "type": "summarizer_progress",
                "stage": "completed",
                "final_count": 0,
                "reason": "no_candidates_after_stage1",
            },
        )
        return []

    # ── 阶段 B：跨源去重 ──────────────────────────────────────────────────────
    before_dedup = len(candidates)
    t0 = time.monotonic()
    candidates = ai_dedup_across_candidates(
        candidates,
        focus=focus,
        call_kwargs=call_kwargs,
        language=language,
        backend=backend,
    )
    _emit_progress(
        progress_callback,
        {
            "type": "summarizer_progress",
            "stage": "cross_source_dedup",
            "before_count": before_dedup,
            "after_count": len(candidates),
            "duration_ms": round((time.monotonic() - t0) * 1000),
        },
    )
    logger.info(f"  after_cross_source_dedup_count={len(candidates)}")
    if not candidates:
        logger.info(
            f"  新闻筛选完成: final_count=0 effective_cap={effective_max_output}"
        )
        _emit_progress(
            progress_callback,
            {
                "type": "summarizer_progress",
                "stage": "completed",
                "final_count": 0,
                "reason": "no_candidates_after_dedup",
            },
        )
        return []

    # ── RSS 每 feed 封顶 ──────────────────────────────────────────────────────
    feed_counts: dict[str, int] = {}
    capped: list[dict] = []
    for item in candidates:
        if item.get("source") == "rss":
            feed_key = item.get("feed_title", item.get("url", "unknown"))
            feed_counts[feed_key] = feed_counts.get(feed_key, 0) + 1
            if feed_counts[feed_key] > rss_per_feed_limit:
                continue
        capped.append(item)
    if len(capped) < len(candidates):
        logger.info(
            f"  RSS per-feed 封顶：{len(candidates)} → {len(capped)} 条进入摘要"
        )
    _emit_progress(
        progress_callback,
        {
            "type": "summarizer_progress",
            "stage": "rss_feed_cap",
            "before_count": len(candidates),
            "after_count": len(capped),
            "rss_per_feed_limit": rss_per_feed_limit,
        },
    )
    candidates = capped

    # ── 候选不足时从剩余池补全 ────────────────────────────────────────────────
    if len(candidates) < effective_max_output:
        need_fill = effective_max_output - len(candidates)
        existing_keys = {item_key(item) for item in candidates}
        remaining_pool = [
            item for item in items_after_history if item_key(item) not in existing_keys
        ]
        t0 = time.monotonic()
        fill_indices = ai_pick_fill_candidates(
            candidates,
            remaining_pool,
            need_fill,
            focus,
            call_kwargs,
            language,
            backend=backend,
        )
        filled = 0
        for idx in fill_indices:
            if not (0 <= idx < len(remaining_pool)):
                continue
            item = remaining_pool[idx]
            key = item_key(item)
            if key in existing_keys:
                continue
            if item.get("source") == "rss":
                feed_key = item.get("feed_title", item.get("url", "unknown"))
                if feed_counts.get(feed_key, 0) >= rss_per_feed_limit:
                    continue
                feed_counts[feed_key] = feed_counts.get(feed_key, 0) + 1
            candidates.append(item)
            existing_keys.add(key)
            filled += 1
            if len(candidates) >= effective_max_output:
                break
        logger.info(
            f"  after_fill_candidates_count={len(candidates)} (filled={filled}, need={need_fill})"
        )
        _emit_progress(
            progress_callback,
            {
                "type": "summarizer_progress",
                "stage": "fill_candidates",
                "remaining_pool_count": len(remaining_pool),
                "need_count": need_fill,
                "filled_count": filled,
                "candidate_count": len(candidates),
                "duration_ms": round((time.monotonic() - t0) * 1000),
            },
        )

    if not candidates:
        logger.info(
            f"  新闻筛选完成: final_count=0 effective_cap={effective_max_output}"
        )
        return []

    # ── 精读前：为 YouTube 视频补充字幕 ──────────────────────────────────────
    yt_candidates = [item for item in candidates if item.get("source") == "youtube"]
    if yt_candidates:
        logger.info(f"  拉取 YouTube 字幕（{len(yt_candidates)} 个视频）...")
        t0 = time.monotonic()
        try:
            from src.collectors.youtube_collector import _get_transcript

            for item in yt_candidates:
                video_id = item.get("video_id", "")
                if not video_id and "v=" in item.get("url", ""):
                    video_id = item["url"].split("v=")[-1].split("&")[0]
                if video_id:
                    item["transcript_snippet"] = _get_transcript(video_id)
        except Exception as e:
            logger.warning(f"YouTube 字幕补充失败: {e}")
        _emit_progress(
            progress_callback,
            {
                "type": "summarizer_progress",
                "stage": "youtube_transcripts",
                "youtube_candidate_count": len(yt_candidates),
                "duration_ms": round((time.monotonic() - t0) * 1000),
            },
        )

    # ── 阶段 2：并行完整评分+摘要 ─────────────────────────────────────────────
    system_prompt = build_scoring_system_prompt(taste_examples, language, focus=focus)
    results: list[dict] = []
    low_score_pool: list[dict] = []
    total = len(candidates)
    _emit_progress(
        progress_callback,
        {
            "type": "summarizer_progress",
            "stage": "stage2_score_start",
            "candidate_count": total,
            "max_workers": max_workers,
        },
    )

    t0 = time.monotonic()
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {
            executor.submit(
                score_single_item,
                item,
                system_prompt,
                backend,
                call_kwargs,
                min_score,
                i,
                total,
            ): item
            for i, item in enumerate(candidates)
        }
        completed = 0
        for future in as_completed(futures):
            high, low = future.result()
            if high is not None:
                results.append(high)
            elif low is not None:
                low_score_pool.append(low)
            completed += 1
            _emit_progress(
                progress_callback,
                {
                    "type": "summarizer_progress",
                    "stage": "stage2_score_progress",
                    "completed": completed,
                    "total": total,
                    "high_score_count": len(results),
                    "low_score_count": len(low_score_pool),
                },
            )

    _emit_progress(
        progress_callback,
        {
            "type": "summarizer_progress",
            "stage": "stage2_score_done",
            "candidate_count": total,
            "high_score_count": len(results),
            "low_score_count": len(low_score_pool),
            "duration_ms": round((time.monotonic() - t0) * 1000),
        },
    )

    logger.info(f"  after_stage2_score_count={len(results)}")
    results.sort(key=lambda x: x.get("ai_score", 0), reverse=True)

    # 按来源分桶，每源均匀分配
    source_buckets: dict[str, list] = {}
    for item in results:
        source_buckets.setdefault(item.get("source", "unknown"), []).append(item)

    per_source_min = (
        effective_max_output // len(source_buckets)
        if source_buckets
        else effective_max_output
    )
    selected: list[dict] = []
    leftover: list[dict] = []
    for items in source_buckets.values():
        selected.extend(items[:per_source_min])
        leftover.extend(items[per_source_min:])

    remaining = effective_max_output - len(selected)
    if remaining > 0 and leftover:
        leftover.sort(key=lambda x: x.get("ai_score", 0), reverse=True)
        selected.extend(leftover[:remaining])

    selected = enforce_source_minimums(
        selected=selected,
        high_score_items=results,
        low_score_items=low_score_pool,
        source_minimums=source_minimums,
        max_output=effective_max_output,
    )
    final_items = selected[:effective_max_output]
    final_items.sort(key=lambda x: x.get("ai_score", 0), reverse=True)

    _emit_progress(
        progress_callback,
        {
            "type": "summarizer_progress",
            "stage": "completed",
            "final_count": len(final_items),
            "selected_count": len(selected),
            "high_score_count": len(results),
            "low_score_count": len(low_score_pool),
            "effective_cap": effective_max_output,
        },
    )
    logger.info(
        f"  新闻筛选完成: final_count={len(final_items)} "
        f"effective_cap={effective_max_output} requested_max={requested_max} config_cap={config_cap}"
    )
    return final_items
