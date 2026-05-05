# AGENTS.md — RehabAsCode

Two audiences:

1. **AI coding agents** (Cursor, Copilot, Claude, Codex) pair programming on
   this repo. The cloud agent's clinical guardrails live in
   `protocols/.cursorrules`; this file is for harness-level concerns.
2. **Human collaborators returning to the repo** — read the "What changed
   since PR #34" section below before diving into the agents/ directory.
   The agent surface area was rewired between PR #34 (commit `fdb636a`) and
   now; the file map and call paths here are current.

## What changed since PR #34 (for Nikki)

PR #34 (`fdb636a`) introduced a five-agent patient pipeline:
`SessionManagerAgent` → `IntakeAgent` → `PlanGenerationAgent` →
`GuidedVideoAgent` → `CheckInAgent`, all routed through `/patient/interact`
behind a `slack_user_id` / `token` body field.

Three of the five agents are gone, and the routing changed:

| Removed | Why | Replacement |
|---|---|---|
| `SessionManagerAgent` | Hand-rolled token issuance is now redundant — Supabase Auth issues a JWT and `auth.uid()` becomes the patient token everywhere. `current_user_id` (in `backend/auth.py`) is the only entry point | `Depends(current_user_id)` on every patient-facing endpoint |
| `GuidedVideoAgent` | Pose form-check moved fully in-browser via MediaPipe; the agent had nothing to add over the live overlay + voice cues | `frontend/pose.js` + `POST /pose/session` (one row per set) |
| `CheckInAgent` | The form-check pipeline already writes a `set_completion` checkin row per set, and Coach Maya lifts the most recent one into her system prompt | `/pose/session` writes + `coach_chat.fire_checkin_trigger` for explicit narrative check-ins |

`IntakeAgent` and `PlanGenerationAgent` survived but were rewired:

- **Driven by a structured modal**, not an ad-hoc chat fallback. The
  intake modal (`#intakeModal` in `frontend/index.html`) and the plan-gen
  modal (`#planGenModal`) are the only authed-mode entry surfaces. The
  legacy `triggerIntake()` chat flow in `app.js` is now demo-mode-only.
- **Server-derived state.** `GET /patient/me/intake-status` returns
  `state ∈ {needs_intake, needs_plan, ready}` from the
  `intake_records` row + `protocol_state.last_pr_url`. The frontend asks
  this on auth-ready and after every modal close. No `localStorage` flag
  for authed users.
- **Two-state router on `/patient/interact`.** No intake row →
  `IntakeAgent`. `metadata.force == "plan_generation"` → re-run
  `PlanGenerationAgent`. Anything else → 409 (use `/chat`).
- **`PatientInteractionRequest`** dropped `slack_user_id` and `token`
  (auth identifies the patient) and added `history: list[ChatTurn]` so
  the modal stays the only stateful component on the client.
- **`coach_chat.fire_intake_trigger`** is now a narrow admin escape
  hatch: it deletes the intake row via `user_store.delete_intake(token)`
  so the next status check returns `needs_intake` and the modal opens
  again on reload. Used only when the patient explicitly asks to restart.
- **`PlanGenerationAgent.handle()`** has a fallback path
  (`_fallback_direct_pr`) for when `ANTHROPIC_API_KEY` is missing — it
  skips the planner LLM and hands the intake straight to the CodingAgent.
  Keeps the demo workable on Vercel even if planner credentials are
  unset (the CodingAgent itself may still be `cached_replay`).
- **`PlanGenerationAgent` import fix.** `from main import write_context_files`
  was a circular import. It now reads from `protocol_loader` directly.

If you wrote tests against `PatientInteractionRequest.token` or against
`get_patient_agent("session_manager"|"guided_video"|"checkin")`, those
will fail — the registry now only has `"intake"` and `"plan_generation"`.

## What this project is

A FastAPI app where a Cursor cloud agent updates `protocols/protocol.yaml`
each time the patient hits a trigger (intake / weekly plan / check-in /
symptom). The agent reads wearables + library + the current protocol, opens
a draft PR with reasoning + cited library entries, and a clinician approves
it from the UI.

The repo IS the message bus. Don't add side channels.

**Stack**: FastAPI · AG2 (multi-agent pipeline, default coding-agent path,
Anthropic Claude Sonnet 4.6) · `@cursor/sdk` (TypeScript, wrapped by a Node
helper, alternate live path) · OpenAI gpt-4o-mini (chat) · Supabase Postgres
+ Supabase Auth (HS256 JWT, magic-link sign-in) · Tavus CVI (optional video)
· vanilla JS frontend · Vercel hosting.

## Repo layout

See the root `README.md` for the full tree. Quick orientation:

