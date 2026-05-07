-- Row Level Security lockdown across public.* tables.
--
-- Posture: defense in depth. The FastAPI backend writes via the Postgres
-- service role, which bypasses RLS by default — so enabling RLS does not
-- break any current code path. What it DOES protect against is a future
-- (or accidental) frontend that talks PostgREST directly under the
-- patient's anon key + JWT. Without RLS, anyone with the public anon key
-- could read or write every row in every table.
--
-- Frontend audit (2026-05-06): grep for `supabase.from(` and `/rest/v1/`
-- across `frontend/` returned zero matches outside auth.js's createClient
-- (which uses the SDK only for sign-in / session). All data reads and
-- writes already go through FastAPI. Safe to enable.
--
-- Policy summary:
--   users               patient self-access by token = auth.uid()::text;
--                       clinicians can SELECT every patient row (for the
--                       review dashboard's name lookup).
--   intake_records      patient self-access; clinicians SELECT all.
--   health_records      patient self-access; clinicians SELECT all.
--   protocol_state      patient self-access; clinicians SELECT all.
--   checkins            patient self-access; clinicians SELECT all.
--   protocols           patient SELECT own; clinicians SELECT all (for
--                       the pending dashboard); writes still go through
--                       service role.
--   clinicians          self-SELECT only (a clinician can confirm their
--                       own role); service role manages adds/removes.
--
-- The clinician-SELECT-all policies are scoped by membership in
-- public.clinicians via auth.uid()::text. Re-using the same predicate
-- everywhere keeps the policy surface diffable.

-- ── Enable RLS ────────────────────────────────────────────────────────────
ALTER TABLE public.users           ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.intake_records  ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.health_records  ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.protocol_state  ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.checkins        ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.protocols       ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.clinicians      ENABLE ROW LEVEL SECURITY;

-- ── Clinician-membership helper ───────────────────────────────────────────
-- SECURITY DEFINER + the search_path pin makes this safe to call from
-- USING/WITH CHECK clauses without recursive RLS lookups.
CREATE OR REPLACE FUNCTION public.is_clinician()
RETURNS BOOLEAN
LANGUAGE sql
SECURITY DEFINER
STABLE
SET search_path = public
AS $$
    SELECT EXISTS (
        SELECT 1 FROM public.clinicians WHERE user_id = auth.uid()::text
    );
$$;

-- ── public.users ──────────────────────────────────────────────────────────
DROP POLICY IF EXISTS users_self_select   ON public.users;
DROP POLICY IF EXISTS users_self_modify   ON public.users;
DROP POLICY IF EXISTS users_clinician_select ON public.users;

CREATE POLICY users_self_select ON public.users
    FOR SELECT USING (auth.uid()::text = token);

CREATE POLICY users_self_modify ON public.users
    FOR UPDATE USING (auth.uid()::text = token)
    WITH CHECK (auth.uid()::text = token);

CREATE POLICY users_clinician_select ON public.users
    FOR SELECT USING (public.is_clinician());

-- ── public.intake_records ─────────────────────────────────────────────────
DROP POLICY IF EXISTS intake_self            ON public.intake_records;
DROP POLICY IF EXISTS intake_clinician_select ON public.intake_records;

CREATE POLICY intake_self ON public.intake_records
    FOR ALL USING (auth.uid()::text = token)
    WITH CHECK (auth.uid()::text = token);

CREATE POLICY intake_clinician_select ON public.intake_records
    FOR SELECT USING (public.is_clinician());

-- ── public.health_records ─────────────────────────────────────────────────
DROP POLICY IF EXISTS health_self            ON public.health_records;
DROP POLICY IF EXISTS health_clinician_select ON public.health_records;

CREATE POLICY health_self ON public.health_records
    FOR ALL USING (auth.uid()::text = token)
    WITH CHECK (auth.uid()::text = token);

CREATE POLICY health_clinician_select ON public.health_records
    FOR SELECT USING (public.is_clinician());

-- ── public.protocol_state ─────────────────────────────────────────────────
DROP POLICY IF EXISTS protocol_state_self            ON public.protocol_state;
DROP POLICY IF EXISTS protocol_state_clinician_select ON public.protocol_state;

CREATE POLICY protocol_state_self ON public.protocol_state
    FOR ALL USING (auth.uid()::text = token)
    WITH CHECK (auth.uid()::text = token);

CREATE POLICY protocol_state_clinician_select ON public.protocol_state
    FOR SELECT USING (public.is_clinician());

-- ── public.checkins ───────────────────────────────────────────────────────
DROP POLICY IF EXISTS checkins_self            ON public.checkins;
DROP POLICY IF EXISTS checkins_clinician_select ON public.checkins;

CREATE POLICY checkins_self ON public.checkins
    FOR ALL USING (auth.uid()::text = token)
    WITH CHECK (auth.uid()::text = token);

CREATE POLICY checkins_clinician_select ON public.checkins
    FOR SELECT USING (public.is_clinician());

-- ── public.protocols ──────────────────────────────────────────────────────
-- Patients can read their own protocols (every status). Writes go through
-- the service role; we don't grant an INSERT/UPDATE policy because the
-- write path is /protocols/{id}/approve / /protocols/{id}/reject /
-- protocol_repo.save_pending — all server-side.
DROP POLICY IF EXISTS protocols_self_select     ON public.protocols;
DROP POLICY IF EXISTS protocols_clinician_select ON public.protocols;

CREATE POLICY protocols_self_select ON public.protocols
    FOR SELECT USING (auth.uid()::text = token);

CREATE POLICY protocols_clinician_select ON public.protocols
    FOR SELECT USING (public.is_clinician());

-- ── public.clinicians ─────────────────────────────────────────────────────
-- A clinician can SELECT their own row to confirm their role. Adds /
-- removals are done with service-role keys (seed_clinician.py / Supabase
-- dashboard). No INSERT/UPDATE/DELETE policy = no client-side mutation.
DROP POLICY IF EXISTS clinicians_self_select ON public.clinicians;

CREATE POLICY clinicians_self_select ON public.clinicians
    FOR SELECT USING (auth.uid()::text = user_id);
