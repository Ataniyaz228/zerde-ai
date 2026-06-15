"""SQLite-backed jobs store for analysis pipeline runs.

Sync sqlite3 wrapped in asyncio.to_thread; one connection per call.
"""

import asyncio
import json
import sqlite3
import time
from pathlib import Path

from config import settings

_SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    analysis_id TEXT PRIMARY KEY,
    status TEXT NOT NULL,
    filename TEXT NOT NULL,
    stored_path TEXT NOT NULL,
    report_id TEXT,
    score INTEGER,
    error TEXT,
    events_json TEXT NOT NULL DEFAULT '[]',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
"""


def _connect(db_path: Path | str) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.row_factory = sqlite3.Row
    return conn


def _init_db_sync(db_path: Path | str) -> None:
    conn = _connect(db_path)
    try:
        conn.execute(_SCHEMA)
        conn.commit()
    finally:
        conn.close()


async def init_db(db_path: Path | str | None = None) -> None:
    path = db_path if db_path is not None else settings.jobs_db_path
    await asyncio.to_thread(_init_db_sync, path)


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S")


def _create_job_sync(db_path: Path | str, analysis_id: str, filename: str, stored_path: str) -> None:
    conn = _connect(db_path)
    try:
        now = _now()
        conn.execute(
            "INSERT INTO jobs (analysis_id, status, filename, stored_path, events_json, created_at, updated_at) "
            "VALUES (?, 'queued', ?, ?, '[]', ?, ?)",
            (analysis_id, filename, stored_path, now, now),
        )
        conn.commit()
    finally:
        conn.close()


async def create_job(analysis_id: str, filename: str, stored_path: str, db_path: Path | str | None = None) -> None:
    path = db_path if db_path is not None else settings.jobs_db_path
    await asyncio.to_thread(_create_job_sync, path, analysis_id, filename, stored_path)


def _set_status_sync(
    db_path: Path | str,
    analysis_id: str,
    status: str,
    report_id: str | None,
    score: int | None,
    error: str | None,
) -> None:
    conn = _connect(db_path)
    try:
        fields = ["status = ?", "updated_at = ?"]
        params: list = [status, _now()]
        if report_id is not None:
            fields.append("report_id = ?")
            params.append(report_id)
        if score is not None:
            fields.append("score = ?")
            params.append(score)
        if error is not None:
            fields.append("error = ?")
            params.append(error)
        params.append(analysis_id)
        conn.execute(f"UPDATE jobs SET {', '.join(fields)} WHERE analysis_id = ?", params)
        conn.commit()
    finally:
        conn.close()


async def set_status(
    analysis_id: str,
    status: str,
    *,
    report_id: str | None = None,
    score: int | None = None,
    error: str | None = None,
    db_path: Path | str | None = None,
) -> None:
    path = db_path if db_path is not None else settings.jobs_db_path
    await asyncio.to_thread(_set_status_sync, path, analysis_id, status, report_id, score, error)


def _append_event_sync(db_path: Path | str, analysis_id: str, event: dict) -> None:
    conn = _connect(db_path)
    try:
        row = conn.execute("SELECT events_json FROM jobs WHERE analysis_id = ?", (analysis_id,)).fetchone()
        if row is None:
            return
        events = json.loads(row["events_json"])
        events.append(event)
        conn.execute(
            "UPDATE jobs SET events_json = ?, updated_at = ? WHERE analysis_id = ?",
            (json.dumps(events, ensure_ascii=False), _now(), analysis_id),
        )
        conn.commit()
    finally:
        conn.close()


async def append_event(analysis_id: str, event: dict, db_path: Path | str | None = None) -> None:
    path = db_path if db_path is not None else settings.jobs_db_path
    await asyncio.to_thread(_append_event_sync, path, analysis_id, event)


def _get_job_sync(db_path: Path | str, analysis_id: str) -> dict | None:
    conn = _connect(db_path)
    try:
        row = conn.execute("SELECT * FROM jobs WHERE analysis_id = ?", (analysis_id,)).fetchone()
        if row is None:
            return None
        return dict(row)
    finally:
        conn.close()


async def get_job(analysis_id: str, db_path: Path | str | None = None) -> dict | None:
    path = db_path if db_path is not None else settings.jobs_db_path
    return await asyncio.to_thread(_get_job_sync, path, analysis_id)


def _reconcile_interrupted_sync(db_path: Path | str) -> int:
    """Fail any job left 'running'/'queued' by a crash or restart.

    A single-loop backend executes the pipeline in-process, so a restart kills
    the running task without ever emitting a terminal event. Such a job stays
    'running' forever and the frontend's resume logic re-attaches to it,
    showing an endless spinner. On startup we flip these to 'error' and append
    a terminal error event so any (re)connecting client stops waiting.
    """
    conn = _connect(db_path)
    try:
        rows = conn.execute(
            "SELECT analysis_id, events_json FROM jobs WHERE status IN ('running', 'queued')"
        ).fetchall()
        now = _now()
        msg = "Анализ прерван (сервер был перезапущен). Запустите анализ заново."
        for row in rows:
            events = json.loads(row["events_json"])
            events.append({"type": "error", "message": msg})
            conn.execute(
                "UPDATE jobs SET status = 'error', error = ?, events_json = ?, updated_at = ? "
                "WHERE analysis_id = ?",
                (msg, json.dumps(events, ensure_ascii=False), now, row["analysis_id"]),
            )
        conn.commit()
        return len(rows)
    finally:
        conn.close()


async def reconcile_interrupted(db_path: Path | str | None = None) -> int:
    path = db_path if db_path is not None else settings.jobs_db_path
    return await asyncio.to_thread(_reconcile_interrupted_sync, path)


def _get_events_sync(db_path: Path | str, analysis_id: str) -> list[dict]:
    conn = _connect(db_path)
    try:
        row = conn.execute("SELECT events_json FROM jobs WHERE analysis_id = ?", (analysis_id,)).fetchone()
        if row is None:
            return []
        return json.loads(row["events_json"])
    finally:
        conn.close()


async def get_events(analysis_id: str, db_path: Path | str | None = None) -> list[dict]:
    path = db_path if db_path is not None else settings.jobs_db_path
    return await asyncio.to_thread(_get_events_sync, path, analysis_id)