```
backend/
  main.py                   FastAPI app: trigger endpoints + /pr/apply +
                            /patient/interact + /patient/me/intake-status +
                            /chat (auth) + /pose/session (auth)
  agents/
    __init__.py             AGENT_PROVIDER factory (CodingAgent) +
                            patient agent registry (PatientAgent)
    base.py                 ABCs + dataclasses shared by both registries
    ag2_agent.py            default live coding-agent path (Anthropic + AG2)
    cursor_sdk.py           alternate live path; orchestrator/ subprocess
    cached_replay.py        replays JSON traces (silent demo fallback)
    intake_agent.py         IntakeAgent (structured 7-field intake)
    plan_generation_agent.py PlanGenerationAgent (intake + KB + wearables →
                                                   CodingAgent → PR)
  cached_runs/              JSON traces for cached_replay fallback
  coach_chat.py             OpenAI chat. Tools: recommend_exercise,
                            list_phase_exercises, fire_symptom_trigger,
                            fire_checkin_trigger, fire_weekly_plan_trigger,
                            fire_intake_trigger (admin restart escape hatch)
  protocol_loader.py        fetches protocol.yaml via GitHub API (no CDN cache);
                            also owns write_context_files
  user_store.py             3-way pluggable (flat/sqlite/postgres) — owns
                            save_intake / get_intake / delete_intake
  auth.py                   Supabase HS256 JWT verification → current_user_id
orchestrator/
  src/orchestrator.ts       @cursor/sdk wrapper (only used when
                            AGENT_PROVIDER=cursor_sdk)
  configs/care-plan.yaml    parent prompt + sub-agent roster
protocols/
  protocol.yaml             the patient's current program
  protocol-library/         evidence base (read-only for the agent)
  .cursorrules              clinical guardrails
frontend/
  index.html                dashboard + #intakeModal + #planGenModal
  app.js                    state machine: refreshPatientState +
                            showIntakeModal + showPlanGenModal +
                            streamPlanGenTrace, plus the legacy demo flows
  pose.js                   in-browser MediaPipe form-check
```

## Running the backend

```bash
cd backend && uvicorn main:app --reload --host 0.0.0.0 --port 8000
```

Backend serves both API and frontend (mounted at `/static`, `/` returns
`frontend/index.html`). No separate frontend build or dev server needed.

## Provider configuration

`AGENT_PROVIDER` selects the live path; `DEMO_LIVE_AGENT=1` arms it. When
the live path raises, `_invoke_with_fallback()` (`backend/main.py:220-267`)
swaps in `cached_replay` silently — the response's `provider` field
reflects which one actually ran.

| Provider | Notes |
|---|---|
| `ag2` | Default live path. AG2 multi-agent pipeline (repo_reader → protocol_editor → git_publisher) backed by Anthropic Claude Sonnet 4.6. Requires `ANTHROPIC_API_KEY`. |
| `cursor_sdk` | Alternate live path. Spawns `tsx orchestrator/src/orchestrator.ts` via subprocess. Requires `CURSOR_API_KEY` and `cd orchestrator && npm install`. |
| `cached_replay` | Replays JSON from `backend/cached_runs/{flow}.json`. Default fallback when env is unset and the silent safety net inside `_invoke_with_fallback()`. |
| `cursor_github` | `@cursor` GitHub mention via gh CLI. Backup path. |
| `mock` | Scripted fake, no network. Dev / unit tests. |

## Workflow chain (intake → weekly_plan → check-in → symptom)

Each flow opens a PR. State only advances on `main` after the clinician
clicks "Approve and apply" in the UI (which calls `POST /pr/apply` →
`gh pr ready` then `gh pr merge --squash`). The next flow's agent reads
the freshly-merged `main` via `protocol_loader.fetch_protocol()` (which
uses the GitHub contents API — never `raw.githubusercontent.com`, that
has a CDN cache).

Do not bypass this. Direct commits to `protocols/protocol.yaml` will
break the audit story. Only the Reset demo button is allowed to write
`protocol.yaml` directly (it nukes back to `pending_intake`).

## .env setup gotcha

`.env.example` ships with placeholder values like `your_anthropic_key_here`.
These are truthy — code paths that check `if os.getenv("X")` will attempt
real API calls and fail with 401. When running without real keys, clear the
placeholder values to empty strings so mock fallbacks activate.

For the live `cursor_sdk` path you need at minimum:
```
CURSOR_API_KEY=crsr_...
AGENT_PROVIDER=cursor_sdk
DEMO_LIVE_AGENT=1
OPENAI_API_KEY=sk-...        # for chat
ANTHROPIC_API_KEY=sk-ant-... # for Tavus context generation
```

## Agent smoke tests

```bash
cd backend && python3 -m scripts.smoke_test_agents
```

