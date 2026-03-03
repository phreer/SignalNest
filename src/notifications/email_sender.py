"""
email_sender.py - SMTP 邮件发送（Jinja2 HTML 模板）
"""

from collections import OrderedDict
import logging
import os
import re
import smtplib
from datetime import date
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path
from typing import Any

from jinja2 import Environment, FileSystemLoader

logger = logging.getLogger(__name__)

TEMPLATES_DIR = Path(__file__).parent.parent / "templates"

WEEKDAY_ZH = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]


def _parse_recipients(raw: str) -> dict[str, str]:
    """
    解析 EMAIL_TO 字符串，返回 {email: name} 字典。
    支持两种格式：
      - 姓名:邮箱  （新格式，如 "张三:foo@example.com"）
      - 纯邮箱     （旧格式，如 "foo@example.com"，name 留空）
    """
    result: dict[str, str] = {}
    for entry in raw.split(","):
        entry = entry.strip()
        if not entry:
            continue
        if ":" in entry:
            # 只以第一个冒号分割，避免邮箱中无冒号但名字含冒号的情况
            name, _, email = entry.partition(":")
            result[email.strip()] = name.strip()
        else:
            result[entry] = ""
    return result


def _get_recipients(config: dict) -> list[str]:
    """收件人：优先 EMAIL_TO 环境变量，其次 config 中的 recipients。"""
    env_to = os.environ.get("EMAIL_TO", "")
    if env_to:
        return list(_parse_recipients(env_to).keys())
    return config.get("notifications", {}).get("email", {}).get("recipients", [])


def _get_recipient_map(config: dict) -> dict[str, str]:
    """返回 {email: name} 映射，供日志或个性化使用。"""
    env_to = os.environ.get("EMAIL_TO", "")
    if env_to:
        return _parse_recipients(env_to)
    recipients = config.get("notifications", {}).get("email", {}).get("recipients", [])
    return {r: "" for r in recipients}


def _normalize_name_for_key(name: str) -> str:
    """
    将姓名转为环境变量 key 片段：
      yy -> YY
      foo-bar -> FOO_BAR
    """
    key = re.sub(r"[^0-9A-Za-z]+", "_", name.strip().upper()).strip("_")
    return key or "RECIPIENT"


def _parse_name_set(raw: str) -> set[str]:
    result: set[str] = set()
    for part in (raw or "").split(","):
        name = part.strip().lower()
        if name:
            result.add(name)
    return result


def _build_ai_call_kwargs(config: dict) -> dict[str, Any]:
    ai_cfg = config.get("ai", {})
    model = os.environ.get("AI_MODEL") or ai_cfg.get("model", "openai/gpt-4o-mini")
    api_key = os.environ.get("AI_API_KEY", "")
    api_base = os.environ.get("AI_API_BASE") or ai_cfg.get("api_base") or None

    kwargs: dict[str, Any] = {
        "model": model,
        "api_key": api_key,
        "max_tokens": 80,
    }
    if api_base:
        kwargs["api_base"] = api_base
    return kwargs


def _generate_opening_line_with_ai(recipient_name: str, payload: dict, config: dict) -> str:
    """
    用 AI 生成收件人开场句（单句短文案）。
    """
    from src.ai.cli_backend import _call_ai

    backend = os.environ.get("AI_BACKEND") or config.get("ai", {}).get("backend", "litellm")
    call_kwargs = _build_ai_call_kwargs(config)

    if backend == "litellm" and not call_kwargs.get("api_key"):
        logger.warning("开场句 AI 生成跳过：backend=litellm 且 AI_API_KEY 未配置")
        return ""

    news_titles = [
        str(item.get("title", "")).strip()
        for item in (payload.get("news_items") or [])[:2]
        if str(item.get("title", "")).strip()
    ]
    schedule_entries = payload.get("schedule_entries") or []
    projects = payload.get("projects") or []
    schedule_hint = schedule_entries[0].get("title", "") if schedule_entries else ""
    project_hint = projects[0].get("title", "") if projects else ""

    user_message = f"""请为收件人写一句邮件开场话。

要求：
- 只输出一句中文，不要任何解释
- 控制在 20-35 个字
- 不要 Markdown，不要引号，不要编号
- 语气自然、简洁、正向

收件人：{recipient_name}
日报主题：{payload.get("schedule_name", "")}
关注方向：{payload.get("focus", "")}
今日新闻参考：{"；".join(news_titles) if news_titles else "无"}
今日日程参考：{schedule_hint or "无"}
今日项目参考：{project_hint or "无"}
"""

    try:
        messages = [
            {"role": "system", "content": "你是邮件开场文案助手。只返回一句最终文案。"},
            {"role": "user", "content": user_message},
        ]
        raw = _call_ai(messages, backend, call_kwargs)
    except Exception as e:
        logger.warning("开场句 AI 生成失败: name=%s error=%s", recipient_name, e)
        return ""

    if not raw:
        return ""

    line = raw.splitlines()[0].strip()
    line = re.sub(r"^[\-\*\u2022\d\.\)\s]+", "", line)
    line = line.strip().strip("\"'`")
    line = line.strip("“”")
    line = re.sub(r"\s+", " ", line)
    return line


