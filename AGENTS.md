# AGENTS.md — RehabAsCode

Two audiences:

1. **AI coding agents** (Cursor, Copilot, Claude, Codex) pair programming
   on this repo.
2. **Human collaborators returning to the repo** — read the "What changed
   since PR #62" section before diving into `backend/agents/`.

## Productionization sprint update (2026-05-07)

The night of 2026-05-06 → 2026-05-07 shipped 19 PRs that moved the app
from hackathon-stage to productionized. The single-LLM `chat_protocol_drafter`
that handled plan generation was replaced with a deterministic multi-agent
pipeline; trust loop closed in both directions; today's-session became
DB-backed; Tavus video hardened; palette swapped.

Current write path:

- **Plan generation** runs through `backend/agents/plan_generation_agent.py`,
  which is now a thin orchestrator:
  ```
  asyncio.gather(
      researcher.candidates(intake, history),     # KB-grounded candidates
      trend_analyst.analyze(checkins, sessions)   # 4-8wk pattern
  )
    → evaluator.signal(intake, health, trend)     # progress | hold | regress
    → planner.compose(candidates, signal, intake) # protocol YAML
    → safety_reviewer.check(payload, intake)      # pain / contra / hold rules
    → protocol_repo.save_pending(token, payload, source_metadata,
                                  status, safety_concerns)
  ```
  All five agents are Anthropic Sonnet 4.6. Orchestration is deterministic
  (asyncio.gather + sequential composition). Never LLM-routed.
- **`safety_reviewer`** can flip status to `needs_clinician_review` with a
  `safety_concerns` JSONB array when severity is high. Clinician dashboard
  surfaces these to the top of the queue with a red banner.
- **`symptom_classifier`** (Anthropic Haiku 4.5) runs as a pre-flight on
  `/chat` whenever the patient mentions pain keywords, before Maya
  generates her response. `clinician-attention` severity writes a
  `needs_clinician_review` row directly. `hold-load` injects a regression
  suggestion into Maya's system prompt. `minor` is acknowledged inline.
- **`diff_narrator`** (Anthropic Haiku 4.5) generates a 2-3 sentence
  plain-English summary of pending protocol diffs for clinicians, returned
  on `GET /protocols/{id}` via `narrator_status` enum (`ok | no_diff |
  no_api_key | sdk_error | empty_response`).
- **`start_intake_tool`** is now a chat tool — Maya runs intake
  conversationally instead of bouncing the patient to the modal. The modal
  is preserved for new-patient first-time flows.
- **Body-region anchoring** is enforced by `clinical_taxonomy.resolve_body_region(injury_type)`
  + `exercise_kb.body_region_for(exercise_id)`. Drafter and planner refuse
  cross-region exercises. Post-LLM validator catches stragglers.
- **Today's-session** is now a DB-backed training log (`sessions` table).
  `frontend/app.js` reads/writes via `/sessions/today`, `POST /sessions`,
  `PATCH /sessions/{id}`. Adherence rolls up onto the clinician detail
  pane.
- **Tavus video calls** were unauthenticated (PR-O audit found). PR-P
  added `Depends(current_user_id)` on `/start-session`, persistence to
  `tavus_sessions` table, identity passthrough so Maya knows the patient.
