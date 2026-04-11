from __future__ import annotations

import json
import sqlite3
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from src.ai.dedup import dedup_key_for_item


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _json_default(value: Any) -> str:
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    return str(value)


def _sqlite_datetime_range_expr(column: str) -> str:
    return f"COALESCE(datetime({column}), datetime(collected_at))"


def _item_source_name_expr(alias: str = "r") -> str:
    return (
        f"COALESCE(NULLIF({alias}.feed_title, ''), "
        f"NULLIF({alias}.author, ''), "
        f"NULLIF({alias}.external_id, ''), "
        f"NULLIF({alias}.title, ''), "
        f"{alias}.url)"
    )


def _make_dedup_key(source: str, url: str, title: str = "", **extra: Any) -> str:
    return dedup_key_for_item(
        {
            "source": source,
            "url": url,
            "title": title,
            **extra,
        }
    )


class AppStateStore:
    def __init__(self, db_path: Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)

    @classmethod
    def from_config(cls, config: dict) -> "AppStateStore":
        data_dir = Path(config.get("storage", {}).get("data_dir", "data"))
        return cls(data_dir / "app.db")

    def _connect(self) -> sqlite3.Connection:
        # busy_timeout (set via PRAGMA below) handles lock contention at the
        # SQLite C level; no need for the redundant Python-level timeout arg.
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")
        conn.execute("PRAGMA foreign_keys = ON")
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
                    worker_id       TEXT,
                    claimed_at      TEXT,
                    heartbeat_at    TEXT,
                    lease_expires_at TEXT,
                    scheduled_for   TEXT,
                    attempt         INTEGER NOT NULL DEFAULT 1,
                    idempotency_key TEXT,
                    final_reason    TEXT,
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

                CREATE TABLE IF NOT EXISTS raw_items (
                    id              INTEGER PRIMARY KEY AUTOINCREMENT,
                    source          TEXT NOT NULL,
                    url             TEXT NOT NULL,
                    title           TEXT NOT NULL,
                    translated_title TEXT,
                    dedup_key       TEXT NOT NULL,
                    external_id     TEXT,
                    author          TEXT,
                    feed_title      TEXT,
                    language        TEXT,
                    published_at    TEXT,
                    first_seen_at   TEXT NOT NULL,
                    last_seen_at    TEXT NOT NULL,
                    seen_count      INTEGER NOT NULL DEFAULT 1,
                    raw_json        TEXT NOT NULL
                );

                CREATE UNIQUE INDEX IF NOT EXISTS idx_raw_items_dedup
                    ON raw_items(dedup_key);

                CREATE INDEX IF NOT EXISTS idx_raw_items_source_time
                    ON raw_items(source, published_at DESC, id DESC);

                CREATE TABLE IF NOT EXISTS item_annotations (
                    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
                    raw_item_id         INTEGER NOT NULL,
                    job_run_id          INTEGER NOT NULL,
                    digest_id           INTEGER,
                    selected_for_digest INTEGER NOT NULL DEFAULT 0,
                    ai_score            INTEGER,
                    ai_summary          TEXT,
                    ai_reason           TEXT,
                    created_at          TEXT NOT NULL,
                    FOREIGN KEY(raw_item_id) REFERENCES raw_items(id),
                    FOREIGN KEY(job_run_id) REFERENCES job_runs(id),
                    FOREIGN KEY(digest_id) REFERENCES digests(id)
                );

                CREATE INDEX IF NOT EXISTS idx_item_annotations_raw
                    ON item_annotations(raw_item_id, job_run_id);

                CREATE INDEX IF NOT EXISTS idx_item_annotations_job
                    ON item_annotations(job_run_id);

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
                    FOREIGN KEY(item_id) REFERENCES raw_items(id),
                    FOREIGN KEY(job_run_id) REFERENCES job_runs(id)
                );

                CREATE INDEX IF NOT EXISTS idx_deep_summaries_item
                    ON deep_summaries(item_id, created_at DESC);
                """
            )
            self._ensure_job_runs_columns(conn)
            self._ensure_raw_items_columns(conn)
            self._migrate_collected_items_to_raw(conn)
            conn.executescript(
                """
                CREATE INDEX IF NOT EXISTS idx_job_runs_lease
                    ON job_runs(status, lease_expires_at DESC);

                CREATE UNIQUE INDEX IF NOT EXISTS idx_job_runs_idempotency
                    ON job_runs(idempotency_key)
                    WHERE idempotency_key IS NOT NULL AND idempotency_key != '';
                """
            )
            conn.commit()
        finally:
            conn.close()

    def _ensure_job_runs_columns(self, conn: sqlite3.Connection) -> None:
        migrations = [
            "ALTER TABLE job_runs ADD COLUMN worker_id TEXT",
            "ALTER TABLE job_runs ADD COLUMN claimed_at TEXT",
            "ALTER TABLE job_runs ADD COLUMN heartbeat_at TEXT",
            "ALTER TABLE job_runs ADD COLUMN lease_expires_at TEXT",
            "ALTER TABLE job_runs ADD COLUMN scheduled_for TEXT",
            "ALTER TABLE job_runs ADD COLUMN attempt INTEGER NOT NULL DEFAULT 1",
            "ALTER TABLE job_runs ADD COLUMN idempotency_key TEXT",
            "ALTER TABLE job_runs ADD COLUMN final_reason TEXT",
        ]
        for sql in migrations:
            try:
                conn.execute(sql)
            except sqlite3.OperationalError:
                pass

    def _ensure_raw_items_columns(self, conn: sqlite3.Connection) -> None:
        migrations = ["ALTER TABLE raw_items ADD COLUMN translated_title TEXT"]
        for sql in migrations:
            try:
                conn.execute(sql)
            except sqlite3.OperationalError:
                pass

    def _migrate_collected_items_to_raw(self, conn: sqlite3.Connection) -> None:
        """One-time migration: move collected_items data into raw_items + item_annotations.

        This is idempotent: it checks whether collected_items exists and has rows
        that haven't been migrated yet. After migration the old table is renamed to
        _collected_items_backup and deep_summaries.item_id is updated to point at
        the new raw_items rows.
        """
        # Check if collected_items still exists (migration not yet done)
        row = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='collected_items'"
        ).fetchone()
        if row is None:
            return  # already migrated

        now = _utcnow_iso()

        # Migrate raw_items: one row per unique dedup_key, oldest first_seen_at wins
        conn.execute(
            """
            INSERT OR IGNORE INTO raw_items (
                source, url, title, translated_title, dedup_key, external_id, author, feed_title,
                language, published_at, first_seen_at, last_seen_at, seen_count, raw_json
            )
            SELECT
                source, url, title, NULL,
                CASE
                    WHEN lower(source) = 'youtube' AND COALESCE(external_id, '') != '' THEN 'youtube::' || external_id
                    WHEN lower(source) = 'github' AND instr(lower(title), '/') > 0 THEN 'github::' || lower(title)
                    WHEN COALESCE(url, '') != '' THEN lower(url)
                    ELSE lower(source) || '::' || lower(title)
                END AS dedup_key,
                external_id, author, feed_title, language, published_at,
                MIN(collected_at) AS first_seen_at,
                MAX(collected_at) AS last_seen_at,
                COUNT(*) AS seen_count,
                raw_json
            FROM collected_items
            GROUP BY CASE
                WHEN lower(source) = 'youtube' AND COALESCE(external_id, '') != '' THEN 'youtube::' || external_id
                WHEN lower(source) = 'github' AND instr(lower(title), '/') > 0 THEN 'github::' || lower(title)
                WHEN COALESCE(url, '') != '' THEN lower(url)
                ELSE lower(source) || '::' || lower(title)
            END
            """
        )

        # Migrate item_annotations: one row per collected_items row
        conn.execute(
            """
            INSERT INTO item_annotations (
                raw_item_id, job_run_id, digest_id,
                selected_for_digest, ai_score, ai_summary, ai_reason, created_at
            )
            SELECT
                r.id, c.job_run_id, c.digest_id,
                c.selected_for_digest, c.ai_score, c.ai_summary, c.ai_reason, c.collected_at
            FROM collected_items c
            JOIN raw_items r
              ON r.dedup_key = CASE
                  WHEN lower(c.source) = 'youtube' AND COALESCE(c.external_id, '') != '' THEN 'youtube::' || c.external_id
                  WHEN lower(c.source) = 'github' AND instr(lower(c.title), '/') > 0 THEN 'github::' || lower(c.title)
                  WHEN COALESCE(c.url, '') != '' THEN lower(c.url)
                  ELSE lower(c.source) || '::' || lower(c.title)
              END
            WHERE NOT EXISTS (
                SELECT 1 FROM item_annotations ia
                WHERE ia.raw_item_id = r.id AND ia.job_run_id = c.job_run_id
            )
            """
        )

        # Migrate deep_summaries.item_id: old collected_items.id → new raw_items.id
        # We rebuild deep_summaries into a new table because SQLite can't ALTER FK.
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS deep_summaries_new (
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
                FOREIGN KEY(item_id) REFERENCES raw_items(id),
                FOREIGN KEY(job_run_id) REFERENCES job_runs(id)
            )
            """
        )
        conn.execute(
            """
            INSERT INTO deep_summaries_new
            SELECT
                ds.id,
                COALESCE(r.id, ds.item_id),
                ds.job_run_id, ds.trigger_type, ds.status,
                ds.source_fetch_status, ds.source_content, ds.source_content_meta_json,
                ds.deep_summary, ds.model, ds.error_message,
                ds.created_at, ds.updated_at
            FROM deep_summaries ds
            LEFT JOIN collected_items c ON c.id = ds.item_id
            LEFT JOIN raw_items r ON r.dedup_key = CASE
                WHEN lower(c.source) = 'youtube' AND COALESCE(c.external_id, '') != '' THEN 'youtube::' || c.external_id
                WHEN lower(c.source) = 'github' AND instr(lower(c.title), '/') > 0 THEN 'github::' || lower(c.title)
                WHEN COALESCE(c.url, '') != '' THEN lower(c.url)
                ELSE lower(c.source) || '::' || lower(c.title)
            END
            """
        )
        conn.execute("DROP TABLE deep_summaries")
        conn.execute("ALTER TABLE deep_summaries_new RENAME TO deep_summaries")
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_deep_summaries_item ON deep_summaries(item_id, created_at DESC)"
        )

        # Backup old table
        conn.execute("ALTER TABLE collected_items RENAME TO _collected_items_backup")

    def create_job_run(
        self,
        *,
        schedule_name: str,
        trigger_type: str,
        dry_run: bool,
        status: str = "queued",
        scheduled_for: str = "",
        idempotency_key: str = "",
    ) -> int:
        now = _utcnow_iso()
        conn = self._connect()
        try:
            cursor = conn.execute(
                """
                INSERT INTO job_runs (
                    schedule_name, trigger_type, status, dry_run, scheduled_for,
                    idempotency_key, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    schedule_name,
                    trigger_type,
                    status,
                    1 if dry_run else 0,
                    scheduled_for or None,
                    idempotency_key or None,
                    now,
                    now,
                ),
            )
            conn.commit()
            return int(cursor.lastrowid)
        finally:
            conn.close()

    def mark_job_running(
        self,
        job_run_id: int,
        *,
        stage: str,
        message: str,
        worker_id: str = "",
        lease_seconds: int = 45,
    ) -> None:
        now = _utcnow_iso()
        lease_expires_at = (
            datetime.now(timezone.utc) + timedelta(seconds=max(int(lease_seconds), 1))
        ).isoformat()
        conn = self._connect()
        try:
            conn.execute(
                """
                UPDATE job_runs
                SET status='running', current_stage=?, current_message=?,
                    worker_id=COALESCE(NULLIF(?, ''), worker_id),
                    claimed_at=COALESCE(claimed_at, ?),
                    heartbeat_at=?, lease_expires_at=?,
                    started_at=COALESCE(started_at, ?), updated_at=?
                WHERE id=?
                """,
                (
                    stage,
                    message,
                    worker_id,
                    now,
                    now,
                    lease_expires_at,
                    now,
                    now,
                    job_run_id,
                ),
            )
            conn.commit()
        finally:
            conn.close()

    def heartbeat_job_run(self, job_run_id: int, *, lease_seconds: int = 45) -> None:
        now = _utcnow_iso()
        lease_expires_at = (
            datetime.now(timezone.utc) + timedelta(seconds=max(int(lease_seconds), 1))
        ).isoformat()
        conn = self._connect()
        try:
            conn.execute(
                """
                UPDATE job_runs
                SET heartbeat_at=?, lease_expires_at=?, updated_at=?
                WHERE id=? AND status='running'
                """,
                (now, lease_expires_at, now, job_run_id),
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
        final_reason: str = "",
    ) -> None:
        now = _utcnow_iso()
        conn = self._connect()
        try:
            conn.execute(
                """
                UPDATE job_runs
                SET status=?, error_message=?, session_id=COALESCE(NULLIF(?, ''), session_id),
                    lease_expires_at=NULL,
                    final_reason=COALESCE(NULLIF(?, ''), final_reason),
                    ended_at=?, updated_at=?
                WHERE id=?
                """,
                (status, error_message, session_id, final_reason, now, now, job_run_id),
            )
            conn.commit()
        finally:
            conn.close()

    def recover_stale_job_runs(self) -> int:
        now = _utcnow_iso()
        conn = self._connect()
        try:
            cursor = conn.execute(
                """
                UPDATE job_runs
                SET status='lost',
                    error_message=COALESCE(NULLIF(error_message, ''), 'Worker heartbeat expired before the job finished.'),
                    current_message=COALESCE(NULLIF(current_message, ''), 'Worker heartbeat expired'),
                    lease_expires_at=NULL,
                    final_reason=COALESCE(NULLIF(final_reason, ''), 'worker_lost'),
                    ended_at=COALESCE(ended_at, ?),
                    updated_at=?
                WHERE status='running' AND (lease_expires_at IS NULL OR lease_expires_at <= ?)
                """,
                (now, now, now),
            )
            conn.commit()
            return max(int(cursor.rowcount), 0)
        finally:
            conn.close()

    def claim_next_job_run(
        self,
        *,
        worker_id: str,
        lease_seconds: int = 45,
        trigger_types: tuple[str, ...] = ("manual", "cron"),
    ) -> dict[str, Any] | None:
        self.recover_stale_job_runs()
        placeholders = ", ".join("?" for _ in trigger_types)
        now = _utcnow_iso()
        lease_expires_at = (
            datetime.now(timezone.utc) + timedelta(seconds=max(int(lease_seconds), 1))
        ).isoformat()

        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                f"""
                SELECT id FROM job_runs
                WHERE status='queued' AND trigger_type IN ({placeholders})
                ORDER BY created_at ASC, id ASC
                LIMIT 1
                """,
                trigger_types,
            ).fetchone()
            if row is None:
                conn.commit()
                return None

            job_run_id = int(row["id"])
            updated = conn.execute(
                """
                UPDATE job_runs
                SET status='running', worker_id=?, claimed_at=COALESCE(claimed_at, ?),
                    heartbeat_at=?, lease_expires_at=?,
                    current_stage=COALESCE(NULLIF(current_stage, ''), 'boot'),
                    current_message=COALESCE(NULLIF(current_message, ''), 'Worker claimed queued job'),
                    started_at=COALESCE(started_at, ?), updated_at=?
                WHERE id=? AND status='queued'
                """,
                (
                    worker_id,
                    now,
                    now,
                    lease_expires_at,
                    now,
                    now,
                    job_run_id,
                ),
            )
            if updated.rowcount != 1:
                conn.rollback()
                return None

            claimed_row = conn.execute(
                "SELECT * FROM job_runs WHERE id=?", (job_run_id,)
            ).fetchone()
            conn.commit()
            return self._job_row_to_dict(claimed_row) if claimed_row else None
        finally:
            conn.close()

    def get_job_by_idempotency_key(self, idempotency_key: str) -> dict[str, Any] | None:
        if not idempotency_key:
            return None
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT * FROM job_runs WHERE idempotency_key=?",
                (idempotency_key,),
            ).fetchone()
            return self._job_row_to_dict(row) if row else None
        finally:
            conn.close()

    def get_latest_scheduled_for(self, *, schedule_name: str, trigger_type: str) -> str:
        conn = self._connect()
        try:
            row = conn.execute(
                """
                SELECT scheduled_for FROM job_runs
                WHERE schedule_name=? AND trigger_type=? AND scheduled_for IS NOT NULL
                ORDER BY scheduled_for DESC, id DESC LIMIT 1
                """,
                (schedule_name, trigger_type),
            ).fetchone()
            return str(row["scheduled_for"] or "") if row else ""
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
        self.recover_stale_job_runs()
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
        self.recover_stale_job_runs()
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
                f"SELECT * FROM job_runs {where_sql} ORDER BY COALESCE(updated_at, created_at) DESC, id DESC LIMIT ?",
                (*params, limit),
            ).fetchall()
            return [self._job_row_to_dict(row) for row in rows]
        finally:
            conn.close()

    def count_jobs(
        self,
        *,
        status: str = "",
        trigger_type: str = "",
        schedule_name: str = "",
    ) -> int:
        self.recover_stale_job_runs()
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
            row = conn.execute(
                f"SELECT COUNT(*) AS count FROM job_runs {where_sql}", params
            ).fetchone()
            return int(row["count"] or 0) if row else 0
        finally:
            conn.close()

    def get_latest_running_job(self) -> dict[str, Any] | None:
        self.recover_stale_job_runs()
        now = _utcnow_iso()
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT * FROM job_runs WHERE status='running' AND lease_expires_at > ? ORDER BY started_at DESC, id DESC LIMIT 1",
                (now,),
            ).fetchone()
            return self._job_row_to_dict(row) if row else None
        finally:
            conn.close()

    def get_running_job_for_schedule(self, schedule_name: str) -> dict[str, Any] | None:
        """Return the most recent queued or running job for a given schedule, or None."""
        self.recover_stale_job_runs()
        now = _utcnow_iso()
        conn = self._connect()
        try:
            row = conn.execute(
                """
                SELECT * FROM job_runs
                WHERE schedule_name=?
                  AND (
                    status='queued'
                    OR (status='running' AND lease_expires_at > ?)
                  )
                ORDER BY created_at DESC, id DESC LIMIT 1
                """,
                (schedule_name, now),
            ).fetchone()
            return self._job_row_to_dict(row) if row else None
        finally:
            conn.close()

    def get_eligible_items_for_auto_deep_summary(
        self,
        *,
        job_run_id: int,
        score_threshold: int,
        exclude_sources: list[str],
        limit: int,
    ) -> list[dict[str, Any]]:
        """Return selected digest items from a run that qualify for auto deep summary.

        Eligibility: selected_for_digest=1, ai_score >= threshold, not in exclude_sources,
        and no existing succeeded deep_summary record for the same item.
        """
        placeholders = ",".join("?" * len(exclude_sources)) if exclude_sources else None
        exclude_clause = f"AND r.source NOT IN ({placeholders})" if placeholders else ""
        conn = self._connect()
        try:
            rows = conn.execute(
                f"""
                SELECT
                    r.*,
                    ia.id         AS ann_id,
                    ia.job_run_id AS ann_job_run_id,
                    ia.digest_id  AS ann_digest_id,
                    ia.selected_for_digest,
                    ia.ai_score,
                    ia.ai_summary,
                    ia.ai_reason,
                    ia.created_at AS ann_created_at
                FROM item_annotations ia
                JOIN raw_items r ON r.id = ia.raw_item_id
                WHERE ia.job_run_id = ?
                  AND ia.selected_for_digest = 1
                  AND ia.ai_score >= ?
                  {exclude_clause}
                  AND NOT EXISTS (
                      SELECT 1 FROM deep_summaries ds
                      WHERE ds.item_id = r.id AND ds.status = 'succeeded'
                  )
                ORDER BY ia.ai_score DESC, r.id ASC
                LIMIT ?
                """,
                (
                    job_run_id,
                    score_threshold,
                    *([s for s in exclude_sources] if exclude_sources else []),
                    limit,
                ),
            ).fetchall()
            return [self._raw_item_row_to_dict(row) for row in rows]
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
                "SELECT * FROM digests ORDER BY created_at DESC, id DESC LIMIT 1"
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

    def count_digests(self, *, schedule_name: str = "") -> int:
        conn = self._connect()
        try:
            if schedule_name:
                row = conn.execute(
                    "SELECT COUNT(*) AS count FROM digests WHERE schedule_name=?",
                    (schedule_name,),
                ).fetchone()
            else:
                row = conn.execute("SELECT COUNT(*) AS count FROM digests").fetchone()
            return int(row["count"] or 0) if row else 0
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

    def upsert_raw_items(self, items: list[dict[str, Any]]) -> list[int]:
        """Insert or update raw_items by dedup_key. Returns list of raw_item ids in input order."""
        now = _utcnow_iso()
        conn = self._connect()
        ids: list[int] = []
        try:
            for item in items:
                source = str(item.get("source", "unknown")).strip().lower()
                url = str(item.get("url", "")).strip()
                title = str(item.get("title", "")).strip()
                translated_title = str(item.get("translated_title", "")).strip()
                dedup_key = _make_dedup_key(
                    source,
                    url,
                    title,
                    video_id=item.get("video_id", ""),
                    external_id=item.get("external_id", ""),
                    repo_full_name=item.get("repo_full_name", ""),
                )
                raw_json = json.dumps(
                    item.get("raw", item), ensure_ascii=False, default=_json_default
                )
                conn.execute(
                    """
                    INSERT INTO raw_items (
                        source, url, title, translated_title, dedup_key, external_id, author, feed_title,
                        language, published_at, first_seen_at, last_seen_at, seen_count, raw_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?)
                    ON CONFLICT(dedup_key) DO UPDATE SET
                        translated_title = COALESCE(excluded.translated_title, raw_items.translated_title),
                        last_seen_at = excluded.last_seen_at,
                        seen_count   = seen_count + 1,
                        raw_json     = excluded.raw_json
                    """,
                    (
                        source,
                        url,
                        title,
                        translated_title or None,
                        dedup_key,
                        item.get("external_id", "") or None,
                        item.get("author", "") or None,
                        item.get("feed_title", "") or None,
                        item.get("language", "") or None,
                        item.get("published_at", "") or None,
                        now,
                        now,
                        raw_json,
                    ),
                )
                row = conn.execute(
                    "SELECT id FROM raw_items WHERE dedup_key=?", (dedup_key,)
                ).fetchone()
                ids.append(int(row["id"]) if row else 0)
            conn.commit()
        finally:
            conn.close()
        return ids

    def list_raw_items_missing_translation(
        self, limit: int = 100
    ) -> list[dict[str, Any]]:
        conn = self._connect()
        try:
            rows = conn.execute(
                """
                SELECT *
                FROM raw_items
                WHERE source != 'github'
                  AND COALESCE(translated_title, '') = ''
                ORDER BY id ASC
                LIMIT ?
                """,
                (max(1, int(limit)),),
            ).fetchall()
            return [self._raw_item_row_to_dict(row) for row in rows]
        finally:
            conn.close()

    def replace_annotations_for_job(
        self,
        *,
        job_run_id: int,
        digest_id: int | None,
        annotations: list[dict[str, Any]],
    ) -> None:
        """Store per-run AI annotations. Replaces any existing annotations for this job_run_id."""
        now = _utcnow_iso()
        conn = self._connect()
        try:
            conn.execute(
                "DELETE FROM item_annotations WHERE job_run_id=?", (job_run_id,)
            )
            for ann in annotations:
                conn.execute(
                    """
                    INSERT INTO item_annotations (
                        raw_item_id, job_run_id, digest_id,
                        selected_for_digest, ai_score, ai_summary, ai_reason, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        ann["raw_item_id"],
                        job_run_id,
                        digest_id,
                        1 if ann.get("selected_for_digest") else 0,
                        ann.get("ai_score"),
                        ann.get("ai_summary", "") or None,
                        ann.get("ai_reason", "") or None,
                        now,
                    ),
                )
            conn.commit()
        finally:
            conn.close()

    def get_annotated_dedup_keys(self) -> set[str]:
        """Return dedup_keys for all raw_items that have at least one AI annotation (ai_score IS NOT NULL)."""
        conn = self._connect()
        try:
            rows = conn.execute(
                """
                SELECT DISTINCT r.dedup_key, r.source, r.url, r.title, r.external_id
                FROM raw_items r
                JOIN item_annotations ia ON ia.raw_item_id = r.id
                WHERE ia.ai_score IS NOT NULL
                """
            ).fetchall()
            keys: set[str] = set()
            for row in rows:
                keys.add(row["dedup_key"])
                keys.add(
                    dedup_key_for_item(
                        {
                            "source": row["source"],
                            "url": row["url"],
                            "title": row["title"],
                            "external_id": row["external_id"],
                        }
                    )
                )
            return keys
        finally:
            conn.close()

    def get_selected_dedup_keys(self) -> set[str]:
        """Return dedup_keys for all raw_items that have ever been selected for a digest."""
        conn = self._connect()
        try:
            rows = conn.execute(
                """
                SELECT DISTINCT r.dedup_key, r.source, r.url, r.title, r.external_id
                FROM raw_items r
                JOIN item_annotations ia ON ia.raw_item_id = r.id
                WHERE ia.selected_for_digest = 1
                """
            ).fetchall()
            keys: set[str] = set()
            for row in rows:
                keys.add(row["dedup_key"])
                keys.add(
                    dedup_key_for_item(
                        {
                            "source": row["source"],
                            "url": row["url"],
                            "title": row["title"],
                            "external_id": row["external_id"],
                        }
                    )
                )
            return keys
        finally:
            conn.close()

    def replace_items_for_job(
        self,
        *,
        job_run_id: int,
        digest_id: int | None,
        items: list[dict[str, Any]],
    ) -> None:
        """Compatibility shim: upserts raw_items and replaces annotations for this job run.

        Callers (tests, older code paths) can keep using this signature unchanged.
        """
        raw_ids = self.upsert_raw_items(items)
        annotations = []
        for item, raw_id in zip(items, raw_ids):
            if raw_id:
                annotations.append(
                    {
                        "raw_item_id": raw_id,
                        "selected_for_digest": item.get("selected_for_digest", False),
                        "ai_score": item.get("ai_score"),
                        "ai_summary": item.get("ai_summary", ""),
                        "ai_reason": item.get("ai_reason", ""),
                    }
                )
        self.replace_annotations_for_job(
            job_run_id=job_run_id, digest_id=digest_id, annotations=annotations
        )

    def list_items(
        self,
        *,
        limit: int = 100,
        offset: int = 0,
        keyword: str = "",
        source: str = "",
        source_name: str = "",
        time_range: str = "",
        selected_only: bool = False,
    ) -> list[dict[str, Any]]:
        """List raw_items joined with their most recent annotation."""
        where_sql, params, time_expr = self._build_item_filters(
            keyword=keyword,
            source=source,
            source_name=source_name,
            time_range=time_range,
            selected_only=selected_only,
        )

        conn = self._connect()
        try:
            rows = conn.execute(
                f"""
                SELECT
                    r.*,
                    ia.id         AS ann_id,
                    ia.job_run_id AS ann_job_run_id,
                    ia.digest_id  AS ann_digest_id,
                    ia.selected_for_digest,
                    ia.ai_score,
                    ia.ai_summary,
                    ia.ai_reason,
                    ia.created_at AS ann_created_at
                FROM raw_items r
                LEFT JOIN item_annotations ia ON ia.id = (
                    SELECT id FROM item_annotations
                    WHERE raw_item_id = r.id
                    ORDER BY created_at DESC, id DESC
                    LIMIT 1
                )
                {where_sql}
                ORDER BY {time_expr} DESC, r.id DESC
                LIMIT ? OFFSET ?
                """,
                (*params, limit, offset),
            ).fetchall()
            return [self._raw_item_row_to_dict(row) for row in rows]
        finally:
            conn.close()

    def count_items(
        self,
        *,
        keyword: str = "",
        source: str = "",
        source_name: str = "",
        time_range: str = "",
        selected_only: bool = False,
    ) -> int:
        where_sql, params, _time_expr = self._build_item_filters(
            keyword=keyword,
            source=source,
            source_name=source_name,
            time_range=time_range,
            selected_only=selected_only,
        )

        conn = self._connect()
        try:
            row = conn.execute(
                f"""
                SELECT COUNT(*) AS count
                FROM raw_items r
                LEFT JOIN item_annotations ia ON ia.id = (
                    SELECT id FROM item_annotations
                    WHERE raw_item_id = r.id
                    ORDER BY created_at DESC, id DESC
                    LIMIT 1
                )
                {where_sql}
                """,
                params,
            ).fetchone()
            return int(row["count"] or 0) if row else 0
        finally:
            conn.close()

    def list_item_sources(self) -> list[str]:
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT DISTINCT source FROM raw_items WHERE COALESCE(source, '') != '' ORDER BY source ASC"
            ).fetchall()
            return [str(row["source"]) for row in rows if row["source"]]
        finally:
            conn.close()

    def list_item_source_names(self, *, source: str = "") -> list[str]:
        clauses = [f"COALESCE({_item_source_name_expr()}, '') != ''"]
        params: list[Any] = []
        normalized_source = str(source or "").strip().lower()
        if normalized_source:
            clauses.append("LOWER(r.source)=?")
            params.append(normalized_source)
        where_sql = f"WHERE {' AND '.join(clauses)}"

        conn = self._connect()
        try:
            rows = conn.execute(
                f"""
                SELECT DISTINCT {_item_source_name_expr()} AS source_name
                FROM raw_items r
                {where_sql}
                ORDER BY LOWER(source_name) ASC
                """,
                params,
            ).fetchall()
            return [str(row["source_name"]) for row in rows if row["source_name"]]
        finally:
            conn.close()

    def _build_item_filters(
        self,
        *,
        keyword: str = "",
        source: str = "",
        source_name: str = "",
        time_range: str = "",
        selected_only: bool = False,
    ) -> tuple[str, list[Any], str]:
        clauses: list[str] = []
        params: list[Any] = []
        time_expr = "COALESCE(datetime(r.published_at), datetime(r.first_seen_at))"
        source_name_expr = _item_source_name_expr()

        normalized_keyword = str(keyword or "").strip()
        normalized_source = str(source or "").strip().lower()
        normalized_source_name = str(source_name or "").strip().lower()

        if normalized_keyword:
            keyword_like = f"%{normalized_keyword}%"
            clauses.append(
                "(r.title LIKE ? OR r.translated_title LIKE ? OR ia.ai_summary LIKE ? OR r.feed_title LIKE ?)"
            )
            params.extend([keyword_like, keyword_like, keyword_like, keyword_like])
        if normalized_source:
            clauses.append("LOWER(r.source)=?")
            params.append(normalized_source)
        if normalized_source_name:
            clauses.append(f"LOWER({source_name_expr})=?")
            params.append(normalized_source_name)
        if selected_only:
            clauses.append("ia.selected_for_digest=1")
        if time_range in {"1d", "7d", "30d"}:
            days = int(time_range[:-1])
            cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).replace(
                microsecond=0
            )
            clauses.append(f"{time_expr} >= datetime(?)")
            params.append(cutoff.isoformat())

        where_sql = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        return where_sql, params, time_expr

    def get_url_to_item_id_map(self, job_run_id: int) -> dict[str, int]:
        """Return {url: raw_item_id} for all items annotated in a given job run."""
        conn = self._connect()
        try:
            rows = conn.execute(
                """
                SELECT r.id, r.url
                FROM raw_items r
                JOIN item_annotations ia ON ia.raw_item_id = r.id
                WHERE ia.job_run_id=?
                """,
                (job_run_id,),
            ).fetchall()
            return {row["url"]: int(row["id"]) for row in rows if row["url"]}
        finally:
            conn.close()

    def get_item(self, item_id: int) -> dict[str, Any] | None:
        """Fetch a raw_item with its latest annotation by raw_items.id."""
        conn = self._connect()
        try:
            row = conn.execute(
                """
                SELECT
                    r.*,
                    ia.id         AS ann_id,
                    ia.job_run_id AS ann_job_run_id,
                    ia.digest_id  AS ann_digest_id,
                    ia.selected_for_digest,
                    ia.ai_score,
                    ia.ai_summary,
                    ia.ai_reason,
                    ia.created_at AS ann_created_at
                FROM raw_items r
                LEFT JOIN item_annotations ia ON ia.id = (
                    SELECT id FROM item_annotations
                    WHERE raw_item_id = r.id
                    ORDER BY created_at DESC, id DESC
                    LIMIT 1
                )
                WHERE r.id=?
                """,
                (item_id,),
            ).fetchone()
            return self._raw_item_row_to_dict(row) if row else None
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
            "worker_id": row["worker_id"] or "",
            "claimed_at": row["claimed_at"] or "",
            "heartbeat_at": row["heartbeat_at"] or "",
            "lease_expires_at": row["lease_expires_at"] or "",
            "scheduled_for": row["scheduled_for"] or "",
            "attempt": int(row["attempt"] or 1),
            "idempotency_key": row["idempotency_key"] or "",
            "final_reason": row["final_reason"] or "",
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

    def _raw_item_row_to_dict(self, row: sqlite3.Row) -> dict[str, Any]:
        """Convert a raw_items row (optionally joined with item_annotations) to dict.

        The output shape is kept compatible with the old _item_row_to_dict so that
        templates and callers don't need changes.
        """
        raw: dict[str, Any] | None = None
        try:
            raw = json.loads(row["raw_json"])
        except (json.JSONDecodeError, IndexError):
            raw = None

        # Safely read annotation columns that may not be present in every query
        def _col(name: str, default: Any = None) -> Any:
            try:
                return row[name]
            except IndexError:
                return default

        job_run_id = _col("ann_job_run_id") or _col("job_run_id")
        digest_id = _col("ann_digest_id") or _col("digest_id")
        translated_title = _col("translated_title") or ""
        if not translated_title and isinstance(raw, dict):
            translated_title = str(raw.get("translated_title") or "")

        return {
            "id": int(row["id"]),
            "job_run_id": int(job_run_id) if job_run_id is not None else None,
            "digest_id": int(digest_id) if digest_id is not None else None,
            "source": row["source"],
            "external_id": row["external_id"] or "",
            "title": row["title"],
            "translated_title": translated_title,
            "url": row["url"],
            "author": row["author"] or "",
            "feed_title": row["feed_title"] or "",
            "source_name": (
                row["feed_title"]
                or row["author"]
                or row["external_id"]
                or row["title"]
                or row["url"]
            ),
            "language": row["language"] or "",
            "published_at": row["published_at"] or "",
            # Expose first_seen_at as collected_at for template compatibility
            "collected_at": row["first_seen_at"],
            "selected_for_digest": bool(_col("selected_for_digest") or 0),
            "ai_score": _col("ai_score"),
            "ai_summary": _col("ai_summary") or "",
            "ai_reason": _col("ai_reason") or "",
            "raw": raw,
        }

    # Keep old name as alias so any remaining callers continue to work
    _item_row_to_dict = _raw_item_row_to_dict

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
