-- public.sessions — DB-backed today's-session log.
--
-- Replaces the in-memory `todaySession` array on the patient frontend. Every
-- exercise the patient stages or completes lands here, scoped to the
-- patient (auth.uid()::text = token). Pose-form-check sets also write a
-- completed row through the existing /pose/session endpoint so the same
-- record holds the pose_metrics JSON.
--
-- Posture: append-only migration. Sessions are clinical state — once written,
-- the audit trail matters. Use status transitions (planned -> in_progress
-- -> completed | skipped) instead of mutating in place.
--
-- RLS: patient self-access for everything; clinician SELECT on every patient
-- so the dashboard's "last 7 days" panel can read across patients without a
-- service-role roundtrip.

CREATE TABLE IF NOT EXISTS public.sessions (
  id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  token           TEXT NOT NULL REFERENCES public.users(token) ON DELETE CASCADE,
  exercise_id     TEXT NOT NULL,
  protocol_id     UUID REFERENCES public.protocols(id),
  planned_sets    INTEGER,
  planned_reps    INTEGER,
  completed_sets  INTEGER,
  completed_reps  INTEGER,
  pose_metrics    JSONB,
  status          TEXT NOT NULL DEFAULT 'planned'
                       CHECK (status IN ('planned','in_progress','completed','skipped')),
  started_at      TIMESTAMPTZ,
  completed_at    TIMESTAMPTZ,
  created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS sessions_token_created_at_idx
    ON public.sessions(token, created_at DESC);

ALTER TABLE public.sessions ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS sessions_self            ON public.sessions;
DROP POLICY IF EXISTS sessions_clinician_select ON public.sessions;

-- Patient: full self-access (insert / update / select / delete).
CREATE POLICY sessions_self ON public.sessions
    FOR ALL USING (auth.uid()::text = token)
    WITH CHECK (auth.uid()::text = token);

-- Clinician: SELECT only across all patients (dashboard adherence panel).
-- Mirrors the policy pattern in 20260506231000_rls_lockdown.sql.
CREATE POLICY sessions_clinician_select ON public.sessions
    FOR SELECT USING (public.is_clinician());
