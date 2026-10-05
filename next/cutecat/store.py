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

SCHEMA_VERSION = 4

JOB_STATUSES = {
    "waiting", "paused", "queued", "probing", "running", "finalizing",
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

CREATE TABLE IF NOT EXISTS task_templates (
    id TEXT PRIMARY KEY, name TEXT NOT NULL, description TEXT NOT NULL,
    payload_json TEXT NOT NULL, updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS output_reservations (
    absolute_path TEXT PRIMARY KEY, job_id TEXT NOT NULL UNIQUE
);
CREATE TABLE IF NOT EXISTS job_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT, job_id TEXT NOT NULL,
    status TEXT NOT NULL, payload_json TEXT NOT NULL, ts TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_job_events_job ON job_events(job_id);

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
    execution: dict | None = None
    execution_active: bool = False
    pause_requested: bool = False
    paused_from: str | None = None
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

    def actions(self) -> list[str]:
        active = self.execution_active or self.status in ACTIVE_STATUSES or (self.status == "paused" and self.paused_from in ACTIVE_STATUSES)
        if self.error == "cancel requested":
            return []
        result = []
        if self.status == "paused" or (self.status in {"waiting", "failed", "canceled", "interrupted"} and not active):
            result.append("start")
        remote = (self.execution or {}).get("engine") == "rffmpeg"
        if self.status in {"waiting", "queued"} | ACTIVE_STATUSES and not self.pause_requested and not (remote and active):
            result.append("pause")
        if self.status not in TERMINAL_STATUSES:
            result.append("cancel")
        if not active:
            result.append("delete")
        return result

    def as_dict(self, *, include_spec: bool = True, include_args: bool = True) -> dict:
        data = {
            "id": self.id,
            "input": {"root": self.input_root, "path": self.input_path},
            "output": {"root": self.output_root, "path": self.output_path},
            "engine": (self.execution or {}).get("engine", "handbrake"),
            "template_id": (self.execution or {}).get("template_id"),
            "template_name": (self.execution or {}).get("template_name"),
            "preset_id": self.preset_id,
            "preset_name": self.preset_name,
            "container": self.container,
            "status": self.status,
            "pause_requested": self.pause_requested,
            "paused_from": self.paused_from,
            "actions": self.actions(),
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
        if self.execution and self.execution.get("settings"):
            requested = self.execution["settings"].get("requested_output", self.output_path)
            data["requested_output_path"] = requested
            data["output_renamed"] = requested != self.output_path
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
            columns = {row["name"] for row in self._conn.execute("PRAGMA table_info(jobs)")}
            if "execution_json" not in columns:
                self._conn.execute("ALTER TABLE jobs ADD COLUMN execution_json TEXT")
            if "execution_active" not in columns:
                self._conn.execute("ALTER TABLE jobs ADD COLUMN execution_active INTEGER NOT NULL DEFAULT 0")
            if "pause_requested" not in columns:
                self._conn.execute("ALTER TABLE jobs ADD COLUMN pause_requested INTEGER NOT NULL DEFAULT 0")
            if "paused_from" not in columns:
                self._conn.execute("ALTER TABLE jobs ADD COLUMN paused_from TEXT")
            # Preserve IDs while replacing the old cross-root uniqueness rule.
            if self.get_setting("preset_identity_version") != "2":
                self._conn.execute("BEGIN IMMEDIATE")
                self._conn.execute("ALTER TABLE presets RENAME TO presets_v1")
                self._conn.execute("""CREATE TABLE presets (
                    id TEXT PRIMARY KEY, name TEXT NOT NULL, source TEXT NOT NULL,
                    category TEXT, description TEXT, root TEXT, file TEXT,
                    doc_json TEXT, created_at TEXT NOT NULL,
                    UNIQUE(source, name, root, file))""")
                self._conn.execute("INSERT INTO presets SELECT * FROM presets_v1")
                self._conn.execute("DROP TABLE presets_v1")
                self._set_setting("preset_identity_version", "2")
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

    def set_setting(self, key: str, value: str) -> None:
        with self._lock:
            self._set_setting(key, value)
            self._conn.commit()

    def set_settings(self, values: dict) -> None:
        with self._lock:
            for key, value in values.items():
                self._set_setting(key, value)
            self._conn.commit()

    def pending_outputs(self, exclude=None):
        with self._lock:
            return [(r[0], r[1]) for r in self._conn.execute("SELECT output_root,output_path FROM jobs WHERE (status NOT IN ('succeeded','failed','canceled','interrupted') OR execution_active=1) AND id!=?", (exclude or "",))]

    def create_allocated_job(self, allocator, **kwargs) -> Job:
        """Serialize allocation, reservation and insertion in one transaction."""
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                self._conn.execute("DELETE FROM output_reservations WHERE job_id NOT IN (SELECT id FROM jobs WHERE status NOT IN ('succeeded','failed','canceled','interrupted') OR execution_active=1)")
                reserved = {row[0] for row in self._conn.execute("SELECT absolute_path FROM output_reservations")}
                target = allocator(reserved)
                job = self.create_job(**{**kwargs, "output_path": target.relative}, _commit=False)
                self._conn.execute("INSERT INTO output_reservations VALUES(?,?)", (str(target.absolute), job.id))
                self._conn.commit()
                return job
            except Exception:
                self._conn.rollback()
                raise

    def publish_allocated(self, job_id, allocate, publish):
        """Called inside complete_job's lock; commit path only after publication."""
        job = self.get_job(job_id)
        reserved = {row[0] for row in self._conn.execute("SELECT absolute_path FROM output_reservations WHERE job_id!=?", (job_id,))}
        target = allocate(reserved)
        publish(target)
        self._conn.execute("DELETE FROM output_reservations WHERE job_id=?", (job_id,))
        self._conn.execute("INSERT INTO output_reservations VALUES(?,?)", (str(target.absolute), job_id))
        self._conn.execute("UPDATE jobs SET output_path=? WHERE id=?", (target.relative, job_id))
        return target

    def event_report(self, after=None, limit=100):
        with self._lock:
            latest = self._conn.execute("SELECT COALESCE(MAX(id),0) FROM job_events").fetchone()[0]
            if after is None:
                return {"events": [], "cursor": latest, "has_more": False}
            rows = self._conn.execute("SELECT * FROM job_events WHERE id>? ORDER BY id LIMIT ?", (after, limit + 1)).fetchall()
            events = [{"id": row["id"], "job_id": row["job_id"], "status": row["status"], "ts": row["ts"], **json.loads(row["payload_json"])} for row in rows[:limit]]
            return {"events": events, "cursor": events[-1]["id"] if events else after, "has_more": len(rows) > limit}

    def _terminal_event(self, job_id, status):
        job = self.get_job(job_id)
        payload = {"source": job.input_path, "output": job.output_path} if job else {"deleted": True}
        self._conn.execute("INSERT INTO job_events(job_id,status,payload_json,ts) VALUES(?,?,?,?)", (job_id, status, json.dumps(payload), _now()))

    def cleanup_candidates(self, statuses, before, limit=1000):
        with self._lock:
            marks = ",".join("?" for _ in statuses)
            rows = self._conn.execute(f"SELECT id,status,finished_at FROM jobs WHERE status IN ({marks}) AND execution_active=0 AND finished_at IS NOT NULL AND julianday(finished_at)<julianday(?) ORDER BY finished_at LIMIT ?", (*sorted(statuses), before, limit)).fetchall()
            return [dict(row) for row in rows]

    def cleanup_jobs(self, candidates, statuses, before):
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                eligible = {row["id"]: row for row in self.cleanup_candidates(statuses, before, 100000)}
                deleted = 0
                for candidate in candidates:
                    identity = candidate["id"]
                    if eligible.get(identity) != candidate:
                        continue
                    self._conn.execute("DELETE FROM job_logs WHERE job_id=?", (identity,))
                    self._conn.execute("DELETE FROM output_reservations WHERE job_id=?", (identity,))
                    self._conn.execute("UPDATE job_events SET payload_json=? WHERE job_id=?", ('{"deleted":true}', identity))
                    deleted += self._conn.execute("DELETE FROM jobs WHERE id=?", (identity,)).rowcount
                self._conn.commit()
                return {"deleted": deleted, "skipped": len(candidates) - deleted}
            except Exception:
                self._conn.rollback()
                raise

    def save_template(self, payload: dict, identity: str | None = None) -> dict:
        payload = {k: v for k, v in payload.items() if k != "id"}
        identity = identity or str(uuid.uuid4())
        with self._lock:
            self._conn.execute("INSERT INTO task_templates VALUES(?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET name=excluded.name, description=excluded.description, payload_json=excluded.payload_json, updated_at=excluded.updated_at",
                               (identity, payload["name"], payload.get("description", ""), json.dumps(payload), _now()))
            self._conn.commit()
        return {"id": identity, **payload}

    def import_template_bundle(self, prepared):
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            result = []
            try:
                for payload, document in prepared:
                    payload = dict(payload)
                    if document:
                        preset_id = str(uuid.uuid4())
                        self._conn.execute("INSERT INTO presets(id,name,source,category,description,root,file,doc_json,created_at) VALUES(?,?,?,?,?,?,?,?,?)", (preset_id, payload["spec"]["preset"], "imported", "Task templates", "Imported template dependency", None, "bundle-" + preset_id, json.dumps(document), _now()))
                        payload["preset_id"] = preset_id
                    else:
                        payload["preset_id"] = "custom"
                    identity = str(uuid.uuid4())
                    self._conn.execute("INSERT INTO task_templates VALUES(?,?,?,?,?)", (identity, payload["name"], payload.get("description", ""), json.dumps(payload), _now()))
                    result.append({**payload, "id": identity})
                self._conn.commit()
                return result
            except Exception:
                self._conn.rollback()
                raise

    def list_templates(self) -> list[dict]:
        with self._lock:
            rows = self._conn.execute("SELECT * FROM task_templates ORDER BY name, id").fetchall()
        return [{"id": row["id"], **json.loads(row["payload_json"])} for row in rows]

    def delete_template(self, identity: str) -> bool:
        with self._lock:
            cur = self._conn.execute("DELETE FROM task_templates WHERE id=?", (identity,))
            raw = self.get_setting("runtime_settings")
            if raw:
                settings = json.loads(raw)
                if settings.get("default_task_template_id") == identity:
                    settings["default_task_template_id"] = None
                    self._set_setting("runtime_settings", json.dumps(settings))
                    self._set_setting("runtime_revision", str(int(self.get_setting("runtime_revision", "0")) + 1))
            self._conn.commit()
            return cur.rowcount > 0

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
        execution: dict | None = None,
        auto_start: bool = True,
        _commit: bool = True,
    ) -> Job:
        job_id = str(uuid.uuid4())
        created = _now()
        with self._lock:
            self._conn.execute(
                """INSERT INTO jobs
                (id,input_root,input_path,output_root,output_path,preset_id,preset_name,
                 container,status,spec_json,args_json,created_at,execution_json)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    job_id, input_root, input_path, output_root, output_path,
                    preset_id, preset_name, container, "queued" if auto_start else "waiting", json.dumps(spec),
                    json.dumps(args), created, json.dumps(execution) if execution is not None else None,
                ),
            )
            if _commit:
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
            execution_active=bool(row["execution_active"]),
            pause_requested=bool(row["pause_requested"]),
            paused_from=row["paused_from"],
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
            execution=json.loads(row["execution_json"]) if row["execution_json"] else None,
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
                    "UPDATE jobs SET status='probing', execution_active=1, started_at=? WHERE id=? AND status='queued'",
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
            previous = self.get_job(job_id)
            self._conn.execute(
                f"UPDATE jobs SET status=?, error=CASE WHEN ? IS NULL AND error='cancel requested' "
                f"AND ? IN ('probing','running','finalizing') THEN error ELSE ? END, "
                f"finished_at=COALESCE(?, finished_at)"
                f"{progress_clause} WHERE id=?",
                (status, error, status, error, finished, job_id),
            )
            if previous and previous.status != status and status in TERMINAL_STATUSES:
                self._terminal_event(job_id, status)
            self._conn.commit()

    def control_job(self, job_id: str, action: str) -> bool:
        with self._lock:
            job = self.get_job(job_id)
            if not job or action not in job.actions():
                return False
            if action == "pause":
                if job.status in ACTIVE_STATUSES:
                    self._conn.execute("UPDATE jobs SET pause_requested=1 WHERE id=?", (job_id,))
                else:
                    self._conn.execute("UPDATE jobs SET status='paused', paused_from=status, pause_requested=1 WHERE id=?", (job_id,))
            elif action == "start":
                if job.status == "paused" and job.paused_from in ACTIVE_STATUSES:
                    self._conn.execute("UPDATE jobs SET pause_requested=0 WHERE id=?", (job_id,))
                else:
                    self._conn.execute("UPDATE jobs SET status='queued', pause_requested=0, paused_from=NULL, progress=0, speed=NULL, eta_seconds=NULL, error=NULL, started_at=NULL, finished_at=NULL WHERE id=?", (job_id,))
            elif action == "delete":
                self._conn.execute("DELETE FROM job_logs WHERE job_id=?", (job_id,))
                self._conn.execute("DELETE FROM output_reservations WHERE job_id=?", (job_id,))
                self._conn.execute("UPDATE job_events SET payload_json=? WHERE job_id=?", ('{"deleted":true}', job_id))
                self._conn.execute("DELETE FROM jobs WHERE id=?", (job_id,))
            else:
                return False
            if action != "delete":
                self._conn.execute("INSERT INTO job_logs(job_id,ts,level,message) VALUES(?,?,?,?)", (job_id, _now(), "info", f"{action} requested"))
            self._conn.commit()
            return True

    def pause_requested(self, job_id: str) -> bool:
        job = self.get_job(job_id)
        return bool(job and job.pause_requested)

    def acknowledge_pause(self, job_id: str, paused: bool) -> None:
        with self._lock:
            job = self.get_job(job_id)
            if not job or job.status in TERMINAL_STATUSES:
                return
            if paused and job.status in ACTIVE_STATUSES:
                self._conn.execute("UPDATE jobs SET status='paused', paused_from=status WHERE id=?", (job_id,))
            elif not paused and job.status == "paused" and job.paused_from in ACTIVE_STATUSES:
                self._conn.execute("UPDATE jobs SET status=paused_from, paused_from=NULL WHERE id=?", (job_id,))
            self._conn.commit()

    def delete_job(self, job_id: str) -> bool:
        return self.control_job(job_id, "delete")

    def complete_job(self, job_id: str, publish) -> bool:
        """Serialize publication against cancellation; never lose an accepted cancel."""

        with self._lock:
            job = self.get_job(job_id)
            if job is None or job.status != "finalizing" or self.cancel_requested(job_id) or job.pause_requested:
                return False
            publish()
            self.set_status(job_id, "succeeded")
            return True

    def finish_execution(self, job_id: str) -> None:
        with self._lock:
            self._conn.execute("UPDATE jobs SET execution_active=0, paused_from=NULL, pause_requested=0 WHERE id=?", (job_id,))
            self._conn.execute("DELETE FROM output_reservations WHERE job_id=? AND job_id IN (SELECT id FROM jobs WHERE status IN ('succeeded','failed','canceled','interrupted'))", (job_id,))
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
            job = self.get_job(job_id)
            if status in ("queued", "waiting") or (status == "paused" and job.paused_from not in ACTIVE_STATUSES):
                self._conn.execute(
                    "UPDATE jobs SET status='canceled', finished_at=? WHERE id=?",
                    (_now(), job_id),
                )
                self._terminal_event(job_id, "canceled")
                self._conn.execute("DELETE FROM output_reservations WHERE job_id=?", (job_id,))
            else:
                # Active job: worker observes the flag and terminates the engine.
                self._conn.execute(
                    "UPDATE jobs SET error='cancel requested' WHERE id=?", (job_id,)
                )
            self._conn.commit()
        return True

    def cancel_requested(self, job_id: str) -> bool:
        job = self.get_job(job_id)
        return bool(job and (job.status in ACTIVE_STATUSES or job.status == "paused") and job.error == "cancel requested")

    def recover_interrupted(self) -> int:
        """Mark jobs that were active at shutdown as interrupted."""

        with self._lock:
            interrupted = [row[0] for row in self._conn.execute("SELECT id FROM jobs WHERE status IN ('probing','running','finalizing') OR (status='paused' AND paused_from IN ('probing','running','finalizing'))")]
            cur = self._conn.execute(
                "UPDATE jobs SET status='interrupted', "
                "error='service restarted while this job was running', finished_at=? "
                "WHERE status IN ('probing','running','finalizing') OR (status='paused' AND paused_from IN ('probing','running','finalizing'))",
                (_now(),),
            )
            self._conn.execute("UPDATE jobs SET execution_active=0, pause_requested=0 WHERE execution_active=1")
            for identity in interrupted:
                self._terminal_event(identity, "interrupted")
            self._conn.execute("DELETE FROM output_reservations WHERE job_id NOT IN (SELECT id FROM jobs WHERE status NOT IN ('succeeded','failed','canceled','interrupted'))")
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
        with self._lock:
            row = self._conn.execute(
                "SELECT id FROM presets WHERE source=? AND name=? AND root IS ? AND file IS ?",
                (source, name, root, file),
            ).fetchone()
            preset_id = row["id"] if row else str(uuid.uuid4())
            self._conn.execute(
                """INSERT INTO presets(id,name,source,category,description,root,file,doc_json,created_at)
                   VALUES(?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(id) DO UPDATE SET
                     category=excluded.category, description=excluded.description,
                     doc_json=excluded.doc_json""",
                (preset_id, name, source, category, description, root, file,
                 json.dumps(doc) if doc is not None else None, _now()),
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
