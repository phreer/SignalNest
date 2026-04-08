from __future__ import annotations

import logging
from concurrent.futures import Future, ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from src.agent.session_store import AgentSessionStore
from src.web.content import (
    build_indexed_items,
    fetch_original_content,
    generate_deep_summary,
)
from src.web.store import AppStateStore

logger = logging.getLogger(__name__)


def _execute_schedule(
    schedule_name: str,
    config: dict,
    *,
    dry_run: bool,
    progress_callback,
) -> dict[str, Any]:
    from src.main import run_schedule

    return run_schedule(
        schedule_name=schedule_name,
        config=config,
        dry_run=dry_run,
        progress_callback=progress_callback,
    )


def _resolve_schedule(schedule_name: str, config: dict) -> dict[str, Any]:
    schedules = config.get("schedules", [])
    schedule = next(
        (item for item in schedules if item.get("name") == schedule_name), None
    )
    if schedule is not None:
        return schedule
    if schedules:
        return schedules[0]
    return {}


def _load_agent_state(config: dict, session_id: str) -> dict[str, Any]:
    if not session_id:
        return {}
    data_dir = Path(config.get("storage", {}).get("data_dir", "data"))
    session_store = AgentSessionStore(data_dir / "agent_sessions.db")
    return session_store.load_state(session_id)


def _build_payload_from_state(
    schedule_name: str, state: dict[str, Any], config: dict
) -> dict[str, Any] | None:
    payload = state.get("payload")
    if isinstance(payload, dict) and payload:
        return payload

    news_items = state.get("news_items")
    digest_summary = state.get("digest_summary")
    if not isinstance(news_items, list) or not news_items:
        return None

    schedule = _resolve_schedule(schedule_name, config)
    tz = ZoneInfo(config.get("app", {}).get("timezone", "Asia/Shanghai"))
    now = datetime.now(tz)
    schedule_entries = (
        state.get("schedule_entries")
        if isinstance(state.get("schedule_entries"), list)
        else []
    )
    projects = state.get("projects") if isinstance(state.get("projects"), list) else []

    content_blocks: list[str] = []
    if schedule_entries:
        content_blocks.append("schedule")
    if projects:
        content_blocks.append("todos")
    if news_items:
        content_blocks.append("news")

    return {
        "schedule_name": schedule.get("name", schedule_name),
        "subject_prefix": schedule.get("subject_prefix", "SignalNest"),
        "focus": schedule.get("focus", ""),
        "date": now.date().isoformat(),
        "datetime": now.isoformat(),
        "schedule_entries": schedule_entries,
        "projects": projects,
        "news_items": news_items,
        "digest_summary": str(digest_summary or ""),
        "content_blocks": content_blocks,
    }


def _make_progress_callback(store: AppStateStore, job_run_id: int):
    def _callback(event: dict[str, Any]) -> None:
        event_type = str(event.get("type", "")).strip()
        if event_type == "turn_started":
            session_id = str(event.get("session_id", ""))
            if session_id:
                store.set_job_session(job_run_id, session_id)
            store.update_job_progress(
                job_run_id,
                stage="agent",
                message=f"Agent turn #{event.get('turn_index', '?')} started",
            )
            store.add_job_log(
                job_run_id,
                level="INFO",
                component="agent",
                event_type="turn_started",
                message="Agent turn started",
                extra=event,
            )
            return

        if event_type == "tool_start":
            tool_name = str(event.get("tool_name", ""))
            store.update_job_progress(
                job_run_id,
                stage=tool_name or "tool",
                message=f"Running {tool_name} (step {event.get('step_no', '?')})",
            )
            store.add_job_log(
                job_run_id,
                level="INFO",
                component="agent",
                event_type="tool_start",
                message=f"Started tool {tool_name}",
                extra=event,
            )
            return

        if event_type == "tool_finish":
            tool_name = str(event.get("tool_name", ""))
            success = bool(event.get("success"))
            store.update_job_progress(
                job_run_id,
                stage=tool_name or "tool",
                message=(
                    f"Finished {tool_name} successfully"
                    if success
                    else f"{tool_name} failed: {event.get('error', '')}"
                ),
            )
            store.add_job_log(
                job_run_id,
                level="INFO" if success else "ERROR",
                component="agent",
                event_type="tool_finish",
                message=(
                    f"Finished tool {tool_name}"
                    if success
                    else f"Tool {tool_name} failed"
                ),
                extra=event,
            )
            return

        if event_type == "turn_finished":
            store.add_job_log(
                job_run_id,
                level="INFO" if event.get("status") == "ok" else "ERROR",
                component="agent",
                event_type="turn_finished",
                message="Agent turn finished",
                extra=event,
            )

    return _callback


