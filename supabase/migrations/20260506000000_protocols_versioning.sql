-- Versioned protocol storage with clinician approval gating.
--
-- Replaces the GitHub PR-based workflow where protocols/protocol.yaml on
-- main was the source of truth. After this migration ships and the read /
-- write paths are swapped (in follow-up PRs), the `protocols` table is
-- the source of truth: one row per protocol version, linked through
-- `parent_id`, gated by `status` ∈ {pending_review, active, superseded,
-- rejected}. The unique partial index enforces "one active protocol per
-- patient" without application-level locking.
--
-- This migration is additive only. `protocol_state` and the GitHub-backed
-- read path stay untouched. The read-path swap
-- (protocol_loader.fetch_protocol → query this table) lands in a follow-up
-- PR; the PR-based write path is removed in a later PR once the read
-- swap has been live for a release cycle.
--
-- Why TIMESTAMPTZ here when sibling tables use TEXT for timestamps:
-- the version-history queries this table is built for (newest-first,
-- date-range filters for the clinician dashboard) are clean with proper
-- timestamp types and awkward with string comparisons. Existing TEXT
-- timestamp columns stay as-is to avoid touching live data.
--
-- RLS is intentionally NOT enabled here, matching the convention in
-- 20260504185400_init_user_store.sql. The current backend writes via a
-- server-side connection (service role), so RLS policies referencing
-- auth.uid() would block every server-side insert. RLS comes in a
-- separate migration once the frontend talks to Supabase directly under
-- the patient's JWT.

CREATE TABLE IF NOT EXISTS protocols (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    token TEXT NOT NULL REFERENCES users(token) ON DELETE CASCADE,
    parent_id UUID REFERENCES protocols(id),
    payload JSONB NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('pending_review', 'active', 'superseded', 'rejected')),
    created_by_agent TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    -- reviewed_by stores the clinician's auth.uid() as TEXT without a FK to
    -- users(token); clinicians are not modeled as patient `users` rows yet
    -- and a clinicians table / role claim is a separate migration.
    reviewed_by TEXT,
    reviewed_at TIMESTAMPTZ,
    review_notes TEXT
);

-- Enforces "at most one active protocol per patient" at the DB level.
-- Promotion is a two-statement transaction: UPDATE the new row to 'active',
-- UPDATE the old row to 'superseded'. Doing it in the wrong order trips
-- this index and the transaction rolls back.
CREATE UNIQUE INDEX IF NOT EXISTS protocols_one_active_per_token
    ON protocols (token) WHERE status = 'active';

-- Clinician dashboard: pending review across all patients, newest first.
CREATE INDEX IF NOT EXISTS protocols_pending_idx
    ON protocols (created_at DESC) WHERE status = 'pending_review';

-- Per-patient version history queries.
CREATE INDEX IF NOT EXISTS protocols_token_created_idx
    ON protocols (token, created_at DESC);
