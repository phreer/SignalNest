"""
dedup.py - URL/标题归一化 + 内容身份键 + 跨源去重
"""

from __future__ import annotations

import logging
import re
from datetime import datetime
from difflib import SequenceMatcher
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from src.ai.cli_backend import _call_ai

logger = logging.getLogger(__name__)

_TRACKING_QUERY_KEYS = {
    "spm",
    "from",
    "ref",
    "source",
    "fbclid",
    "gclid",
    "si",
    "feature",
    "mc_cid",
    "mc_eid",
}


# ── URL / 标题归一化 ──────────────────────────────────────────────────────────


def normalize_title(title: str) -> str:
    text = str(title or "").strip().lower()
    text = re.sub(r"\s+", " ", text)
    text = re.sub(r"^[^\w\u4e00-\u9fff]+|[^\w\u4e00-\u9fff]+$", "", text)
    return text


def normalize_url(url: str) -> str:
    raw = str(url or "").strip()
    if not raw:
        return ""

    try:
        parsed = urlsplit(raw)
    except Exception:
        return raw.lower()

    if not parsed.scheme and not parsed.netloc:
        return raw.lower()

    scheme = parsed.scheme.lower()
    netloc = parsed.netloc.lower()
    if scheme == "http" and netloc.endswith(":80"):
        netloc = netloc[:-3]
    if scheme == "https" and netloc.endswith(":443"):
        netloc = netloc[:-4]

    path = re.sub(r"/{2,}", "/", parsed.path or "/")
    if path != "/":
        path = path.rstrip("/")

    cleaned_query: list[tuple[str, str]] = []
    for k, v in parse_qsl(parsed.query, keep_blank_values=True):
        key = str(k).strip()
        if key.lower().startswith("utm_") or key.lower() in _TRACKING_QUERY_KEYS:
            continue
        cleaned_query.append((key, str(v).strip()))
    cleaned_query.sort(key=lambda x: (x[0].lower(), x[1]))
    query = urlencode(cleaned_query, doseq=True)

    return urlunsplit((scheme, netloc, path, query, ""))


def _title_similarity(a: str, b: str) -> float:
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    return SequenceMatcher(None, a, b).ratio()


def _is_strict_title_duplicate(a: str, b: str) -> bool:
    if not a or not b:
        return False
    if a == b:
        return True
    if min(len(a), len(b)) < 20:
        return False
    return _title_similarity(a, b) >= 0.97


def _parse_published_ts(value: str) -> float:
    raw = str(value or "").strip()
    if not raw:
        return 0.0
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).timestamp()
    except Exception:
        return 0.0


def _item_completeness_score(item: dict) -> int:
    score = 0
    if normalize_url(str(item.get("url", ""))):
        score += 3
    if str(item.get("title", "")).strip():
        score += 2
    if str(item.get("published_at", "")).strip():
        score += 2
    if (
        str(item.get("description", "")).strip()
        or str(item.get("content_snippet", "")).strip()
    ):
        score += 1
    if str(item.get("feed_title", "")).strip() or str(item.get("channel", "")).strip():
        score += 1
    return score


def _pick_better_item_index(items: list[dict], idx_a: int, idx_b: int) -> int:
    a, b = items[idx_a], items[idx_b]
    key_a = (
        _item_completeness_score(a),
        _parse_published_ts(str(a.get("published_at", ""))),
        -idx_a,
    )
    key_b = (
        _item_completeness_score(b),
        _parse_published_ts(str(b.get("published_at", ""))),
        -idx_b,
    )
    return idx_a if key_a >= key_b else idx_b


def item_key(item: dict) -> str:
    return dedup_key_for_item(item)


def dedup_key_for_item(item: dict) -> str:
    """Return the canonical cross-run identity key for an item."""
    source = str(item.get("source", "unknown")).strip().lower()

    if source == "youtube":
        video_id = str(item.get("video_id", "") or item.get("external_id", "")).strip()
        if not video_id:
            nurl = normalize_url(str(item.get("url", "")))
            match = re.search(r"(?:v=|youtu\.be/)([A-Za-z0-9_-]{6,})", nurl)
            if match:
                video_id = match.group(1)
        if video_id:
            return f"youtube::{video_id}"

    if source == "github":
        repo = (
            str(item.get("repo_full_name", "") or item.get("title", "")).strip().lower()
        )
        if repo and "/" in repo:
            return f"github::{repo}"

    nurl = normalize_url(str(item.get("url", "")))
    if nurl:
        return nurl

    title = normalize_title(str(item.get("title", "")))
    return f"{source}::{title}"


def stable_history_key(item: dict) -> str:
    """Return a stronger cross-run identity key for previously recommended items."""
    return dedup_key_for_item(item)


