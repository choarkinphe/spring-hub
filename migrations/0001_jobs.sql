CREATE TABLE IF NOT EXISTS jobs (
    id TEXT PRIMARY KEY NOT NULL,
    input_root TEXT NOT NULL,
    input_path TEXT NOT NULL,
    output_root TEXT NOT NULL,
    output_path TEXT NOT NULL,
    preset_id TEXT NOT NULL,
    status TEXT NOT NULL,
    progress REAL NOT NULL DEFAULT 0,
    speed TEXT,
    duration_seconds REAL,
    error TEXT,
    created_at TEXT NOT NULL,
    started_at TEXT,
    finished_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_jobs_status_created ON jobs(status, created_at);
