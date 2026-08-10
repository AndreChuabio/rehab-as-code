-- 20260810230000_protocol_auto_apply_corrections.sql
-- Reconciles prod with the audited intent of 20260628120000_protocol_auto_apply.
--
-- Why this exists: 20260628120000 was ghost-applied to prod from the feature
-- branch on 2026-06-28 in its ORIGINAL, pre-audit form. Because its version is
-- already recorded in supabase_migrations.schema_migrations, the corrected file
-- merged in #123 will never re-run, and every statement in it is guarded by
-- IF NOT EXISTS, so even a forced replay would be a no-op against the existing
-- column and index. Append-only discipline binds it now that it has been
-- applied to a real database, so the corrections land here instead.
--
-- Fresh databases (supabase db reset) get the right shape from 20260628120000
-- directly; every statement below is written to be a safe no-op in that case.

-- Fail fast rather than queue behind a long-running transaction and block every
-- writer on protocols. NOT CREATE INDEX CONCURRENTLY: Supabase CLI >= 2.92.1
-- wraps each migration file in an implicit pipeline where CONCURRENTLY fails
-- with SQLSTATE 25001, which would break the merge-to-main deploy path.
SET lock_timeout = '3s';

-- 1. reverted_by uuid -> text.
--
-- Every actor column in this schema is TEXT (protocols.reviewed_by,
-- staff_users.user_id, admin_phi_reveals.admin_user_id) and there are no FKs to
-- auth.users anywhere, per the convention documented in
-- 20260506220000_clinicians_table.sql. uuid also breaks the revert path for any
-- non-UUID actor string.
--
-- The rewrite is trivial here (30 rows, 248 kB, reverted_by all NULL as of
-- 2026-08-10 -- no revert has ever run), but it is guarded so it is skipped
-- entirely on a database where the column is already text.
DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_schema = 'public'
          AND table_name   = 'protocols'
          AND column_name  = 'reverted_by'
          AND data_type    = 'uuid'
    ) THEN
        ALTER TABLE public.protocols
            ALTER COLUMN reverted_by TYPE text USING reverted_by::text;
    END IF;
END
$$;

-- 2. Reshape the clinician "auto-applied, unreviewed" feed index.
--
-- protocol_repo.list_auto_applied_open is CROSS-PATIENT -- it carries no token
-- predicate and orders by created_at alone:
--     WHERE auto_applied = true AND reverted_at IS NULL
--     ORDER BY created_at DESC LIMIT %s
-- A leading token column cannot be walked to satisfy that ORDER BY, so the old
-- shape degraded to a full partial-index scan plus a Sort and the LIMIT could
-- not push into the scan. It was also redundant against the existing
-- protocols_token_created_idx (token, created_at DESC). Drop and rebuild to
-- mirror the sibling protocols_pending_idx, which correctly omits token.
DROP INDEX IF EXISTS public.protocols_auto_applied_open_idx;

CREATE INDEX IF NOT EXISTS protocols_auto_applied_open_idx
    ON public.protocols (created_at DESC)
    WHERE auto_applied = true AND reverted_at IS NULL;

-- 3. Index the self-referencing parent_id FK.
--
-- It has never been indexed. revert() makes it the load-bearing undo pointer,
-- and protocols.token is ON DELETE CASCADE from users, so deleting a patient
-- forces a sequential scan per cascaded row to check this FK.
CREATE INDEX IF NOT EXISTS protocols_parent_id_idx
    ON public.protocols (parent_id)
    WHERE parent_id IS NOT NULL;

-- 4. Restore the column comments prod never received.
--
-- The pre-audit 20260628120000 that actually ran on prod (git 31cb567) set a
-- comment on auto_applied ONLY -- verified against pg_attribute: auto_applied
-- has its comment and is byte-identical to the corrected file, while
-- reverted_at and reverted_by are both NULL. ALTER COLUMN TYPE preserves
-- comments, so nothing else restores these. Idempotent overwrite elsewhere.
COMMENT ON COLUMN public.protocols.reverted_at IS
    'Set when a clinician reverted this auto-applied row; parent is reactivated';
COMMENT ON COLUMN public.protocols.reverted_by IS
    'auth.uid() of the reverting clinician, as text (matches reviewed_by)';