Exercises mock, cached_replay, and cursor_sdk providers. The cached_replay
provider paces trace events in real-time by default (~30s+); the
cursor_sdk provider hangs without `CURSOR_API_KEY` and the orchestrator
installed. For quick validation, test mock and cached_replay individually
or pass a high speed multiplier (`CachedReplayAgent(speed=100.0)`).

## PyJWT conflict

The base VM image ships with a system-managed `PyJWT 2.7.0` that pip
cannot uninstall. Run `pip install --ignore-installed pyjwt` before
`pip install -r requirements.txt` to work around this.

## Key endpoints for testing

| Method | Path | Notes |
|---|---|---|
| GET | `/protocol` | Always works (falls back to local stub if API unreachable) |
| GET | `/health-data` | Always works (mock data if no Apple Watch sync) |
| GET | `/calendar` | Mock fallback if Google creds missing |
| POST | `/agent/invoke` | Live or cached based on env flags above |
| GET | `/agent/stream/{id}` | SSE; pair with the `invocation_id` from invoke |
| POST | `/pr/apply` | Body: `{"pr_url": "..."}` or `{"pr_number": N}` |
| POST | `/demo/reset` | Wipes protocol.yaml back to `pending_intake` on main |
| POST | `/patient/interact` | **Auth.** Two-state router: no intake → IntakeAgent, `metadata.force=plan_generation` → PlanGenerationAgent. Body: `{message, history[], metadata}`. No `token` field — auth resolves it |
| GET | `/patient/me/intake-status` | **Auth.** Returns `{state, has_intake, has_protocol, last_pr_url, ...}` for the frontend state machine |
| POST | `/chat` | **Auth.** SSE; `coach_chat.chat_stream` with `user_token` threaded so `fire_intake_trigger` can call `user_store.delete_intake` |
| POST | `/pose/session` | **Auth.** One row per set into `checkins` with `payload.kind = "set_completion"` |
| POST | `/start-session` | Tavus CVI session create |
| GET | `/docs` | Swagger UI |
| GET | `/debug-env` | All env vars surfaced (values masked) |

## Coding guidelines

- Python 3.11+, type hints preferred
- No secrets in code — all keys via `.env` / `os.getenv()`
- Graceful degradation — every external API call falls back to mock when
  keys are missing
- Don't break the mock fallbacks — demo must work without any API keys
- Don't write to `protocols/protocol.yaml` directly except via the
  agent's PR flow or the demo reset
- Don't add emojis to docs / commits / code
- Frontend is vanilla JS — no framework, no build step
- Run `uvicorn` from the repo root with `--app-dir backend`, not from
  inside `backend/`

## Pair programming tips

- New endpoint: add to `backend/main.py`, match existing pattern. If the
  endpoint is patient-scoped, gate with `Depends(current_user_id)` and
  pass the `user_id` to anything downstream — never trust a `token` from
  the request body
- New trigger flow: add config in `orchestrator/configs/care-plan.yaml`,
  new `_build_agent_prompt` branch in `main.py`, new `/triggers/X`
  endpoint that funnels through `_invoke_with_fallback`
- New CodingAgent provider: add a class implementing `CodingAgent` (see
  `agents/base.py`) and register in `agents/__init__.py:get_agent`
- New PatientAgent: subclass `PatientAgent`, decorate with
  `@register_patient_agent`, and add a route in `/patient/interact`
  that resolves which one to invoke. Don't reintroduce a router agent —
  the dispatch is intentionally explicit in `main.py` so the state machine
  stays readable
- New chat tool: add in `coach_chat.py` tool registry; if it mutates
  patient state, accept `user_token` via `_dispatch_tool(... user_token=...)`
  and reach into `user_store` directly instead of POSTing to a
  `/triggers/...` endpoint
- Touching the trace UI: `streamTrace` in `frontend/app.js` consumes the
  SSE stream and renders inline as a chat bubble; `streamPlanGenTrace`
  does the same thing for the plan-gen modal

## Origin and current posture

- Built at Slop Con NYC 2026-05-02 (single day). Two-person team (Andre + Nikki Hu).
- **Hackathon mode is over.** The repo is now in production posture: real
  Supabase auth, real Postgres, real patients in scope. Priority order is
  now patient safety > production reliability > velocity. Tests are
  required for backend changes that mutate patient state. Migrations are
  append-only. No silent fallbacks masking real bugs. See the project
  CLAUDE.md for the full operating manual.
- `ag2` is the default live coding-agent path; `cursor_sdk` is an alternate;
  `cached_replay` is the silent safety net that catches anything that
  breaks live. `cached_replay` should not be used to mask production bugs
  — surface errors instead.
- The Approve and apply button is the clinician safety gate. AG2 opens a
  draft PR; Cursor cloud agents auto-merge their own PRs in seconds, so
  the Approve handler treats "already merged" as success on the cursor
  path. Don't add an auto-merge for clinical content.
