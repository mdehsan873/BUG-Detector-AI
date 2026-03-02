-- Buglyft - Supabase Database Schema
-- Run this in your Supabase SQL Editor

-- Enable UUID generation
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";

-- Note: Users are managed by Supabase Auth (auth.users) — no custom users table needed.

-- ── Projects ────────────────────────────────────────────────────────────────

CREATE TABLE projects (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    user_id VARCHAR(255) NOT NULL DEFAULT '',  -- Supabase Auth user ID
    name VARCHAR(255) NOT NULL,
    session_provider VARCHAR(50) NOT NULL DEFAULT 'posthog',  -- posthog, fullstory, logrocket, clarity
    provider_api_key TEXT NOT NULL,          -- encrypted (PostHog / FullStory / LogRocket / Clarity key)
    provider_project_id VARCHAR(255) NOT NULL, -- project/org ID inside the provider
    provider_host VARCHAR(255) NOT NULL DEFAULT '',  -- optional: custom host for self-hosted providers
    github_repo VARCHAR(255) NOT NULL DEFAULT '',  -- owner/repo format (optional)
    github_token TEXT NOT NULL DEFAULT '',         -- encrypted (optional)
    detection_threshold INTEGER NOT NULL DEFAULT 5,
    min_sessions_threshold INTEGER NOT NULL DEFAULT 2,  -- min unique sessions before creating an issue
    skip_page_patterns TEXT[] DEFAULT '{}',  -- URL patterns to skip in flow analysis (e.g. /auth/callback)
    is_active BOOLEAN NOT NULL DEFAULT true,
    notification_email_enabled BOOLEAN NOT NULL DEFAULT true,
    notification_email_address VARCHAR(255) DEFAULT '',
    notification_slack_enabled BOOLEAN NOT NULL DEFAULT false,
    notification_slack_webhook_url TEXT DEFAULT '',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- ── Events ──────────────────────────────────────────────────────────────────

CREATE TABLE events (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    project_id UUID NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    event_type VARCHAR(50) NOT NULL,          -- console_error, api_failure, rage_click, exception, dead_click, dead_end, confusing_flow, _pageview, _pageleave
    fingerprint VARCHAR(64) NOT NULL,         -- SHA-256 hash
    error_message TEXT,
    endpoint VARCHAR(2048),
    page_url VARCHAR(2048),
    css_selector VARCHAR(1024),
    session_id VARCHAR(255),
    user_id VARCHAR(255),
    status_code INTEGER,
    raw_properties JSONB,
    timestamp TIMESTAMPTZ NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_events_project_timestamp ON events(project_id, timestamp DESC);
CREATE INDEX idx_events_fingerprint ON events(project_id, fingerprint);
CREATE INDEX idx_events_type_timestamp ON events(project_id, event_type, timestamp DESC);

-- ── Anomaly Clusters ────────────────────────────────────────────────────────

CREATE TABLE anomaly_clusters (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    project_id UUID NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    fingerprint VARCHAR(64) NOT NULL,
    event_type VARCHAR(50) NOT NULL,
    error_message TEXT,
    endpoint VARCHAR(2048),
    css_selector VARCHAR(1024),
    page_url VARCHAR(2048),
    count INTEGER NOT NULL DEFAULT 0,
    affected_users INTEGER NOT NULL DEFAULT 0,
    first_seen TIMESTAMPTZ NOT NULL,
    last_seen TIMESTAMPTZ NOT NULL,
    sample_session_ids TEXT[] DEFAULT '{}',
    status VARCHAR(50) NOT NULL DEFAULT 'new',  -- new, github_issued, resolved
    ai_details JSONB DEFAULT NULL,  -- {description, why_issue, reproduction_steps, evidence, severity, confidence}
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE(project_id, fingerprint)
);

CREATE INDEX idx_clusters_project_status ON anomaly_clusters(project_id, status);
CREATE INDEX idx_clusters_fingerprint ON anomaly_clusters(project_id, fingerprint);

-- ── GitHub Issues ───────────────────────────────────────────────────────────

CREATE TABLE github_issues (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    project_id UUID NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    cluster_fingerprint VARCHAR(64) NOT NULL,
    github_issue_id INTEGER NOT NULL,
    github_issue_url VARCHAR(2048) NOT NULL,
    status VARCHAR(50) NOT NULL DEFAULT 'open',  -- open, closed
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE(project_id, cluster_fingerprint)
);

CREATE INDEX idx_github_issues_fingerprint ON github_issues(project_id, cluster_fingerprint);

-- ── Job Runs ────────────────────────────────────────────────────────────────

CREATE TABLE job_runs (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    project_id UUID NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    last_fetched_at TIMESTAMPTZ NOT NULL,
    status VARCHAR(50) NOT NULL DEFAULT 'running',  -- running, completed, failed
    events_fetched INTEGER NOT NULL DEFAULT 0,
    anomalies_detected INTEGER NOT NULL DEFAULT 0,
    issues_created INTEGER NOT NULL DEFAULT 0,
    error_message TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_job_runs_project_latest ON job_runs(project_id, created_at DESC);

-- ── Dismissed Fingerprints ────────────────────────────────────────────────

CREATE TABLE dismissed_fingerprints (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    project_id UUID NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    fingerprint VARCHAR(64) NOT NULL,
    dismissed_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE(project_id, fingerprint)
);

CREATE INDEX idx_dismissed_fp_project ON dismissed_fingerprints(project_id);

-- ── Auto-update updated_at ──────────────────────────────────────────────────

CREATE OR REPLACE FUNCTION update_updated_at_column()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = NOW();
    RETURN NEW;
END;
$$ language 'plpgsql';

CREATE TRIGGER update_projects_updated_at
    BEFORE UPDATE ON projects
    FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();

CREATE TRIGGER update_clusters_updated_at
    BEFORE UPDATE ON anomaly_clusters
    FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();

CREATE TRIGGER update_github_issues_updated_at
    BEFORE UPDATE ON github_issues
    FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();

-- ── Processed Sessions (track which sessions have been analyzed) ──────────

CREATE TABLE processed_sessions (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    project_id UUID NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    session_id VARCHAR(255) NOT NULL,
    processed_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE(project_id, session_id)
);

CREATE INDEX idx_processed_sessions_project ON processed_sessions(project_id);
CREATE INDEX idx_processed_sessions_lookup ON processed_sessions(project_id, session_id);

-- ── Migration: Add notification columns (run if upgrading) ───────────────
-- ALTER TABLE projects ADD COLUMN IF NOT EXISTS notification_email_enabled BOOLEAN NOT NULL DEFAULT true;
-- ALTER TABLE projects ADD COLUMN IF NOT EXISTS notification_email_address VARCHAR(255) DEFAULT '';
-- ALTER TABLE projects ADD COLUMN IF NOT EXISTS notification_slack_enabled BOOLEAN NOT NULL DEFAULT false;
-- ALTER TABLE projects ADD COLUMN IF NOT EXISTS notification_slack_webhook_url TEXT DEFAULT '';
-- ALTER TABLE projects ADD COLUMN IF NOT EXISTS posthog_host VARCHAR(255) NOT NULL DEFAULT 'eu.posthog.com';
-- ALTER TABLE anomaly_clusters ADD COLUMN IF NOT EXISTS ai_details JSONB DEFAULT NULL;

-- ── Migration: Add processed_sessions table (run if upgrading) ───────────
-- CREATE TABLE IF NOT EXISTS processed_sessions (
--     id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
--     project_id UUID NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
--     session_id VARCHAR(255) NOT NULL,
--     processed_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
--     UNIQUE(project_id, session_id)
-- );
-- CREATE INDEX IF NOT EXISTS idx_processed_sessions_project ON processed_sessions(project_id);
-- CREATE INDEX IF NOT EXISTS idx_processed_sessions_lookup ON processed_sessions(project_id, session_id);

-- ── Migration: Add multi-provider columns (run if upgrading from PostHog-only) ──
-- ALTER TABLE projects ADD COLUMN IF NOT EXISTS session_provider VARCHAR(50) NOT NULL DEFAULT 'posthog';
-- ALTER TABLE projects ADD COLUMN IF NOT EXISTS provider_api_key TEXT;
-- ALTER TABLE projects ADD COLUMN IF NOT EXISTS provider_project_id VARCHAR(255);
-- ALTER TABLE projects ADD COLUMN IF NOT EXISTS provider_host VARCHAR(255) NOT NULL DEFAULT '';
-- UPDATE projects SET provider_api_key = posthog_api_key, provider_project_id = posthog_project_id, provider_host = COALESCE(posthog_host, 'eu.posthog.com') WHERE provider_api_key IS NULL;
-- ALTER TABLE projects DROP COLUMN IF EXISTS posthog_api_key;
-- ALTER TABLE projects DROP COLUMN IF EXISTS posthog_project_id;
-- ALTER TABLE projects DROP COLUMN IF EXISTS posthog_host;

-- ── Migration: Add dismissed_fingerprints + rename min_users_threshold ────
-- CREATE TABLE IF NOT EXISTS dismissed_fingerprints (...);
-- ALTER TABLE projects RENAME COLUMN min_users_threshold TO min_sessions_threshold;

-- ── Migration: Add user_id column ──────────────────────────────────────────
-- ALTER TABLE projects ADD COLUMN IF NOT EXISTS user_id VARCHAR(255) NOT NULL DEFAULT '';
-- CREATE INDEX IF NOT EXISTS idx_projects_user_id ON projects(user_id);

-- ── Migration: Make GitHub fields optional (run if upgrading) ──────────────
-- ALTER TABLE projects ALTER COLUMN github_repo SET DEFAULT '';
-- ALTER TABLE projects ALTER COLUMN github_token SET DEFAULT '';
