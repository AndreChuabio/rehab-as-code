-- 20260624155859_tavus_session_ref.sql
--
-- Add an opaque per-conversation reference to tavus_sessions for the BYO-LLM
-- proxy patient mapping. The proxy (api/tavus_proxy.py) recovers which patient
-- a Tavus custom-LLM call belongs to via this value (or conversation_id).
-- The reference is never echoed to model output: the proxy strips the system
-- message that carries it before any model call.
--
-- Lock posture: NULLable column add = no backfill, no table rewrite, no
-- NOT NULL on existing rows. Lock-cheap. The partial UNIQUE index (WHERE
-- session_ref IS NOT NULL) keeps legacy rows with NULL session_ref valid while
-- guaranteeing uniqueness of every minted reference.
--
-- RLS: existing policies tavus_sessions_self and tavus_sessions_clinician_read
-- already cover this table column-wide; no policy change needed.
--
-- Run migration-auditor before applying to prod. Do NOT supabase db push from
-- this change set; author-only.

ALTER TABLE public.tavus_sessions
    ADD COLUMN IF NOT EXISTS session_ref TEXT;

CREATE UNIQUE INDEX IF NOT EXISTS tavus_sessions_session_ref_uidx
    ON public.tavus_sessions (session_ref)
    WHERE session_ref IS NOT NULL;
