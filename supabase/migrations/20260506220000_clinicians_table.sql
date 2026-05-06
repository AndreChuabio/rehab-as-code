-- Clinician role table.
--
-- Pre-existing posture: any authenticated user can approve/reject pending
-- protocols (mirrors today's /pr/apply, where the patient self-approves
-- in the demo). This migration introduces a separate identity for clinicians
-- so the dashboard endpoints can be gated to a real reviewer audience.
--
-- Why a table instead of a JWT custom claim:
--   * Easier to revoke — DELETE one row, no need to re-issue tokens.
--   * Easier to audit — `SELECT * FROM clinicians` answers "who can approve?"
--   * Smaller blast radius — adding a clinician doesn't require rotating
--     auth-server config.
--   * Trivially convertible to RLS rules later (USING auth.uid() IN
--     (SELECT user_id FROM clinicians)).
--
-- The user_id column intentionally is not FK'd to auth.users because
-- some bootstrap flows seed clinicians before their auth.users row
-- exists (e.g., via Supabase Auth invite). The is_clinician() helper in
-- backend/auth.py is the only consumer.
--
-- Seeding: one row per clinician's auth.uid(). Use the seed script in
-- backend/scripts/seed_clinician.py once a clinician has signed up.

CREATE TABLE IF NOT EXISTS clinicians (
    user_id TEXT PRIMARY KEY,
    display_name TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    notes TEXT
);
