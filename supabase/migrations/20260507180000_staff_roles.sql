-- Staff role table.
--
-- Evolves the clinician-only `clinicians` table (migration
-- 20260506220000_clinicians_table.sql) into a unified staff table that
-- supports both clinician and admin roles. Admin is a strict superset of
-- clinician — anyone with role='admin' can also approve protocols. This
-- pairs with the new /admin/* surface (admin dashboard for pipeline-run
-- debugging during productionization).
--
-- Why one table with a role column instead of two tables:
--   * Andre is currently both the de-facto clinician on test data AND the
--     admin debugging the pipeline; forcing two accounts adds login
--     friction with zero safety upside.
--   * Single source of truth for "who has staff access?" — `SELECT *
--     FROM staff_users` answers it.
--   * Trivial to extend (e.g., a future 'support' role) without another
--     table-rename migration.
--
-- Append-only by design: no UPDATE/DELETE policy needed; staff is added
-- by direct INSERT and removed via DELETE-when-needed only by ops.

-- 1. Rename the existing table. CASCADE keeps any dependent objects
--    (the FK back to user_id is already PRIMARY KEY-only, no FKs to it).
ALTER TABLE IF EXISTS clinicians RENAME TO staff_users;

-- 2. Add the role column. Defaults to 'clinician' so every existing row
--    keeps its current behaviour without a backfill statement. New
--    inserts must declare role explicitly via INSERT into staff_users.
ALTER TABLE staff_users
    ADD COLUMN IF NOT EXISTS role TEXT NOT NULL DEFAULT 'clinician'
        CHECK (role IN ('clinician', 'admin'));

-- 3. Composite index for the hot-path role check (`SELECT 1 FROM
--    staff_users WHERE user_id=$1 AND role IN (...)`). Keeps a small
--    index scan rather than a sequential scan as the table grows.
CREATE INDEX IF NOT EXISTS idx_staff_users_uid_role
    ON staff_users (user_id, role);

-- 4. Backwards-compat view so any forgotten reference to `clinicians`
--    still resolves cleanly. Drops in a follow-up migration once the
--    code grep is clean.
CREATE OR REPLACE VIEW clinicians AS
    SELECT user_id, display_name, created_at, notes
    FROM staff_users
    WHERE role IN ('clinician', 'admin');

COMMENT ON TABLE staff_users IS
    'Clinician and admin staff. role=admin is a strict superset of clinician.';
COMMENT ON VIEW clinicians IS
    'Backwards-compat view; new code should query staff_users directly.';
