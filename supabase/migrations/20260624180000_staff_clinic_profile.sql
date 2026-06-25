-- Clinic profile + clinician notification prefs on staff_users.
--
-- Adds the fields the clinician sets in Settings -> Clinic profile:
--   clinic_name      shown on the generated super-bill header
--   clinic_phone     the escalation number patients are told to call in a
--                    flare; takes precedence over the CLINIC_PHONE env var
--   license_number   clinician license # (super-bill attestation context)
--   signature        printed signature / credentials line on the super-bill
--   notif_prefs      JSONB blob of clinician alert preferences (new
--                    needs_clinician_review drafts, high-severity symptom
--                    flags). Stored only — no delivery system in v1.
--   goal_templates   JSONB blob of per-payer (insurance/medicare/cash) default
--                    goal-language templates. Surfaced in Settings and used as
--                    cheap planner style guidance; generic clinician free text,
--                    never patient-specific (it becomes Anthropic-bound).
--
-- Append-only, lock-cheap: six ADD COLUMN IF NOT EXISTS, every column
-- NULLable with no DEFAULT and no backfill. On Postgres an ADD COLUMN with
-- no volatile default is a metadata-only catalog change (no table rewrite,
-- no row-level lock held), so this is safe on a live table.
--
-- No new RLS policy: staff_users already carries the role-table policies from
-- 20260507180000_staff_roles.sql; these columns inherit them. The settings
-- endpoints self-scope every write to the authenticated clinician's user_id
-- via require_clinician_id (the service-role DSN bypasses RLS, so the
-- WHERE user_id = %s predicate is the guard — same posture as
-- set_clinician_display_name).
--
-- Do NOT touch the `clinicians` VIEW: it is still consumed by is_clinician()
-- via public.clinicians and these new columns are not part of that view's
-- contract.

ALTER TABLE staff_users
    ADD COLUMN IF NOT EXISTS clinic_name    TEXT,
    ADD COLUMN IF NOT EXISTS clinic_phone   TEXT,
    ADD COLUMN IF NOT EXISTS license_number TEXT,
    ADD COLUMN IF NOT EXISTS signature      TEXT,
    ADD COLUMN IF NOT EXISTS notif_prefs    JSONB,
    ADD COLUMN IF NOT EXISTS goal_templates JSONB;

COMMENT ON COLUMN staff_users.clinic_name IS
    'Clinic display name; printed on the generated super-bill header.';
COMMENT ON COLUMN staff_users.clinic_phone IS
    'Clinic escalation phone; takes precedence over CLINIC_PHONE env for flares.';
COMMENT ON COLUMN staff_users.license_number IS
    'Clinician license number (super-bill attestation context).';
COMMENT ON COLUMN staff_users.signature IS
    'Printed signature / credentials line for the super-bill.';
COMMENT ON COLUMN staff_users.notif_prefs IS
    'Clinician review-alert preferences (stored only; no delivery in v1).';
COMMENT ON COLUMN staff_users.goal_templates IS
    'Per-payer default goal-language templates (generic; planner style guidance).';
