-- 20260628120000_protocol_auto_apply.sql
-- Adds auto-apply provenance to the versioned protocols table.
-- auto_applied marks a row Coach Maya promoted to active without a clinician
-- gate (low-risk tier). Revert target is the existing parent_id pointer.

-- Fail fast rather than queue behind a long-running transaction and block
-- every writer on protocols. These DDLs need milliseconds; if 3s is not
-- enough the table is busy, and this should be retried when it is quiet.
-- NOTE: CREATE INDEX CONCURRENTLY is deliberately NOT used. Supabase CLI
-- >= 2.92.1 wraps each migration file in an implicit pipeline, and
-- CONCURRENTLY cannot run inside one (SQLSTATE 25001) -- it would break the
-- deploy on the merge-to-main GitHub-integration path. lock_timeout is the
-- correct guard here.
SET lock_timeout = '3s';

-- Catalog-only in PG 11+: a non-volatile DEFAULT is stored as a missing
-- value, so no table rewrite and no backfill. false is the correct historical
-- value -- every pre-existing row went through the clinician gate.
-- reverted_by is TEXT, not uuid, to match protocols.reviewed_by and
-- staff_users.user_id. No FK to auth.users, per the convention documented in
-- 20260506220000_clinicians_table.sql (staff rows are seeded before their
-- auth.users row exists).
ALTER TABLE public.protocols
    ADD COLUMN IF NOT EXISTS auto_applied boolean NOT NULL DEFAULT false,
    ADD COLUMN IF NOT EXISTS reverted_at  timestamptz NULL,
    ADD COLUMN IF NOT EXISTS reverted_by  text NULL;

-- Clinician "auto-applied, unreviewed" feed: cross-patient, newest first.
-- Mirrors protocols_pending_idx -- no leading token column, because
-- protocol_repo.list_auto_applied_open carries no token predicate and orders
-- by created_at alone. A leading token would make the index unwalkable for
-- that ORDER BY and cost writes for nothing.
CREATE INDEX IF NOT EXISTS protocols_auto_applied_open_idx
    ON public.protocols (created_at DESC)
    WHERE auto_applied = true AND reverted_at IS NULL;

-- The self-referencing parent_id FK has never been indexed. revert() makes it
-- the load-bearing undo pointer, and protocols.token is ON DELETE CASCADE from
-- users, so deleting a patient forces a scan per cascaded row to check this FK.
CREATE INDEX IF NOT EXISTS protocols_parent_id_idx
    ON public.protocols (parent_id)
    WHERE parent_id IS NOT NULL;

COMMENT ON COLUMN public.protocols.auto_applied IS
    'true = promoted to active by Coach Maya low-risk tier, no clinician gate';
COMMENT ON COLUMN public.protocols.reverted_at IS
    'Set when a clinician reverted this auto-applied row; parent is reactivated';
COMMENT ON COLUMN public.protocols.reverted_by IS
    'auth.uid() of the reverting clinician, as text (matches reviewed_by)';