def run_tracked_schedule(
    schedule_name: str,
    config: dict,
    *,
    dry_run: bool = False,
    trigger_type: str = "cron",
    job_run_id: int | None = None,
) -> dict[str, Any]:
    store = AppStateStore.from_config(config)
    store.init_db()

    effective_schedule = _resolve_schedule(schedule_name, config)
    effective_name = str(effective_schedule.get("name") or schedule_name or "(default)")
    if job_run_id is None:
        job_run_id = store.create_job_run(
            schedule_name=effective_name,
            trigger_type=trigger_type,
            dry_run=dry_run,
        )

    store.mark_job_running(job_run_id, stage="boot", message="Preparing job execution")
    store.add_job_log(
        job_run_id,
        level="INFO",
        component="runtime",
        event_type="job_started",
        message=f"Started {trigger_type} run for {effective_name}",
        extra={"dry_run": dry_run},
    )

    progress_callback = _make_progress_callback(store, job_run_id)

    try:
        result = _execute_schedule(
            effective_name,
            config,
            dry_run=dry_run,
            progress_callback=progress_callback,
        )
        session_id = str(result.get("session_id", ""))
        if session_id:
            store.set_job_session(job_run_id, session_id)

        store.update_job_progress(
            job_run_id, stage="finalizing", message="Persisting digest projection"
        )
        state = _load_agent_state(config, session_id)
        payload = _build_payload_from_state(effective_name, state, config)
        digest_id: int | None = None
        if payload is not None:
            digest_id = store.upsert_digest(job_run_id=job_run_id, payload=payload)
            store.add_job_log(
                job_run_id,
                level="INFO",
                component="runtime",
                event_type="digest_persisted",
                message="Stored digest projection",
                extra={"news_count": len(payload.get("news_items") or [])},
            )

        raw_items = (
            state.get("raw_items") if isinstance(state.get("raw_items"), list) else []
        )
        news_items = (
            state.get("news_items") if isinstance(state.get("news_items"), list) else []
        )
        if raw_items:
            indexed_items = build_indexed_items(
                raw_items=raw_items, news_items=news_items
            )
            store.replace_items_for_job(
                job_run_id=job_run_id, digest_id=digest_id, items=indexed_items
            )
            store.add_job_log(
                job_run_id,
                level="INFO",
                component="runtime",
                event_type="items_indexed",
                message="Indexed collected items",
                extra={"count": len(indexed_items)},
            )

        store.finish_job_run(job_run_id, status="succeeded", session_id=session_id)
        store.add_job_log(
            job_run_id,
            level="INFO",
            component="runtime",
            event_type="job_finished",
            message="Job finished successfully",
            extra={"session_id": session_id},
        )
        enriched = dict(result)
        enriched["job_run_id"] = job_run_id
        return enriched
    except Exception as exc:
        logger.exception("tracked schedule run failed")
        store.finish_job_run(job_run_id, status="failed", error_message=str(exc))
        store.add_job_log(
            job_run_id,
            level="ERROR",
            component="runtime",
            event_type="job_failed",
            message=str(exc),
        )
        raise


def enqueue_manual_run(
    *,
    executor: ThreadPoolExecutor,
    config: dict,
    schedule_name: str,
    dry_run: bool,
) -> tuple[int, Future[Any]]:
    store = AppStateStore.from_config(config)
    store.init_db()
    effective_schedule = _resolve_schedule(schedule_name, config)
    effective_name = str(effective_schedule.get("name") or schedule_name or "(default)")
    job_run_id = store.create_job_run(
        schedule_name=effective_name,
        trigger_type="manual",
        dry_run=dry_run,
        status="queued",
    )
    store.add_job_log(
        job_run_id,
        level="INFO",
        component="runtime",
        event_type="job_queued",
        message=f"Queued manual run for {effective_name}",
        extra={"dry_run": dry_run},
    )
    future = executor.submit(
        run_tracked_schedule,
        effective_name,
        config,
        dry_run=dry_run,
        trigger_type="manual",
        job_run_id=job_run_id,
    )
    return job_run_id, future


def run_deep_summary(
    store: AppStateStore, config: dict, *, deep_summary_id: int
) -> dict[str, Any]:
    deep_summary = store.get_deep_summary(deep_summary_id)
    if deep_summary is None:
        raise ValueError(f"deep summary {deep_summary_id} not found")
    item = store.get_item(deep_summary["item_id"])
    if item is None:
        raise ValueError(f"item {deep_summary['item_id']} not found")

    store.update_deep_summary(deep_summary_id, status="running")
    try:
        source_content, meta = fetch_original_content(item, config)
        store.update_deep_summary(
            deep_summary_id,
            status="running",
            source_fetch_status=str(meta.get("status", "ok")),
            source_content=source_content,
            source_content_meta=meta,
        )
        summary, model = generate_deep_summary(item, source_content, config)
        store.update_deep_summary(
            deep_summary_id,
            status="succeeded",
            source_fetch_status=str(meta.get("status", "ok")),
            source_content=source_content,
            source_content_meta=meta,
            deep_summary=summary,
            model=model,
        )
        return store.get_deep_summary(deep_summary_id) or {}
    except Exception as exc:
        store.update_deep_summary(
            deep_summary_id,
            status="failed",
            error_message=str(exc),
        )
        raise


def enqueue_manual_deep_summary(
    *,
    executor: ThreadPoolExecutor,
    config: dict,
    item_id: int,
) -> tuple[int, Future[Any]]:
    store = AppStateStore.from_config(config)
    store.init_db()
    deep_summary_id = store.create_deep_summary(
        item_id=item_id,
        job_run_id=None,
        trigger_type="manual",
        status="queued",
    )
    future = executor.submit(
        run_deep_summary, store, config, deep_summary_id=deep_summary_id
    )
    return deep_summary_id, future
