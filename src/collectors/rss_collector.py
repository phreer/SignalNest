"""
rss_collector.py - RSS 订阅抓取器
====================================
v2: 两阶段抓取——每 feed 先多抓（max_items_per_feed_initial）条标题，
    由 AI 批量筛选后再按 max_items_per_feed 上限进摘要阶段。
"""

from __future__ import annotations

import logging
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from typing import Optional

import feedparser
import requests

logger = logging.getLogger(__name__)

HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; SignalNestBot/1.0)"}


@dataclass
class FeedFetchDiagnostics:
    feed_url: str
    feed_title: str = ""
    http_status: int | None = None
    response_bytes: int = 0
    entry_count: int = 0
    kept_count: int = 0
    old_count: int = 0
    missing_title_count: int = 0
    missing_link_count: int = 0
    undated_count: int = 0
    malformed_date_count: int = 0
    newest_published_at: str = ""
    cutoff_at: str = ""
    parse_error: str = ""
    request_error: str = ""
    bozo: bool = False
    bozo_exception: str = ""

    @property
    def failure_reason(self) -> str:
        if self.request_error:
            return "request_error"
        if self.parse_error:
            return "parse_error"
        if self.kept_count > 0:
            return ""
        if self.entry_count == 0:
            return "empty_feed"
        if self.old_count == self.entry_count:
            return "all_entries_outside_lookback"
        if self.missing_title_count or self.missing_link_count:
            return "entries_missing_required_fields"
        return "no_eligible_entries"

    def to_dict(self) -> dict:
        data = asdict(self)
        data["failure_reason"] = self.failure_reason
        return data


def _parse_entry_date(
    entry, diagnostics: FeedFetchDiagnostics | None = None
) -> Optional[datetime]:
    for field in ("published_parsed", "updated_parsed"):
        t = getattr(entry, field, None)
        if t:
            try:
                import calendar

                ts = calendar.timegm(t)
                return datetime.fromtimestamp(ts, tz=timezone.utc)
            except Exception:
                if diagnostics is not None:
                    diagnostics.malformed_date_count += 1

    for field in ("published", "updated"):
        raw = getattr(entry, field, None)
        if raw:
            try:
                return parsedate_to_datetime(raw).astimezone(timezone.utc)
            except Exception:
                if diagnostics is not None:
                    diagnostics.malformed_date_count += 1

    return None


def _extract_content(entry) -> str:
    content_list = getattr(entry, "content", [])
    if content_list:
        raw = content_list[0].get("value", "")
        if raw:
            import re

            text = re.sub(r"<[^>]+>", "", raw)
            text = re.sub(r"\s+", " ", text).strip()
            return text[:2000]

    summary = getattr(entry, "summary", "")
    if summary:
        import re

        text = re.sub(r"<[^>]+>", "", summary)
        text = re.sub(r"\s+", " ", text).strip()
        return text[:2000]

    return ""


def _log_fetch_diagnostics(diagnostics: FeedFetchDiagnostics) -> None:
    if diagnostics.request_error or diagnostics.parse_error:
        logger.warning(
            "RSS 诊断: %s | reason=%s | status=%s | bytes=%s | entries=%s | cutoff=%s | error=%s",
            diagnostics.feed_url,
            diagnostics.failure_reason,
            diagnostics.http_status or "-",
            diagnostics.response_bytes,
            diagnostics.entry_count,
            diagnostics.cutoff_at,
            diagnostics.request_error or diagnostics.parse_error,
        )
        return

    title = diagnostics.feed_title or diagnostics.feed_url
    logger.info(
        "RSS: %s → %s 篇文章 (entries=%s, old=%s, undated=%s, missing_title=%s, missing_link=%s, newest=%s, cutoff=%s%s)",
        title,
        diagnostics.kept_count,
        diagnostics.entry_count,
        diagnostics.old_count,
        diagnostics.undated_count,
        diagnostics.missing_title_count,
        diagnostics.missing_link_count,
        diagnostics.newest_published_at or "-",
        diagnostics.cutoff_at,
        ", bozo=true" if diagnostics.bozo else "",
    )


