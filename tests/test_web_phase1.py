from __future__ import annotations

import copy
import json
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from src.web.app import create_app
from src.web.runtime import run_deep_summary, run_tracked_schedule
from src.web.store import AppStateStore


def _sample_config(data_dir: str) -> dict:
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
        "_personal_dir": str(Path(data_dir) / "personal"),
    }


def _fake_schedule_run(
    schedule_name: str, config: dict, dry_run: bool, progress_callback
):
    if progress_callback:
        progress_callback(
            {"type": "turn_started", "session_id": "session-1", "turn_index": 1}
        )
        progress_callback(
            {
                "type": "tool_start",
                "step_no": 1,
                "tool_name": "collect_rss",
                "arguments": {},
            }
        )
        progress_callback(
            {
                "type": "tool_finish",
                "step_no": 1,
                "tool_name": "collect_rss",
                "arguments": {},
                "success": True,
                "result": {"fetched_count": 1},
            }
        )
        progress_callback(
            {
                "type": "turn_finished",
                "session_id": "session-1",
                "turn_index": 1,
                "status": "ok",
            }
        )

    data_dir = Path(config["storage"]["data_dir"])
    data_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "schedule_name": schedule_name,
        "subject_prefix": "SignalNest",
        "focus": "AI",
        "date": "2026-04-08",
        "datetime": "2026-04-08T10:00:00+08:00",
        "schedule_entries": [],
        "projects": [],
        "news_items": [
            {
                "title": "Example item",
                "url": "https://example.com/item",
                "source": "rss",
                "ai_score": 9,
                "ai_summary": "summary",
            }
        ],
        "digest_summary": "digest summary",
        "content_blocks": ["news"],
    }

    state_path = data_dir / "agent_sessions.db"
    from src.agent.session_store import AgentSessionStore

    store = AgentSessionStore(state_path)
    store.ensure_session("session-1", title="test")
    store.save_state(
        "session-1",
        {
            "payload": payload,
            "raw_items": [
                {
                    "title": "Example item",
                    "url": "https://example.com/item",
                    "source": "rss",
                    "feed_title": "Feed",
                    "published_at": "2026-04-08T10:00:00+08:00",
                    "description": "summary",
                    "content_snippet": "content",
                }
            ],
            "news_items": payload["news_items"],
            "digest_summary": payload["digest_summary"],
        },
    )
    return {
        "session_id": "session-1",
        "turn_index": 1,
        "status": "ok",
        "response": "done",
        "steps": [],
    }


