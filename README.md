# RehabAsCode

Rehab protocols as code. A cloud coding agent (AG2 multi-agent pipeline by
default; Cursor SDK as an alternate) reads the patient's wearables and current
protocol, opens a PR with reasoning and library citations, and a clinician
approves it from the chat. Coach Maya (a GPT-4o chat persona, with Tavus video
as an optional surface) walks the patient through the result.

Built at Slop Con NYC 2026-05-02. Now in production: deployed on Vercel,
backed by Supabase Postgres + Supabase Auth (HS256 JWT, magic-link sign-in)
for the patient-scoped surfaces. Live at https://rehab-as-code-five.vercel.app.

## How it works

Five surfaces, one repo:

```
  patient / clinician
        │
        ▼
  Coach Maya (chat or Tavus video)
        │  fires
        ▼
  POST /agent/invoke  ──►  Cursor cloud agent (live)
        │                     │
        │                     ├─ reads protocols/protocol.yaml + .cursorrules
        │                     ├─ reads protocols/protocol-library/**
        │                     ├─ reads protocols/data/wearables-{date}.json
        │                     └─ writes new protocols/protocol.yaml on a branch
        ▼                              │
  SSE trace stream                     ▼
  inline in chat              opens draft PR on rehab-as-code
        │
        │
        ▼
  Clinician clicks "Approve and apply" ──► POST /pr/apply
                                                │
                                                ├─ gh pr ready
                                                └─ gh pr merge --squash
                                                        │
                                                        ▼
                                          Current Protocol card refreshes
                                          (next agent reads the new state)
```

The repo IS the message bus. Every protocol update is a reviewable diff with
cited evidence in the PR body.

## The four-flow workflow chain

| Trigger button | Endpoint | What the agent does | UI artifact |
|---|---|---|---|
| `1 intake` | `POST /patient/interact` (auth) | Run the structured `IntakeAgent` chat in the intake modal; on completion auto-fire `PlanGenerationAgent`, which calls the CodingAgent to write `protocol.yaml` and open a PR | Intake modal → plan-gen modal streaming the AG2 trace → PR card |
| `2 weekly plan` | `POST /chat` tool `fire_weekly_plan_trigger` (auth) | Read current protocol, evaluate progression criteria against wearable trends, advance/hold per `.cursorrules` | Current Protocol card updates to next week |
| `3 check-in` | `POST /chat` tool `fire_checkin_trigger` (auth) | Append today's check-in to `log.yaml`, flag any trend that should trigger a follow-up | log entry visible in PR diff |
| `4 symptom` | `POST /chat` tool `fire_symptom_trigger` (auth) | Patch one exercise in `protocol.yaml` based on the symptom report, cite a regression entry | Current Protocol card shows the patched exercise |

Demo starts empty (`patient: null, phase: pending_intake, exercises: []`).
Each flow's PR must be approved before the next flow runs, so the chain
state propagates through `protocol.yaml` on `main`.

The frontend asks `GET /patient/me/intake-status` on auth-ready and routes
to the right modal based on server-derived state (`needs_intake` →
intake modal, `needs_plan` → plan-gen modal, `ready` → main UI). The
`intake_records` row + `protocol_state.last_pr_url` are the source of truth;
no localStorage flag drives the gating for authed users.

Flows 2-4 can also be fired through chat (Coach Maya parses natural
language and routes via `fire_*_trigger` tools). `fire_intake_trigger` is now
a narrow admin escape hatch — it deletes the patient's intake row so the
modal re-opens on next reload, and is only invoked when the patient explicitly
asks to restart their intake.

## Stack

- **Hosting**: Vercel serverless (`api/index.py` re-exports `backend/main.py`)
- **Database**: Supabase Postgres in production; SQLite locally; flat-file
  legacy. Selected via `STORAGE_BACKEND` env var. Schema lives in
  `supabase/migrations/` and auto-applies on push to main via Supabase's
  GitHub integration
