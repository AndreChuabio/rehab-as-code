-- Initial schema for the rehab-as-code user store.
--
-- Mirrors backend/user_store.py _PG_SCHEMA. Single source of truth for the
-- Postgres backend lives in code; this migration captures the same shape so
-- the Supabase GitHub integration can apply it on push to main.
--
-- Running this twice is safe (CREATE TABLE IF NOT EXISTS / CREATE INDEX
-- IF NOT EXISTS). When the schema evolves, add a new dated migration file
-- next to this one — never edit a migration that has already shipped.
--
-- Row-Level Security (RLS) intentionally NOT enabled here. The current app
-- writes via a server-side connection (no Supabase Auth wired yet), so RLS
-- policies referencing auth.uid() would block every insert. Phase-1 step 2
-- introduces Supabase Auth + RLS in a separate migration.

CREATE TABLE IF NOT EXISTS users (
    token TEXT PRIMARY KEY,
    slack_user_id TEXT UNIQUE,
    patient_name TEXT,
    created_at TEXT NOT NULL,
    last_active TEXT NOT NULL,
    last_sync TEXT,
    injury_category TEXT
);

CREATE TABLE IF NOT EXISTS health_records (
    token TEXT NOT NULL REFERENCES users(token) ON DELETE CASCADE,
    recorded_at TEXT NOT NULL,
    payload JSONB NOT NULL,
    PRIMARY KEY (token, recorded_at)
);

CREATE TABLE IF NOT EXISTS intake_records (
    token TEXT PRIMARY KEY REFERENCES users(token) ON DELETE CASCADE,
    recorded_at TEXT NOT NULL,
    payload JSONB NOT NULL
);

CREATE TABLE IF NOT EXISTS protocol_state (
    token TEXT PRIMARY KEY REFERENCES users(token) ON DELETE CASCADE,
    last_updated TEXT NOT NULL,
    current_phase TEXT,
    current_week INTEGER,
    last_pr_url TEXT,
    payload JSONB NOT NULL
);

CREATE TABLE IF NOT EXISTS checkins (
    session_id TEXT PRIMARY KEY,
    token TEXT NOT NULL REFERENCES users(token) ON DELETE CASCADE,
    recorded_at TEXT NOT NULL,
    pain_level INTEGER,
    payload JSONB NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_checkins_token_time ON checkins(token, recorded_at);
CREATE INDEX IF NOT EXISTS idx_users_slack ON users(slack_user_id);
