-- 20260811010000_protocol_auto_apply_acknowledge.sql
-- Persist clinician acknowledgement of an auto-applied protocol row.
--
-- Why: the clinician revert feed rendered an "Acknowledge" button whose handler
-- was `() => el.remove()` - purely client-side. The row came back on the next
-- reload, and a clinician who acknowledged on one device recorded nothing
-- anywhere, so there was no way to tell "reviewed and accepted" apart from
-- "never looked at". Auto-applied rows reach a patient with NO approval gate,
-- so that acknowledgement is the only audit trail that a human ever saw the
-- change. It has to be durable.
--
-- Shape mirrors the existing reverted_at / reverted_by pair: a timestamp plus a
-- TEXT actor. TEXT, not uuid - every actor column in this schema is text
-- (protocols.reviewed_by, protocols.reverted_by after 20260810230000,
-- staff_users.user_id) and there are no FKs to auth.users anywhere.

-- Fail fast rather than queue behind a long-running transaction and block every
-- writer on protocols. NOT CREATE INDEX CONCURRENTLY: Supabase CLI >= 2.92.1
-- wraps each migration file in an implicit pipeline where CONCURRENTLY fails
-- with SQLSTATE 25001, which would break the merge-to-main deploy path.
SET lock_timeout = '3s';

-- 1. The columns.
--
-- Both nullable with no DEFAULT, so this is a catalog-only change in PG11+:
-- no table rewrite, no full-table lock, safe on a live table regardless of row
-- count. NULL is the correct "not yet acknowledged" state and needs no backfill
-- - every existing row is genuinely unacknowledged.
ALTER TABLE public.protocols
    ADD COLUMN IF NOT EXISTS acknowledged_at timestamptz;

ALTER TABLE public.protocols
    ADD COLUMN IF NOT EXISTS acknowledged_by text;

-- 2. Keep the feed index aligned with the feed query.
--
-- protocol_repo.list_auto_applied_open now also filters acknowledged_at IS
-- NULL. Leaving the old partial index in place would still match acknowledged
-- rows, so the planner would read them and discard them via a filter. Rebuild
-- the predicate to match the query exactly.
--
-- DROP + CREATE (both IF [NOT] EXISTS) rather than a bare CREATE: this
-- converges from either shape, which is the pattern 20260810230000 established
-- after the ghost-apply incident. Cheap here - the table is ~30 rows.
DROP INDEX IF EXISTS public.protocols_auto_applied_open_idx;

CREATE INDEX IF NOT EXISTS protocols_auto_applied_open_idx
    ON public.protocols (created_at DESC)
    WHERE auto_applied = true
      AND reverted_at IS NULL
      AND acknowledged_at IS NULL;

-- 3. Document the columns.
COMMENT ON COLUMN public.protocols.acknowledged_at IS
    'Set when a clinician acknowledged this auto-applied row; drops it from the review feed without reverting';
COMMENT ON COLUMN public.protocols.acknowledged_by IS
    'auth.uid() of the acknowledging clinician, as text (matches reviewed_by/reverted_by)';

-- RLS: no policy change needed. Adding columns to an existing table inherits
-- the table's existing row-level policies, and none of the protocols policies
-- enumerate columns. Clinician access continues to flow through is_clinician(),
-- which is untouched here - this migration does not go near the `clinicians`
-- VIEW over staff_users or the function that depends on it.