- **Auth**: Supabase Auth on the chat surface — HS256 JWT verified in
  `backend/auth.py`, `auth.uid()` becomes the patient identifier server-side
- **Backend**: FastAPI (Python 3.11+), serves both API and frontend
- **Patient agents**: AG2 / Anthropic Claude Sonnet 4.6 — `IntakeAgent`
  (structured 7-field intake) and `PlanGenerationAgent` (loads intake +
  wearables + KB, calls the CodingAgent to write `protocol.yaml` + open PR).
  Wired to the frontend through `POST /patient/interact` and the intake +
  plan-gen modals
- **Cloud coding agent**: AG2 (`AGENT_PROVIDER=ag2`) is the default live
  path; Cursor SDK / Cursor GitHub mention available as alternates;
  `cached_replay` is the silent demo fallback
- **Chat coach**: OpenAI `gpt-4o-mini` via the `coach_chat` module — covers
  ongoing coaching after the plan exists. Fires `fire_symptom_trigger`,
  `fire_checkin_trigger`, `fire_weekly_plan_trigger`, and the
  intake-restart admin escape hatch `fire_intake_trigger`
- **Form-check (in-browser)**: MediaPipe Pose Landmarker + custom rep
  counter; per-set summaries POST to `/pose/session` (one row per set)
- **Video coach (optional)**: Tavus CVI iframe — narrative layer only,
  not an intake/checkin agent
- **Wearables**: Apple Health via iOS Shortcut → `/health-sync`, with Open
  Wearables as an optional read-only source
- **Frontend**: Vanilla JS, no build step

## Provider abstraction

`AGENT_PROVIDER` env var selects the live path; `DEMO_LIVE_AGENT=1` arms
it. When live fails for any reason, `_invoke_with_fallback()` swaps in
`cached_replay` silently so the demo never dies.

| Provider | What it does | When to use |
|---|---|---|
| `cursor_sdk` | Spawns `tsx orchestrator/src/orchestrator.ts`, calls `@cursor/sdk`, opens real PR | Demo + production path |
| `cached_replay` | Replays a JSON trace from `backend/cached_runs/` with paced timing | Auto-fallback, deterministic stage demo |
| `cursor_github` | Posts an `@cursor` GitHub mention via gh CLI | Backup, no SDK access required |
| `mock` | Scripted fake, no network | Dev / unit tests |

Verify which path actually ran by inspecting the response's `provider`
field, the PR branch name, or the Cloud Agent card badge in the UI:

