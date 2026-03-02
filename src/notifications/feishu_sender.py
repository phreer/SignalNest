"""
feishu_sender.py - 飞书群机器人 Webhook 推送
改编自 TrendRadar/trendradar/notification/senders.py
"""

import logging
import os
import requests

logger = logging.getLogger(__name__)

MAX_TEXT_BYTES = 28000  # 飞书单条消息上限约 30KB，留余量

WEEKDAY_ZH = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]
STATUS_LABELS = {"overdue": "⚠ 逾期", "today": "★ 今日", "upcoming": "○ 即将"}


def _build_text(payload: dict) -> str:
    today = payload["date"]
    date_str = today.strftime("%Y-%m-%d")
    weekday = WEEKDAY_ZH[today.weekday()]
    subject = payload.get("subject_prefix", "DailyRadar")

    lines = [f"【{subject}】{date_str} ({weekday})", ""]

    # ── 日程 ──────────────────────────────────────────────────
    schedule = payload.get("schedule_entries") or []
    if schedule:
        lines.append("━━━━ 今日日程 ━━━━")
        for e in schedule:
            loc = f" @ {e['location']}" if e.get("location") else ""
            notes = f"（{e['notes']}）" if e.get("notes") else ""
            lines.append(f"{e['time']}  {e['title']}{loc}{notes}")
        lines.append("")

    # ── TODO ──────────────────────────────────────────────────
    todos = payload.get("todos") or []
    if todos:
        lines.append("━━━━ 待办提醒 ━━━━")
        for t in todos:
            label = STATUS_LABELS.get(t["status"], "")
            days = t.get("days_until", 0)
            if t["status"] == "overdue":
                date_info = f"（逾期 {abs(days)} 天，{t['due']}）"
            elif t["status"] == "today":
                date_info = "（今日截止）"
            else:
                date_info = f"（{t['due']}，还有 {days} 天）"
            lines.append(f"{label}：{t['title']}{date_info}")
        lines.append("")

    # ── 新闻 ──────────────────────────────────────────────────
    news = payload.get("news_items") or []
    if news:
        lines.append(f"━━━━ 今日精选 ({len(news)} 条) ━━━━")
        for item in news:
            score = item.get("ai_score", "?")
            source = item.get("source", "").upper()
            lines.append(f"[{score}/10][{source}] {item['title']}")
            if item.get("ai_summary"):
                lines.append(f"  {item['ai_summary']}")
            lines.append(f"  {item['url']}")
            lines.append("")

    lines.append("━━━━━━━━━━━━━━━━━━")
    lines.append(f"DailyRadar · {payload.get('schedule_name', '')}")
    return "\n".join(lines)


def send_feishu(payload: dict, config: dict) -> bool:
    """发送飞书文本消息，超长时分条发送。"""
    webhook_url = os.environ.get("FEISHU_WEBHOOK_URL", "")
    if not webhook_url:
        logger.error("FEISHU_WEBHOOK_URL 未配置")
        return False

    text = _build_text(payload)

    # 按换行符拆分成不超过 MAX_TEXT_BYTES 字节的片段
    chunks = _split_text(text, MAX_TEXT_BYTES)
    success = True

    for i, chunk in enumerate(chunks):
        data = {"msg_type": "text", "content": {"text": chunk}}
        try:
            resp = requests.post(webhook_url, json=data, timeout=10)
            resp.raise_for_status()
            result = resp.json()
            if result.get("code", 0) != 0:
                logger.error(f"飞书 Webhook 返回错误: {result}")
                success = False
            else:
                logger.info(f"飞书消息已发送 ({i+1}/{len(chunks)})")
        except Exception as e:
            logger.error(f"飞书发送失败: {e}")
            success = False

    return success


def _split_text(text: str, max_bytes: int) -> list[str]:
    """将文本按最大字节数分割，在换行处断开。"""
    if len(text.encode("utf-8")) <= max_bytes:
        return [text]

    chunks = []
    current_lines = []
    current_size = 0

    for line in text.split("\n"):
        line_bytes = len((line + "\n").encode("utf-8"))
        if current_size + line_bytes > max_bytes and current_lines:
            chunks.append("\n".join(current_lines))
            current_lines = [line]
            current_size = line_bytes
        else:
            current_lines.append(line)
            current_size += line_bytes

    if current_lines:
        chunks.append("\n".join(current_lines))

    return chunks
