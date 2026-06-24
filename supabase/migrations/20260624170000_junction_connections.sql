-- public.junction_connections — one row per patient who linked a wearable via
-- Junction (the rebrand of Vital, https://docs.junction.com). Holds the mapping
-- from our patient token -> Junction user_id plus a small cache of the latest
-- mapped metrics so the dashboard can render REAL sleep / HRV / recovery without
-- a synchronous Junction round-trip on every page load.
--
-- Data flow:
--   POST /api/junction/link  -> create-or-get the Junction user, mint a link
--                               token, upsert a row here in status 'pending'.
--   Junction Link widget     -> patient connects Oura / Garmin / Apple Health.
--   POST /api/junction/refresh (or the svix webhook receiver) -> pull the latest
--                               sleep / activity / readiness, map into the health
--                               schema, write cached_metrics + last_synced_at and
--                               flip status -> 'connected'.
--   get_health_data resolver -> junction-first when status='connected' AND the
--                               cache is fresh; otherwise the existing mock
--                               defaults (real data is purely additive).
--
-- Posture: append-only migration. Status transitions in place
-- (pending -> connected -> error) mirror sessions / protocols / tavus_sessions.
--
-- RLS: patient self-access on everything (auth.uid()::text = token); clinician
-- SELECT-only across all patients so the dashboard can read connection state +
-- cached metrics without a service-role roundtrip. Mirrors the policy shape in
-- 20260507024204_sessions_table.sql and 20260507053230_tavus_sessions.sql.
--
-- PHI hygiene: wearable metrics ARE PHI. vital_user_id is a Junction-side
-- pointer (not the patient's identity) and cached_metrics holds derived scores
-- only. Never log raw metric values or vital_user_id at INFO. The Junction Team
-- API key is server-side only (VITAL_API_KEY env), never stored in this table,
-- never exposed client-side. One row per patient (UNIQUE token) — re-linking a
-- device updates the same row instead of accumulating history.

CREATE TABLE IF NOT EXISTS public.junction_connections (
  id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  token           TEXT NOT NULL UNIQUE
                       REFERENCES public.users(token) ON DELETE CASCADE,
  vital_user_id   TEXT,                       -- Junction/Vital user UUID (nullable until create-user succeeds)
  providers       TEXT[] NOT NULL DEFAULT '{}',-- connected sources, e.g. {oura,garmin}
  status          TEXT NOT NULL DEFAULT 'pending'
                       CHECK (status IN ('pending','connected','error')),
  last_synced_at  TIMESTAMPTZ,                 -- when cached_metrics was last refreshed
  cached_metrics  JSONB,                       -- latest mapped health-schema blob (derived scores only)
  created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- No explicit token index: the UNIQUE constraint on token (above) already
-- creates a unique B-tree index that serves both token lookups (get_by_token)
-- and the ON CONFLICT(token) upsert. A standalone single-column index would be
-- pure write amplification with no query benefit.

ALTER TABLE public.junction_connections ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS junction_connections_self            ON public.junction_connections;
DROP POLICY IF EXISTS junction_connections_clinician_read  ON public.junction_connections;

-- Patient: full self-access (insert / update / select / delete).
CREATE POLICY junction_connections_self ON public.junction_connections
    FOR ALL USING (auth.uid()::text = token)
    WITH CHECK (auth.uid()::text = token);

-- Clinician: SELECT only across all patients (dashboard reads connection state).
CREATE POLICY junction_connections_clinician_read ON public.junction_connections
    FOR SELECT USING (public.is_clinician());
