from __future__ import annotations

import json
import sqlite3
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _json_default(value: Any) -> str:
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    return str(value)


class AppStateStore:
    def __init__(self, db_path: Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)

    @classmethod
    def from_config(cls, config: dict) -> "AppStateStore":
        data_dir = Path(config.get("storage", {}).get("data_dir", "data"))
        return cls(data_dir / "app.db")

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def init_db(self) -> None:
        conn = self._connect()
        try:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS job_runs (
                    id              INTEGER PRIMARY KEY AUTOINCREMENT,
                    schedule_name   TEXT NOT NULL,
                    trigger_type    TEXT NOT NULL,
                    status          TEXT NOT NULL,
                    dry_run         INTEGER NOT NULL DEFAULT 0,
                    session_id      TEXT,
                    current_stage   TEXT,
                    current_message TEXT,
                    error_message   TEXT,
                    started_at      TEXT,
                    ended_at        TEXT,
                    created_at      TEXT NOT NULL,
                    updated_at      TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_job_runs_status
                    ON job_runs(status, updated_at DESC);

                CREATE INDEX IF NOT EXISTS idx_job_runs_schedule
                    ON job_runs(schedule_name, created_at DESC);

                CREATE TABLE IF NOT EXISTS job_logs (
                    id          INTEGER PRIMARY KEY AUTOINCREMENT,
                    job_run_id  INTEGER NOT NULL,
                    ts          TEXT NOT NULL,
                    level       TEXT NOT NULL,
                    component   TEXT NOT NULL,
                    event_type  TEXT NOT NULL,
                    message     TEXT NOT NULL,
                    extra_json  TEXT,
                    FOREIGN KEY(job_run_id) REFERENCES job_runs(id)
                );

                CREATE INDEX IF NOT EXISTS idx_job_logs_job
                    ON job_logs(job_run_id, ts ASC);

                CREATE TABLE IF NOT EXISTS digests (
                    id              INTEGER PRIMARY KEY AUTOINCREMENT,
                    job_run_id      INTEGER,
                    schedule_name   TEXT NOT NULL,
                    digest_date     TEXT,
                    digest_datetime TEXT,
                    summary_text    TEXT,
                    payload_json    TEXT NOT NULL,
                    source_path     TEXT,
                    created_at      TEXT NOT NULL,
                    updated_at      TEXT NOT NULL,
                    FOREIGN KEY(job_run_id) REFERENCES job_runs(id)
                );

                CREATE UNIQUE INDEX IF NOT EXISTS idx_digests_source_path
                    ON digests(source_path)
                    WHERE source_path IS NOT NULL;

                CREATE INDEX IF NOT EXISTS idx_digests_schedule_time
                    ON digests(schedule_name, digest_datetime DESC, created_at DESC);

                CREATE TABLE IF NOT EXISTS collected_items (
                    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
                    job_run_id          INTEGER,
                    digest_id           INTEGER,
                    source              TEXT NOT NULL,
                    external_id         TEXT,
                    title               TEXT NOT NULL,
                    url                 TEXT NOT NULL,
                    author              TEXT,
                    feed_title          TEXT,
                    language            TEXT,
                    published_at        TEXT,
                    collected_at        TEXT NOT NULL,
                    selected_for_digest INTEGER NOT NULL DEFAULT 0,
                    ai_score            INTEGER,
                    ai_summary          TEXT,
                    ai_reason           TEXT,
                    raw_json            TEXT NOT NULL,
                    FOREIGN KEY(job_run_id) REFERENCES job_runs(id),
                    FOREIGN KEY(digest_id) REFERENCES digests(id)
                );

                CREATE INDEX IF NOT EXISTS idx_collected_items_source_time
                    ON collected_items(source, collected_at DESC, id DESC);

                CREATE INDEX IF NOT EXISTS idx_collected_items_selected
                    ON collected_items(selected_for_digest, collected_at DESC);

                CREATE TABLE IF NOT EXISTS deep_summaries (
                    id                       INTEGER PRIMARY KEY AUTOINCREMENT,
                    item_id                  INTEGER NOT NULL,
                    job_run_id               INTEGER,
                    trigger_type             TEXT NOT NULL,
                    status                   TEXT NOT NULL,
                    source_fetch_status      TEXT,
                    source_content           TEXT,
                    source_content_meta_json TEXT,
                    deep_summary             TEXT,
                    model                    TEXT,
                    error_message            TEXT,
                    created_at               TEXT NOT NULL,
                    updated_at               TEXT NOT NULL,
                    FOREIGN KEY(item_id) REFERENCES collected_items(id),
                    FOREIGN KEY(job_run_id) REFERENCES job_runs(id)
                );

                CREATE INDEX IF NOT EXISTS idx_deep_summaries_item
                    ON deep_summaries(item_id, created_at DESC);
                """
            )
            conn.commit()
        finally:
            conn.close()

    def create_job_run(
        self,
        *,
        schedule_name: str,
        trigger_type: str,
        dry_run: bool,
        status: str = "queued",
    ) -> int:
        now = _utcnow_iso()
        conn = self._connect()
        try:
            cursor = conn.execute(
                """
                INSERT INTO job_runs (
                    schedule_name, trigger_type, status, dry_run, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (schedule_name, trigger_type, status, 1 if dry_run else 0, now, now),
            )
            conn.commit()
            return int(cursor.lastrowid)
        finally:
            conn.close()

    def mark_job_running(self, job_run_id: int, *, stage: str, message: str) -> None:
        now = _utcnow_iso()
        conn = self._connect()
        try:
            conn.execute(
                """
                UPDATE job_runs
                SET status='running', current_stage=?, current_message=?, started_at=COALESCE(started_at, ?), updated_at=?
                WHERE id=?
                """,
                (stage, message, now, now, job_run_id),
            )
            conn.commit()
        finally:
            conn.close()

    def set_job_session(self, job_run_id: int, session_id: str) -> None:
        now = _utcnow_iso()
        conn = self._connect()
        try:
            conn.execute(
                "UPDATE job_runs SET session_id=?, updated_at=? WHERE id=?",
                (session_id, now, job_run_id),
            )
            conn.commit()
        finally:
            conn.close()

    def update_job_progress(self, job_run_id: int, *, stage: str, message: str) -> None:
        now = _utcnow_iso()
        conn = self._connect()
        try:
            conn.execute(
                """
                UPDATE job_runs
                SET current_stage=?, current_message=?, updated_at=?
                WHERE id=?
                """,
                (stage, message, now, job_run_id),
            )
            conn.commit()
        finally:
            conn.close()

    def finish_job_run(
        self,
        job_run_id: int,
        *,
        status: str,
        error_message: str = "",
        session_id: str = "",
    ) -> None:
        now = _utcnow_iso()
        conn = self._connect()
        try:
            conn.execute(
                """
                UPDATE job_runs
                SET status=?, error_message=?, session_id=COALESCE(NULLIF(?, ''), session_id),
                    ended_at=?, updated_at=?
                WHERE id=?
                """,
                (status, error_message, session_id, now, now, job_run_id),
            )
            conn.commit()
        finally:
            conn.close()

    def add_job_log(
        self,
        job_run_id: int,
        *,
        level: str,
        component: str,
        event_type: str,
        message: str,
        extra: dict[str, Any] | None = None,
    ) -> None:
        conn = self._connect()
        try:
            conn.execute(
                """
                INSERT INTO job_logs (job_run_id, ts, level, component, event_type, message, extra_json)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    job_run_id,
                    _utcnow_iso(),
                    level.upper(),
                    component,
                    event_type,
                    message,
                    json.dumps(extra, ensure_ascii=False, default=_json_default)
                    if extra is not None
                    else None,
                ),
            )
            conn.commit()
        finally:
            conn.close()

    def upsert_digest(
        self,
        *,
        payload: dict[str, Any],
        job_run_id: int | None = None,
        source_path: str | None = None,
    ) -> int:
        now = _utcnow_iso()
        schedule_name = str(payload.get("schedule_name", "")).strip() or "(unknown)"
        digest_date = str(payload.get("date", "")).strip() or None
        digest_datetime = str(payload.get("datetime", "")).strip() or None
        summary_text = str(payload.get("digest_summary", "") or "")
        payload_json = json.dumps(
            payload, ensure_ascii=False, indent=2, default=_json_default
        )

        conn = self._connect()
        try:
            if source_path:
                row = conn.execute(
                    "SELECT id FROM digests WHERE source_path=?",
                    (source_path,),
                ).fetchone()
                if row:
                    conn.execute(
                        """
                        UPDATE digests
                        SET schedule_name=?, digest_date=?, digest_datetime=?, summary_text=?, payload_json=?, updated_at=?
                        WHERE id=?
                        """,
                        (
                            schedule_name,
                            digest_date,
                            digest_datetime,
                            summary_text,
                            payload_json,
                            now,
                            int(row["id"]),
                        ),
                    )
                    conn.commit()
                    return int(row["id"])

            cursor = conn.execute(
                """
                INSERT INTO digests (
                    job_run_id, schedule_name, digest_date, digest_datetime,
                    summary_text, payload_json, source_path, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    job_run_id,
                    schedule_name,
                    digest_date,
                    digest_datetime,
                    summary_text,
                    payload_json,
                    source_path,
                    now,
                    now,
                ),
            )
            conn.commit()
            return int(cursor.lastrowid)
        finally:
            conn.close()

    def get_job(self, job_run_id: int) -> dict[str, Any] | None:
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT * FROM job_runs WHERE id=?", (job_run_id,)
            ).fetchone()
            return self._job_row_to_dict(row) if row else None
        finally:
            conn.close()

    def list_jobs(
        self,
        *,
        limit: int = 50,
        status: str = "",
        trigger_type: str = "",
        schedule_name: str = "",
    ) -> list[dict[str, Any]]:
        clauses = []
        params: list[Any] = []
        if status:
            clauses.append("status=?")
            params.append(status)
        if trigger_type:
            clauses.append("trigger_type=?")
            params.append(trigger_type)
        if schedule_name:
            clauses.append("schedule_name=?")
            params.append(schedule_name)
        where_sql = f"WHERE {' AND '.join(clauses)}" if clauses else ""

        conn = self._connect()
        try:
            rows = conn.execute(
                f"SELECT * FROM job_runs {where_sql} ORDER BY created_at DESC LIMIT ?",
                (*params, limit),
            ).fetchall()
            return [self._job_row_to_dict(row) for row in rows]
        finally:
            conn.close()

    def get_latest_running_job(self) -> dict[str, Any] | None:
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT * FROM job_runs WHERE status='running' ORDER BY started_at DESC, id DESC LIMIT 1"
            ).fetchone()
            return self._job_row_to_dict(row) if row else None
        finally:
            conn.close()

    def list_job_logs(
        self, job_run_id: int, *, limit: int = 500
    ) -> list[dict[str, Any]]:
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT * FROM job_logs WHERE job_run_id=? ORDER BY ts ASC, id ASC LIMIT ?",
                (job_run_id, limit),
            ).fetchall()
            return [self._log_row_to_dict(row) for row in rows]
        finally:
            conn.close()

    def get_latest_digest(self) -> dict[str, Any] | None:
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT * FROM digests ORDER BY COALESCE(digest_datetime, created_at) DESC, id DESC LIMIT 1"
            ).fetchone()
            return self._digest_row_to_dict(row) if row else None
        finally:
            conn.close()

    def list_digests(
        self, *, limit: int = 50, schedule_name: str = ""
    ) -> list[dict[str, Any]]:
        conn = self._connect()
        try:
            if schedule_name:
                rows = conn.execute(
                    """
                    SELECT * FROM digests
                    WHERE schedule_name=?
                    ORDER BY COALESCE(digest_datetime, created_at) DESC, id DESC
                    LIMIT ?
                    """,
                    (schedule_name, limit),
                ).fetchall()
            else:
                rows = conn.execute(
                    """
                    SELECT * FROM digests
                    ORDER BY COALESCE(digest_datetime, created_at) DESC, id DESC
                    LIMIT ?
                    """,
                    (limit,),
                ).fetchall()
            return [
                self._digest_row_to_dict(row, include_payload=False) for row in rows
            ]
        finally:
            conn.close()

    def get_digest(self, digest_id: int) -> dict[str, Any] | None:
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT * FROM digests WHERE id=?", (digest_id,)
            ).fetchone()
            return self._digest_row_to_dict(row) if row else None
        finally:
            conn.close()

    def get_digest_for_job(self, job_run_id: int) -> dict[str, Any] | None:
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT * FROM digests WHERE job_run_id=? ORDER BY id DESC LIMIT 1",
                (job_run_id,),
            ).fetchone()
            return self._digest_row_to_dict(row) if row else None
        finally:
            conn.close()

    def replace_items_for_job(
        self,
        *,
        job_run_id: int,
        digest_id: int | None,
        items: list[dict[str, Any]],
    ) -> None:
        collected_at = _utcnow_iso()
        conn = self._connect()
        try:
            conn.execute(
                "DELETE FROM collected_items WHERE job_run_id=?", (job_run_id,)
            )
            for item in items:
                conn.execute(
                    """
                    INSERT INTO collected_items (
                        job_run_id, digest_id, source, external_id, title, url, author, feed_title,
                        language, published_at, collected_at, selected_for_digest, ai_score,
                        ai_summary, ai_reason, raw_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        job_run_id,
                        digest_id,
                        item.get("source", "unknown"),
                        item.get("external_id", "") or None,
                        item.get("title", ""),
                        item.get("url", ""),
                        item.get("author", "") or None,
                        item.get("feed_title", "") or None,
                        item.get("language", "") or None,
                        item.get("published_at", "") or None,
                        collected_at,
                        1 if item.get("selected_for_digest") else 0,
                        item.get("ai_score"),
                        item.get("ai_summary", "") or None,
                        item.get("ai_reason", "") or None,
                        json.dumps(
                            item.get("raw", item),
                            ensure_ascii=False,
                            default=_json_default,
                        ),
                    ),
                )
            conn.commit()
        finally:
            conn.close()

    def list_items(
        self,
        *,
        limit: int = 100,
        keyword: str = "",
        source: str = "",
        time_range: str = "",
        selected_only: bool = False,
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        if keyword:
            keyword_like = f"%{keyword}%"
            clauses.append("(title LIKE ? OR ai_summary LIKE ? OR feed_title LIKE ?)")
            params.extend([keyword_like, keyword_like, keyword_like])
        if source:
            clauses.append("source=?")
            params.append(source)
        if selected_only:
            clauses.append("selected_for_digest=1")
        if time_range in {"1d", "7d", "30d"}:
            days = int(time_range[:-1])
            clauses.append("COALESCE(published_at, collected_at) >= datetime('now', ?)")
            params.append(f"-{days} days")
        where_sql = f"WHERE {' AND '.join(clauses)}" if clauses else ""

        conn = self._connect()
        try:
            rows = conn.execute(
                f"""
                SELECT * FROM collected_items
                {where_sql}
                ORDER BY COALESCE(published_at, collected_at) DESC, id DESC
                LIMIT ?
                """,
                (*params, limit),
            ).fetchall()
            return [self._item_row_to_dict(row) for row in rows]
        finally:
            conn.close()

    def get_item(self, item_id: int) -> dict[str, Any] | None:
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT * FROM collected_items WHERE id=?", (item_id,)
            ).fetchone()
            return self._item_row_to_dict(row) if row else None
        finally:
            conn.close()

    def create_deep_summary(
        self,
        *,
        item_id: int,
        job_run_id: int | None,
        trigger_type: str,
        status: str,
    ) -> int:
        now = _utcnow_iso()
        conn = self._connect()
        try:
            cursor = conn.execute(
                """
                INSERT INTO deep_summaries (
                    item_id, job_run_id, trigger_type, status, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (item_id, job_run_id, trigger_type, status, now, now),
            )
            conn.commit()
            return int(cursor.lastrowid)
        finally:
            conn.close()

    def update_deep_summary(
        self,
        deep_summary_id: int,
        *,
        status: str,
        source_fetch_status: str = "",
        source_content: str = "",
        source_content_meta: dict[str, Any] | None = None,
        deep_summary: str = "",
        model: str = "",
        error_message: str = "",
    ) -> None:
        now = _utcnow_iso()
        conn = self._connect()
        try:
            conn.execute(
                """
                UPDATE deep_summaries
                SET status=?, source_fetch_status=?, source_content=?, source_content_meta_json=?,
                    deep_summary=?, model=?, error_message=?, updated_at=?
                WHERE id=?
                """,
                (
                    status,
                    source_fetch_status or None,
                    source_content or None,
                    json.dumps(
                        source_content_meta, ensure_ascii=False, default=_json_default
                    )
                    if source_content_meta is not None
                    else None,
                    deep_summary or None,
                    model or None,
                    error_message or None,
                    now,
                    deep_summary_id,
                ),
            )
            conn.commit()
        finally:
            conn.close()

    def get_deep_summary(self, deep_summary_id: int) -> dict[str, Any] | None:
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT * FROM deep_summaries WHERE id=?", (deep_summary_id,)
            ).fetchone()
            return self._deep_summary_row_to_dict(row) if row else None
        finally:
            conn.close()

    def get_latest_deep_summary_for_item(self, item_id: int) -> dict[str, Any] | None:
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT * FROM deep_summaries WHERE item_id=? ORDER BY created_at DESC, id DESC LIMIT 1",
                (item_id,),
            ).fetchone()
            return self._deep_summary_row_to_dict(row) if row else None
        finally:
            conn.close()

    def sync_output_archives(self, config: dict) -> int:
        notif_cfg = config.get("notifications", {}).get("file", {})
        data_dir = Path(config.get("storage", {}).get("data_dir", "data"))
        output_dir = data_dir / str(notif_cfg.get("output_dir", "outputs"))
        if not output_dir.exists():
            return 0

        inserted = 0
        for path in sorted(output_dir.glob("digest_*.json")):
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                continue
            if not isinstance(payload, dict):
                continue
            digest_id_before = self._find_digest_id_by_source_path(str(path))
            self.upsert_digest(payload=payload, source_path=str(path))
            if digest_id_before is None:
                inserted += 1
        return inserted

    def _find_digest_id_by_source_path(self, source_path: str) -> int | None:
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT id FROM digests WHERE source_path=?", (source_path,)
            ).fetchone()
            return int(row["id"]) if row else None
        finally:
            conn.close()

    def _job_row_to_dict(self, row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": int(row["id"]),
            "schedule_name": row["schedule_name"],
            "trigger_type": row["trigger_type"],
            "status": row["status"],
            "dry_run": bool(row["dry_run"]),
            "session_id": row["session_id"] or "",
            "current_stage": row["current_stage"] or "",
            "current_message": row["current_message"] or "",
            "error_message": row["error_message"] or "",
            "started_at": row["started_at"] or "",
            "ended_at": row["ended_at"] or "",
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    def _log_row_to_dict(self, row: sqlite3.Row) -> dict[str, Any]:
        extra_json = row["extra_json"]
        extra = None
        if extra_json:
            try:
                extra = json.loads(extra_json)
            except json.JSONDecodeError:
                extra = extra_json
        return {
            "id": int(row["id"]),
            "job_run_id": int(row["job_run_id"]),
            "ts": row["ts"],
            "level": row["level"],
            "component": row["component"],
            "event_type": row["event_type"],
            "message": row["message"],
            "extra": extra,
        }

    def _digest_row_to_dict(
        self, row: sqlite3.Row, *, include_payload: bool = True
    ) -> dict[str, Any]:
        payload: dict[str, Any] | None = None
        if include_payload:
            try:
                payload = json.loads(row["payload_json"])
            except json.JSONDecodeError:
                payload = None
        return {
            "id": int(row["id"]),
            "job_run_id": int(row["job_run_id"])
            if row["job_run_id"] is not None
            else None,
            "schedule_name": row["schedule_name"],
            "digest_date": row["digest_date"] or "",
            "digest_datetime": row["digest_datetime"] or "",
            "summary_text": row["summary_text"] or "",
            "payload": payload,
            "source_path": row["source_path"] or "",
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    def _item_row_to_dict(self, row: sqlite3.Row) -> dict[str, Any]:
        raw: dict[str, Any] | None = None
        try:
            raw = json.loads(row["raw_json"])
        except json.JSONDecodeError:
            raw = None
        return {
            "id": int(row["id"]),
            "job_run_id": int(row["job_run_id"])
            if row["job_run_id"] is not None
            else None,
            "digest_id": int(row["digest_id"])
            if row["digest_id"] is not None
            else None,
            "source": row["source"],
            "external_id": row["external_id"] or "",
            "title": row["title"],
            "url": row["url"],
            "author": row["author"] or "",
            "feed_title": row["feed_title"] or "",
            "language": row["language"] or "",
            "published_at": row["published_at"] or "",
            "collected_at": row["collected_at"],
            "selected_for_digest": bool(row["selected_for_digest"]),
            "ai_score": row["ai_score"],
            "ai_summary": row["ai_summary"] or "",
            "ai_reason": row["ai_reason"] or "",
            "raw": raw,
        }

    def _deep_summary_row_to_dict(self, row: sqlite3.Row) -> dict[str, Any]:
        meta: dict[str, Any] | None = None
        raw_meta = row["source_content_meta_json"]
        if raw_meta:
            try:
                meta = json.loads(raw_meta)
            except json.JSONDecodeError:
                meta = None
        return {
            "id": int(row["id"]),
            "item_id": int(row["item_id"]),
            "job_run_id": int(row["job_run_id"])
            if row["job_run_id"] is not None
            else None,
            "trigger_type": row["trigger_type"],
            "status": row["status"],
            "source_fetch_status": row["source_fetch_status"] or "",
            "source_content": row["source_content"] or "",
            "source_content_meta": meta,
            "deep_summary": row["deep_summary"] or "",
            "model": row["model"] or "",
            "error_message": row["error_message"] or "",
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }
