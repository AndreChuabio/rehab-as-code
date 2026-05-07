-- Safety review extension for the protocols table.
--
-- Adds a new terminal status `needs_clinician_review` for drafts the
-- SafetyReviewAgent flags as high-severity, plus a JSONB column to
-- carry the structured concern list back to the clinician dashboard.
--
-- Append-only: drops the existing CHECK constraint by name (which was
-- declared in 20260506000000_protocols_versioning.sql) and re-adds it
-- with the new value list. Rows already in the table use one of the
-- prior four statuses; this is a strict superset, so no row is invalidated.

ALTER TABLE public.protocols
  DROP CONSTRAINT IF EXISTS protocols_status_check;

ALTER TABLE public.protocols
  ADD CONSTRAINT protocols_status_check
  CHECK (status IN (
    'pending_review',
    'active',
    'superseded',
    'rejected',
    'needs_clinician_review'
  ));

ALTER TABLE public.protocols
  ADD COLUMN IF NOT EXISTS safety_concerns JSONB;

COMMENT ON COLUMN public.protocols.safety_concerns IS
  'SafetyReviewAgent output. Shape: [{check, severity, detail}, ...]. '
  'Populated when a draft was flagged by the safety reviewer (med after '
  'retries, or high direct). NULL when the draft passed safety review.';
