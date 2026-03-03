"""
SQLite persistence for local agent sessions/turns/tool calls.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True)
class TurnRef:
    turn_id: int
    turn_index: int


class AgentSessionStore:
    def __init__(self, db_path: Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        conn = self._connect()
        try:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS agent_sessions (
                    session_id      TEXT PRIMARY KEY,
                    title           TEXT,
                    created_at      TEXT NOT NULL,
                    updated_at      TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS agent_turns (
                    id              INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id      TEXT NOT NULL,
                    turn_index      INTEGER NOT NULL,
                    user_message    TEXT NOT NULL,
                    assistant_reply TEXT,
                    status          TEXT NOT NULL,
                    backend         TEXT,
                    model           TEXT,
                    started_at      TEXT NOT NULL,
                    ended_at        TEXT,
                    FOREIGN KEY(session_id) REFERENCES agent_sessions(session_id)
                );

                CREATE INDEX IF NOT EXISTS idx_agent_turns_session
                    ON agent_turns(session_id, turn_index);

                CREATE TABLE IF NOT EXISTS agent_tool_calls (
                    id              INTEGER PRIMARY KEY AUTOINCREMENT,
                    turn_id          INTEGER NOT NULL,
                    step_no          INTEGER NOT NULL,
                    tool_name        TEXT NOT NULL,
                    args_json        TEXT NOT NULL,
                    result_json      TEXT,
                    success          INTEGER NOT NULL,
                    error            TEXT,
                    created_at       TEXT NOT NULL,
                    FOREIGN KEY(turn_id) REFERENCES agent_turns(id)
                );

                CREATE INDEX IF NOT EXISTS idx_agent_tool_calls_turn
                    ON agent_tool_calls(turn_id, step_no);

                CREATE TABLE IF NOT EXISTS agent_session_state (
                    session_id      TEXT PRIMARY KEY,
                    state_json      TEXT NOT NULL,
                    updated_at      TEXT NOT NULL,
                    FOREIGN KEY(session_id) REFERENCES agent_sessions(session_id)
                );
                """
            )
            conn.commit()
        finally:
            conn.close()

    def ensure_session(self, session_id: str, title: str = "") -> None:
        now = _utcnow_iso()
        conn = self._connect()
        try:
            conn.execute(
                """
                INSERT INTO agent_sessions (session_id, title, created_at, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(session_id) DO UPDATE SET updated_at=excluded.updated_at
                """,
                (session_id, title, now, now),
            )
            conn.commit()
        finally:
            conn.close()

    def start_turn(
        self,
        session_id: str,
        user_message: str,
        *,
        backend: str,
        model: str,
    ) -> TurnRef:
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT COALESCE(MAX(turn_index), 0) AS max_idx FROM agent_turns WHERE session_id=?",
                (session_id,),
            ).fetchone()
            turn_index = int((row["max_idx"] if row else 0) or 0) + 1
            now = _utcnow_iso()
            cursor = conn.execute(
                """
                INSERT INTO agent_turns (
                    session_id, turn_index, user_message, status, backend, model, started_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (session_id, turn_index, user_message, "running", backend, model, now),
            )
            conn.execute(
                "UPDATE agent_sessions SET updated_at=? WHERE session_id=?",
                (now, session_id),
            )
            conn.commit()
            return TurnRef(turn_id=int(cursor.lastrowid), turn_index=turn_index)
        finally:
            conn.close()

    def finish_turn(self, turn_id: int, assistant_reply: str, status: str) -> None:
        now = _utcnow_iso()
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT session_id FROM agent_turns WHERE id=?",
                (turn_id,),
            ).fetchone()
            conn.execute(
                """
                UPDATE agent_turns
                SET assistant_reply=?, status=?, ended_at=?
                WHERE id=?
                """,
                (assistant_reply, status, now, turn_id),
            )
            if row:
                conn.execute(
                    "UPDATE agent_sessions SET updated_at=? WHERE session_id=?",
                    (now, row["session_id"]),
                )
            conn.commit()
        finally:
            conn.close()

    def add_tool_call(
        self,
        turn_id: int,
        *,
        step_no: int,
        tool_name: str,
        args: dict[str, Any],
        result: Any = None,
        success: bool,
        error: str = "",
    ) -> None:
        conn = self._connect()
        try:
            conn.execute(
                """
                INSERT INTO agent_tool_calls (
                    turn_id, step_no, tool_name, args_json, result_json, success, error, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    turn_id,
                    step_no,
                    tool_name,
                    json.dumps(args, ensure_ascii=False, default=str),
                    json.dumps(result, ensure_ascii=False, default=str) if result is not None else None,
                    1 if success else 0,
                    error,
                    _utcnow_iso(),
                ),
            )
            conn.commit()
        finally:
            conn.close()

    def load_recent_turns(self, session_id: str, limit: int = 6) -> list[dict]:
        conn = self._connect()
        try:
            rows = conn.execute(
                """
                SELECT turn_index, user_message, assistant_reply, status, started_at, ended_at
                FROM agent_turns
                WHERE session_id=?
                ORDER BY turn_index DESC
                LIMIT ?
                """,
                (session_id, limit),
            ).fetchall()
            rows = list(reversed(rows))
            return [
                {
                    "turn_index": int(row["turn_index"]),
                    "user_message": row["user_message"],
                    "assistant_reply": row["assistant_reply"] or "",
                    "status": row["status"],
                    "started_at": row["started_at"],
                    "ended_at": row["ended_at"] or "",
                }
                for row in rows
            ]
        finally:
            conn.close()

    def load_state(self, session_id: str) -> dict[str, Any]:
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT state_json FROM agent_session_state WHERE session_id=?",
                (session_id,),
            ).fetchone()
            if not row:
                return {}
            try:
                parsed = json.loads(row["state_json"])
                return parsed if isinstance(parsed, dict) else {}
            except json.JSONDecodeError:
                return {}
        finally:
            conn.close()

    def save_state(self, session_id: str, state: dict[str, Any]) -> None:
        now = _utcnow_iso()
        conn = self._connect()
        try:
            conn.execute(
                """
                INSERT INTO agent_session_state (session_id, state_json, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(session_id) DO UPDATE
                SET state_json=excluded.state_json, updated_at=excluded.updated_at
                """,
                (
                    session_id,
                    json.dumps(state, ensure_ascii=False, default=str),
                    now,
                ),
            )
            conn.execute(
                "UPDATE agent_sessions SET updated_at=? WHERE session_id=?",
                (now, session_id),
            )
            conn.commit()
        finally:
            conn.close()