class AppStateStoreTests(unittest.TestCase):
    def test_store_roundtrip(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = _sample_config(tmp)
            store = AppStateStore.from_config(config)
            store.init_db()
            job_id = store.create_job_run(
                schedule_name="早间日报", trigger_type="manual", dry_run=True
            )
            store.mark_job_running(job_id, stage="boot", message="starting")
            store.add_job_log(
                job_id,
                level="INFO",
                component="test",
                event_type="hello",
                message="world",
            )
            digest_id = store.upsert_digest(
                job_run_id=job_id,
                payload={
                    "schedule_name": "早间日报",
                    "date": "2026-04-08",
                    "datetime": "2026-04-08T10:00:00+08:00",
                    "digest_summary": "summary",
                    "news_items": [],
                },
            )
            self.assertGreater(job_id, 0)
            self.assertGreater(digest_id, 0)
            self.assertEqual(store.get_job(job_id)["status"], "running")
            self.assertEqual(len(store.list_job_logs(job_id)), 1)
            self.assertEqual(store.get_latest_digest()["summary_text"], "summary")

    def test_deep_summary_requires_existing_item(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = _sample_config(tmp)
            store = AppStateStore.from_config(config)
            store.init_db()

            with self.assertRaises(sqlite3.IntegrityError):
                store.create_deep_summary(
                    item_id=999,
                    job_run_id=None,
                    trigger_type="manual",
                    status="queued",
                )


class RuntimeTests(unittest.TestCase):
    def test_tracked_schedule_persists_job_and_digest(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = _sample_config(tmp)
            with patch(
                "src.web.runtime._execute_schedule", side_effect=_fake_schedule_run
            ):
                result = run_tracked_schedule(
                    "早间日报", config, dry_run=True, trigger_type="manual"
                )

            self.assertEqual(result["status"], "ok")
            store = AppStateStore.from_config(config)
            job = store.get_job(result["job_run_id"])
            self.assertEqual(job["status"], "succeeded")
            digest = store.get_digest_for_job(result["job_run_id"])
            self.assertIsNotNone(digest)
            self.assertEqual(digest["summary_text"], "digest summary")
            items = store.list_items(limit=20)
            self.assertEqual(len(items), 1)
            self.assertTrue(items[0]["selected_for_digest"])
            self.assertEqual(items[0]["raw"]["content_snippet"], "content")
            self.assertEqual(items[0]["raw"]["ai_summary"], "summary")

    def test_manual_deep_summary_flow(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = _sample_config(tmp)
            store = AppStateStore.from_config(config)
            store.init_db()
            job_id = store.create_job_run(
                schedule_name="早间日报", trigger_type="manual", dry_run=True
            )
            store.replace_items_for_job(
                job_run_id=job_id,
                digest_id=None,
                items=[
                    {
                        "source": "rss",
                        "external_id": "",
                        "title": "Example item",
                        "url": "https://example.com/item",
                        "author": "",
                        "feed_title": "Feed",
                        "language": "",
                        "published_at": "2026-04-08T10:00:00+08:00",
                        "selected_for_digest": True,
                        "ai_score": 9,
                        "ai_summary": "summary",
                        "ai_reason": "reason",
                        "raw": {
                            "source": "rss",
                            "url": "https://example.com/item",
                            "title": "Example item",
                        },
                    }
                ],
            )
            item = store.list_items(limit=10)[0]
            deep_summary_id = store.create_deep_summary(
                item_id=item["id"],
                job_run_id=None,
                trigger_type="manual",
                status="queued",
            )

            with (
                patch(
                    "src.web.runtime.fetch_original_content",
                    return_value=("source body", {"status": "ok"}),
                ),
                patch(
                    "src.web.runtime.generate_deep_summary",
                    return_value=("deep summary body", "mock-model"),
                ),
            ):
                result = run_deep_summary(
                    store, config, deep_summary_id=deep_summary_id
                )

            self.assertEqual(result["status"], "succeeded")
            self.assertEqual(result["deep_summary"], "deep summary body")


class ApiTests(unittest.TestCase):
    def test_items_api_time_range_excludes_older_same_day_cutoff_item(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = _sample_config(tmp)
            store = AppStateStore.from_config(config)
            store.init_db()
            job_id = store.create_job_run(
                schedule_name="早间日报", trigger_type="manual", dry_run=True
            )
            now = datetime.now(timezone.utc).replace(microsecond=0)
            store.replace_items_for_job(
                job_run_id=job_id,
                digest_id=None,
                items=[
                    {
                        "source": "rss",
                        "external_id": "",
                        "title": "recent item",
                        "url": "https://example.com/recent",
                        "author": "",
                        "feed_title": "Feed",
                        "language": "",
                        "published_at": (now - timedelta(hours=2)).isoformat(),
                        "selected_for_digest": True,
                        "ai_score": 8,
                        "ai_summary": "recent",
                        "ai_reason": "",
                        "raw": {"source": "rss", "url": "https://example.com/recent"},
                    },
                    {
                        "source": "rss",
                        "external_id": "",
                        "title": "old item",
                        "url": "https://example.com/old",
                        "author": "",
                        "feed_title": "Feed",
                        "language": "",
                        "published_at": (now - timedelta(days=1, hours=1)).isoformat(),
                        "selected_for_digest": False,
                        "ai_score": None,
                        "ai_summary": "",
                        "ai_reason": "",
                        "raw": {"source": "rss", "url": "https://example.com/old"},
                    },
                ],
            )

            app = create_app(copy.deepcopy(config))
            client = TestClient(app)
            resp = client.get("/api/items?time_range=1d")
            self.assertEqual(resp.status_code, 200)
            items = resp.json()["items"]
            self.assertEqual([item["title"] for item in items], ["recent item"])

    def test_status_and_manual_run_api(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = _sample_config(tmp)
            app = create_app(copy.deepcopy(config))
            client = TestClient(app)

            status_resp = client.get("/api/status")
            self.assertEqual(status_resp.status_code, 200)
            self.assertIn("next_runs", status_resp.json())

            with patch("src.web.app.enqueue_manual_run", return_value=(123, None)):
                run_resp = client.post("/api/schedules/早间日报/run?dry_run=true")
            self.assertEqual(run_resp.status_code, 200)
            self.assertEqual(run_resp.json()["job_run_id"], 123)

    def test_digest_archive_sync_visible_in_ui(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = _sample_config(tmp)
            output_dir = Path(tmp) / "outputs"
            output_dir.mkdir(parents=True, exist_ok=True)
            payload = {
                "schedule_name": "早间日报",
                "date": "2026-04-08",
                "datetime": "2026-04-08T10:00:00+08:00",
                "digest_summary": "from archive",
                "news_items": [],
            }
            (output_dir / "digest_20260408_100000_000000_test.json").write_text(
                json.dumps(payload, ensure_ascii=False),
                encoding="utf-8",
            )

            app = create_app(copy.deepcopy(config))
            client = TestClient(app)
            resp = client.get("/api/digests/latest")
            self.assertEqual(resp.status_code, 200)
            self.assertEqual(resp.json()["digest"]["summary_text"], "from archive")

    def test_items_api_filters(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = _sample_config(tmp)
            with patch(
                "src.web.runtime._execute_schedule", side_effect=_fake_schedule_run
            ):
                run_tracked_schedule(
                    "早间日报", config, dry_run=True, trigger_type="manual"
                )

            app = create_app(copy.deepcopy(config))
            client = TestClient(app)
            resp = client.get("/api/items?source=rss&selected_only=true")
            self.assertEqual(resp.status_code, 200)
            items = resp.json()["items"]
            self.assertEqual(len(items), 1)
            self.assertEqual(items[0]["source"], "rss")

    def test_item_deep_summary_api(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = _sample_config(tmp)
            store = AppStateStore.from_config(config)
            store.init_db()
            job_id = store.create_job_run(
                schedule_name="早间日报", trigger_type="manual", dry_run=True
            )
            store.replace_items_for_job(
                job_run_id=job_id,
                digest_id=None,
                items=[
                    {
                        "source": "github",
                        "external_id": "owner/repo",
                        "title": "owner/repo",
                        "url": "https://github.com/owner/repo",
                        "author": "",
                        "feed_title": "",
                        "language": "Python",
                        "published_at": "2026-04-08T10:00:00+08:00",
                        "selected_for_digest": False,
                        "ai_score": None,
                        "ai_summary": "",
                        "ai_reason": "",
                        "raw": {
                            "source": "github",
                            "url": "https://github.com/owner/repo",
                            "title": "owner/repo",
                        },
                    }
                ],
            )
            item = store.list_items(limit=10)[0]

            app = create_app(copy.deepcopy(config))
            client = TestClient(app)
            with patch(
                "src.web.app.enqueue_manual_deep_summary", return_value=(77, None)
            ):
                resp = client.post(f"/api/items/{item['id']}/deep-summary")
            self.assertEqual(resp.status_code, 200)
            self.assertEqual(resp.json()["deep_summary_id"], 77)

    def test_item_deep_summary_api_rejects_missing_item(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = _sample_config(tmp)
            app = create_app(copy.deepcopy(config))
            client = TestClient(app)

            resp = client.post("/api/items/999/deep-summary")
            self.assertEqual(resp.status_code, 404)
            self.assertEqual(resp.json()["detail"], "item not found")


if __name__ == "__main__":
    unittest.main()
