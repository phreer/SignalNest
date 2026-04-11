from __future__ import annotations

import copy
import sqlite3
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

from zoneinfo import ZoneInfo

from fastapi.testclient import TestClient

from src.web.app import bootstrap_app_state, create_app
from src.web.runtime import (
    ScheduleAlreadyRunningError,
    _auto_deep_summaries,
    enqueue_scheduled_run,
    enqueue_manual_run,
    run_scheduler_tick,
    run_worker_loop,
    run_tracked_schedule,
)
from src.web.store import AppStateStore


def _sample_config(data_dir: str, deep_summary_overrides: dict | None = None) -> dict:
    ds_cfg = {
        "auto_enabled": False,
        "score_threshold": 8,
        "max_per_run": 5,
        "timeout_per_item": 120,
        "exclude_sources": [],
    }
    if deep_summary_overrides:
        ds_cfg.update(deep_summary_overrides)
    return {
        "app": {"timezone": "Asia/Shanghai", "language": "zh"},
        "schedules": [
            {
                "name": "早间日报",
                "cron": "0 9 * * *",
                "content": ["news"],
                "sources": ["rss"],
                "focus": "AI",
                "subject_prefix": "SignalNest",
            }
        ],
        "collectors": {},
        "ai": {},
        "agent": {
            "max_steps": 6,
            "schedule_max_steps": 8,
            "max_steps_hard_limit": 20,
            "schedule_allow_side_effects": True,
            "require_dispatch_tool_call": False,
            "fallback_response_max_tokens": 800,
            "session_title_template": "Scheduled Push | {schedule_name}",
            "recent_turns_context_limit": 6,
            "policy": {
                "allow_tools": [],
                "deny_tools": [],
                "allow_side_effects": False,
            },
        },
        "notifications": {
            "file": {"enabled": True, "output_dir": "outputs", "archive": True}
        },
        "storage": {"data_dir": data_dir},
        "deep_summary": ds_cfg,
        "_personal_dir": str(Path(data_dir) / "personal"),
    }


def _insert_item(
    store: AppStateStore,
    job_run_id: int,
    *,
    title: str = "Test Item",
    url: str = "https://example.com/item",
    source: str = "rss",
    ai_score: int = 9,
    selected: bool = True,
) -> int:
    """Helper: insert a single collected item and return its id."""
    store.replace_items_for_job(
        job_run_id=job_run_id,
        digest_id=None,
        items=[
            {
                "source": source,
                "external_id": url,
                "title": title,
                "url": url,
                "author": "",
                "feed_title": "Feed",
                "language": "",
                "published_at": "2026-04-08T10:00:00+08:00",
                "selected_for_digest": selected,
                "ai_score": ai_score,
                "ai_summary": "summary",
                "ai_reason": "reason",
                "raw": {"source": source, "url": url, "title": title},
            }
        ],
    )
    return store.list_items(limit=10)[0]["id"]


# ──────────────────────────────────────────────────────────────────────────
# Store: SQLite WAL mode
# ──────────────────────────────────────────────────────────────────────────


