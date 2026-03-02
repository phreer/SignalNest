"""
main.py - DailyRadar 主编排器
===================================
被 Docker entrypoint / supercronic 调用:
  python -m src.main --schedule-name "早间日报"
  python -m src.main --schedule-name "早间日报" --dry-run

执行流程:
  1. 加载配置 (config.yaml + .env)
  2. 找到匹配的 schedule entry
  3. 按 content 列表决定运行哪些模块
  4. 采集 → AI摘要 → 组装payload → 分发通知
"""

import argparse
import logging
import sys
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from src.config_loader import load_config

logger = logging.getLogger("dailyradar")

WEEKDAY_ZH = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]


def run(schedule_name: str, config: dict, dry_run: bool = False):
    """
    执行一次完整的日报生成和推送流程。

    Args:
        schedule_name: 匹配 config["schedules"] 中的 name 字段
        config:        AppConfig dict
        dry_run:       True 时打印预览，不发送通知
    """
    # ── 找到匹配的 schedule entry ─────────────────────────────
    schedule = next(
        (s for s in config.get("schedules", []) if s.get("name") == schedule_name),
        None,
    )
    if schedule is None:
        schedules = config.get("schedules", [])
        if schedules:
            schedule = schedules[0]
            logger.warning(f"Schedule '{schedule_name}' 未找到，使用第一个: '{schedule['name']}'")
        else:
            logger.error("config.yaml 中没有定义任何 schedules")
            sys.exit(1)

    content_blocks = schedule.get("content", ["news"])
    sources        = schedule.get("sources", ["github", "youtube", "rss"])
    subject_prefix = schedule.get("subject_prefix", "DailyRadar")
    tz = ZoneInfo(config.get("app", {}).get("timezone", "Asia/Shanghai"))
    now = datetime.now(tz)
    today = now.date()

    logger.info(f"▶ Schedule: '{schedule['name']}' | content={content_blocks} | sources={sources}")

    # ── Section 1: 个人助手 ───────────────────────────────────
    schedule_entries = None
    todos = None

    if "schedule" in content_blocks:
        from src.personal.schedule_reader import read_today_schedule
        personal_dir = config.get("_personal_dir", "/app/config/personal")
        schedule_path = str(Path(personal_dir) / "schedule.yaml")
        schedule_entries = read_today_schedule(schedule_path, today, tz)
        logger.info(f"  日程: {len(schedule_entries)} 条")

    if "todos" in content_blocks:
        from src.personal.todo_reader import read_due_todos
        personal_dir = config.get("_personal_dir", "/app/config/personal")
        todos_path = str(Path(personal_dir) / "todos.yaml")
        lookahead = config.get("storage", {}).get("todo_lookahead_days", 3)
        todos = read_due_todos(todos_path, today, lookahead)
        logger.info(f"  TODO: {len(todos)} 条需提醒")

    # ── Section 2: 新闻采集 + AI 摘要 ──────────────────────────
    news_items = []

    if "news" in content_blocks:
        raw_items = []
        collectors_cfg = config.get("collectors", {})

        if "github" in sources and collectors_cfg.get("github", {}).get("enabled", True):
            logger.info("📦 抓取 GitHub...")
            try:
                from src.collectors.github_collector import collect_github
                items = collect_github(config)
                raw_items.extend(items)
                logger.info(f"   GitHub: {len(items)} 个仓库")
            except Exception as e:
                logger.error(f"   GitHub 失败: {e}")

        if "youtube" in sources and collectors_cfg.get("youtube", {}).get("enabled", False):
            logger.info("📺 抓取 YouTube...")
            try:
                from src.collectors.youtube_collector import collect_youtube
                items = collect_youtube(config)
                raw_items.extend(items)
                logger.info(f"   YouTube: {len(items)} 个视频")
            except Exception as e:
                logger.error(f"   YouTube 失败: {e}")

        if "rss" in sources and collectors_cfg.get("rss", {}).get("enabled", True):
            logger.info("📰 抓取 RSS...")
            try:
                from src.collectors.rss_collector import collect_rss
                items = collect_rss(config)
                raw_items.extend(items)
                logger.info(f"   RSS: {len(items)} 篇文章")
            except Exception as e:
                logger.error(f"   RSS 失败: {e}")

        logger.info(f"采集完成，共 {len(raw_items)} 条原始内容")

        if raw_items:
            logger.info("🤖 Claude 摘要中...")
            from src.ai.summarizer import summarize_items
            news_items = summarize_items(raw_items, config)
            logger.info(f"   筛选后: {len(news_items)} 条")

    # ── Section 3: 组装 Payload ──────────────────────────────
    payload = {
        "schedule_name":    schedule["name"],
        "subject_prefix":   subject_prefix,
        "date":             today,
        "datetime":         now,
        "schedule_entries": schedule_entries,
        "todos":            todos,
        "news_items":       news_items,
        "content_blocks":   content_blocks,
    }

    # ── Section 4: 分发通知 ──────────────────────────────────
    if dry_run:
        _print_dry_run(payload)
    else:
        from src.notifications.dispatcher import dispatch
        dispatch(payload, config)

    logger.info("✅ 完成")


def _print_dry_run(payload: dict):
    print(f"\n{'='*60}")
    print(f"DRY RUN: {payload['subject_prefix']} | {payload['date']}")
    print(f"{'='*60}")

    entries = payload.get("schedule_entries") or []
    if entries:
        print(f"\n--- 今日日程 ({len(entries)} 条) ---")
        for e in entries:
            loc = f" @ {e['location']}" if e.get("location") else ""
            print(f"  {e['time']}  {e['title']}{loc}")

    todos = payload.get("todos") or []
    if todos:
        print(f"\n--- TODO ({len(todos)} 条) ---")
        for t in todos:
            print(f"  [{t['status']}] {t['title']} (due: {t['due']})")

    news = payload.get("news_items") or []
    if news:
        print(f"\n--- 新闻精选 ({len(news)} 条) ---")
        for item in news[:5]:
            print(f"  [{item['ai_score']}/10][{item['source']}] {item['title'][:60]}")
        if len(news) > 5:
            print(f"  ... 还有 {len(news)-5} 条")


def main():
    parser = argparse.ArgumentParser(description="DailyRadar - 个人 AI 日报服务")
    parser.add_argument(
        "--schedule-name",
        default="",
        help="要执行的调度名称（匹配 config.schedules[].name，空则用第一个）",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="打印预览，不发送通知",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    config = load_config()
    run(
        schedule_name=args.schedule_name,
        config=config,
        dry_run=args.dry_run,
    )


if __name__ == "__main__":
    main()
