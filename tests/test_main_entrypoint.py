from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

from src import main
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


if __name__ == "__main__":
    unittest.main()
