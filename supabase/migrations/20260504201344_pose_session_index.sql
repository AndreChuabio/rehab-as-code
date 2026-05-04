-- Speeds up "most recent live set this exercise" lookups for the form-check
-- workflow. Live-set rows are stored in `checkins` with payload->>'kind' =
-- 'set_completion'. Maya's chat system prompt reads the latest one so she
-- can acknowledge what the patient just did in their next reply.
CREATE INDEX IF NOT EXISTS idx_checkins_kind_exercise
  ON checkins (token, ((payload->>'exercise_id')), recorded_at DESC)
  WHERE payload->>'kind' = 'set_completion';
