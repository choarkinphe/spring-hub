ALTER TABLE jobs ADD COLUMN spec_json TEXT NOT NULL DEFAULT '{"version":1}';
CREATE INDEX IF NOT EXISTS idx_jobs_created ON jobs(created_at);
