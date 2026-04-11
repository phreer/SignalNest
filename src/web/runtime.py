from __future__ import annotations

import logging
import os
import threading
import time
import uuid
from concurrent.futures import (
    Future,
    ThreadPoolExecutor,
    TimeoutError as FuturesTimeoutError,
)
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from croniter import croniter

from src.agent.session_store import AgentSessionStore
from src.web.content import (
    build_indexed_items,
    fetch_original_content,
    generate_deep_summary,
)
from src.web.store import AppStateStore

logger = logging.getLogger(__name__)

_JOB_LEASE_SECONDS = 45
_JOB_HEARTBEAT_INTERVAL_SECONDS = 10
_WORKER_POLL_INTERVAL_SECONDS = 2
_SCHEDULER_POLL_INTERVAL_SECONDS = 15

_DEFAULT_DEEP_SUMMARY_CONFIG = {
    "auto_enabled": False,
    "score_threshold": 8,
    "max_per_run": 5,
    "timeout_per_item": 120,
    "exclude_sources": [],
}


class ScheduleAlreadyRunningError(RuntimeError):
    """Raised when a manual run is requested for a schedule that is already active."""


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

        if event_type == "agent_reasoning":
            text = str(event.get("text", ""))
            if text:
                store.add_job_log(
                    job_run_id,
                    level="INFO",
                    component="agent",
                    event_type="agent_reasoning",
                    message=text[:120] + ("…" if len(text) > 120 else ""),
                    extra={"step_no": event.get("step_no"), "text": text},
                )
            return

        if event_type == "llm_usage":
            total = event.get("total_tokens", 0)
            store.add_job_log(
                job_run_id,
                level="INFO",
                component="agent",
                event_type="llm_usage",
                message=f"LLM tokens: {total} total ({event.get('prompt_tokens', 0)} prompt + {event.get('completion_tokens', 0)} completion)",
                extra={
                    "step_no": event.get("step_no"),
                    "prompt_tokens": event.get("prompt_tokens", 0),
                    "completion_tokens": event.get("completion_tokens", 0),
                    "total_tokens": total,
                },
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


class _JobHeartbeat:
    def __init__(
        self,
        store: AppStateStore,
        *,
        job_run_id: int,
        lease_seconds: int,
        interval_seconds: int,
    ) -> None:
        self._store = store
        self._job_run_id = job_run_id
        self._lease_seconds = lease_seconds
        self._interval_seconds = interval_seconds
        self._stop = threading.Event()
        self._thread = threading.Thread(
            target=self._run,
            name=f"job-heartbeat-{job_run_id}",
            daemon=True,
        )

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(timeout=self._interval_seconds + 1)

    def _run(self) -> None:
        while not self._stop.wait(self._interval_seconds):
            try:
                self._store.heartbeat_job_run(
                    self._job_run_id, lease_seconds=self._lease_seconds
                )
            except Exception:
                logger.warning(
                    "failed to refresh job heartbeat",
                    exc_info=True,
                    extra={"job_run_id": self._job_run_id},
                )


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
    store.recover_stale_job_runs()

    effective_schedule = _resolve_schedule(schedule_name, config)
    effective_name = str(effective_schedule.get("name") or schedule_name or "(default)")

    if job_run_id is None:
        # Re-entrancy guard for cron-triggered runs: skip if already running.
        # Note: this check and the subsequent create_job_run use separate DB
        # connections, so a narrow TOCTOU window exists under concurrent requests.
        # At single-instance scale this is acceptable; the worst outcome is two
        # overlapping jobs for the same schedule.
        existing = store.get_running_job_for_schedule(effective_name)
        if existing is not None:
            logger.warning(
                "Schedule '%s' already has an active job (id=%s, status=%s); skipping this run.",
                effective_name,
                existing["id"],
                existing["status"],
            )
            return {"skipped": True, "existing_job_run_id": existing["id"]}

        job_run_id = store.create_job_run(
            schedule_name=effective_name,
            trigger_type=trigger_type,
            dry_run=dry_run,
        )

    job = store.get_job(job_run_id)
    if job is None:
        raise RuntimeError(f"job_run {job_run_id} not found")
    worker_id = job.get("worker_id") or f"local-{uuid.uuid4().hex[:12]}"
    if job.get("status") != "running":
        store.mark_job_running(
            job_run_id,
            stage="boot",
            message="Preparing job execution",
            worker_id=worker_id,
            lease_seconds=_JOB_LEASE_SECONDS,
        )
    store.add_job_log(
        job_run_id,
        level="INFO",
        component="runtime",
        event_type="job_started",
        message=f"Started {trigger_type} run for {effective_name}",
        extra={"dry_run": dry_run, "worker_id": worker_id},
    )

    progress_callback = _make_progress_callback(store, job_run_id)
    heartbeat = _JobHeartbeat(
        store,
        job_run_id=job_run_id,
        lease_seconds=_JOB_LEASE_SECONDS,
        interval_seconds=_JOB_HEARTBEAT_INTERVAL_SECONDS,
    )
    heartbeat.start()

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
        raw_items_prefetched = bool(state.get("raw_items_prefetched"))
        indexed_items: list = []
        if raw_items:
            indexed_items = build_indexed_items(
                raw_items=raw_items, news_items=news_items
            )
            if raw_items_prefetched:
                raw_ids = store.lookup_raw_item_ids(indexed_items)
            else:
                # Upsert all collected items into the persistent raw_items table
                raw_ids = store.upsert_raw_items(indexed_items)

            # Build per-run AI annotations and store them
            annotations = []
            for item, raw_id in zip(indexed_items, raw_ids):
                if raw_id:
                    annotations.append(
                        {
                            "raw_item_id": raw_id,
                            "selected_for_digest": item.get(
                                "selected_for_digest", False
                            ),
                            "ai_score": item.get("ai_score"),
                            "ai_summary": item.get("ai_summary", ""),
                            "ai_reason": item.get("ai_reason", ""),
                        }
                    )
            store.replace_annotations_for_job(
                job_run_id=job_run_id, digest_id=digest_id, annotations=annotations
            )
            store.add_job_log(
                job_run_id,
                level="INFO",
                component="runtime",
                event_type="items_indexed",
                message="Indexed collected items",
                extra={"count": len(indexed_items)},
            )

        store.finish_job_run(
            job_run_id,
            status="succeeded",
            session_id=session_id,
            final_reason="completed",
        )
        store.add_job_log(
            job_run_id,
            level="INFO",
            component="runtime",
            event_type="job_finished",
            message="Job finished successfully",
            extra={"session_id": session_id},
        )

        # Auto deep summary for high-scoring items — runs after the job is marked
        # succeeded so failures here do not change the main job outcome.
        if indexed_items:
            _auto_deep_summaries(store, config, job_run_id=job_run_id)

        enriched = dict(result)
        enriched["job_run_id"] = job_run_id
        return enriched
    except Exception as exc:
        logger.exception("tracked schedule run failed")
        store.finish_job_run(
            job_run_id,
            status="failed",
            error_message=str(exc),
            final_reason="execution_failed",
        )
        store.add_job_log(
            job_run_id,
            level="ERROR",
            component="runtime",
            event_type="job_failed",
            message=str(exc),
        )
        raise
    finally:
        heartbeat.stop()


def enqueue_manual_run(*, config: dict, schedule_name: str, dry_run: bool) -> int:
    store = AppStateStore.from_config(config)
    store.init_db()
    store.recover_stale_job_runs()
    effective_schedule = _resolve_schedule(schedule_name, config)
    effective_name = str(effective_schedule.get("name") or schedule_name or "(default)")

    # Re-entrancy guard: reject if an active job exists for this schedule.
    # Note: same TOCTOU caveat as in run_tracked_schedule — check and insert
    # use separate connections. Duplicate jobs are unlikely at single-instance
    # scale but possible under a burst of concurrent web requests.
    existing = store.get_running_job_for_schedule(effective_name)
    if existing is not None:
        raise ScheduleAlreadyRunningError(
            f"Schedule '{effective_name}' already has an active job "
            f"(id={existing['id']}, status={existing['status']})"
        )

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
    return job_run_id


def enqueue_scheduled_run(
    *,
    config: dict,
    schedule_name: str,
    dry_run: bool,
    trigger_type: str = "cron",
    scheduled_for: str = "",
    idempotency_key: str = "",
) -> dict[str, Any]:
    store = AppStateStore.from_config(config)
    store.init_db()
    store.recover_stale_job_runs()
    effective_schedule = _resolve_schedule(schedule_name, config)
    effective_name = str(effective_schedule.get("name") or schedule_name or "(default)")

    existing = store.get_running_job_for_schedule(effective_name)
    if existing is not None:
        logger.warning(
            "Schedule '%s' already has an active job (id=%s, status=%s); skipping queue submission.",
            effective_name,
            existing["id"],
            existing["status"],
        )
        return {"skipped": True, "existing_job_run_id": existing["id"]}

    if idempotency_key:
        existing_queued = store.get_job_by_idempotency_key(idempotency_key)
        if existing_queued is not None:
            return {
                "skipped": True,
                "existing_job_run_id": existing_queued["id"],
                "reason": "duplicate_idempotency_key",
            }

    job_run_id = store.create_job_run(
        schedule_name=effective_name,
        trigger_type=trigger_type,
        dry_run=dry_run,
        status="queued",
        scheduled_for=scheduled_for,
        idempotency_key=idempotency_key,
    )
    store.add_job_log(
        job_run_id,
        level="INFO",
        component="runtime",
        event_type="job_queued",
        message=f"Queued {trigger_type} run for {effective_name}",
        extra={
            "dry_run": dry_run,
            "scheduled_for": scheduled_for,
            "idempotency_key": idempotency_key,
        },
    )
    return {"job_run_id": job_run_id, "queued": True}


def _resolve_scheduler_timezone(config: dict) -> ZoneInfo:
    return ZoneInfo(config.get("app", {}).get("timezone", "Asia/Shanghai"))


def _compute_due_schedule_slots(
    *,
    config: dict,
    schedule_name: str,
    trigger_type: str,
    now: datetime,
    catch_up_limit: int = 1,
) -> list[datetime]:
    store = AppStateStore.from_config(config)
    store.init_db()

    schedule = _resolve_schedule(schedule_name, config)
    cron_expr = str(schedule.get("cron", "")).strip()
    effective_name = str(schedule.get("name") or schedule_name or "(default)")
    if not cron_expr:
        return []

    latest_scheduled_for = store.get_latest_scheduled_for(
        schedule_name=effective_name,
        trigger_type=trigger_type,
    )
    if latest_scheduled_for:
        base = datetime.fromisoformat(latest_scheduled_for)
    else:
        base = now
        if croniter.match(cron_expr, now):
            base = now
        else:
            base = croniter(cron_expr, now).get_prev(datetime)

    due_slots: list[datetime] = []
    cursor = base
    for _ in range(max(int(catch_up_limit), 1)):
        next_run = croniter(cron_expr, cursor).get_next(datetime)
        if next_run > now:
            break
        due_slots.append(next_run)
        cursor = next_run

    if not latest_scheduled_for and not due_slots and croniter.match(cron_expr, now):
        due_slots.append(now)
    return due_slots


def run_scheduler_tick(
    config: dict,
    *,
    now: datetime | None = None,
    dry_run: bool = False,
    trigger_type: str = "cron",
    catch_up_limit: int = 1,
) -> list[dict[str, Any]]:
    tz = _resolve_scheduler_timezone(config)
    effective_now = now.astimezone(tz) if now is not None else datetime.now(tz)
    queued: list[dict[str, Any]] = []

    for schedule in config.get("schedules", []):
        schedule_name = str(schedule.get("name", "")).strip()
        if not schedule_name:
            continue
        for slot in _compute_due_schedule_slots(
            config=config,
            schedule_name=schedule_name,
            trigger_type=trigger_type,
            now=effective_now,
            catch_up_limit=catch_up_limit,
        ):
            slot_iso = slot.isoformat()
            result = enqueue_scheduled_run(
                config=config,
                schedule_name=schedule_name,
                dry_run=dry_run,
                trigger_type=trigger_type,
                scheduled_for=slot_iso,
                idempotency_key=f"{schedule_name}:{slot_iso}:{trigger_type}",
            )
            if result.get("queued"):
                queued.append(result)
    return queued


def run_scheduler_loop(
    config: dict,
    *,
    stop_event: threading.Event | None = None,
    poll_interval_seconds: int = _SCHEDULER_POLL_INTERVAL_SECONDS,
    dry_run: bool = False,
    catch_up_limit: int = 1,
) -> int:
    scheduled = 0
    while True:
        if stop_event is not None and stop_event.is_set():
            break
        scheduled += len(
            run_scheduler_tick(
                config,
                dry_run=dry_run,
                catch_up_limit=catch_up_limit,
            )
        )
        if stop_event is not None and stop_event.wait(poll_interval_seconds):
            break
        if stop_event is None:
            time.sleep(poll_interval_seconds)
    return scheduled


def run_worker_loop(
    config: dict,
    *,
    stop_event: threading.Event | None = None,
    poll_interval_seconds: int = _WORKER_POLL_INTERVAL_SECONDS,
    worker_id: str | None = None,
    run_once: bool = False,
    scheduler_enabled: bool = False,
    scheduler_poll_interval_seconds: int = _SCHEDULER_POLL_INTERVAL_SECONDS,
    scheduler_dry_run: bool = False,
    scheduler_catch_up_limit: int = 1,
) -> int:
    store = AppStateStore.from_config(config)
    store.init_db()
    store.recover_stale_job_runs()

    effective_worker_id = worker_id or f"worker-{os.getpid()}-{uuid.uuid4().hex[:8]}"
    processed = 0
    last_scheduler_tick = 0.0

    while True:
        if stop_event is not None and stop_event.is_set():
            break

        if scheduler_enabled:
            now_monotonic = time.monotonic()
            if (
                last_scheduler_tick == 0.0
                or now_monotonic - last_scheduler_tick
                >= scheduler_poll_interval_seconds
            ):
                run_scheduler_tick(
                    config,
                    dry_run=scheduler_dry_run,
                    catch_up_limit=scheduler_catch_up_limit,
                )
                last_scheduler_tick = now_monotonic

        claimed = store.claim_next_job_run(
            worker_id=effective_worker_id,
            lease_seconds=_JOB_LEASE_SECONDS,
        )
        if claimed is None:
            if run_once:
                break
            if stop_event is not None and stop_event.wait(poll_interval_seconds):
                break
            if stop_event is None:
                time.sleep(poll_interval_seconds)
            continue

        processed += 1
        store.add_job_log(
            claimed["id"],
            level="INFO",
            component="worker",
            event_type="job_claimed",
            message=f"Worker {effective_worker_id} claimed queued job",
            extra={"worker_id": effective_worker_id},
        )
        try:
            run_tracked_schedule(
                claimed["schedule_name"],
                config,
                dry_run=claimed["dry_run"],
                trigger_type=claimed["trigger_type"],
                job_run_id=claimed["id"],
            )
        except Exception:
            logger.exception(
                "worker job execution failed", extra={"job_run_id": claimed["id"]}
            )

        if run_once:
            break

    return processed


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


def _auto_deep_summaries(
    store: AppStateStore,
    config: dict,
    *,
    job_run_id: int,
) -> None:
    """Auto-trigger deep summaries for high-scoring items after a digest run.

    Runs serially after the main job is marked succeeded. Failures per item are
    caught and logged; they do not affect the parent job_run status.
    """
    ds_cfg = {**_DEFAULT_DEEP_SUMMARY_CONFIG, **config.get("deep_summary", {})}
    if not ds_cfg.get("auto_enabled"):
        return

    score_threshold = int(ds_cfg.get("score_threshold", 8))
    max_per_run = int(ds_cfg.get("max_per_run", 5))
    timeout_per_item = int(ds_cfg.get("timeout_per_item", 120))
    exclude_sources: list[str] = list(ds_cfg.get("exclude_sources") or [])

    candidates = store.get_eligible_items_for_auto_deep_summary(
        job_run_id=job_run_id,
        score_threshold=score_threshold,
        exclude_sources=exclude_sources,
        limit=max_per_run,
    )

    if not candidates:
        return

    store.add_job_log(
        job_run_id,
        level="INFO",
        component="runtime",
        event_type="auto_deep_summary_started",
        message=f"Auto deep summary: {len(candidates)} eligible items",
        extra={"count": len(candidates), "score_threshold": score_threshold},
    )

    for item in candidates:
        item_id = item["id"]
        deep_summary_id = store.create_deep_summary(
            item_id=item_id,
            job_run_id=job_run_id,
            trigger_type="auto_high_score",
            status="queued",
        )
        try:
            # Run in a sub-thread so we can enforce a wall-clock timeout safely
            # regardless of whether the caller is the main thread or a worker.
            with ThreadPoolExecutor(max_workers=1) as sub_executor:
                fut = sub_executor.submit(
                    run_deep_summary, store, config, deep_summary_id=deep_summary_id
                )
                try:
                    fut.result(timeout=timeout_per_item)
                except FuturesTimeoutError:
                    raise TimeoutError(
                        f"deep summary timed out after {timeout_per_item}s"
                    )
            store.add_job_log(
                job_run_id,
                level="INFO",
                component="runtime",
                event_type="auto_deep_summary_finished",
                message=f"Auto deep summary succeeded for item {item_id}",
                extra={"item_id": item_id, "deep_summary_id": deep_summary_id},
            )
        except Exception as exc:
            logger.warning(
                "Auto deep summary failed for item %s (deep_summary_id=%s): %s",
                item_id,
                deep_summary_id,
                exc,
            )
            store.add_job_log(
                job_run_id,
                level="WARNING",
                component="runtime",
                event_type="auto_deep_summary_failed",
                message=f"Auto deep summary failed for item {item_id}: {exc}",
                extra={
                    "item_id": item_id,
                    "deep_summary_id": deep_summary_id,
                    "error": str(exc),
                },
            )