| Signal | Live (cursor_sdk) | Fallback (cached_replay) |
|---|---|---|
| `provider` | `cursor_sdk` | `cached_replay` |
| Branch | `cursor/<flow>-<random4>` | pinned (e.g. `cursor/week-4-progression-ad45`) |
| PR | new (#N+1) | pinned (e.g. `pull/1`) |

## Demo controls

- **"Approve and apply"** button on each PR result bubble — runs `gh pr
  ready` then `gh pr merge --squash --delete-branch` so the next flow's
  agent reads the updated state
- **"Reset demo"** button at the bottom of the left sidebar — calls
  `POST /demo/reset` which atomically rewrites `protocols/protocol.yaml`
  back to the `pending_intake` empty state via the GitHub contents API
- **Provider badge** on the Cloud Agent card — `cursor_sdk` (live) or
  `cached_replay` (fallback)

## Layout

```
rehab-as-code/
  backend/
    main.py                        FastAPI app
    agents/
      __init__.py                  factory keyed on AGENT_PROVIDER + patient
                                   agent registry (intake, plan_generation)
      base.py                      ABC + dataclasses (CodingAgent, PatientAgent,
                                   TraceEvent, InvocationRequest, etc.)
      ag2_agent.py                 default live coding-agent path (Anthropic
                                   Claude + AG2 multi-agent: repo_reader,
                                   protocol_editor, git_publisher)
      cursor_sdk.py                alternate live path; spawns orchestrator/
      cursor_github.py             @cursor mention via gh CLI
      cached_replay.py             replays JSON traces (silent demo fallback)
      mock.py                      scripted fake
      intake_agent.py              PatientAgent — structured 7-field intake
                                   chat surfaced through /patient/interact
      plan_generation_agent.py     PatientAgent — loads intake + wearables +
                                   KB, calls the CodingAgent, persists
                                   protocol_state.last_pr_url
    cached_runs/
      intake.json                  pinned fallback trace per flow
      weekly_plan.json
      checkin.json
      symptom_adjustment.json
    protocol_loader.py             fetches protocol.yaml via GitHub API
    coach_chat.py                  OpenAI chat with fire_*_trigger tools
    health_mock.py                 wearable data + Apple Health ingest
    open_wearables_client.py       optional read-only Open Wearables source
    user_store.py                  per-user records (3-way pluggable:
                                   flatfile / sqlite / postgres)
    auth.py                        Supabase JWT verification (HS256)
    shortcut_template.py           iOS Shortcut binary plist generator
    calendar_fetch.py              Google Calendar
    context_builder.py             Tavus persona context
    tavus_client.py                Tavus CVI session client
  orchestrator/
    src/
      orchestrator.ts              Node entry point (@cursor/sdk wrapper)
      config.ts                    YAML config loader
    configs/
      care-plan.yaml               parent prompt + sub-agent roster
    package.json                   @cursor/sdk + tsx
  protocols/
    protocol.yaml                  patient's current program (starts empty)
    protocol-library/              evidence-based progressions (read-only)
    .cursorrules                   clinical guardrails for the agent
    schema.json                    protocol.yaml schema
    .demo-snapshots/               snapshots for demo reset
    log.yaml                       check-in log (agent appends)
  frontend/
    index.html                     dashboard + intake modal + plan-gen modal
                                   + auth overlay
    app.js                         SSE consumer + tool calls + Approve/Reset +
                                   patient state machine (refreshPatientState,
                                   showIntakeModal, showPlanGenModal)
    style.css                      dark theme + modal styles
    pose.js                        in-browser MediaPipe form-check
  api/
    index.py                       Vercel entrypoint (re-exports backend/main.py)
  supabase/
    migrations/                    SQL files auto-applied on push to main
                                   via Supabase GitHub integration
  vercel.json                      Vercel build/route config
  requirements.txt                 Vercel installs from THIS file (root)
  backend/requirements.txt         local dev installs from this one — keep in sync
  .env.example                     all required env vars
```

## Run locally

```bash
# 1. Install deps
pip install -r requirements.txt
cd orchestrator && npm install && cd ..

# 2. Configure env
cp .env.example .env
# Required for live cursor_sdk path:
#   CURSOR_API_KEY=crsr_...
#   AGENT_PROVIDER=cursor_sdk
#   DEMO_LIVE_AGENT=1
# Required for chat:
#   OPENAI_API_KEY=sk-...
# Required for context:
#   ANTHROPIC_API_KEY=sk-ant-...
# Required for /chat auth (or POST /chat returns 401):
#   SUPABASE_JWT_SECRET=...      # Supabase → Settings → API → JWT Secret
# Storage backend (default sqlite for local; postgres in production):
#   STORAGE_BACKEND=sqlite

# 3. Boot
python -m uvicorn main:app --reload --app-dir backend --port 8000
# UI: http://127.0.0.1:8000
# Swagger: http://127.0.0.1:8000/docs
```

## Deploy (Vercel + Supabase)

Production: https://rehab-as-code-five.vercel.app

- **Vercel** builds from `vercel.json` + `api/index.py` and installs from
  the **root** `requirements.txt`. If you add a Python dep, update both
  `requirements.txt` files in the same PR or the lambda will crash with
  `ModuleNotFoundError`.
- **Supabase Postgres** is wired via the GitHub integration with
  "Deploy to production" ON — every push to main applies new SQL files
  in `supabase/migrations/<YYYYMMDDHHMMSS>_name.sql`. Never edit a shipped
  migration; ship a new one instead.
- **Required Vercel env vars**: `STORAGE_BACKEND=postgres`, `DATABASE_URL`
  (transaction pooler URL on port 6543), `SUPABASE_JWT_SECRET`, plus the
  same OpenAI/Anthropic/Cursor keys as local.
- **DATABASE_URL gotcha**: the direct `db.<ref>.supabase.co` URL is
  IPv6-only on new Supabase projects and unreachable from Vercel.
  Use the **session pooler** (port 5432) for local dev and the
  **transaction pooler** (port 6543) for Vercel.

## Endpoints

| Method | Path | Purpose |
|---|---|---|
| GET | `/protocol` | Current `protocols/protocol.yaml` from main, fetched via GitHub API (no CDN cache) |
| GET | `/health-data` | Today's wearable metrics |
| GET | `/calendar` | Today's calendar events |
| POST | `/agent/invoke` | Fire an agent run; returns `invocation_id`, `pr_url`, `branch`, `provider` |
| GET | `/agent/stream/{id}` | SSE stream of TraceEvents |
| ~~POST `/triggers/*`~~ | removed 2026-05-06 | Use `/chat` tools (`fire_*_trigger`) or `/agent/invoke` |
| POST | `/patient/interact` | Auth-gated. Drives the IntakeAgent → PlanGenerationAgent flow that backs the intake + plan-gen modals. `metadata.force = "plan_generation"` re-runs plan generation |
| GET | `/patient/me/intake-status` | Auth-gated. Returns `state ∈ {needs_intake, needs_plan, ready}` so the frontend can route to the right modal |
| POST | `/pr/apply` | Mark draft ready then `gh pr merge --squash` (the Approve gesture) |
| POST | `/demo/reset` | Rewrite `protocol.yaml` to `pending_intake` via GitHub contents API |
| POST | `/chat` | OpenAI chat; tools include `fire_intake_trigger` etc. **Requires `Authorization: Bearer <supabase_jwt>` header** |
| POST | `/start-session` | Create Tavus CVI session with Coach Maya persona |
| POST | `/health-sync` | Ingest Apple Watch metrics from iOS Shortcut |
| POST | `/connect/apple-health` | Generate per-user token + onboard URL (QR flow) |
| GET | `/onboard/{token}` | Mobile HTML onboarding page |
| GET | `/shortcut/{token}` | Serve `.shortcut` file for iOS import |

## Open Wearables (optional read-only source)

`backend/open_wearables_client.py` integrates [Open
Wearables](https://github.com/the-momentum/open-wearables) as a normalized
source for cloud-OAuth wearable providers, alongside the Apple Shortcut
cache and mock fallbacks.

Set in `.env`:
```bash
OPEN_WEARABLES_API_URL=http://localhost:8000   # OW server, not this app
OPEN_WEARABLES_API_KEY=sk-your-open-wearables-key
OPEN_WEARABLES_USER_ID=your-open-wearables-user-uuid
HEALTH_DATA_SOURCE=auto    # auto | open_wearables | apple_cache
```

Both stacks default to port 8000; run RehabAsCode on a different port if
both are local. Verify via `GET /health-data`: `source` will be
`open_wearables` on success.

## Sub-docs

- `protocols/README.md` — what the agent reads and writes, how `.cursorrules` constrains it
- `orchestrator/README.md` — `@cursor/sdk` wrapper contract (stdin / stdout / exit codes)
- `AGENTS.md` — instructions for AI coding agents pair-programming on the repo
- `PLAN.md` — historical planning doc (cursor cloud agent path C, sub-agent roster, scope guardrails)
