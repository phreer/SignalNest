from __future__ import annotations

import tempfile
import unittest
from unittest.mock import MagicMock, patch

from src import main
from src.web.store import AppStateStore
from src.web.app import create_app


def _sample_config() -> dict:
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
        "storage": {"data_dir": "data"},
    }


class MainEntrypointTests(unittest.TestCase):
    def test_default_main_runs_service_mode(self) -> None:
        config = _sample_config()
        with (
            patch("src.main.load_config", return_value=config),
            patch("src.main.serve") as serve,
            patch("sys.argv", ["src.main"]),
        ):
            main.main()

        serve.assert_called_once_with(config)

    def test_schedule_args_enqueue_once(self) -> None:
        config = _sample_config()
        with (
            patch("src.main.load_config", return_value=config),
            patch(
                "src.web.runtime.enqueue_scheduled_run",
                return_value={"job_run_id": 123, "queued": True},
            ) as enqueue,
            patch("sys.argv", ["src.main", "--schedule-name", "早间日报"]),
        ):
            main.main()

        enqueue.assert_called_once()
        self.assertEqual(enqueue.call_args.kwargs["schedule_name"], "早间日报")
        self.assertFalse(enqueue.call_args.kwargs["dry_run"])

    def test_serve_enables_embedded_scheduler(self) -> None:
        config = _sample_config()
        store = object()
        created_app = object()
        worker_thread = MagicMock()
        with (
            patch("src.web.app.bootstrap_app_state", return_value=store) as bootstrap,
            patch("src.web.app.create_app", return_value=created_app) as create_app,
            patch(
                "src.main.threading.Thread", return_value=worker_thread
            ) as thread_cls,
            patch("src.main.uvicorn.run") as uvicorn_run,
        ):
            main.serve(config)

        bootstrap.assert_called_once()
        create_app.assert_called_once()
        served_config = create_app.call_args.args[0]
        self.assertIsNot(served_config, config)
        self.assertTrue(served_config["runtime"]["embedded_scheduler"])
        self.assertEqual(create_app.call_args.kwargs["store"], store)
        thread_cls.assert_called_once()
        self.assertEqual(
            thread_cls.call_args.kwargs["kwargs"]["scheduler_enabled"], True
        )
        worker_thread.start.assert_called_once()
        worker_thread.join.assert_called_once_with(timeout=5)
        uvicorn_run.assert_called_once_with(created_app, host="0.0.0.0", port=8080)

    def test_create_app_requires_explicit_store(self) -> None:
        config = _sample_config()
        with self.assertRaises(TypeError):
            create_app(config)

    def test_run_schedule_prefetches_raw_items_and_denies_collect_tools(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = _sample_config()
            config["storage"]["data_dir"] = tmp

            observed = {}

            def _fake_run_agent_turn(message, run_config, options):
                observed["message"] = message
                observed["deny_tools"] = run_config["agent"]["policy"]["deny_tools"]
                observed["session_id"] = options.session_id
                return {
                    "session_id": options.session_id,
                    "turn_index": 1,
                    "status": "ok",
                    "response": "done",
                    "steps": [],
                }

            with (
                patch(
                    "src.main.collect_rss",
                    return_value=[
                        {
                            "source": "rss",
                            "title": "Example item",
                            "url": "https://example.com/item",
                            "feed_title": "Feed",
                            "published_at": "2026-04-08T10:00:00+08:00",
                        }
                    ],
                ),
                patch(
                    "src.agent.kernel.run_agent_turn", side_effect=_fake_run_agent_turn
                ),
            ):
                result = main.run_schedule("早间日报", config, dry_run=True)

            self.assertEqual(result["status"], "ok")
            self.assertIn(
                "不要再调用 collect_github / collect_rss / collect_youtube",
                observed["message"],
            )
            self.assertIn("collect_rss", observed["deny_tools"])
            self.assertIn("collect_github", observed["deny_tools"])
            self.assertIn("collect_youtube", observed["deny_tools"])

            store = AppStateStore.from_config(config)
            store.init_db()
            items = store.list_items(limit=10)
            self.assertEqual(len(items), 1)
            self.assertEqual(items[0]["title"], "Example item")
            conn = store._connect()
            try:
                row = conn.execute(
                    "SELECT seen_count FROM raw_items WHERE url=?",
                    ("https://example.com/item",),
                ).fetchone()
            finally:
                conn.close()
            self.assertIsNotNone(row)
            self.assertEqual(row["seen_count"], 1)

    def test_run_schedule_keeps_full_raw_items_but_caps_rss_candidates(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = _sample_config()
            config["storage"]["data_dir"] = tmp
            config["collectors"] = {"rss": {"max_items_per_feed_initial": 2}}

            observed = {}

            def _fake_run_agent_turn(message, run_config, options):
                from src.agent.session_store import AgentSessionStore
                from pathlib import Path

                store = AgentSessionStore(Path(tmp) / "agent_sessions.db")
                state = store.load_state(options.session_id)
                observed["raw_items"] = state.get("raw_items", [])
                observed["candidate_raw_items"] = state.get("candidate_raw_items", [])
                return {
                    "session_id": options.session_id,
                    "turn_index": 1,
                    "status": "ok",
                    "response": "done",
                    "steps": [],
                }

            rss_items = [
                {
                    "source": "rss",
                    "title": f"Item {i}",
                    "url": f"https://example.com/item-{i}",
                    "feed_title": "Feed",
                    "published_at": "2026-04-08T10:00:00+08:00",
                }
                for i in range(3)
            ]

            with (
                patch("src.main.collect_rss", return_value=rss_items),
                patch(
                    "src.agent.kernel.run_agent_turn", side_effect=_fake_run_agent_turn
                ),
            ):
                result = main.run_schedule("早间日报", config, dry_run=True)

            self.assertEqual(result["status"], "ok")
            self.assertEqual(len(observed["raw_items"]), 3)
            self.assertEqual(len(observed["candidate_raw_items"]), 2)

            store = AppStateStore.from_config(config)
            store.init_db()
            items = store.list_items(limit=10)
            self.assertEqual(len(items), 3)


if __name__ == "__main__":
    unittest.main()