- **Clinical Twilight palette** (PR #83) replaced the dull palette. See
  `frontend/DESIGN_SYSTEM.md` for tokens + semantic rules.

## Post-PR-bus update (2026-05-06)

The cursor / ag2 / cached_replay PR-bus surface (`/agent/invoke`,
`/agent/stream`, `_invoke_with_fallback`, `CodingAgent`, `cached_runs/`,
the orchestrator/ Node sidecar, `AGENT_PROVIDER`, `DEMO_LIVE_AGENT`,
`CURSOR_API_KEY`) was retired right after PR #62. None of those env vars
are read anymore. None of those modules exist anymore.

Current write path (still accurate as of 2026-05-07):

- Chat tool fires call into `chat_protocol_drafter` (legacy single-call
  drafter, preserved for symptom + check-in short paths) or the multi-agent
  plan pipeline (weekly plan + post-intake), both writing `pending_review`
  rows via `protocol_repo.save_pending`.
- `/patient/interact` runs `IntakeAgent` → multi-agent plan pipeline.
- Clinicians approve drafts on `/clinician`, which hits
  `POST /protocols/{id}/approve` to flip the row to `active`
  transactionally.
- No GitHub PR is opened anywhere in the runtime path.

## What changed since PR #62 (for Nikki)

PR #62 left a single-LLM drafter (`chat_protocol_drafter`) handling all
plan generation. The 2026-05-07 sprint replaced that for the heavy
plan-gen path with a five-agent pipeline. Things you'll notice:

| Was | Now |
|---|---|
| `PlanGenerationAgent.run()` made one Anthropic call | Calls `researcher` + `trend_analyst` in parallel, then `evaluator`, `planner`, `safety_reviewer`. Same public signature, callers unchanged |
| Symptom messages went straight to Maya | Pre-flight `symptom_classifier` (Haiku) classifies severity; high severity writes a `needs_clinician_review` row directly |
| Clinician saw raw JSON diff | `DiffNarrator` (Haiku) writes a 2-3 sentence plain-English summary; clinician sees patient-at-a-glance card + summary + diff |
| Today's-session was localStorage | DB-backed `sessions` table with RLS; survives refresh; clinician sees adherence |
| Tavus video had no auth | `Depends(current_user_id)` on `/start-session` + `tavus_sessions` persistence |
| Patient saw no review state | `review_status` pill in header + state-aware Maya greeting + "Start today's session" CTA after approval |
| Single-injury assumption | `clinical_taxonomy.resolve_body_region()` + region guards (multi-injury still deferred — see PR-L) |

Public function signatures preserved for `IntakeAgent`,
`PlanGenerationAgent`, `protocol_repo.save_pending`, `coach_chat.chat_stream`.
You can pick up cold without rewiring callers.

`PatientInteractionRequest` still drops `slack_user_id` and `token` (auth
identifies the patient). `coach_chat.fire_intake_trigger` is still the
admin escape hatch (deletes the intake row to re-open the modal).
`coach_chat.start_intake_tool` is the new conversational intake entry
point — Maya runs the 7 intake questions inline.

## What this project is

A FastAPI app where Coach Maya (OpenAI gpt-4o-mini, tool-calling) runs
the patient experience and a deterministic multi-agent pipeline drafts
protocol revisions. Every revision is a `pending_review` row in Supabase
that a clinician approves through `/clinician`. Approval supersedes the
prior active row in a single transaction.

**Stack**: FastAPI · Anthropic Claude Sonnet 4.6 (researcher / evaluator /
planner / safety_reviewer / trend_analyst) · Anthropic Haiku 4.5
(symptom_classifier / diff_narrator) · OpenAI gpt-4o-mini (Maya) · Supabase
Postgres + Supabase Auth (HS256 + ES256 JWT, magic-link or password) ·
Tavus CVI (auth + persisted) · MediaPipe BlazePose (in-browser) · vanilla
JS frontend · Vercel hosting.

## Repo layout

See the root `README.md` for the full tree. Quick orientation:

```
backend/
  main.py                    FastAPI app — all endpoints
  agents/
    __init__.py              PatientAgent registry (intake, plan_generation)
    base.py                  ABCs + dataclasses
    intake_agent.py          structured 7-field intake
    plan_generation_agent.py thin orchestrator (5-agent pipeline)
    researcher.py            Sonnet — KB candidates with citations
    trend_analyst.py         Sonnet — multi-week pattern detection
    evaluator.py             Sonnet — progress | hold | regress
    planner.py               Sonnet — composes protocol YAML
    safety_reviewer.py       Sonnet — pain / contraindication / hold rules
    symptom_classifier.py    Haiku — pain triage pre-flight on /chat
  diff_narrator.py           Haiku — plain-English diff summary
  chat_protocol_drafter.py   legacy single-call drafter (still used for
                             symptom + check-in short paths)
  clinical_taxonomy.py       resolve_body_region + region guards
  exercise_kb.py             exercise library + body_region_for()
  protocol_repo.py           save_pending / approve / reject / list_pending
  protocol_loader.py         fetch active protocol from Supabase
  session_repo.py            sessions training-log read/write
  tavus_repo.py              tavus_sessions read/write
  coach_chat.py              OpenAI chat — start_intake_tool, fire_*_trigger,
                             recommend_exercise, list_phase_exercises;
                             state-aware greeting render
  user_store.py              owns get_display_name(token) resolver
  auth.py                    HS256 + ES256 JWT (JWKS) → current_user_id,
                             is_clinician
protocols/
  protocol.yaml              patient's current program (starts empty)
  protocol-library/          evidence base (read-only for the agent)
  schema.json                protocol.yaml schema
knowledge/
  exercise-library.json      full library (publicly readable via /exercises)
frontend/
  index.html                 dashboard + #intakeModal + #planGenModal +
                             review_status pill in header
  app.js                     state machine: refreshPatientState +
                             showIntakeModal + showPlanGenModal +
                             sessions integration + state-aware greeting
  pose.js                    MediaPipe form-check; guided exercise mode
                             (spoken cues / corrections)
  clinician.html / .js / .css  /clinician dashboard
  style.css                  Clinical Twilight palette
  DESIGN_SYSTEM.md           token + component spec
supabase/
  migrations/                10 applied as of 2026-05-07
```

## Running the backend

```bash
cd backend && uvicorn main:app --reload --host 0.0.0.0 --port 8000
```

Backend serves both API and frontend (mounted at `/static`, `/` returns
`frontend/index.html`). No separate frontend build or dev server.

## Required env vars

```
OPENAI_API_KEY=sk-...        # Coach Maya
ANTHROPIC_API_KEY=sk-ant-... # plan pipeline + diff_narrator + symptom_classifier
SUPABASE_JWT_SECRET=...      # Settings → API → JWT Secret (HS256)
SUPABASE_URL=https://...     # JWKS lookup for ES256 (PR #58)
DATABASE_URL=postgresql://...transaction-pooler-on-6543...
PROTOCOL_SOURCE=supabase
STORAGE_BACKEND=postgres     # postgres in prod; sqlite locally
```

`AGENT_PROVIDER`, `DEMO_LIVE_AGENT`, `CURSOR_API_KEY` are no longer read.

## Workflow chain (intake → weekly_plan → check-in → symptom)

Each flow writes a `pending_review` row in `protocols`. State only
advances on approval:

1. Clinician opens `/clinician`, sees the queue with `needs_clinician_review`
   rows pinned to the top with a red banner.
2. Detail pane renders the DiffNarrator summary, patient-at-a-glance card,
   safety concerns banner (red), region-mismatch banner (amber), last-7-days
   adherence panel, and the JSON diff.
3. `POST /protocols/{id}/approve` flips the row to `active` in a single
   transaction; prior active becomes `superseded`.
4. The next flow's pipeline reads the new active protocol as its starting
   point. Patient surface flips `review_status` pill from `pending` to
   `approved` and surfaces a "Start today's session" CTA.

Do not bypass this. The audit trail (`token, parent_id, created_by_agent,
reviewed_by, reviewed_at, review_notes, safety_concerns`) is the
clinician's defense in a chart review. Never write a status='active' row
from a runtime path other than `/protocols/{id}/approve`.

## Key endpoints for testing

| Method | Path | Notes |
|---|---|---|
| GET | `/protocol` | Active protocol; falls back gracefully if Supabase is unreachable |
| GET | `/health-data` | Mock data if no Apple Watch sync |
| GET | `/calendar` | Mock fallback if Google creds missing |
| GET | `/exercises` | Public library (no auth required) |
| POST | `/chat` | **Auth.** SSE; pre-flight symptom_classifier; tool calls write to Supabase |
| POST | `/patient/interact` | **Auth.** Two-state router. No intake → IntakeAgent. `metadata.force=plan_generation` → multi-agent pipeline. No `token` in body |
| GET | `/patient/me/intake-status` | **Auth.** Returns `{state, display_name, last_active, review_status, has_intake, has_protocol, ...}` |
| GET | `/protocols/pending` | **Auth (clinician).** Sorted with `needs_clinician_review` first |
| GET | `/protocols/{id}` | **Auth (clinician).** Includes `narrator_summary`, `narrator_status`, `safety_concerns` |
| POST | `/protocols/{id}/approve` | **Auth.** Transactional active swap |
| POST | `/protocols/{id}/reject` | **Auth.** Notes required |
| GET | `/sessions/today` | **Auth (patient).** Today's planned + completed |
| POST | `/sessions` | **Auth.** Plan an exercise into today |
| PATCH | `/sessions/{id}` | **Auth.** Mark completed + attach pose_metrics |
| GET | `/sessions/last7?token=...` | **Auth (clinician).** Adherence panel |
| POST | `/pose/session` | **Auth.** One row per set; updates linked session |
| POST | `/checkins` | **Auth.** Manual narrative check-ins |
| POST | `/start-session` | **Auth.** Tavus CVI; persisted to `tavus_sessions` |
| GET | `/tavus/sessions` | **Auth.** Patient's Tavus history |
| GET | `/docs` | Swagger UI |
| GET | `/debug-env` | Env vars surfaced (values masked) |

## Coding guidelines

- Python 3.11+, type hints preferred
- No secrets in code — all keys via `.env` / `os.getenv()`
- No silent fallbacks masking real errors. `cached_replay` is gone for a
  reason. Surface 500s; toast 401s
- **Migrations are append-only.** New SQL file under
  `supabase/migrations/<YYYYMMDDHHMMSS>_name.sql`. Never edit a shipped
  migration
- **Tests are required for backend changes that mutate patient state**:
  pytest covering the happy path + the auth-rejected path. ~250 tests
  pass as of merge of PR #83
- Don't write to `protocols/protocol.yaml` directly. The runtime is
  Supabase-canonical
- Don't add emojis to docs / commits / code
- Frontend is vanilla JS — no framework, no build step, no new dependencies
  without discussion
- Run `uvicorn` from the repo root with `--app-dir backend`, not from
  inside `backend/`

## Pair programming tips

- **New endpoint**: add to `backend/main.py`. If patient-scoped, gate with
  `Depends(current_user_id)` and pass the `user_id` to anything downstream.
  Never trust a `token` from the request body
- **New plan-pipeline agent**: drop a module in `backend/agents/`, accept
  structured inputs, return a Pydantic model. Wire it into
  `plan_generation_agent.py` orchestration. Don't introduce LLM-routed
  orchestration where if/elif suffices
- **New chat tool**: add in `coach_chat.py` tool registry; if it mutates
  patient state, accept `user_token` via `_dispatch_tool(... user_token=...)`
  and reach into the right repo (`protocol_repo`, `session_repo`,
  `user_store`) directly
- **New PatientAgent**: subclass `PatientAgent`, decorate with
  `@register_patient_agent`, add a route in `/patient/interact` that
  resolves which one to invoke. Don't reintroduce a router agent — the
  dispatch is intentionally explicit so the state machine stays readable
- **Schema change**: write the migration, run it locally with `supabase db
  push`, verify in Supabase Studio, then commit. The Supabase Preview
  branch CI marks migrations as applied without running DDL on prod —
  always verify via `supabase migration list --db-url $DATABASE_URL` after
  merge
- **Color / component changes**: read `frontend/DESIGN_SYSTEM.md` first.
  Use the semantic tokens (`--accent`, `--success`, `--ai-accent`); don't
  introduce raw hex mid-stylesheet

## Origin and current posture

- Built at Slop Con NYC 2026-05-02 (single day). Two-person team
  (Andre + Nikki Hu). Won the hackathon.
- **Hackathon mode is over.** Repo is in production posture: real Supabase
  auth, real Postgres, real patients in scope. Priority: patient safety >
  production reliability > velocity. See the project CLAUDE.md for the
  full operating manual.
- The Approve handler is the clinician safety gate. Don't add an
  auto-merge for clinical content. Don't introduce a path that writes
  `status='active'` from anywhere other than `/protocols/{id}/approve`.
- Multi-injury support (PR-L) is deferred. Decision pending: one protocol
  per body_region with `(token, body_region) WHERE status='active'` partial
  unique index, or single active protocol with multi-region exercises.
  Needs Nikki's clinical input.
