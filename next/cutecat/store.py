"""SQLite persistence for jobs, logs, presets and settings.

Uses only the standard-library ``sqlite3`` module. The connection is opened
with ``check_same_thread=False`` and guarded by a lock, which is sufficient for
the single-process worker model used here. The database is intended to live on
local storage (never on SMB/NFS).

Schema versioning is tracked in ``settings`` under ``schema_version`` so future
changes can migrate in place. This database is independent from the legacy
Rust service's database; nothing from the old schema is dropped or migrated.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 1

JOB_STATUSES = {
    "queued", "probing", "running", "finalizing",
    "succeeded", "failed", "canceled", "interrupted",
}
ACTIVE_STATUSES = {"probing", "running", "finalizing"}
TERMINAL_STATUSES = {"succeeded", "failed", "canceled", "interrupted"}

_SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id              TEXT PRIMARY KEY,
    input_root      TEXT NOT NULL,
    input_path      TEXT NOT NULL,
    output_root     TEXT NOT NULL,
    output_path     TEXT NOT NULL,
    preset_id       TEXT NOT NULL,
    preset_name     TEXT,
    container       TEXT,
    status          TEXT NOT NULL,
    progress        REAL NOT NULL DEFAULT 0,
    speed           TEXT,
    eta_seconds     INTEGER,
    duration_seconds REAL,
    title_count     INTEGER,
    spec_json       TEXT NOT NULL,
    args_json       TEXT NOT NULL DEFAULT '[]',
    error           TEXT,
    created_at      TEXT NOT NULL,
    started_at      TEXT,
    finished_at     TEXT
);
CREATE INDEX IF NOT EXISTS idx_jobs_status_created ON jobs(status, created_at);

CREATE TABLE IF NOT EXISTS job_logs (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id    TEXT NOT NULL,
    ts        TEXT NOT NULL,
    level     TEXT NOT NULL,
    message   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_job_logs_job ON job_logs(job_id, id);

CREATE TABLE IF NOT EXISTS presets (
    id          TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    source      TEXT NOT NULL,
    category    TEXT,
    description TEXT,
    root        TEXT,
    file        TEXT,
    doc_json    TEXT,
    created_at  TEXT NOT NULL,
    UNIQUE(source, name, file)
);

CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime())


@dataclass
class Job:
    id: str
    input_root: str
    input_path: str
    output_root: str
    output_path: str
    preset_id: str
    status: str
    spec: dict
    args: list[str] = field(default_factory=list)
    preset_name: str | None = None
    container: str | None = None
    progress: float = 0.0
    speed: str | None = None
    eta_seconds: int | None = None
    duration_seconds: float | None = None
    title_count: int | None = None
    error: str | None = None
    created_at: str = ""
    started_at: str | None = None
    finished_at: str | None = None

    def as_dict(self, *, include_spec: bool = True, include_args: bool = True) -> dict:
        data = {
            "id": self.id,
            "input": {"root": self.input_root, "path": self.input_path},
            "output": {"root": self.output_root, "path": self.output_path},
            "preset_id": self.preset_id,
            "preset_name": self.preset_name,
            "container": self.container,
            "status": self.status,
            "progress": self.progress,
            "speed": self.speed,
            "eta_seconds": self.eta_seconds,
            "duration_seconds": self.duration_seconds,
            "title_count": self.title_count,
            "error": self.error,
            "created_at": self.created_at,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
        }
        if include_spec:
            data["spec"] = self.spec
        if include_args:
            data["args"] = self.args
        return data


class Store:
    def __init__(self, database_path: str):
        self.path = database_path
        parent = Path(database_path).parent
        if str(parent) and not parent.exists():
            parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(database_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        with self._lock:
            self._conn.executescript(_SCHEMA)
            self._set_setting("schema_version", str(SCHEMA_VERSION))
            self._conn.commit()

    # -- settings ----------------------------------------------------------

    def _set_setting(self, key: str, value: str) -> None:
        self._conn.execute(
            "INSERT INTO settings(key,value) VALUES(?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )

    def get_setting(self, key: str, default: str | None = None) -> str | None:
        with self._lock:
            row = self._conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return row["value"] if row else default

    # -- jobs --------------------------------------------------------------

    def create_job(
        self,
        *,
        input_root: str,
        input_path: str,
        output_root: str,
        output_path: str,
        preset_id: str,
        preset_name: str | None,
        container: str | None,
        spec: dict,
        args: list[str],
    ) -> Job:
        job_id = str(uuid.uuid4())
        created = _now()
        with self._lock:
            self._conn.execute(
                """INSERT INTO jobs
                (id,input_root,input_path,output_root,output_path,preset_id,preset_name,
                 container,status,spec_json,args_json,created_at)
                VALUES(?,?,?,?,?,?,?,?,'queued',?,?,?)""",
                (
                    job_id, input_root, input_path, output_root, output_path,
                    preset_id, preset_name, container, json.dumps(spec),
                    json.dumps(args), created,
                ),
            )
            self._conn.commit()
        job = self.get_job(job_id)
        assert job is not None
        return job

    def _row_to_job(self, row: sqlite3.Row) -> Job:
        return Job(
            id=row["id"],
            input_root=row["input_root"],
            input_path=row["input_path"],
            output_root=row["output_root"],
            output_path=row["output_path"],
            preset_id=row["preset_id"],
            preset_name=row["preset_name"],
            container=row["container"],
            status=row["status"],
            progress=row["progress"],
            speed=row["speed"],
            eta_seconds=row["eta_seconds"],
            duration_seconds=row["duration_seconds"],
            title_count=row["title_count"],
            error=row["error"],
            created_at=row["created_at"],
            started_at=row["started_at"],
            finished_at=row["finished_at"],
            spec=json.loads(row["spec_json"]),
            args=json.loads(row["args_json"] or "[]"),
        )

    def get_job(self, job_id: str) -> Job | None:
        with self._lock:
            row = self._conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        return self._row_to_job(row) if row else None

    def list_jobs(self, *, limit: int = 100, statuses: set[str] | None = None) -> list[Job]:
        query = "SELECT * FROM jobs"
        params: list[Any] = []
        if statuses:
            placeholders = ",".join("?" for _ in statuses)
            query += f" WHERE status IN ({placeholders})"
            params.extend(sorted(statuses))
        query += " ORDER BY created_at DESC, rowid DESC LIMIT ?"
        params.append(limit)
        with self._lock:
            rows = self._conn.execute(query, params).fetchall()
        return [self._row_to_job(r) for r in rows]

    def claim_next_queued(self) -> Job | None:
        """Atomically move the oldest queued job to ``probing`` and return it."""

        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                row = self._conn.execute(
                    "SELECT * FROM jobs WHERE status='queued' ORDER BY created_at, rowid LIMIT 1"
                ).fetchone()
                if row is None:
                    self._conn.execute("COMMIT")
                    return None
                now = _now()
                self._conn.execute(
                    "UPDATE jobs SET status='probing', started_at=? WHERE id=? AND status='queued'",
                    (now, row["id"]),
                )
                self._conn.execute("COMMIT")
            except Exception:
                self._conn.execute("ROLLBACK")
                raise
        return self.get_job(row["id"])

    def update_job(self, job_id: str, **fields: Any) -> None:
        allowed = {
            "status", "progress", "speed", "eta_seconds", "duration_seconds",
            "title_count", "error", "started_at", "finished_at", "args_json",
        }
        updates = {k: v for k, v in fields.items() if k in allowed}
        if not updates:
            return
        assignments = ", ".join(f"{k}=?" for k in updates)
        values = list(updates.values()) + [job_id]
        with self._lock:
            self._conn.execute(f"UPDATE jobs SET {assignments} WHERE id=?", values)
            self._conn.commit()

    def set_status(self, job_id: str, status: str, error: str | None = None) -> None:
        if status not in JOB_STATUSES:
            raise ValueError(f"invalid status: {status}")
        finished = _now() if status in TERMINAL_STATUSES else None
        progress_clause = ", progress=1.0" if status == "succeeded" else ""
        with self._lock:
            self._conn.execute(
                f"UPDATE jobs SET status=?, error=?, finished_at=COALESCE(?, finished_at)"
                f"{progress_clause} WHERE id=?",
                (status, error, finished, job_id),
            )
            self._conn.commit()

    def request_cancel(self, job_id: str) -> bool:
        """Mark a queued/active job for cancellation. Returns True if applied."""

        with self._lock:
            row = self._conn.execute("SELECT status FROM jobs WHERE id=?", (job_id,)).fetchone()
            if row is None:
                return False
            status = row["status"]
            if status in TERMINAL_STATUSES:
                return False
            if status == "queued":
                self._conn.execute(
                    "UPDATE jobs SET status='canceled', finished_at=? WHERE id=?",
                    (_now(), job_id),
                )
            else:
                # Active job: worker observes the flag and terminates the engine.
                self._conn.execute(
                    "UPDATE jobs SET error='cancel requested' WHERE id=?", (job_id,)
                )
            self._conn.commit()
        return True

    def cancel_requested(self, job_id: str) -> bool:
        job = self.get_job(job_id)
        return bool(job and job.status == "running" and job.error == "cancel requested") or bool(
            job and job.status == "probing" and job.error == "cancel requested"
        )

    def recover_interrupted(self) -> int:
        """Mark jobs that were active at shutdown as interrupted."""

        with self._lock:
            cur = self._conn.execute(
                "UPDATE jobs SET status='interrupted', "
                "error='service restarted while this job was running', finished_at=? "
                "WHERE status IN ('probing','running','finalizing')",
                (_now(),),
            )
            self._conn.commit()
            return cur.rowcount

    def counts(self) -> dict[str, int]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT status, COUNT(*) AS n FROM jobs GROUP BY status"
            ).fetchall()
        return {r["status"]: r["n"] for r in rows}

    # -- logs --------------------------------------------------------------

    def add_log(self, job_id: str, level: str, message: str) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO job_logs(job_id,ts,level,message) VALUES(?,?,?,?)",
                (job_id, _now(), level, message),
            )
            self._conn.commit()

    def get_logs(self, job_id: str, *, limit: int = 500) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT ts,level,message FROM job_logs WHERE job_id=? ORDER BY id DESC LIMIT ?",
                (job_id, limit),
            ).fetchall()
        return [{"ts": r["ts"], "level": r["level"], "message": r["message"]} for r in reversed(rows)]

    # -- presets -----------------------------------------------------------

    def upsert_preset(
        self,
        *,
        name: str,
        source: str,
        category: str | None = None,
        description: str | None = None,
        root: str | None = None,
        file: str | None = None,
        doc: dict | None = None,
    ) -> str:
        preset_id = str(uuid.uuid4())
        with self._lock:
            self._conn.execute(
                """INSERT INTO presets(id,name,source,category,description,root,file,doc_json,created_at)
                   VALUES(?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(source,name,file) DO UPDATE SET
                     category=excluded.category, description=excluded.description,
                     doc_json=excluded.doc_json""",
                (
                    preset_id, name, source, category, description, root, file,
                    json.dumps(doc) if doc is not None else None, _now(),
                ),
            )
            self._conn.commit()
        return preset_id

    def list_presets(self, *, source: str | None = None) -> list[dict]:
        query = "SELECT id,name,source,category,description,root,file FROM presets"
        params: list[Any] = []
        if source:
            query += " WHERE source=?"
            params.append(source)
        query += " ORDER BY source, category, name"
        with self._lock:
            rows = self._conn.execute(query, params).fetchall()
        return [dict(r) for r in rows]

    def get_preset(self, preset_id: str) -> dict | None:
        with self._lock:
            row = self._conn.execute("SELECT * FROM presets WHERE id=?", (preset_id,)).fetchone()
        if row is None:
            return None
        data = dict(row)
        data["doc"] = json.loads(row["doc_json"]) if row["doc_json"] else None
        return data

    def close(self) -> None:
        with self._lock:
            self._conn.close()
