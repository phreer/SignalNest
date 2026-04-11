from __future__ import annotations

import os
import re
from typing import Any

import requests
import trafilatura
from bs4 import BeautifulSoup

from src.ai.cli_backend import _call_ai


def _strip_html(html: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()
    text = soup.get_text(" ", strip=True)
    return re.sub(r"\s+", " ", text).strip()


def _extract_web_article(url: str) -> tuple[str, dict[str, Any]]:
    downloaded = trafilatura.fetch_url(url)
    if downloaded:
        extracted = trafilatura.extract(
            downloaded,
            include_comments=False,
            include_tables=False,
            favor_precision=True,
        )
        if extracted:
            return extracted[:12000], {"extractor": "trafilatura", "url": url}

    response = requests.get(
        url, timeout=15, headers={"User-Agent": "SignalNestBot/1.0"}
    )
    response.raise_for_status()
    text = _strip_html(response.text)[:12000]
    return text, {"extractor": "bs4-fallback", "url": url}


def fetch_original_content(
    item: dict[str, Any], config: dict
) -> tuple[str, dict[str, Any]]:
    source = str(item.get("source", "")).strip().lower()
    raw = item.get("raw") if isinstance(item.get("raw"), dict) else {}

    if source == "rss":
        url = str(item.get("url", "")).strip()
        if not url:
            return "", {"source": source, "status": "missing_url"}
        text, meta = _extract_web_article(url)
        if not text:
            text = str(raw.get("content_snippet") or raw.get("description") or "")
            meta = {"extractor": "item-fallback", "url": url}
        return text, {"source": source, "status": "ok", **meta}

    if source == "youtube":
        from src.collectors.youtube_collector import _get_transcript

        video_id = str(raw.get("video_id") or item.get("external_id") or "").strip()
        if not video_id:
            url = str(item.get("url", ""))
            if "v=" in url:
                video_id = url.split("v=")[-1].split("&")[0]
        transcript = _get_transcript(video_id, max_chars=12000) if video_id else ""
        if transcript:
            return transcript, {
                "source": source,
                "status": "ok",
                "video_id": video_id,
                "kind": "transcript",
            }
        fallback = str(raw.get("description") or "")
        return fallback, {
            "source": source,
            "status": "ok",
            "video_id": video_id,
            "kind": "description",
        }

    if source == "github":
        url = str(item.get("url", "")).strip()
        match = re.match(r"https://github\.com/([^/]+)/([^/]+?)/?$", url)
        if not match:
            return str(raw.get("description") or ""), {
                "source": source,
                "status": "missing_repo_path",
            }
        owner, repo = match.group(1), match.group(2)
        readme_url = f"https://raw.githubusercontent.com/{owner}/{repo}/HEAD/README.md"
        response = requests.get(
            readme_url, timeout=15, headers={"User-Agent": "SignalNestBot/1.0"}
        )
        if response.ok and response.text.strip():
            return response.text[:12000], {
                "source": source,
                "status": "ok",
                "kind": "readme",
                "url": readme_url,
            }
        fallback = "\n".join(
            part
            for part in [
                str(raw.get("description") or ""),
                str(raw.get("stars_gained") or ""),
            ]
            if part
        )
        return fallback, {
            "source": source,
            "status": "ok",
            "kind": "fallback_metadata",
            "url": url,
        }

    return "", {"source": source, "status": "unsupported"}


def generate_deep_summary(
    item: dict[str, Any], source_content: str, config: dict
) -> tuple[str, str]:
    ai_cfg = config.get("ai", {})
    backend = os.environ.get("AI_BACKEND") or ai_cfg.get("backend", "litellm")
    model = os.environ.get("AI_MODEL") or ai_cfg.get("model", "openai/gpt-4o-mini")
    api_key = os.environ.get("AI_API_KEY", "")
    api_base = os.environ.get("AI_API_BASE") or ai_cfg.get("api_base") or None
    call_kwargs: dict[str, Any] = {
        "model": model,
        "api_key": api_key,
        "max_tokens": int(ai_cfg.get("max_tokens", 1200)),
    }
    if api_base:
        call_kwargs["api_base"] = api_base

    prompt = (
        "你是 SignalNest 的深度摘要助手。请基于原始内容写一份更详细、更有深度的中文摘要。\n\n"
        "要求：\n"
        "- 先用 3-5 条要点总结核心信息\n"
        "- 再写 2-4 段深入分析\n"
        "- 明确说明这条内容为什么值得持续关注\n"
        "- 不要输出 Markdown 标题，只使用普通段落和项目符号\n\n"
        f"条目标题：{item.get('title', '')}\n"
        f"来源：{item.get('source', '')}\n"
        f"链接：{item.get('url', '')}\n"
        f"已有摘要：{item.get('ai_summary', '')}\n\n"
        f"原始内容：\n{source_content[:10000]}"
    )
    messages = [
        {"role": "system", "content": "你是擅长科技、金融和研究信息分析的中文编辑。"},
        {"role": "user", "content": prompt},
    ]
    return _call_ai(messages, backend, call_kwargs), model


def build_indexed_items(
    *,
    raw_items: list[dict[str, Any]],
    news_items: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    selected_by_key: dict[tuple[str, str], dict[str, Any]] = {}
    for item in news_items:
        key = (
            str(item.get("source", "")).strip().lower(),
            str(item.get("url", "")).strip(),
        )
        selected_by_key[key] = item

    indexed: list[dict[str, Any]] = []
    for raw in raw_items:
        source = str(raw.get("source", "")).strip().lower()
        url = str(raw.get("url", "")).strip()
        selected = selected_by_key.get((source, url))
        external_id = ""
        if source == "youtube":
            external_id = str(raw.get("video_id", ""))
        elif source == "github":
            external_id = url.rstrip("/").split("github.com/")[-1] if url else ""

        merged_raw = dict(raw)
        if selected:
            merged_raw.update(selected)

        indexed.append(
            {
                "source": source,
                "external_id": external_id,
                "title": str(raw.get("title", "")),
                "translated_title": str(
                    raw.get("translated_title")
                    or merged_raw.get("translated_title")
                    or ""
                ),
                "url": url,
                "author": str(raw.get("channel") or ""),
                "feed_title": str(raw.get("feed_title") or ""),
                "language": str(raw.get("language") or ""),
                "published_at": str(raw.get("published_at") or ""),
                "selected_for_digest": bool(selected),
                "ai_score": selected.get("ai_score") if selected else None,
                "ai_summary": str(selected.get("ai_summary") or "") if selected else "",
                "ai_reason": str(selected.get("ai_reason") or "") if selected else "",
                "raw": merged_raw,
            }
        )
    return indexed
