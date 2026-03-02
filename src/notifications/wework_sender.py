"""
wework_sender.py - 企业微信群机器人 Webhook 推送
改编自 TrendRadar/trendradar/notification/senders.py
"""

import logging
import os
import requests

logger = logging.getLogger(__name__)

MAX_MARKDOWN_BYTES = 4000  # 企业微信 Markdown 上限 4096 字节，留余量

WEEKDAY_ZH = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]
STATUS_LABELS = {"overdue": "⚠ 逾期", "today": "★ 今日", "upcoming": "○ 即将"}


def _build_markdown(payload: dict) -> str:
    today = payload["date"]
    date_str = today.strftime("%Y-%m-%d")
    weekday = WEEKDAY_ZH[today.weekday()]
    subject = payload.get("subject_prefix", "DailyRadar")

    lines = [f"**【{subject}】** {date_str} ({weekday})", ""]

    # ── 日程 ──────────────────────────────────────────────────
    schedule = payload.get("schedule_entries") or []
    if schedule:
        lines.append("**📅 今日日程**")
        for e in schedule:
            loc = f" `{e['location']}`" if e.get("location") else ""
            notes = f" — {e['notes']}" if e.get("notes") else ""
            lines.append(f"> **{e['time']}** {e['title']}{loc}{notes}")
        lines.append("")

    # ── TODO ──────────────────────────────────────────────────
    todos = payload.get("todos") or []
    if todos:
        lines.append("**✅ 待办提醒**")
        for t in todos:
            label = STATUS_LABELS.get(t["status"], "")
            days = t.get("days_until", 0)
            if t["status"] == "overdue":
                date_info = f"逾期 {abs(days)} 天（{t['due']}）"
            elif t["status"] == "today":
                date_info = "今日截止"
            else:
                date_info = f"{t['due']}，还有 {days} 天"
            lines.append(f"> {label}：**{t['title']}** — {date_info}")
        lines.append("")

    # ── 新闻（按条目可分片）──────────────────────────────────
    news = payload.get("news_items") or []
    if news:
        lines.append(f"**📰 今日精选（{len(news)} 条）**")
        lines.append("")
        for item in news:
            score = item.get("ai_score", "?")
            source = item.get("source", "").upper()
            lines.append(f"**[{score}/10] {source}** · `{item['title'][:50]}`")
            if item.get("ai_summary"):
                lines.append(f"> {item['ai_summary']}")
            lines.append(f"> [查看详情]({item['url']})")
            lines.append("")

    lines.append(f"*DailyRadar · {payload.get('schedule_name', '')}*")
    return "\n".join(lines)


def send_wework(payload: dict, config: dict) -> bool:
    """发送企业微信 Markdown 消息，超出 4KB 时分条发送。"""
    webhook_url = os.environ.get("WEWORK_WEBHOOK_URL", "")
    if not webhook_url:
        logger.error("WEWORK_WEBHOOK_URL 未配置")
        return False

    msg_type = os.environ.get("WEWORK_MSG_TYPE",
                              config.get("notifications", {}).get("wework", {}).get("msg_type", "markdown"))

    full_text = _build_markdown(payload)
    chunks = _split_markdown(payload, MAX_MARKDOWN_BYTES)
    success = True

    for i, chunk in enumerate(chunks):
        if msg_type == "markdown":
            data = {"msgtype": "markdown", "markdown": {"content": chunk}}
        else:
            data = {"msgtype": "text", "text": {"content": chunk}}

        try:
            resp = requests.post(webhook_url, json=data, timeout=10)
            resp.raise_for_status()
            result = resp.json()
            if result.get("errcode", 0) != 0:
                logger.error(f"企业微信 Webhook 返回错误: {result}")
                success = False
            else:
                logger.info(f"企业微信消息已发送 ({i+1}/{len(chunks)})")
        except Exception as e:
            logger.error(f"企业微信发送失败: {e}")
            success = False

    return success


def _split_markdown(payload: dict, max_bytes: int) -> list[str]:
    """
    将完整 Markdown 按新闻条目边界拆分，确保每片不超过 max_bytes。
    非新闻部分（日程、TODO、header）合为第一片。
    """
    today = payload["date"]
    date_str = today.strftime("%Y-%m-%d")
    weekday = WEEKDAY_ZH[today.weekday()]
    subject = payload.get("subject_prefix", "DailyRadar")

    # 构造 header + 日程 + TODO 部分
    header_lines = [f"**【{subject}】** {date_str} ({weekday})", ""]

    schedule = payload.get("schedule_entries") or []
    if schedule:
        header_lines.append("**📅 今日日程**")
        for e in schedule:
            loc = f" `{e['location']}`" if e.get("location") else ""
            notes = f" — {e['notes']}" if e.get("notes") else ""
            header_lines.append(f"> **{e['time']}** {e['title']}{loc}{notes}")
        header_lines.append("")

    todos = payload.get("todos") or []
    if todos:
        header_lines.append("**✅ 待办提醒**")
        for t in todos:
            label = STATUS_LABELS.get(t["status"], "")
            days = t.get("days_until", 0)
            if t["status"] == "overdue":
                date_info = f"逾期 {abs(days)} 天（{t['due']}）"
            elif t["status"] == "today":
                date_info = "今日截止"
            else:
                date_info = f"{t['due']}，还有 {days} 天"
            header_lines.append(f"> {label}：**{t['title']}** — {date_info}")
        header_lines.append("")

    header_text = "\n".join(header_lines)
    news_items = payload.get("news_items") or []

    if not news_items:
        return [header_text]

    # 按条目贪心分片
    chunks = []
    current = header_text + f"\n**📰 今日精选（{len(news_items)} 条）**\n\n"

    for item in news_items:
        score = item.get("ai_score", "?")
        source = item.get("source", "").upper()
        item_text = f"**[{score}/10] {source}** · `{item['title'][:50]}`\n"
        if item.get("ai_summary"):
            item_text += f"> {item['ai_summary']}\n"
        item_text += f"> [查看详情]({item['url']})\n\n"

        if len((current + item_text).encode("utf-8")) > max_bytes:
            if current.strip():
                chunks.append(current.rstrip())
            current = item_text
        else:
            current += item_text

    if current.strip():
        current += f"\n*DailyRadar · {payload.get('schedule_name', '')}*"
        chunks.append(current.rstrip())

    return chunks if chunks else [header_text]
