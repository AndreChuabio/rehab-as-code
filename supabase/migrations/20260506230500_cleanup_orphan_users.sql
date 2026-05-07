-- One-shot data cleanup: remove public.users rows with no auth.users counterpart.
--
-- Background: prior to the auth.users -> public.users provisioning trigger
-- (20260506230000_users_auth_provisioning.sql), patient flows minted public.users
-- rows directly from Slack-onboarding flows or anonymous Apple-Health onboards
-- without ever touching Supabase Auth. The result was a small set of orphan
-- patient rows whose `token` was a fresh UUID with no corresponding auth.uid().
-- These rows can never sign in, so they're effectively dead state — but
-- they're still visible to any code that joins on public.users.token.
--
-- As of 2026-05-06 the affected rows are (recorded here for the audit trail):
--   1aa78e91-ec36-4fdb-bb64-5531022ee42f  Alex      slack=U_alex_v2  (1 intake, 5 checkins, 1 health, 1 protocol_state)
--   2f873155-e861-4ecc-abb7-d7b1b412089e  (none)    slack=U_bob       (no children)
--   64f1f3b9-05bd-49c6-b3e1-aab417343c4b  (none)    slack=null        (no children)
--   1ac7bfaf-9625-40a2-b136-d320fff14cfe  (none)    slack=null        (no children)
--   2a9be0bb-eb69-49ae-8ad5-7c43a2f8a531  (none)    slack=null        (1 health record)
--
-- Cascade delete handles intake_records / health_records / protocol_state /
-- checkins / protocols via the existing ON DELETE CASCADE FKs.
--
-- The cleanup is wrapped in BEGIN/COMMIT so a mid-run failure rolls the
-- whole thing back. Idempotent in the trivial sense: re-running it later
-- finds zero matches and is a no-op.

BEGIN;

DELETE FROM public.users
WHERE token NOT IN (SELECT id::text FROM auth.users);

COMMIT;