def short_item_line(i: int, item: dict) -> str:
    source = str(item.get("source", "unknown")).upper()
    title = str(item.get("title", "")).replace("\n", " ").strip()
    url = str(item.get("url", "")).strip()
    published = str(item.get("published_at", "")).strip()
    feed_or_channel = str(item.get("feed_title", "") or item.get("channel", "")).strip()
    parts = [f"[{i}] [{source}] {title}"]
    if url:
        parts.append(f"url={url}")
    if published:
        parts.append(f"published_at={published}")
    if feed_or_channel:
        parts.append(f"meta={feed_or_channel}")
    return " | ".join(parts)


def parse_json_dict(raw_text: str) -> dict | None:
    import json

    json_match = re.search(r"\{[\s\S]*\}", raw_text or "")
    if not json_match:
        return None
    try:
        parsed = json.loads(json_match.group())
    except Exception:
        return None
    return parsed if isinstance(parsed, dict) else None


def fallback_dedup_across_candidates(candidates: list[dict]) -> list[dict]:
    if len(candidates) <= 1:
        return candidates

    url_groups: dict[str, list[int]] = {}
    for idx, item in enumerate(candidates):
        nurl = normalize_url(str(item.get("url", "")))
        if nurl:
            url_groups.setdefault(nurl, []).append(idx)

    kept_indices: set[int] = set(range(len(candidates)))
    for group in url_groups.values():
        if len(group) <= 1:
            continue
        keep = group[0]
        for idx in group[1:]:
            keep = _pick_better_item_index(candidates, keep, idx)
        for idx in group:
            if idx != keep:
                kept_indices.discard(idx)

    ordered = sorted(kept_indices)
    deduped_indices: list[int] = []
    for idx in ordered:
        candidate = candidates[idx]
        ntitle = normalize_title(str(candidate.get("title", "")))
        merged = False
        for pos, existing_idx in enumerate(deduped_indices):
            existing_title = normalize_title(
                str(candidates[existing_idx].get("title", ""))
            )
            if not _is_strict_title_duplicate(ntitle, existing_title):
                continue
            better = _pick_better_item_index(candidates, existing_idx, idx)
            deduped_indices[pos] = better
            merged = True
            break
        if not merged:
            deduped_indices.append(idx)

    deduped_indices = sorted(set(deduped_indices))
    result = [candidates[i] for i in deduped_indices]
    logger.info(f"  跨源去重（fallback）：{len(candidates)} → {len(result)}")
    return result


def ai_dedup_across_candidates(
    candidates: list[dict],
    focus: str,
    call_kwargs: dict,
    language: str,
    backend: str = "litellm",
) -> list[dict]:
    if len(candidates) <= 1:
        return candidates

    fallback_result = fallback_dedup_across_candidates(candidates)

    lang_label = "中文" if language == "zh" else "English"
    focus_line = f"用户关注方向：{focus}\n\n" if focus else ""
    items_text = "\n".join(
        short_item_line(i, item) for i, item in enumerate(candidates)
    )

    user_message = f"""{focus_line}请对以下候选做跨源去重，目标是去掉"同一新闻/同一事件"的重复条目。

规则：
1) URL 规范化后一致，视为重复。
2) URL 不同但标题几乎一致且明显同一事件，可判为重复。
3) 对每个重复组，必须选择 1 条"最值得保留"的代表项。
4) 不要把"同主题但不同事件"的新闻误判成重复。

候选列表（{len(candidates)} 条）：
{items_text}

请仅输出 JSON：
{{
  "keep": [0, 3, 4],
  "groups": [
    {{"keep": 0, "drop": [1, 2], "reason": "same event from different sources"}}
  ]
}}
其中 keep 为最终保留的候选序号（0-based）。"""

    try:
        messages = [
            {
                "role": "system",
                "content": f"你是跨源去重助手，请用{lang_label}思考，只输出 JSON。",
            },
            {"role": "user", "content": user_message},
        ]
        raw_text = _call_ai(messages, backend, {**call_kwargs, "max_tokens": 800})
        parsed = parse_json_dict(raw_text)
        if parsed and isinstance(parsed.get("keep"), list):
            keep: list[int] = []
            seen: set[int] = set()
            for idx in parsed.get("keep", []):
                if not isinstance(idx, int) or not (0 <= idx < len(candidates)):
                    continue
                if idx in seen:
                    continue
                seen.add(idx)
                keep.append(idx)

            if len(keep) > len(fallback_result):
                logger.warning("  跨源去重 AI 结果比程序规则更宽松，回退到程序结果")
                return fallback_result

            groups = parsed.get("groups", [])
            if not keep and candidates:
                logger.warning("  跨源去重 AI 结果异常（keep 为空），改用 fallback")
                return fallback_result

            logger.info(
                f"  跨源去重（AI）：{len(candidates)} → {len(keep)} "
                f"(groups={len(groups) if isinstance(groups, list) else 0})"
            )
            return [candidates[i] for i in keep]
    except Exception as e:
        logger.warning(f"跨源去重 AI 失败，改用 fallback: {e}")

    return fallback_result