def _fetch_feed_with_diagnostics(
    feed_url: str, days_back: int, max_items: int
) -> tuple[list[dict], FeedFetchDiagnostics]:
    items: list[dict] = []
    cutoff = datetime.now(timezone.utc) - timedelta(days=days_back)
    diagnostics = FeedFetchDiagnostics(feed_url=feed_url, cutoff_at=cutoff.isoformat())

    try:
        resp = requests.get(feed_url, headers=HEADERS, timeout=15)
        diagnostics.http_status = resp.status_code
        diagnostics.response_bytes = len(resp.content)
        resp.raise_for_status()
        feed = feedparser.parse(resp.content)
    except Exception as e:
        diagnostics.request_error = str(e)
        logger.warning(f"RSS 源拉取失败 {feed_url}: {e}")
        _log_fetch_diagnostics(diagnostics)
        return [], diagnostics

    diagnostics.feed_title = getattr(feed.feed, "title", feed_url)
    diagnostics.bozo = bool(getattr(feed, "bozo", False))
    if diagnostics.bozo:
        diagnostics.bozo_exception = str(getattr(feed, "bozo_exception", "") or "")

    count = 0
    for entry in feed.entries:
        diagnostics.entry_count += 1
        if count >= max_items:
            break

        pub_date = _parse_entry_date(entry, diagnostics)
        if pub_date:
            if (
                not diagnostics.newest_published_at
                or pub_date.isoformat() > diagnostics.newest_published_at
            ):
                diagnostics.newest_published_at = pub_date.isoformat()
        else:
            diagnostics.undated_count += 1

        if pub_date and pub_date < cutoff:
            diagnostics.old_count += 1
            continue

        title = getattr(entry, "title", "").strip()
        url = getattr(entry, "link", "").strip()
        if not title:
            diagnostics.missing_title_count += 1
            continue
        if not url:
            diagnostics.missing_link_count += 1
            continue

        content_snippet = _extract_content(entry)
        items.append(
            {
                "title": title,
                "url": url,
                "description": content_snippet[:500],
                "content_snippet": content_snippet,
                "published_at": pub_date.isoformat() if pub_date else "",
                "feed_title": diagnostics.feed_title,
                "source": "rss",
            }
        )
        count += 1

    diagnostics.kept_count = len(items)
    _log_fetch_diagnostics(diagnostics)
    return items, diagnostics


def _fetch_feed(feed_url: str, days_back: int, max_items: int) -> list[dict]:
    items, _ = _fetch_feed_with_diagnostics(feed_url, days_back, max_items)
    return items


def collect_rss(
    config: dict,
    max_total: Optional[int] = None,
    *,
    return_diagnostics: bool = False,
) -> list[dict] | tuple[list[dict], list[dict]]:
    """
    抓取所有配置的 RSS 源。

    每个 feed 先抓 max_items_per_feed_initial 条（默认 20）用于 AI 标题批量筛选；
    筛选后由 summarizer 按 max_items_per_feed（默认 3）做每 feed 上限。

    Args:
        config: AppConfig dict（来自 config_loader.load_config()）
    """
    rss_cfg = config.get("collectors", {}).get("rss", {})
    if not rss_cfg.get("enabled", True):
        return ([], []) if return_diagnostics else []

    days_back = rss_cfg.get("days_lookback", 2)
    max_per_feed_initial = rss_cfg.get("max_items_per_feed_initial", 20)
    feeds = rss_cfg.get("feeds", [])

    collected: list[dict] = []
    diagnostics_list: list[dict] = []
    seen_urls: set[str] = set()

    for feed in feeds:
        feed_url = feed.get("url", "") if isinstance(feed, dict) else feed
        if not feed_url:
            continue
        per_feed_initial = (
            feed.get("max_items_initial", max_per_feed_initial)
            if isinstance(feed, dict)
            else max_per_feed_initial
        )
        items, diagnostics = _fetch_feed_with_diagnostics(
            feed_url, days_back, per_feed_initial
        )
        if isinstance(feed, dict):
            configured_name = str(feed.get("name", "") or "")
            feed_id = str(feed.get("id", "") or "")
            record = diagnostics.to_dict()
            if configured_name:
                record["configured_name"] = configured_name
            if feed_id:
                record["feed_id"] = feed_id
            diagnostics_list.append(record)
        else:
            diagnostics_list.append(diagnostics.to_dict())

        for item in items:
            if item["url"] not in seen_urls:
                seen_urls.add(item["url"])
                collected.append(item)

    if max_total:
        collected = collected[:max_total]

    logger.info(
        "RSS: 共收集 %s 篇文章（每 feed 最多 %s 条标题入池）",
        len(collected),
        max_per_feed_initial,
    )
    if return_diagnostics:
        return collected, diagnostics_list
    return collected
