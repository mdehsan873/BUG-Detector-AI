-- Migration: Create analysis_runs table for persistent analysis state
-- Replaces the in-memory _analysis_store dict

CREATE TABLE IF NOT EXISTS analysis_runs (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    project_id      UUID NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    status          TEXT NOT NULL DEFAULT 'running' CHECK (status IN ('running', 'completed', 'failed')),
    sessions_total  INTEGER NOT NULL DEFAULT 0,
    sessions_analyzed INTEGER NOT NULL DEFAULT 0,
    issues_found    INTEGER NOT NULL DEFAULT 0,
    issues_json     TEXT DEFAULT '[]',        -- JSON array of issue objects
    ai_cost_json    TEXT DEFAULT '{}',        -- JSON cost summary
    started_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    completed_at    TIMESTAMPTZ,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Index for fast lookups by project + recency
CREATE INDEX IF NOT EXISTS idx_analysis_runs_project_started
    ON analysis_runs (project_id, started_at DESC);

-- Index for finding running analyses
CREATE INDEX IF NOT EXISTS idx_analysis_runs_project_status
    ON analysis_runs (project_id, status)
    WHERE status = 'running';

-- Auto-cleanup: analyses older than 7 days can be purged
-- (optional: run periodically via cron or Supabase edge function)
