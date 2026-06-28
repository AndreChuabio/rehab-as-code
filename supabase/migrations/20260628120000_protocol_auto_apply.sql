-- 20260628120000_protocol_auto_apply.sql
-- Adds auto-apply provenance to the versioned protocols table.
-- auto_applied marks a row Coach Maya promoted to active without a clinician
-- gate (low-risk tier). Revert target is the existing parent_id pointer.
ALTER TABLE public.protocols
    ADD COLUMN IF NOT EXISTS auto_applied boolean NOT NULL DEFAULT false,
    ADD COLUMN IF NOT EXISTS reverted_at  timestamptz NULL,
    ADD COLUMN IF NOT EXISTS reverted_by  uuid NULL;

-- Partial index so the clinician "auto-applied, unreviewed" feed query is cheap.
CREATE INDEX IF NOT EXISTS protocols_auto_applied_open_idx
    ON public.protocols (token, created_at DESC)
    WHERE auto_applied = true AND reverted_at IS NULL;

COMMENT ON COLUMN public.protocols.auto_applied IS
    'true = promoted to active by Coach Maya low-risk tier, no clinician gate';
