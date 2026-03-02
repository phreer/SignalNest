"""
github_collector.py - GitHub 热门内容抓取器
============================================
改编自 obsidian-daily-digest/collectors/github_collector.py
主要改动：用传入的 config dict 替换 import config 模块
"""

import os
import time
import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

from github import Github, GithubException

logger = logging.getLogger(__name__)


def _safe_get_readme(repo) -> str:
    try:
        readme = repo.get_readme()
        content = readme.decoded_content.decode("utf-8", errors="replace")
        import re
        content = re.sub(r"<[^>]+>", "", content)
        return content[:1500].strip()
    except Exception:
        return repo.description or ""


def collect_github(config: dict, max_repos: Optional[int] = None) -> list[dict]:
    """
    按配置的 Topics 搜索热门仓库。

    Args:
        config: AppConfig dict（来自 config_loader.load_config()）
    """
    gh_cfg = config.get("collectors", {}).get("github", {})
    if not gh_cfg.get("enabled", True):
        return []

    github_token = os.environ.get("GITHUB_TOKEN", "")
    if not github_token:
        logger.warning("GITHUB_TOKEN 未配置，使用未认证模式（速率限制较严）")
        gh = Github()
    else:
        gh = Github(github_token)

    max_repos = max_repos or gh_cfg.get("max_repos", 12)
    days_back = gh_cfg.get("days_lookback", 7)
    min_stars = gh_cfg.get("min_stars", 50)
    topics = gh_cfg.get("topics", [])
    cutoff = datetime.now(timezone.utc) - timedelta(days=days_back)

    collected: list[dict] = []
    seen_ids: set[int] = set()

    for topic in topics:
        if len(collected) >= max_repos:
            break
        try:
            query = f"topic:{topic} stars:>={min_stars} pushed:>={cutoff.strftime('%Y-%m-%d')}"
            results = gh.search_repositories(query=query, sort="stars", order="desc")

            count = 0
            for repo in results:
                if len(collected) >= max_repos:
                    break
                if repo.id in seen_ids:
                    continue
                seen_ids.add(repo.id)

                readme_snippet = _safe_get_readme(repo)
                item = {
                    "title": repo.full_name,
                    "url": repo.html_url,
                    "description": repo.description or "",
                    "readme_snippet": readme_snippet,
                    "stars": repo.stargazers_count,
                    "language": repo.language,
                    "topics": repo.get_topics(),
                    "pushed_at": repo.pushed_at.isoformat() if repo.pushed_at else "",
                    "source": "github",
                }
                collected.append(item)
                count += 1

                if count % 5 == 0:
                    time.sleep(1)

        except GithubException as e:
            logger.error(f"GitHub 搜索 topic={topic} 失败: {e}")
        except Exception as e:
            logger.error(f"未知错误 topic={topic}: {e}")

    logger.info(f"GitHub: 共收集 {len(collected)} 个仓库")
    return collected
