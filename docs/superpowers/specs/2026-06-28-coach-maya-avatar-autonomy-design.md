# Coach Maya Avatar Autonomy — Design

Date: 2026-06-28
Branch: `feat/maya-avatar-autonomy`
Status: approved design, pre-implementation

## Problem

The Tavus avatar already shares Coach Maya's brain (BYO-LLM proxy → `coach_chat.chat_stream`), but Maya is a passive drafter: every protocol-affecting tool she calls (`fire_symptom_trigger`, `fire_checkin_trigger`, `fire_weekly_plan_trigger`) writes a `pending_review` row and waits on a clinician. Three gaps block her from being a real patient-facing assistant:

1. Every change is hard-gated, even a patient swapping one already-approved exercise for another appropriate one.
2. Her conversational intake (`start_intake_tool`) captures fields but never produces a first plan.
3. She has no view of protocol version history — only the current active protocol + recent sessions are injected into her prompt.

Plus a latent safety gap: the avatar (voice) path exposes the destructive `fire_intake_trigger` and the full 6-region tool surface.

## Framing / guardrail

Maya is the patient's virtual assistant who acts **within a plan of care the clinician owns**. Low-risk, in-bounds changes happen live; anything that changes the medical direction of care (progressions, new exercises, red flags, a brand-new plan of care) stays clinician-gated. This split is what preserves the billing-legitimacy story (no CPT for AI-only delivery; a licensed PT must own the plan of care).

## Scope (4 upgrades)

### 1. Severity-tiered gates (headline)

New deterministic classifier `backend/change_tier.py`:

```
classify(prior_active, draft, safety_result) -> "auto" | "gate"

AUTO  ⟸  safety_result has no med/high concerns
         AND draft region is in scope (knee, ankle)
         AND change ∈ { regression, swap between exercises already in the
                        current plan or in-library+in-region, volume↓, load↓ }
         AND no brand-new exercise is introduced
GATE  ⟸  otherwise (progression, load↑, new exercise added, any med/high
         safety flag, out-of-scope region)
```

- The classifier is pure/deterministic (no LLM), consistent with the `asyncio.gather` + if/elif orchestration doctrine. It is the load-bearing safety unit and gets a table-driven test suite.
- `auto` path: `protocol_repo.save_active_auto(...)` writes a new `active` version directly, sets `auto_applied=true`, records the superseded version id for one-click revert, and writes a clinician notification row.
- `gate` path: unchanged `save_pending(status="pending_review")`.

New tool **`swap_exercise(from_exercise_id, to_exercise_id, reason)`** — the patient-preference case ("I'd rather do X than Y"). Builds a draft that swaps the named exercise within the current active protocol, runs the safety check + classifier, and auto-applies when clean. Both exercises must be in-library and in-region or it falls to `gate`.

### 2. Intake → auto plan-gen

`start_intake_tool` with `mode="new"` and no required fields missing triggers the full `plan_generation_agent` pipeline after `capture_intake_from_chat` persists. The **first plan of care stays gated** (`pending_review` / `needs_clinician_review`) — a brand-new plan is clinician-owned by definition, so it never auto-applies. Runs async (heavy Anthropic pipeline); Maya tells the patient the plan is drafted and pending clinician review. On a missing-field intake, behavior is unchanged (capture + ask for the rest).

### 3. Historical-protocol read tool

New read tool **`get_protocol_history()`** → recent protocol versions from `protocol_repo` (version, status, date, key exercises, why-changed summary). No gate, no write. Gives Maya the version timeline she lacks today and grounds her `swap_exercise` decisions.

### 4. Voice-tool safety scoping

`coach_chat.chat_stream` gains a `tool_profile: str = "text"` param that filters `TOOLS` before the OpenAI call. The Tavus proxy passes `tool_profile="voice"`, which:
- excludes `fire_intake_trigger` (destructive re-intake — flagged risky on voice in the Tavus memory), and
- region-scopes `list_phase_exercises` / `recommend_exercise` to the patient's in-scope region.

Text chat (`/chat`) keeps the full surface (`tool_profile="text"`).

## Data model

Migration `supabase/migrations/<ts>_protocol_auto_apply.sql` (append-only; through `migration-auditor` before any prod push):

- `protocols.auto_applied boolean NOT NULL DEFAULT false`
- `protocols.supersedes_id uuid NULL` — the active version this auto-apply replaced (revert target). Reuses existing version lineage where present.
- `protocols.reverted_at timestamptz NULL`, `protocols.reverted_by uuid NULL` — set when a clinician reverts.
- RLS: auto-applied rows are still patient-scoped reads + clinician-scoped reads, same as existing protocol rows. Revert writes are clinician-only (`is_clinician()`).

## Endpoints

- `POST /protocols/{id}/revert` — clinician-only (`Depends(current_user_id)` + clinician check). Re-activates `supersedes_id`, stamps `reverted_at/by` on the auto-applied row. Append-only: revert creates the state transition, does not delete.
- Auto-applied feed: extend the existing clinician queue query to return `auto_applied` rows in a distinct "auto-applied — review" group (or a `GET /clinician/auto-applied` if cleaner).

## Frontend

- `clinician.js`: new "Auto-applied — review" feed section above the pending queue; each row shows the DiffNarrator summary + `[Revert]` + `[Acknowledge]`. Reuses existing diff rendering.
- Patient dashboard: when the active protocol is `auto_applied`, show an "Updated by Coach Maya" note on the `review_status` pill area.
- Real-time = on next fetch/reload (no websocket), consistent with current behavior.

## Error handling

- Classifier defaults to `gate` on any ambiguity or exception — fail-safe toward clinician review.
- Auto-apply write failure surfaces a tool_result error to Maya (no silent fake-success), mirroring the existing drafter path.
- Intake→plan pipeline failure: intake is already persisted; Maya reports the plan draft failed and a clinician will follow up. Never blocks the chat/SSE stream.
- Voice tool filter is additive — if `tool_profile` is unknown, default to the safe `voice` set rather than the full set.

## Testing

- `change_tier.classify` — table-driven truth table (regression→auto, swap-in-plan→auto, volume↓→auto, progression→gate, new-exercise→gate, med/high flag→gate, out-of-region→gate, exception→gate). This is the load-bearing suite.
- `swap_exercise` dispatch + executor (clean swap auto-applies; cross-region swap gates).
- Intake→plan trigger (complete new intake fires plan-gen and saves pending; incomplete does not).
- `get_protocol_history` tool returns versions, no write.
- `tool_profile` filter (voice excludes `fire_intake_trigger`; text includes it).
- Auto-apply writer + `/protocols/{id}/revert` round-trip.
- Run full `python3 -m pytest backend/tests/ -q` after backend changes (required for patient-state-mutating endpoints).

## Sequencing

1. Migration + `change_tier.py` classifier + auto-apply writer (the safety spine).
2. Tools: `swap_exercise`, `get_protocol_history`, intake→plan wiring, `tool_profile` filter + proxy pass-through.
3. Endpoints + frontend feed + revert.
4. Clinical sign-off note for Nikki/Kendell on the auto-apply tier boundaries before any real-patient use.

## Out of scope / deferred

- Websocket/push real-time dashboard (stays fetch-on-reload).
- Multi-injury intake schema (PR-L).
- Clinician-face avatar.
- DLP scrubbing of pipeline prompts (separate track; test-data patients only until BAA + DLP).
