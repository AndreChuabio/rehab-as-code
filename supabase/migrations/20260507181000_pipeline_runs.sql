-- Pipeline runs observability table.
--
-- Captures one row per agent invocation across the multi-agent pipeline
-- (researcher, trend_analyst, evaluator, planner, safety_reviewer,
-- diff_narrator, symptom_classifier). Used by the new /admin/* dashboard
-- to drill into "what did the AI actually decide for patient X" without
-- relying on Vercel's ephemeral stdout.
--
-- Append-only by design. No UPDATE/DELETE policy. Retention handled at
-- the application layer (90-day rolling drop) — Supabase free tier
-- doesn't expose pg_partman, so partitioning is a future concern when
-- volume justifies it. For now, indexes + a periodic prune.
--
-- PHI hygiene contract (enforced in backend/observability/trace.py):
--   * output_summary stores ONLY decisions and structured outputs the
--     downstream agent consumes. Never raw transcripts, raw chat text,
--     or full prompts.
--   * error_message truncated to 500 chars + regex-stripped (emails,
--     phone numbers).
--   * patient_uid is auth.uid() (already opaque).

CREATE TABLE IF NOT EXISTS pipeline_runs (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    request_id      UUID NOT NULL,
    patient_uid     TEXT NOT NULL,
    agent           TEXT NOT NULL,
    step_index      SMALLINT NOT NULL,
    status          TEXT NOT NULL CHECK (status IN ('ok', 'error', 'timeout', 'empty')),
    started_at      TIMESTAMPTZ NOT NULL,
    duration_ms     INTEGER NOT NULL,
    model           TEXT,
    tokens_in       INTEGER,
    tokens_out      INTEGER,
    decision        TEXT,
    output_summary  JSONB,
    error_class     TEXT,
    error_message   TEXT,
    protocol_id     UUID REFERENCES protocols(id) ON DELETE SET NULL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Five queries this table must serve cheaply (see admin.py):
--   1. Runs for patient X in last 7d            → idx_runs_patient_time
--   2. Errored runs in last 24h                 → idx_runs_errored
--   3. p95 latency of agent Y                   → idx_runs_agent_time
--   4. Full breakdown of one request_id         → idx_runs_request
--   5. Recent distinct patients with activity   → idx_runs_patient_time
CREATE INDEX IF NOT EXISTS idx_runs_request
    ON pipeline_runs (request_id);
CREATE INDEX IF NOT EXISTS idx_runs_patient_time
    ON pipeline_runs (patient_uid, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_runs_errored
    ON pipeline_runs (created_at DESC)
    WHERE status <> 'ok';
CREATE INDEX IF NOT EXISTS idx_runs_agent_time
    ON pipeline_runs (agent, created_at DESC);

-- RLS on but no policies = nothing reads it via Supabase client. Backend
-- access goes through the service-role / direct DATABASE_URL connection,
-- which bypasses RLS by design — gated at the API layer by require_admin.
ALTER TABLE pipeline_runs ENABLE ROW LEVEL SECURITY;

COMMENT ON TABLE pipeline_runs IS
    'Observability log for the multi-agent pipeline. Append-only. PHI-redacted output_summary.';
