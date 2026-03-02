"""
youtube_collector.py - YouTube 内容抓取器
==========================================
改编自 obsidian-daily-digest/collectors/youtube_collector.py
主要改动：用传入的 config dict 替换 import config 模块
"""

import os
import logging
import requests
from datetime import datetime, timedelta, timezone
from typing import Optional

from youtube_transcript_api import YouTubeTranscriptApi, NoTranscriptFound, TranscriptsDisabled

logger = logging.getLogger(__name__)

YT_API_BASE = "https://www.googleapis.com/youtube/v3"
VIDEO_URL = "https://www.youtube.com/watch?v={video_id}"


class YouTubeClient:
    def __init__(self, api_key: str):
        self.api_key = api_key
        self.session = requests.Session()

    def get(self, endpoint: str, **params) -> dict:
        params["key"] = self.api_key
        resp = self.session.get(f"{YT_API_BASE}/{endpoint}", params=params, timeout=15)
        resp.raise_for_status()
        return resp.json()

    def search(self, **params) -> dict:
        return self.get("search", **params)

    def channels(self, **params) -> dict:
        return self.get("channels", **params)

    def playlist_items(self, **params) -> dict:
        return self.get("playlistItems", **params)


def _iso_cutoff(days_back: int) -> str:
    cutoff = datetime.now(timezone.utc) - timedelta(days=days_back)
    return cutoff.strftime("%Y-%m-%dT%H:%M:%SZ")


def _get_transcript(video_id: str, max_chars: int = 2000) -> str:
    try:
        transcript_list = YouTubeTranscriptApi.list_transcripts(video_id)

        for lang in ["zh", "zh-Hans", "zh-CN", "en"]:
            try:
                transcript = transcript_list.find_transcript([lang])
                entries = transcript.fetch()
                text = " ".join(e["text"] for e in entries)
                return text[:max_chars]
            except Exception:
                continue

        try:
            transcript = transcript_list.find_generated_transcript(["en", "zh"])
            entries = transcript.fetch()
            text = " ".join(e["text"] for e in entries)
            return text[:max_chars]
        except Exception:
            pass

    except (NoTranscriptFound, TranscriptsDisabled):
        pass
    except Exception as e:
        logger.debug(f"获取字幕失败 video_id={video_id}: {e}")

    return ""


def _search_by_keyword(yt: YouTubeClient, keyword: str, days_back: int, max_results: int) -> list[dict]:
    items = []
    try:
        response = yt.search(
            q=keyword,
            part="snippet",
            type="video",
            order="date",
            publishedAfter=_iso_cutoff(days_back),
            maxResults=max_results,
        )
        for item in response.get("items", []):
            snippet = item.get("snippet", {})
            video_id = item.get("id", {}).get("videoId", "")
            if not video_id:
                continue
            transcript = _get_transcript(video_id)
            parsed = {
                "title": snippet.get("title", ""),
                "url": VIDEO_URL.format(video_id=video_id),
                "description": snippet.get("description", "")[:300],
                "channel": snippet.get("channelTitle", ""),
                "published_at": snippet.get("publishedAt", ""),
                "transcript_snippet": transcript,
                "source": "youtube",
            }
            if parsed["title"]:
                items.append(parsed)

    except requests.HTTPError as e:
        logger.error(f"YouTube 关键词搜索失败 keyword={keyword}: {e}")
    except Exception as e:
        logger.error(f"YouTube 关键词搜索异常 keyword={keyword}: {e}")

    return items


def _fetch_channel_videos(yt: YouTubeClient, channel_id: str, days_back: int, max_results: int) -> list[dict]:
    items = []
    try:
        ch_response = yt.channels(id=channel_id, part="contentDetails")
        channels = ch_response.get("items", [])
        if not channels:
            logger.warning(f"频道 {channel_id} 不存在或无权限")
            return []

        uploads_playlist_id = channels[0]["contentDetails"]["relatedPlaylists"]["uploads"]
        cutoff = datetime.now(timezone.utc) - timedelta(days=days_back)
        pl_response = yt.playlist_items(
            playlistId=uploads_playlist_id,
            part="snippet",
            maxResults=max_results,
        )

        for item in pl_response.get("items", []):
            snippet = item.get("snippet", {})
            published = snippet.get("publishedAt", "")
            if published:
                pub_dt = datetime.fromisoformat(published.replace("Z", "+00:00"))
                if pub_dt < cutoff:
                    continue

            video_id = snippet.get("resourceId", {}).get("videoId", "")
            transcript = _get_transcript(video_id) if video_id else ""

            parsed = {
                "title": snippet.get("title", ""),
                "url": VIDEO_URL.format(video_id=video_id),
                "description": snippet.get("description", "")[:300],
                "channel": snippet.get("channelTitle", ""),
                "published_at": published,
                "transcript_snippet": transcript,
                "source": "youtube",
            }
            if parsed["title"]:
                items.append(parsed)

    except requests.HTTPError as e:
        logger.error(f"频道视频获取失败 channel_id={channel_id}: {e}")
    except Exception as e:
        logger.error(f"频道视频获取异常 channel_id={channel_id}: {e}")

    return items


def collect_youtube(config: dict, max_total: Optional[int] = None) -> list[dict]:
    """
    综合抓取：订阅频道 + 关键词搜索。

    Args:
        config: AppConfig dict（来自 config_loader.load_config()）
    """
    yt_cfg = config.get("collectors", {}).get("youtube", {})
    if not yt_cfg.get("enabled", True):
        return []

    api_key = os.environ.get("YOUTUBE_API_KEY", "")
    if not api_key:
        logger.warning("YOUTUBE_API_KEY 未配置，跳过 YouTube 抓取")
        return []

    yt = YouTubeClient(api_key)
    max_per_kw = yt_cfg.get("max_results_per_keyword", 2)
    days_back = yt_cfg.get("days_lookback", 14)
    channel_ids = yt_cfg.get("channel_ids", [])
    keywords = yt_cfg.get("keywords", [])

    collected: list[dict] = []
    seen_urls: set[str] = set()

    for channel_id in channel_ids:
        videos = _fetch_channel_videos(yt, channel_id, days_back, max_results=5)
        for v in videos:
            if v["url"] not in seen_urls:
                seen_urls.add(v["url"])
                collected.append(v)

    for keyword in keywords:
        videos = _search_by_keyword(yt, keyword, days_back, max_per_kw)
        for v in videos:
            if v["url"] not in seen_urls:
                seen_urls.add(v["url"])
                collected.append(v)

    if max_total:
        collected = collected[:max_total]

    logger.info(f"YouTube: 共收集 {len(collected)} 个视频")
    return collected
