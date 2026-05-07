-- auth.users -> public.users provisioning trigger.
--
-- Today every patient flow goes through ensure_user(token) on the FastAPI
-- side, which lazily inserts a public.users row keyed off the JWT's `sub`
-- claim (auth.uid()). That works for endpoints we've remembered to wire up
-- but leaves a gap: a Supabase Auth user can exist without a matching
-- public.users row, which makes joins, the orphan-row audit, and any
-- future RLS policies awkward.
--
-- This migration closes that gap at the source. Whenever a row is inserted
-- into auth.users, we mirror it into public.users with the matching token.
-- ensure_user() in user_store.py keeps working unchanged (it ON CONFLICT
-- DO UPDATEs last_active), but the row is guaranteed to exist from the
-- moment a clinician or patient signs up — even if they never hit a
-- backend endpoint that calls ensure_user (e.g., a clinician who only
-- visits /clinician).
--
-- Backfill at the bottom: insert a row for any existing auth.users that
-- isn't represented in public.users. As of 2026-05-06 this is exactly
-- one row — the clinician account `nikkihu42@gmail.com`.
--
-- One-way: this migration creates rows in public.users but does NOT
-- delete them when an auth.users row is removed. Patient deletion is a
-- compliance topic that needs its own design pass; the cascade FKs on
-- public.users handle child-row cleanup once a row IS removed.

CREATE OR REPLACE FUNCTION public.handle_new_auth_user()
RETURNS TRIGGER
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public
AS $$
BEGIN
    INSERT INTO public.users (token, created_at, last_active)
    VALUES (
        NEW.id::text,
        COALESCE(NEW.created_at::text, NOW()::text),
        COALESCE(NEW.created_at::text, NOW()::text)
    )
    ON CONFLICT (token) DO NOTHING;
    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS on_auth_user_created ON auth.users;

CREATE TRIGGER on_auth_user_created
    AFTER INSERT ON auth.users
    FOR EACH ROW
    EXECUTE FUNCTION public.handle_new_auth_user();

-- Backfill: copy any missing auth.users into public.users.
INSERT INTO public.users (token, created_at, last_active)
SELECT
    au.id::text,
    COALESCE(au.created_at::text, NOW()::text),
    COALESCE(au.created_at::text, NOW()::text)
FROM auth.users au
LEFT JOIN public.users pu ON pu.token = au.id::text
WHERE pu.token IS NULL;