class StoreWALTests(unittest.TestCase):
    def test_sqlite_wal_mode_enabled(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = _sample_config(tmp)
            store = AppStateStore.from_config(config)
            store.init_db()
            conn = sqlite3.connect(store.db_path)
            try:
                row = conn.execute("PRAGMA journal_mode").fetchone()
                self.assertEqual(row[0].lower(), "wal")
            finally:
                conn.close()


# ──────────────────────────────────────────────────────────────────────────
# Re-entrancy guard
# ──────────────────────────────────────────────────────────────────────────


class ReentrancyTests(unittest.TestCase):
    def test_duplicate_schedule_submission_rejected(self) -> None:
        """enqueue_manual_run raises ScheduleAlreadyRunningError when a job is active."""
        with tempfile.TemporaryDirectory() as tmp:
            config = _sample_config(tmp)
            store = AppStateStore.from_config(config)
            store.init_db()
            # Create an active (queued) job directly in the store
            store.create_job_run(
                schedule_name="早间日报",
                trigger_type="manual",
                dry_run=False,
                status="queued",
            )

            with self.assertRaises(ScheduleAlreadyRunningError):
                enqueue_manual_run(
                    config=config,
                    schedule_name="早间日报",
                    dry_run=False,
                )

    def test_cron_run_skips_when_already_running(self) -> None:
        """run_tracked_schedule returns skipped=True if active job exists (cron path)."""
        with tempfile.TemporaryDirectory() as tmp:
            config = _sample_config(tmp)
            store = AppStateStore.from_config(config)
            store.init_db()
            # Pre-seed an active job
            job_id = store.create_job_run(
                schedule_name="早间日报",
                trigger_type="cron",
                dry_run=False,
                status="queued",
            )
            store.mark_job_running(job_id, stage="boot", message="starting")

            # job_run_id=None triggers the cron re-entrancy guard
            result = run_tracked_schedule(
                "早间日报", config, dry_run=False, trigger_type="cron"
            )
            self.assertTrue(result.get("skipped"))
            self.assertIn("existing_job_run_id", result)

    def test_manual_run_is_only_enqueued_until_worker_claims_it(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = _sample_config(tmp)

            job_run_id = enqueue_manual_run(
                config=config,
                schedule_name="早间日报",
                dry_run=True,
            )

            store = AppStateStore.from_config(config)
            job = store.get_job(job_run_id)
            self.assertIsNotNone(job)
            self.assertEqual(job["status"], "queued")
            self.assertEqual(job["trigger_type"], "manual")

    def test_worker_claims_and_executes_queued_manual_job(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = _sample_config(tmp)
            job_run_id = enqueue_manual_run(
                config=config,
                schedule_name="早间日报",
                dry_run=True,
            )

            with (
                patch(
                    "src.web.runtime._execute_schedule",
                    return_value={
                        "session_id": "session-1",
                        "turn_index": 1,
                        "status": "ok",
                        "response": "done",
                        "steps": [],
                    },
                ),
                patch(
                    "src.web.runtime._load_agent_state",
                    return_value={
                        "payload": {
                            "schedule_name": "早间日报",
                            "date": "2026-04-08",
                            "datetime": "2026-04-08T10:00:00+08:00",
                            "digest_summary": "summary",
                            "news_items": [],
                        },
                        "raw_items": [],
                        "news_items": [],
                    },
                ),
            ):
                processed = run_worker_loop(
                    config, run_once=True, worker_id="test-worker"
                )

            self.assertEqual(processed, 1)
            store = AppStateStore.from_config(config)
            job = store.get_job(job_run_id)
            self.assertEqual(job["status"], "succeeded")
            self.assertEqual(job["worker_id"], "test-worker")

    def test_scheduled_run_is_enqueued_without_direct_execution(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = _sample_config(tmp)

            result = enqueue_scheduled_run(
                config=config,
                schedule_name="早间日报",
                dry_run=False,
                trigger_type="cron",
            )

            self.assertTrue(result["queued"])
            store = AppStateStore.from_config(config)
            job = store.get_job(result["job_run_id"])
            self.assertEqual(job["status"], "queued")
            self.assertEqual(job["trigger_type"], "cron")


# ──────────────────────────────────────────────────────────────────────────
# Auto deep summary
# ──────────────────────────────────────────────────────────────────────────


class AutoDeepSummaryTests(unittest.TestCase):
    def _make_store_with_items(
        self,
        tmp: str,
        *,
        items: list[dict],
        ds_overrides: dict | None = None,
    ) -> tuple[AppStateStore, dict, int]:
        config = _sample_config(tmp, deep_summary_overrides=ds_overrides)
        store = AppStateStore.from_config(config)
        store.init_db()
        job_id = store.create_job_run(
            schedule_name="早间日报", trigger_type="manual", dry_run=True
        )
        store.replace_items_for_job(
            job_run_id=job_id,
            digest_id=None,
            items=items,
        )
        return store, config, job_id

    def _item(
        self,
        *,
        title: str = "Item",
        url: str = "https://example.com/1",
        source: str = "rss",
        ai_score: int = 9,
        selected: bool = True,
    ) -> dict:
        return {
            "source": source,
            "external_id": url,
            "title": title,
            "url": url,
            "author": "",
            "feed_title": "Feed",
            "language": "",
            "published_at": "2026-04-08T10:00:00+08:00",
            "selected_for_digest": selected,
            "ai_score": ai_score,
            "ai_summary": "summary",
            "ai_reason": "reason",
            "raw": {"source": source, "url": url, "title": title},
        }

    def test_auto_deep_summary_triggers_for_high_score_items(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store, config, job_id = self._make_store_with_items(
                tmp,
                items=[self._item(url="https://example.com/1", ai_score=9)],
                ds_overrides={"auto_enabled": True, "score_threshold": 8},
            )

            with (
                patch(
                    "src.web.runtime.fetch_original_content",
                    return_value=("body", {"status": "ok"}),
                ),
                patch(
                    "src.web.runtime.generate_deep_summary",
                    return_value=("deep body", "mock-model"),
                ),
            ):
                _auto_deep_summaries(store, config, job_run_id=job_id)

            item_id = store.list_items(limit=10)[0]["id"]
            ds = store.get_latest_deep_summary_for_item(item_id)
            self.assertIsNotNone(ds)
            self.assertEqual(ds["status"], "succeeded")
            self.assertEqual(ds["trigger_type"], "auto_high_score")

    def test_auto_deep_summary_respects_max_per_run(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            items = [
                self._item(
                    url=f"https://example.com/{i}", title=f"Item {i}", ai_score=9
                )
                for i in range(1, 7)  # 6 items, max_per_run=3
            ]
            store, config, job_id = self._make_store_with_items(
                tmp,
                items=items,
                ds_overrides={
                    "auto_enabled": True,
                    "score_threshold": 8,
                    "max_per_run": 3,
                },
            )

            with (
                patch(
                    "src.web.runtime.fetch_original_content",
                    return_value=("body", {"status": "ok"}),
                ),
                patch(
                    "src.web.runtime.generate_deep_summary",
                    return_value=("deep body", "mock-model"),
                ),
            ):
                _auto_deep_summaries(store, config, job_run_id=job_id)

            all_items = store.list_items(limit=20)
            triggered = [
                item
                for item in all_items
                if store.get_latest_deep_summary_for_item(item["id"]) is not None
            ]
            self.assertEqual(len(triggered), 3)

    def test_auto_deep_summary_skips_existing_summaries(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store, config, job_id = self._make_store_with_items(
                tmp,
                items=[self._item(url="https://example.com/1", ai_score=9)],
                ds_overrides={"auto_enabled": True, "score_threshold": 8},
            )
            item_id = store.list_items(limit=10)[0]["id"]
            # Pre-create a succeeded deep_summary for the item
            ds_id = store.create_deep_summary(
                item_id=item_id,
                job_run_id=job_id,
                trigger_type="manual",
                status="queued",
            )
            store.update_deep_summary(
                ds_id, status="succeeded", deep_summary="existing"
            )

            with (
                patch(
                    "src.web.runtime.fetch_original_content",
                    return_value=("body", {"status": "ok"}),
                ) as mock_fetch,
                patch(
                    "src.web.runtime.generate_deep_summary",
                    return_value=("deep body", "mock-model"),
                ) as mock_gen,
            ):
                _auto_deep_summaries(store, config, job_run_id=job_id)

            # Should not have called the AI at all since summary already exists
            mock_fetch.assert_not_called()
            mock_gen.assert_not_called()

    def test_auto_deep_summary_failure_does_not_fail_main_job(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store, config, job_id = self._make_store_with_items(
                tmp,
                items=[self._item(url="https://example.com/1", ai_score=9)],
                ds_overrides={"auto_enabled": True, "score_threshold": 8},
            )
            store.finish_job_run(job_id, status="succeeded")

            with (
                patch(
                    "src.web.runtime.fetch_original_content",
                    side_effect=RuntimeError("network error"),
                ),
            ):
                # Must not raise
                _auto_deep_summaries(store, config, job_run_id=job_id)

            # Main job should still be succeeded
            job = store.get_job(job_id)
            self.assertEqual(job["status"], "succeeded")

            # The deep_summary record should be failed
            item_id = store.list_items(limit=10)[0]["id"]
            ds = store.get_latest_deep_summary_for_item(item_id)
            self.assertIsNotNone(ds)
            self.assertEqual(ds["status"], "failed")

    def test_auto_deep_summary_disabled(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store, config, job_id = self._make_store_with_items(
                tmp,
                items=[self._item(url="https://example.com/1", ai_score=9)],
                ds_overrides={"auto_enabled": False},
            )

            with (
                patch(
                    "src.web.runtime.fetch_original_content",
                    return_value=("body", {"status": "ok"}),
                ) as mock_fetch,
                patch(
                    "src.web.runtime.generate_deep_summary",
                    return_value=("deep body", "mock-model"),
                ) as mock_gen,
            ):
                _auto_deep_summaries(store, config, job_run_id=job_id)

            mock_fetch.assert_not_called()
            mock_gen.assert_not_called()
            item_id = store.list_items(limit=10)[0]["id"]
            self.assertIsNone(store.get_latest_deep_summary_for_item(item_id))


# ──────────────────────────────────────────────────────────────────────────
# Config API
# ──────────────────────────────────────────────────────────────────────────


class ConfigApiTests(unittest.TestCase):
    def test_config_api_masks_sensitive_fields(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = _sample_config(tmp)
            app_config = copy.deepcopy(config)
            app = create_app(app_config, store=bootstrap_app_state(app_config))
            client = TestClient(app)

            # Inject a fake API key into the environment
            with patch.dict("os.environ", {"AI_API_KEY": "sk-supersecret-1234"}):
                resp = client.get("/api/config")

            self.assertEqual(resp.status_code, 200)
            body = resp.json()
            # The raw key must never appear anywhere in the response
            self.assertNotIn("sk-supersecret-1234", str(body))
            # The masked sentinel should be present instead
            self.assertEqual(body["ai"]["api_key"]["value"], "configured")

    def test_config_api_shows_env_overrides(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = _sample_config(tmp)
            app_config = copy.deepcopy(config)
            app = create_app(app_config, store=bootstrap_app_state(app_config))
            client = TestClient(app)

            with patch.dict(
                "os.environ", {"AI_BACKEND": "claude-cli", "AI_MODEL": "claude/opus-4"}
            ):
                resp = client.get("/api/config")

            self.assertEqual(resp.status_code, 200)
            body = resp.json()
            self.assertTrue(body["ai"]["backend"]["overridden_by_env"])
            self.assertEqual(body["ai"]["backend"]["value"], "claude-cli")
            self.assertTrue(body["ai"]["model"]["overridden_by_env"])

    def test_config_page_renders(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = _sample_config(tmp)
            app_config = copy.deepcopy(config)
            app = create_app(app_config, store=bootstrap_app_state(app_config))
            client = TestClient(app)
            resp = client.get("/config")
            self.assertEqual(resp.status_code, 200)
            self.assertIn(b"Config", resp.content)


class LeaseAwareStatusTests(unittest.TestCase):
    def test_status_api_ignores_stale_running_job(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = _sample_config(tmp)
            store = AppStateStore.from_config(config)
            store.init_db()
            job_id = store.create_job_run(
                schedule_name="早间日报", trigger_type="cron", dry_run=False
            )
            store.mark_job_running(job_id, stage="boot", message="starting")

            conn = sqlite3.connect(store.db_path)
            try:
                stale = "2000-01-01T00:00:00+00:00"
                conn.execute(
                    "UPDATE job_runs SET lease_expires_at=?, heartbeat_at=?, updated_at=? WHERE id=?",
                    (stale, stale, stale, job_id),
                )
                conn.commit()
            finally:
                conn.close()

            app_config = copy.deepcopy(config)
            app = create_app(app_config, store=bootstrap_app_state(app_config))
            client = TestClient(app)
            resp = client.get("/api/status")
            self.assertEqual(resp.status_code, 200)
            self.assertIsNone(resp.json()["running_job"])


class SchedulerTests(unittest.TestCase):
    def test_scheduler_tick_enqueues_due_job(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = _sample_config(tmp)
            tz = ZoneInfo("Asia/Shanghai")
            now = datetime(2026, 4, 9, 9, 0, tzinfo=tz)

            queued = run_scheduler_tick(config, now=now)

            self.assertEqual(len(queued), 1)
            store = AppStateStore.from_config(config)
            jobs = store.list_jobs(limit=10)
            self.assertEqual(len(jobs), 1)
            self.assertEqual(jobs[0]["status"], "queued")
            self.assertEqual(jobs[0]["scheduled_for"], now.isoformat())

    def test_scheduler_tick_is_idempotent_for_same_slot(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = _sample_config(tmp)
            tz = ZoneInfo("Asia/Shanghai")
            now = datetime(2026, 4, 9, 9, 0, tzinfo=tz)

            first = run_scheduler_tick(config, now=now)
            second = run_scheduler_tick(config, now=now)

            self.assertEqual(len(first), 1)
            self.assertEqual(len(second), 0)
            store = AppStateStore.from_config(config)
            jobs = store.list_jobs(limit=10)
            self.assertEqual(len(jobs), 1)

    def test_worker_with_scheduler_enabled_claims_due_job(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = _sample_config(tmp)
            config["runtime"] = {"embedded_scheduler": True}
            tz = ZoneInfo("Asia/Shanghai")
            now = datetime(2026, 4, 9, 9, 0, tzinfo=tz)

            run_scheduler_tick(config, now=now)

            with (
                patch(
                    "src.web.runtime._execute_schedule",
                    return_value={
                        "session_id": "session-1",
                        "turn_index": 1,
                        "status": "ok",
                        "response": "done",
                        "steps": [],
                    },
                ),
                patch(
                    "src.web.runtime._load_agent_state",
                    return_value={
                        "payload": {
                            "schedule_name": "早间日报",
                            "date": "2026-04-09",
                            "datetime": now.isoformat(),
                            "digest_summary": "summary",
                            "news_items": [],
                        },
                        "raw_items": [],
                        "news_items": [],
                    },
                ),
            ):
                processed = run_worker_loop(
                    config,
                    run_once=True,
                    worker_id="test-worker",
                    scheduler_enabled=False,
                    scheduler_poll_interval_seconds=0,
                )

            self.assertEqual(processed, 1)
            store = AppStateStore.from_config(config)
            jobs = store.list_jobs(limit=10)
            self.assertEqual(len(jobs), 1)
            self.assertEqual(jobs[0]["status"], "succeeded")


if __name__ == "__main__":
    unittest.main()