def _build_opening_line_for_recipient(recipient_name: str, payload: dict, config: dict) -> str:
    """
    开场句策略：
    1) 仅对 EMAIL_OPENING_AI_NAMES 名单中的收件人生效（默认 yy）
    2) 手写优先（EMAIL_OPENING_<NAME_KEY>）
    3) 没写再调用 AI
    4) AI 失败时使用内置兜底句
    """
    name = recipient_name.strip()
    if not name:
        return ""

    enabled_names = _parse_name_set(os.environ.get("EMAIL_OPENING_AI_NAMES", "yy"))
    if name.lower() not in enabled_names:
        return ""

    manual_key = f"EMAIL_OPENING_{_normalize_name_for_key(name)}"
    manual_line = (os.environ.get(manual_key, "") or "").strip()
    if manual_line:
        return manual_line

    ai_line = _generate_opening_line_with_ai(name, payload, config)
    if ai_line:
        return ai_line

    return f"{name}，今天也加油，记得先做最重要的三件事。"


def _render_html(payload: dict, config: dict) -> str:
    env = Environment(loader=FileSystemLoader(str(TEMPLATES_DIR)), autoescape=True)
    template = env.get_template("email.html")
    today: date = payload["date"]
    return template.render(
        subject_prefix=payload.get("subject_prefix", "SignalNest"),
        schedule_name=payload.get("schedule_name", ""),
        focus=payload.get("focus", ""),
        date_str=today.strftime("%Y-%m-%d"),
        weekday_str=WEEKDAY_ZH[today.weekday()],
        opening_line=payload.get("opening_line", ""),
        schedule_entries=payload.get("schedule_entries") or [],
        projects=payload.get("projects") or [],
        news_items=payload.get("news_items") or [],
        digest_summary=payload.get("digest_summary", ""),
        ai_model=config.get("ai", {}).get("model", ""),
    )


def _news_only_payload(base_payload: dict) -> dict:
    return {**base_payload, "schedule_entries": [], "projects": []}


def _resolve_personal_file_paths(config: dict, recipient_name: str) -> dict[str, Path]:
    """
    根据收件人姓名解析专属文件路径：
    - schedule-<name>.md
    - projects-<name>.md
    """
    personal_dir = Path(config.get("_personal_dir", "/app/config/personal"))
    name = recipient_name.strip()
    return {
        "schedule": (personal_dir / f"schedule-{name}.md").resolve(),
        "projects": (personal_dir / f"projects-{name}.md").resolve(),
    }


def _should_include_block(payload: dict, block: str) -> bool:
    """
    优先遵循 payload.content_blocks；若缺失则做向后兼容推断。
    """
    content_blocks = payload.get("content_blocks")
    if isinstance(content_blocks, list):
        return block in content_blocks
    if block == "schedule":
        return payload.get("schedule_entries") is not None
    if block == "todos":
        return payload.get("projects") is not None
    return False


def _load_personal_blocks_for_recipient(
    recipient_name: str,
    payload: dict,
    config: dict,
) -> dict[str, Any]:
    """
    读取收件人专属的日程/项目文件（按 content_blocks 开关严格控制）。
    文件不存在时对应块返回空列表。
    """
    from src.personal.ai_reader import read_active_projects, read_today_schedule

    today: date = payload["date"]
    paths = _resolve_personal_file_paths(config, recipient_name)

    schedule_entries: list[dict] = []
    if _should_include_block(payload, "schedule") and paths["schedule"].exists():
        schedule_entries = read_today_schedule(str(paths["schedule"]), today, config)

    projects: list[dict] = []
    if _should_include_block(payload, "todos") and paths["projects"].exists():
        lookahead_days = config.get("storage", {}).get("todo_lookahead_days", 7)
        projects = read_active_projects(
            str(paths["projects"]),
            today,
            config,
            lookahead_days=lookahead_days,
        )

    return {
        "schedule_entries": schedule_entries,
        "projects": projects,
        "paths": paths,
    }


