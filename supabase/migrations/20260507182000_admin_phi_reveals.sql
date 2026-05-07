-- Audit log for PHI reveals in the admin dashboard.
--
-- Every time an admin clicks "Reveal" on a redacted field (patient
-- display name, raw chat utterance, intake free-text), the frontend
-- fires POST /admin/phi-reveal which writes one row here BEFORE
-- rendering the value. Append-only so we always have a complete
-- record of "who saw what, when."
--
-- Retention: longer than pipeline_runs (the underlying data) — this
-- is the trail that proves the access happened. 5 years feels right
-- for healthcare-adjacent contexts; revisit with Nikki once compliance
-- scope is firmer.

CREATE TABLE IF NOT EXISTS admin_phi_reveals (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    admin_user_id   TEXT NOT NULL,
    target_user_id  TEXT NOT NULL,
    field           TEXT NOT NULL,
    request_id      UUID,
    revealed_at     TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_phi_reveals_admin_time
    ON admin_phi_reveals (admin_user_id, revealed_at DESC);
CREATE INDEX IF NOT EXISTS idx_phi_reveals_target_time
    ON admin_phi_reveals (target_user_id, revealed_at DESC);

ALTER TABLE admin_phi_reveals ENABLE ROW LEVEL SECURITY;

COMMENT ON TABLE admin_phi_reveals IS
    'Audit trail for admin dashboard PHI reveals. Append-only.';
