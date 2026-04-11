from __future__ import annotations

import os
import re
import threading
from contextlib import asynccontextmanager
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from croniter import croniter
from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from src.config_loader import load_config
from src.web.runtime import (
    ScheduleAlreadyRunningError,
    enqueue_manual_deep_summary,
    enqueue_manual_run,
    run_worker_loop,
)
from src.web.store import AppStateStore

_SENSITIVE_ENV_PATTERNS = re.compile(
    r"(key|password|secret|token|webhook|api_base)", re.IGNORECASE
)


def _template_dir() -> Path:
    return Path(__file__).with_name("templates")


def _parse_datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value

    text = str(value or "").strip()
    if not text:
        return None

    normalized = text.replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(normalized)
    except ValueError:
        return None


def _to_local_datetime(value: Any, tz: ZoneInfo) -> datetime | None:
    dt = _parse_datetime(value)
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=tz)
    return dt.astimezone(tz)


def _format_datetime_local(value: Any, tz: ZoneInfo) -> str:
    dt = _to_local_datetime(value, tz)
    if dt is None:
        return "—"
    return dt.strftime("%Y-%m-%d %H:%M")


def _format_datetime_relative(
    value: Any, tz: ZoneInfo, *, now: datetime | None = None
) -> str:
    dt = _to_local_datetime(value, tz)
    if dt is None:
        return "—"

    now = (now or datetime.now(timezone.utc)).astimezone(tz)
    delta_seconds = int((now - dt).total_seconds())
    future = delta_seconds < 0
    seconds = abs(delta_seconds)

    if seconds < 60:
        return "即将发生" if future else "刚刚"

    if seconds < 3600:
        minutes = max(1, seconds // 60)
        return f"{minutes} 分钟后" if future else f"{minutes} 分钟前"

    if dt.date() == now.date():
        hours = max(1, seconds // 3600)
        if hours <= 6:
            return f"{hours} 小时后" if future else f"{hours} 小时前"
        return f"今天 {dt:%H:%M}"

    days = (dt.date() - now.date()).days
    if days == -1:
        return f"昨天 {dt:%H:%M}"
    if days == 1:
        return f"明天 {dt:%H:%M}"
    if -7 < days < 0:
        return f"{abs(days)} 天前"
    if 0 < days < 7:
        return f"{days} 天后"
    return dt.strftime("%Y-%m-%d %H:%M")


def _compute_next_runs(config: dict) -> list[dict[str, str]]:
    tz = ZoneInfo(config.get("app", {}).get("timezone", "Asia/Shanghai"))
    now = datetime.now(tz)
    items: list[dict[str, str]] = []
    for schedule in config.get("schedules", []):
        cron_expr = str(schedule.get("cron", "")).strip()
        schedule_name = str(schedule.get("name", "")).strip() or "(unnamed)"
        next_run = ""
        error = ""
        if cron_expr:
            try:
                next_run_dt = croniter(cron_expr, now).get_next(datetime)
                next_run = next_run_dt.isoformat()
            except Exception as exc:
                error = str(exc)
        items.append(
            {
                "name": schedule_name,
                "cron": cron_expr,
                "next_run": next_run,
                "error": error,
            }
        )
    return items


def _build_status(config: dict, store: AppStateStore) -> dict[str, Any]:
    return {
        "timezone": config.get("app", {}).get("timezone", "Asia/Shanghai"),
        "running_job": store.get_latest_running_job(),
        "recent_jobs": store.list_jobs(limit=8),
        "next_runs": _compute_next_runs(config),
        "latest_digest": store.get_latest_digest(),
    }


def _bool_query_flag(value: str | bool | None) -> bool:
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in {"1", "true", "on", "yes"}


def _active_filters(filters: list[tuple[str, str]]) -> list[dict[str, str]]:
    return [
        {"label": label, "value": value}
        for label, value in filters
        if str(value or "").strip()
    ]


def _update_query_params(request: Request, **kwargs: Any) -> str:
    from urllib.parse import urlencode

    params = dict(request.query_params)
    for key, value in kwargs.items():
        if value is None or str(value) == "":
            params.pop(key, None)
        else:
            params[key] = str(value)
    if not params:
        return str(request.url.path)
    return f"{request.url.path}?{urlencode(params, doseq=True)}"


def _mask_email(email: str) -> str:
    """Partially mask an email address: p***e@example.com."""
    if not email or "@" not in email:
        return email
    local, domain = email.split("@", 1)
    if len(local) <= 2:
        masked_local = "*" * len(local)
    else:
        masked_local = local[0] + "***" + local[-1]
    return f"{masked_local}@{domain}"


# Tool name -> (display label, emoji)
_TOOL_META: dict[str, tuple[str, str]] = {
    "collect_github": ("Collect GitHub", "🐙"),
    "collect_rss": ("Collect RSS", "📡"),
    "collect_youtube": ("Collect YouTube", "▶️"),
    "summarize_news": ("Summarize News", "🧠"),
    "read_today_schedule": ("Read Schedule", "📅"),
    "read_active_projects": ("Read Projects", "📋"),
    "build_digest_payload": ("Build Digest", "📦"),
    "dispatch_notifications": ("Dispatch", "📬"),
}

# Pipeline stage order for the timeline (tool_name or sentinel stage names)
_PIPELINE_STAGES = [
    ("job_started", "Boot", "🚀"),
    ("collect_github", "GitHub", "🐙"),
    ("collect_rss", "RSS", "📡"),
    ("collect_youtube", "YouTube", "▶️"),
    ("summarize_news", "Summarize", "🧠"),
    ("read_today_schedule", "Schedule", "📅"),
    ("read_active_projects", "Projects", "📋"),
    ("build_digest_payload", "Build", "📦"),
    ("dispatch_notifications", "Dispatch", "📬"),
    ("items_indexed", "Index", "🗂️"),
    ("job_finished", "Done", "✅"),
]


def _build_job_view(logs: list[dict]) -> dict[str, Any]:
    """Derive structured view data from raw job_logs for the job detail template."""
    # --- Pipeline timeline ---
    seen_stages: set[str] = set()
    for log in logs:
        et = log.get("event_type", "")
        if et == "tool_finish":
            extra = log.get("extra") or {}
            seen_stages.add(str(extra.get("tool_name", "")))
        elif et in ("job_started", "items_indexed", "job_finished"):
            seen_stages.add(et)

    timeline = []
    for stage_key, label, icon in _PIPELINE_STAGES:
        timeline.append(
            {
                "key": stage_key,
                "label": label,
                "icon": icon,
                "done": stage_key in seen_stages,
            }
        )

    # --- Tool call cards (pair tool_start + tool_finish) ---
    starts: dict[int, dict] = {}  # step_no -> log
    finishes: dict[int, dict] = {}
    reasoning_entries: list[dict] = []
    usage_entries: list[dict] = []

    for log in logs:
        et = log.get("event_type", "")
        extra = log.get("extra") or {}
        step_no = int(extra.get("step_no") or 0)
        if et == "tool_start":
            starts[step_no] = log
        elif et == "tool_finish":
            finishes[step_no] = log
        elif et == "agent_reasoning":
            reasoning_entries.append(
                {
                    "step_no": step_no,
                    "text": extra.get("text", ""),
                    "ts": log.get("ts", ""),
                }
            )
        elif et == "llm_usage":
            usage_entries.append(
                {
                    "step_no": step_no,
                    "prompt_tokens": extra.get("prompt_tokens", 0),
                    "completion_tokens": extra.get("completion_tokens", 0),
                    "total_tokens": extra.get("total_tokens", 0),
                    "ts": log.get("ts", ""),
                }
            )

    tool_cards = []
    all_steps = sorted(set(list(starts.keys()) + list(finishes.keys())))
    for step in all_steps:
        if step == 0:
            continue
        start_log = starts.get(step, {})
        finish_log = finishes.get(step, {})
        start_extra = start_log.get("extra") or {}
        finish_extra = finish_log.get("extra") or {}
        tool_name = str(
            start_extra.get("tool_name") or finish_extra.get("tool_name") or ""
        )
        label, icon = _TOOL_META.get(tool_name, (tool_name, "🔧"))
        success = finish_extra.get("success", None)
        duration_ms = finish_extra.get("duration_ms")
        result = finish_extra.get("result") or {}
        error = finish_extra.get("error") or ""
        arguments = start_extra.get("arguments") or {}

        # Find reasoning emitted just before this step
        reasoning = next(
            (r["text"] for r in reasoning_entries if r["step_no"] == step),
            None,
        )

        tool_cards.append(
            {
                "step": step,
                "tool_name": tool_name,
                "label": label,
                "icon": icon,
                "success": success,
                "duration_ms": duration_ms,
                "arguments": arguments,
                "result": result,
                "error": error,
                "ts": start_log.get("ts") or finish_log.get("ts", ""),
                "reasoning": reasoning,
            }
        )

    # --- Cumulative token totals ---
    total_tokens = sum(u["total_tokens"] for u in usage_entries)
    total_prompt = sum(u["prompt_tokens"] for u in usage_entries)
    total_completion = sum(u["completion_tokens"] for u in usage_entries)

    # --- Job summary stats extracted from tool results ---
    stats: dict[str, Any] = {}
    for card in tool_cards:
        r = card["result"]
        tn = card["tool_name"]
        if (
            tn in ("collect_github", "collect_rss", "collect_youtube")
            and card["success"]
        ):
            stats.setdefault("collected", 0)
            stats["collected"] += int(r.get("fetched_count") or 0)
        elif tn == "summarize_news" and card["success"]:
            stats["news_count"] = int(r.get("news_count") or 0)
            stats["top_titles"] = r.get("top_titles") or []
        elif tn == "dispatch_notifications" and card["success"]:
            stats["dispatched"] = bool(r.get("dispatched"))
            stats["dry_run"] = bool(r.get("dry_run"))

    # --- Lifecycle logs (non-tool events for the raw log section) ---
    lifecycle_logs = [
        log
        for log in logs
        if log.get("event_type")
        not in ("tool_start", "tool_finish", "agent_reasoning", "llm_usage")
    ]

    return {
        "timeline": timeline,
        "tool_cards": tool_cards,
        "reasoning_entries": reasoning_entries,
        "usage_entries": usage_entries,
        "total_tokens": total_tokens,
        "total_prompt_tokens": total_prompt,
        "total_completion_tokens": total_completion,
        "stats": stats,
        "lifecycle_logs": lifecycle_logs,
    }


def _mask_value(key: str, value: Any) -> Any:
    """Return a masked placeholder if the key looks sensitive, otherwise the value."""
    if isinstance(value, str) and value and _SENSITIVE_ENV_PATTERNS.search(key):
        return "configured"
    return value


def _build_config_view(config: dict) -> dict[str, Any]:
    """Build a safe, masked representation of the active config for the UI."""

    def _env_override(env_var: str, config_val: Any) -> dict[str, Any]:
        env_val = os.environ.get(env_var)
        if env_val is not None and env_val != str(config_val):
            return {
                "value": _mask_value(env_var, env_val),
                "overridden_by_env": True,
                "config_value": config_val,
            }
        return {"value": config_val, "overridden_by_env": False}

    ai_cfg = config.get("ai", {})
    agent_cfg = config.get("agent", {})
    notif_cfg = config.get("notifications", {})
    ds_cfg = {
        **{
            "auto_enabled": False,
            "score_threshold": 8,
            "max_per_run": 5,
            "timeout_per_item": 120,
            "exclude_sources": [],
        },
        **config.get("deep_summary", {}),
    }

    schedules = []
    for s in config.get("schedules", []):
        schedules.append(
            {
                "name": s.get("name", ""),
                "cron": s.get("cron", ""),
                "content": s.get("content", []),
                "sources": s.get("sources", []),
                "focus": s.get("focus", ""),
                "subject_prefix": s.get("subject_prefix", ""),
            }
        )

    email_cfg = notif_cfg.get("email", {})
    recipients_raw = str(os.environ.get("EMAIL_TO") or email_cfg.get("to", "") or "")
    recipients = [
        _mask_email(r.strip()) for r in recipients_raw.split(",") if r.strip()
    ]

    channels = {}
    for ch in ("email", "feishu", "wework", "file"):
        ch_cfg = notif_cfg.get(ch, {})
        enabled = bool(ch_cfg.get("enabled", False))
        channels[ch] = {"enabled": enabled}
        if ch == "email" and enabled:
            channels[ch]["recipients"] = recipients
            channels[ch]["smtp_server"] = email_cfg.get(
                "smtp_server"
            ) or os.environ.get("EMAIL_SMTP_SERVER", "")

    collectors_cfg = config.get("collectors", {})
    rss_sources = collectors_cfg.get("rss", {}).get("feeds", [])
    youtube_channels = collectors_cfg.get("youtube", {}).get("channel_ids", [])
    github_cfg = collectors_cfg.get("github", {})

    return {
        "schedules": schedules,
        "ai": {
            "backend": _env_override("AI_BACKEND", ai_cfg.get("backend", "litellm")),
            "model": _env_override("AI_MODEL", ai_cfg.get("model", "")),
            "api_base": _env_override("AI_API_BASE", ai_cfg.get("api_base", "")),
            "api_key": {
                "value": "configured" if os.environ.get("AI_API_KEY") else "not set",
                "overridden_by_env": bool(os.environ.get("AI_API_KEY")),
            },
            "max_tokens": ai_cfg.get("max_tokens", 2048),
            "max_workers": ai_cfg.get("max_workers", 5),
            "min_relevance_score": ai_cfg.get("min_relevance_score", 5),
            "max_items_per_digest": ai_cfg.get("max_items_per_digest", 20),
        },
        "agent": {
            "max_steps": agent_cfg.get("max_steps", 6),
            "schedule_max_steps": agent_cfg.get("schedule_max_steps", 8),
            "schedule_allow_side_effects": agent_cfg.get(
                "schedule_allow_side_effects", True
            ),
            "require_dispatch_tool_call": agent_cfg.get(
                "require_dispatch_tool_call", True
            ),
        },
        "deep_summary": ds_cfg,
        "notifications": channels,
        "sources": {
            "rss_feed_count": len(rss_sources),
            "rss_feeds": rss_sources,
            "youtube_channel_count": len(youtube_channels),
            "youtube_channels": youtube_channels,
            "github_since": github_cfg.get("trending_since", "daily"),
        },
    }


def create_app(config: dict | None = None) -> FastAPI:
    resolved_config = config or load_config()
    store = AppStateStore.from_config(resolved_config)
    store.init_db()
    store.sync_output_archives(resolved_config)

    templates = Jinja2Templates(directory=str(_template_dir()))
    app_tz = ZoneInfo(resolved_config.get("app", {}).get("timezone", "Asia/Shanghai"))
    templates.env.filters["datetime_local"] = lambda value: _format_datetime_local(
        value, app_tz
    )
    templates.env.filters["datetime_relative"] = lambda value: (
        _format_datetime_relative(value, app_tz)
    )
    deep_summary_executor = ThreadPoolExecutor(
        max_workers=2, thread_name_prefix="signalnest-deep-summary"
    )
    worker_stop_event = threading.Event()
    worker_thread = threading.Thread(
        target=run_worker_loop,
        kwargs={
            "config": resolved_config,
            "stop_event": worker_stop_event,
            "worker_id": f"web-worker-{os.getpid()}",
            "scheduler_enabled": bool(
                resolved_config.get("runtime", {}).get("embedded_scheduler", False)
            ),
        },
        name="signalnest-worker",
        daemon=True,
    )

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        try:
            worker_thread.start()
            yield
        finally:
            worker_stop_event.set()
            worker_thread.join(timeout=5)
            deep_summary_executor.shutdown(wait=False, cancel_futures=False)

    app = FastAPI(title="SignalNest Web", lifespan=lifespan)

    app.state.config = resolved_config
    app.state.store = store
    app.state.templates = templates
    app.state.deep_summary_executor = deep_summary_executor

    def render(
        request: Request, template_name: str, context: dict[str, Any]
    ) -> HTMLResponse:
        current_path = str(request.url.path)
        base_context = {
            "request": request,
            "app_name": "SignalNest",
            "current_path": current_path,
            "flash_message": request.query_params.get("message", "").strip(),
            "flash_error": request.query_params.get("error", "").strip(),
            "update_query": lambda **kwargs: _update_query_params(request, **kwargs),
        }
        base_context.update(context)
        return templates.TemplateResponse(request, template_name, base_context)

    @app.get("/", response_class=HTMLResponse)
    def dashboard(request: Request) -> HTMLResponse:
        status = _build_status(app.state.config, app.state.store)
        return render(request, "dashboard.html", status)

    @app.get("/jobs", response_class=HTMLResponse)
    def jobs_page(
        request: Request,
        status: str = "",
        trigger_type: str = "",
        schedule_name: str = "",
    ) -> HTMLResponse:
        total_jobs = app.state.store.count_jobs(
            status=status,
            trigger_type=trigger_type,
            schedule_name=schedule_name,
        )
        jobs = app.state.store.list_jobs(
            limit=100,
            status=status,
            trigger_type=trigger_type,
            schedule_name=schedule_name,
        )
        schedules = [
            str(item.get("name", "")) for item in app.state.config.get("schedules", [])
        ]
        return render(
            request,
            "jobs.html",
            {
                "jobs": jobs,
                "selected_status": status,
                "selected_trigger_type": trigger_type,
                "selected_schedule_name": schedule_name,
                "schedules": schedules,
                "total_jobs": total_jobs,
                "active_filters": _active_filters(
                    [
                        ("状态", status),
                        ("触发方式", trigger_type),
                        ("Schedule", schedule_name),
                    ]
                ),
                "clear_filters_url": "/jobs",
            },
        )

    @app.post("/jobs/run")
    def trigger_job(
        schedule_name: str = Form(...), dry_run: str = Form("false")
    ) -> RedirectResponse:
        dry_run_flag = str(dry_run).lower() in {"1", "true", "on", "yes"}
        try:
            job_run_id = enqueue_manual_run(
                config=app.state.config,
                schedule_name=schedule_name,
                dry_run=dry_run_flag,
            )
        except ScheduleAlreadyRunningError as exc:
            return RedirectResponse(
                url=f"/?error={str(exc)}",
                status_code=303,
            )
        mode_text = "dry-run" if dry_run_flag else "正式运行"
        return RedirectResponse(
            url=f"/jobs/{job_run_id}?message=已创建任务，当前为{mode_text}。",
            status_code=303,
        )

    @app.get("/jobs/{job_run_id}", response_class=HTMLResponse)
    def job_detail(request: Request, job_run_id: int) -> HTMLResponse:
        job = app.state.store.get_job(job_run_id)
        logs = app.state.store.list_job_logs(job_run_id)
        digest = app.state.store.get_digest_for_job(job_run_id)
        view = _build_job_view(logs)
        return render(
            request,
            "job_detail.html",
            {"job": job, "logs": logs, "digest": digest, "view": view},
        )

    @app.get("/digests", response_class=HTMLResponse)
    def digest_list(request: Request, schedule_name: str = "") -> HTMLResponse:
        total_digests = app.state.store.count_digests(schedule_name=schedule_name)
        digests = app.state.store.list_digests(limit=100, schedule_name=schedule_name)
        schedules = [
            str(item.get("name", "")) for item in app.state.config.get("schedules", [])
        ]
        return render(
            request,
            "digests.html",
            {
                "digests": digests,
                "selected_schedule_name": schedule_name,
                "schedules": schedules,
                "total_digests": total_digests,
                "active_filters": _active_filters([("Schedule", schedule_name)]),
                "clear_filters_url": "/digests",
            },
        )

    @app.get("/digests/latest")
    def latest_digest_redirect() -> RedirectResponse:
        digest = app.state.store.get_latest_digest()
        if digest is None:
            return RedirectResponse(url="/digests", status_code=303)
        return RedirectResponse(url=f"/digests/{digest['id']}", status_code=303)

    @app.get("/digests/{digest_id}", response_class=HTMLResponse)
    def digest_detail(request: Request, digest_id: int) -> HTMLResponse:
        digest = app.state.store.get_digest(digest_id)
        # Build url→item_id map so templates can link to /items/{id}
        url_to_item_id: dict[str, int] = {}
        if digest and digest.get("job_run_id"):
            url_to_item_id = app.state.store.get_url_to_item_id_map(
                digest["job_run_id"]
            )
        return render(
            request,
            "digest_detail.html",
            {"digest": digest, "url_to_item_id": url_to_item_id},
        )

    @app.get("/items", response_class=HTMLResponse)
    def items_page(
        request: Request,
        keyword: str = "",
        source: str = "",
        source_name: str = "",
        time_range: str = "",
        selected_only: str = "false",
        page: int = 1,
    ) -> HTMLResponse:
        selected_only_flag = _bool_query_flag(selected_only)
        available_sources = app.state.store.list_item_sources()
        available_source_names = app.state.store.list_item_source_names(source=source)
        total_items = app.state.store.count_items(
            keyword=keyword,
            source=source,
            source_name=source_name,
            time_range=time_range,
            selected_only=selected_only_flag,
        )

        page = max(1, page)
        limit = 50
        offset = (page - 1) * limit
        total_pages = max(1, (total_items + limit - 1) // limit)
        if page > total_pages:
            page = total_pages
            offset = (page - 1) * limit

        items = app.state.store.list_items(
            limit=limit,
            offset=offset,
            keyword=keyword,
            source=source,
            source_name=source_name,
            time_range=time_range,
            selected_only=selected_only_flag,
        )
        return render(
            request,
            "items.html",
            {
                "items": items,
                "selected_keyword": keyword,
                "selected_source": source,
                "selected_source_name": source_name,
                "selected_time_range": time_range,
                "selected_only": selected_only_flag,
                "available_sources": available_sources,
                "available_source_names": available_source_names,
                "total_items": total_items,
                "current_page": page,
                "total_pages": total_pages,
                "limit": limit,
                "active_filters": _active_filters(
                    [
                        ("关键词", keyword),
                        ("平台", source),
                        ("来源", source_name),
                        ("时间", time_range),
                        ("仅已入选", "是" if selected_only_flag else ""),
                    ]
                ),
                "clear_filters_url": "/items",
            },
        )

    @app.get("/items/{item_id}", response_class=HTMLResponse)
    def item_detail(request: Request, item_id: int) -> HTMLResponse:
        item = app.state.store.get_item(item_id)
        deep_summary = app.state.store.get_latest_deep_summary_for_item(item_id)
        return render(
            request,
            "item_detail.html",
            {"item": item, "deep_summary": deep_summary},
        )

    @app.post("/items/{item_id}/deep-summary")
    def trigger_deep_summary(item_id: int) -> RedirectResponse:
        if app.state.store.get_item(item_id) is None:
            raise HTTPException(status_code=404, detail="item not found")
        deep_summary_id, _future = enqueue_manual_deep_summary(
            executor=app.state.deep_summary_executor,
            config=app.state.config,
            item_id=item_id,
        )
        return RedirectResponse(
            url=f"/deep-summaries/{deep_summary_id}?message=已加入深度总结队列。",
            status_code=303,
        )

    @app.get("/deep-summaries/{deep_summary_id}", response_class=HTMLResponse)
    def deep_summary_detail(request: Request, deep_summary_id: int) -> HTMLResponse:
        deep_summary = app.state.store.get_deep_summary(deep_summary_id)
        item = None
        if deep_summary is not None:
            item = app.state.store.get_item(deep_summary["item_id"])
        return render(
            request,
            "deep_summary_detail.html",
            {"deep_summary": deep_summary, "item": item},
        )

    @app.get("/api/status")
    def api_status() -> dict[str, Any]:
        return _build_status(app.state.config, app.state.store)

    @app.get("/api/schedules")
    def api_schedules() -> list[dict[str, str]]:
        return _compute_next_runs(app.state.config)

    @app.post("/api/schedules/{schedule_name}/run")
    def api_run_schedule(schedule_name: str, dry_run: bool = False) -> dict[str, Any]:
        try:
            job_run_id = enqueue_manual_run(
                config=app.state.config, schedule_name=schedule_name, dry_run=dry_run
            )
        except ScheduleAlreadyRunningError as exc:
            raise HTTPException(status_code=409, detail=str(exc))
        job = app.state.store.get_job(job_run_id)
        return {"job_run_id": job_run_id, "job": job}

    @app.get("/api/jobs")
    def api_jobs(
        status: str = "", trigger_type: str = "", schedule_name: str = ""
    ) -> dict[str, Any]:
        return {
            "jobs": app.state.store.list_jobs(
                limit=100,
                status=status,
                trigger_type=trigger_type,
                schedule_name=schedule_name,
            )
        }

    @app.get("/api/jobs/{job_run_id}")
    def api_job_detail(job_run_id: int) -> dict[str, Any]:
        return {
            "job": app.state.store.get_job(job_run_id),
            "digest": app.state.store.get_digest_for_job(job_run_id),
        }

    @app.get("/api/jobs/{job_run_id}/logs")
    def api_job_logs(job_run_id: int) -> dict[str, Any]:
        return {"logs": app.state.store.list_job_logs(job_run_id)}

    @app.get("/api/digests/latest")
    def api_latest_digest() -> dict[str, Any]:
        return {"digest": app.state.store.get_latest_digest()}

    @app.get("/api/digests")
    def api_digests(schedule_name: str = "") -> dict[str, Any]:
        return {
            "digests": app.state.store.list_digests(
                limit=100, schedule_name=schedule_name
            )
        }

    @app.get("/api/digests/{digest_id}")
    def api_digest_detail(digest_id: int) -> dict[str, Any]:
        return {"digest": app.state.store.get_digest(digest_id)}

    @app.get("/api/items")
    def api_items(
        keyword: str = "",
        source: str = "",
        source_name: str = "",
        time_range: str = "",
        selected_only: str = "false",
        page: int = 1,
        limit: int = 200,
    ) -> dict[str, Any]:
        selected_only_flag = _bool_query_flag(selected_only)
        page = max(1, page)
        limit = min(500, max(1, limit))
        offset = (page - 1) * limit

        return {
            "items": app.state.store.list_items(
                limit=limit,
                offset=offset,
                keyword=keyword,
                source=source,
                source_name=source_name,
                time_range=time_range,
                selected_only=selected_only_flag,
            ),
            "total": app.state.store.count_items(
                keyword=keyword,
                source=source,
                source_name=source_name,
                time_range=time_range,
                selected_only=selected_only_flag,
            ),
            "page": page,
            "limit": limit,
            "available_sources": app.state.store.list_item_sources(),
            "available_source_names": app.state.store.list_item_source_names(
                source=source
            ),
        }

    @app.get("/api/items/{item_id}")
    def api_item_detail(item_id: int) -> dict[str, Any]:
        return {
            "item": app.state.store.get_item(item_id),
            "deep_summary": app.state.store.get_latest_deep_summary_for_item(item_id),
        }

    @app.post("/api/items/{item_id}/deep-summary")
    def api_trigger_deep_summary(item_id: int) -> dict[str, Any]:
        if app.state.store.get_item(item_id) is None:
            raise HTTPException(status_code=404, detail="item not found")
        deep_summary_id, _future = enqueue_manual_deep_summary(
            executor=app.state.deep_summary_executor,
            config=app.state.config,
            item_id=item_id,
        )
        return {
            "deep_summary_id": deep_summary_id,
            "deep_summary": app.state.store.get_deep_summary(deep_summary_id),
        }

    @app.get("/api/deep-summaries/{deep_summary_id}")
    def api_deep_summary_detail(deep_summary_id: int) -> dict[str, Any]:
        return {"deep_summary": app.state.store.get_deep_summary(deep_summary_id)}

    @app.get("/config", response_class=HTMLResponse)
    def config_page(request: Request) -> HTMLResponse:
        config_view = _build_config_view(app.state.config)
        return render(request, "config.html", {"config_view": config_view})

    @app.get("/api/config")
    def api_config() -> dict[str, Any]:
        return _build_config_view(app.state.config)

    return app
