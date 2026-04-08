from __future__ import annotations

import os
import re
from contextlib import asynccontextmanager
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
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
)
from src.web.store import AppStateStore

_SENSITIVE_ENV_PATTERNS = re.compile(
    r"(key|password|secret|token|webhook|api_base)", re.IGNORECASE
)


def _template_dir() -> Path:
    return Path(__file__).with_name("templates")


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

    rss_sources = config.get("rss", {}).get("feeds", [])
    youtube_channels = config.get("youtube", {}).get("channels", [])
    github_cfg = config.get("github", {})

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
            "youtube_channel_count": len(youtube_channels),
            "github_since": github_cfg.get("since", "daily"),
        },
    }


def create_app(config: dict | None = None) -> FastAPI:
    resolved_config = config or load_config()
    store = AppStateStore.from_config(resolved_config)
    store.init_db()
    store.sync_output_archives(resolved_config)

    templates = Jinja2Templates(directory=str(_template_dir()))
    executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="signalnest-web")

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        try:
            yield
        finally:
            executor.shutdown(wait=False, cancel_futures=False)

    app = FastAPI(title="SignalNest Web", lifespan=lifespan)

    app.state.config = resolved_config
    app.state.store = store
    app.state.templates = templates
    app.state.executor = executor

    def render(
        request: Request, template_name: str, context: dict[str, Any]
    ) -> HTMLResponse:
        base_context = {
            "request": request,
            "app_name": "SignalNest",
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
            },
        )

    @app.post("/jobs/run")
    def trigger_job(
        schedule_name: str = Form(...), dry_run: str = Form("false")
    ) -> RedirectResponse:
        dry_run_flag = str(dry_run).lower() in {"1", "true", "on", "yes"}
        try:
            job_run_id, _future = enqueue_manual_run(
                executor=app.state.executor,
                config=app.state.config,
                schedule_name=schedule_name,
                dry_run=dry_run_flag,
            )
        except ScheduleAlreadyRunningError as exc:
            raise HTTPException(status_code=409, detail=str(exc))
        return RedirectResponse(url=f"/jobs/{job_run_id}", status_code=303)

    @app.get("/jobs/{job_run_id}", response_class=HTMLResponse)
    def job_detail(request: Request, job_run_id: int) -> HTMLResponse:
        job = app.state.store.get_job(job_run_id)
        logs = app.state.store.list_job_logs(job_run_id)
        digest = app.state.store.get_digest_for_job(job_run_id)
        return render(
            request,
            "job_detail.html",
            {"job": job, "logs": logs, "digest": digest},
        )

    @app.get("/digests", response_class=HTMLResponse)
    def digest_list(request: Request, schedule_name: str = "") -> HTMLResponse:
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
        return render(request, "digest_detail.html", {"digest": digest})

    @app.get("/items", response_class=HTMLResponse)
    def items_page(
        request: Request,
        keyword: str = "",
        source: str = "",
        time_range: str = "",
        selected_only: bool = False,
    ) -> HTMLResponse:
        items = app.state.store.list_items(
            limit=200,
            keyword=keyword,
            source=source,
            time_range=time_range,
            selected_only=selected_only,
        )
        return render(
            request,
            "items.html",
            {
                "items": items,
                "selected_keyword": keyword,
                "selected_source": source,
                "selected_time_range": time_range,
                "selected_only": selected_only,
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
            executor=app.state.executor,
            config=app.state.config,
            item_id=item_id,
        )
        return RedirectResponse(
            url=f"/deep-summaries/{deep_summary_id}", status_code=303
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
            job_run_id, _future = enqueue_manual_run(
                executor=app.state.executor,
                config=app.state.config,
                schedule_name=schedule_name,
                dry_run=dry_run,
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
        time_range: str = "",
        selected_only: bool = False,
    ) -> dict[str, Any]:
        return {
            "items": app.state.store.list_items(
                limit=200,
                keyword=keyword,
                source=source,
                time_range=time_range,
                selected_only=selected_only,
            )
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
            executor=app.state.executor,
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
