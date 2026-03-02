"""
email_sender.py - SMTP 邮件发送（Jinja2 HTML 模板）
改编自 obsidian-daily-digest/mailer.py
"""

import logging
import os
import smtplib
from datetime import date, datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

from jinja2 import Environment, FileSystemLoader

logger = logging.getLogger(__name__)

TEMPLATES_DIR = Path(__file__).parent.parent / "templates"

WEEKDAY_ZH = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]


def _get_recipients(config: dict) -> list[str]:
    """收件人：优先 EMAIL_TO 环境变量，其次 config 中的 recipients。"""
    env_to = os.environ.get("EMAIL_TO", "")
    if env_to:
        return [e.strip() for e in env_to.split(",") if e.strip()]
    return config.get("notifications", {}).get("email", {}).get("recipients", [])


def send_email(payload: dict, config: dict) -> bool:
    """
    渲染 email.html 模板并通过 SMTP 发送。

    Args:
        payload: 来自 main.py 的数据字典
        config:  AppConfig dict

    Returns:
        True 表示发送成功
    """
    smtp_server = os.environ.get("EMAIL_SMTP_SERVER", "smtp.qq.com")
    smtp_port   = int(os.environ.get("EMAIL_SMTP_PORT", "465"))
    smtp_user   = os.environ.get("EMAIL_FROM", "")
    smtp_pass   = os.environ.get("EMAIL_PASSWORD", "")
    recipients  = _get_recipients(config)

    if not smtp_user or not smtp_pass:
        logger.error("邮件配置缺失: EMAIL_FROM / EMAIL_PASSWORD 未设置")
        return False

    if not recipients:
        logger.error("邮件配置缺失: EMAIL_TO 未设置")
        return False

    # ── 渲染 HTML ────────────────────────────────────────────
    env = Environment(loader=FileSystemLoader(str(TEMPLATES_DIR)), autoescape=True)
    template = env.get_template("email.html")

    today: date = payload["date"]
    context = {
        "subject_prefix": payload.get("subject_prefix", "DailyRadar"),
        "schedule_name": payload.get("schedule_name", ""),
        "date_str": today.strftime("%Y-%m-%d"),
        "weekday_str": WEEKDAY_ZH[today.weekday()],
        "schedule_entries": payload.get("schedule_entries") or [],
        "todos": payload.get("todos") or [],
        "news_items": payload.get("news_items") or [],
        "ai_model": config.get("ai", {}).get("model", "claude-sonnet-4-6"),
    }
    html_body = template.render(**context)

    # ── 组装邮件 ─────────────────────────────────────────────
    subject = f"{payload.get('subject_prefix', 'DailyRadar')} · {today.strftime('%Y-%m-%d')}"

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = smtp_user
    msg["To"] = ", ".join(recipients)
    msg.attach(MIMEText(html_body, "html", "utf-8"))

    # ── 发送 ─────────────────────────────────────────────────
    try:
        if smtp_port == 465:
            with smtplib.SMTP_SSL(smtp_server, smtp_port) as server:
                server.login(smtp_user, smtp_pass)
                server.sendmail(smtp_user, recipients, msg.as_string())
        else:
            with smtplib.SMTP(smtp_server, smtp_port) as server:
                server.ehlo()
                server.starttls()
                server.login(smtp_user, smtp_pass)
                server.sendmail(smtp_user, recipients, msg.as_string())

        logger.info(f"邮件已发送: {subject} → {recipients}")
        return True

    except smtplib.SMTPAuthenticationError:
        logger.error("SMTP 认证失败：请检查 EMAIL_FROM / EMAIL_PASSWORD")
        return False
    except Exception as e:
        logger.error(f"邮件发送失败: {e}")
        return False