def _build_recipient_payload(
    base_payload: dict,
    recipient: str,
    recipient_name: str,
    smtp_user: str,
    config: dict,
) -> tuple[dict, str]:
    """
    构造每个收件人的最终 payload。
    """
    final_payload: dict
    payload_label: str

    if recipient == smtp_user:
        final_payload = base_payload
        payload_label = "完整版"
    elif not recipient_name:
        final_payload = _news_only_payload(base_payload)
        payload_label = "新闻版"
    else:
        paths = _resolve_personal_file_paths(config, recipient_name)
        try:
            blocks = _load_personal_blocks_for_recipient(recipient_name, base_payload, config)
        except Exception as e:
            logger.warning(
                "收件人专属文件解析失败，回退新闻版: name=%s email=%s schedule=%s projects=%s error=%s",
                recipient_name,
                recipient,
                paths["schedule"],
                paths["projects"],
                e,
            )
            final_payload = _news_only_payload(base_payload)
            payload_label = "新闻版(回退)"
        else:
            has_personal = bool(blocks["schedule_entries"] or blocks["projects"])
            if has_personal:
                final_payload = {
                    **base_payload,
                    "schedule_entries": blocks["schedule_entries"],
                    "projects": blocks["projects"],
                }
                payload_label = "按人定制版"
            else:
                final_payload = _news_only_payload(base_payload)
                payload_label = "新闻版"

    opening_line = _build_opening_line_for_recipient(recipient_name, final_payload, config)
    return {**final_payload, "opening_line": opening_line}, payload_label


def _smtp_send(smtp_server: str, smtp_port: int, smtp_user: str, smtp_pass: str,
               recipients: list[str], msg: MIMEMultipart) -> None:
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


def send_email(payload: dict, config: dict) -> bool:
    """
    渲染 email.html 模板并通过 SMTP 发送。

    - EMAIL_FROM 保持默认个人内容（现状优先）。
    - 其他收件人若存在专属文件（schedule-<name>.md / projects-<name>.md），
      则发送按人定制版；否则发送新闻版。

    Args:
        payload: 来自 main.py 的数据字典
        config:  AppConfig dict

    Returns:
        True 表示至少一封邮件发送成功
    """
    smtp_server = os.environ.get("EMAIL_SMTP_SERVER", "smtp.qq.com")
    smtp_port   = int(os.environ.get("EMAIL_SMTP_PORT", "465"))
    smtp_user   = os.environ.get("EMAIL_FROM", "")
    smtp_pass   = os.environ.get("EMAIL_PASSWORD", "")
    recipients  = _get_recipients(config)
    recipient_map = _get_recipient_map(config)

    if not smtp_user or not smtp_pass:
        logger.error("邮件配置缺失: EMAIL_FROM / EMAIL_PASSWORD 未设置")
        return False

    if not recipients:
        logger.error("邮件配置缺失: EMAIL_TO 未设置")
        return False

    today: date = payload["date"]
    subject = f"{payload.get('subject_prefix', 'SignalNest')} · {today.strftime('%Y-%m-%d')}"
    success = False

    def _make_msg(html: str, to: list[str]) -> MIMEMultipart:
        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"] = smtp_user
        msg["To"] = ", ".join(to)
        msg.attach(MIMEText(html, "html", "utf-8"))
        return msg

    def _fmt(addrs: list[str]) -> str:
        return ", ".join(
            f"{recipient_map.get(a, '')}<{a}>" if recipient_map.get(a) else a
            for a in addrs
        )

    # 先按收件人构造 payload 与 HTML，再按 (subject, html) 分组批量发送。
    grouped_messages: "OrderedDict[tuple[str, str], dict[str, Any]]" = OrderedDict()
    for recipient in recipients:
        recipient_name = recipient_map.get(recipient, "").strip()
        final_payload, payload_label = _build_recipient_payload(
            payload,
            recipient,
            recipient_name,
            smtp_user,
            config,
        )
        html = _render_html(final_payload, config)
        key = (subject, html)
        if key not in grouped_messages:
            grouped_messages[key] = {
                "subject": subject,
                "html": html,
                "recipients": [],
                "labels": set(),
            }
        grouped_messages[key]["recipients"].append(recipient)
        grouped_messages[key]["labels"].add(payload_label)

    try:
        for item in grouped_messages.values():
            to_addrs = item["recipients"]
            html = item["html"]
            _smtp_send(smtp_server, smtp_port, smtp_user, smtp_pass,
                       to_addrs, _make_msg(html, to_addrs))
            label = " / ".join(sorted(item["labels"]))
            logger.info(f"邮件已发送（{label}）: {subject} → {_fmt(to_addrs)}")
            success = True

    except smtplib.SMTPAuthenticationError:
        logger.error("SMTP 认证失败：请检查 EMAIL_FROM / EMAIL_PASSWORD")
        return False
    except Exception as e:
        logger.error(f"邮件发送失败: {e}")
        return False

    return success

