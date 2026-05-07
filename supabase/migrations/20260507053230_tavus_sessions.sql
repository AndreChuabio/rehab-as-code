-- public.tavus_sessions — Tavus CVI conversation audit log.
--
-- One row per `POST /start-session` that successfully created a conversation
-- via Tavus. Lets us:
--   * Surface "Continue your last session" instead of always burning a fresh
--     conversation slot (Tavus charges per conversation create, not per minute
--     used).
--   * Show the clinician the patient's video-call history alongside their
--     chat + pose data.
--   * Mark stale conversations as expired/ended so the patient doesn't see a
--     dead iframe URL on return.
--
-- Posture: append-only migration. No row mutation in place — we transition
-- status (active -> ended | expired | errored) instead, mirroring sessions/
-- protocols. expires_at is the Tavus-side TTL (max_call_duration plus a
-- buffer); ended_at is set when the patient explicitly hits the End button or
-- we mark the row done.
--
-- RLS: patient self-access on everything (auth.uid()::text = token);
-- clinician SELECT-only across all patients so the dashboard can read history
-- without a service-role roundtrip. Mirrors the pattern in
-- 20260507024204_sessions_table.sql.
--
-- PHI hygiene: this table holds NO conversation transcript and NO patient-
-- entered content. conversation_id + replica_id + persona_id are Tavus-side
-- pointers; the actual transcript lives on Tavus and is never persisted here.

CREATE TABLE IF NOT EXISTS public.tavus_sessions (
  id               UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  token            TEXT NOT NULL REFERENCES public.users(token) ON DELETE CASCADE,
  conversation_id  TEXT NOT NULL,
  conversation_url TEXT,
  replica_id       TEXT,
  persona_id       TEXT,
  status           TEXT NOT NULL DEFAULT 'active'
                        CHECK (status IN ('active','ended','expired','errored')),
  created_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  expires_at       TIMESTAMPTZ,
  ended_at         TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS tavus_sessions_token_created_idx
    ON public.tavus_sessions(token, created_at DESC);

ALTER TABLE public.tavus_sessions ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS tavus_sessions_self            ON public.tavus_sessions;
DROP POLICY IF EXISTS tavus_sessions_clinician_read  ON public.tavus_sessions;

-- Patient: full self-access (insert / update / select / delete).
CREATE POLICY tavus_sessions_self ON public.tavus_sessions
    FOR ALL USING (auth.uid()::text = token)
    WITH CHECK (auth.uid()::text = token);

-- Clinician: SELECT only across all patients (history surface).
CREATE POLICY tavus_sessions_clinician_read ON public.tavus_sessions
    FOR SELECT USING (public.is_clinician());
