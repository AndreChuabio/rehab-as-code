# RehabAsCode

Rehab protocols, with a clinician in the loop on every change. The patient
chats with Coach Maya; when she fires a tool (new symptom, weekly progression,
session check-in), an LLM drafts a protocol revision and writes it as a
`pending_review` row in Supabase. A clinician opens `/clinician`, diffs the
proposal against the active protocol, and approves or rejects it — only
approval flips the row to `active`.

Built at Slop Con NYC 2026-05-02 on a GitHub-PR-as-message-bus architecture.
Migrated to direct Supabase writes post-hackathon (PR #53 introduced the
write path; PR #62 hardened it with RLS lockdown; the cleanup that retired
the cursor / ag2 / cached_replay PR-bus shipped right after). Now in
production: deployed on Vercel, backed by Supabase Postgres + Supabase Auth
(HS256 JWT, magic-link sign-in) for the patient-scoped surfaces. Live at
https://rehab-as-code-five.vercel.app.

## How it works

```
  patient
     │
     ▼
  Coach Maya chat (OpenAI gpt-4o-mini, /chat SSE)
     │  fire_symptom_trigger / fire_checkin_trigger /
     │  fire_weekly_plan_trigger
     ▼
  chat_protocol_drafter (Anthropic claude-sonnet-4-6)
     │  produces a JSON-validated protocol revision
     ▼
  protocol_repo.save_pending(token, payload, "chat:<flow>")
     │  INSERT into `protocols` table, status=pending_review
     ▼                       ┌──────────────────────────────┐
  pending_review row ──────► │ /clinician dashboard          │
                             │ diff vs active                │
                             │ POST /protocols/{id}/approve  │
                             └──────────────┬───────────────┘
                                            ▼
                                  status=active (transactional;
                                  prior active becomes superseded)
```

The Supabase `protocols` table is the message bus. Every protocol revision
is auditable (token, parent_id, created_by_agent, reviewed_by, reviewed_at,
review_notes) and the `(token) WHERE status='active'` partial unique index
keeps "active" singular.

## The four-flow workflow chain

| Trigger button | Endpoint | What happens | UI artifact |
|---|---|---|---|
| `1 intake` | `POST /patient/interact` (auth) | `IntakeAgent` collects the 7-field structured intake; on completion auto-fires `PlanGenerationAgent`, which writes a `pending_review` row | Intake modal → plan-gen modal → pending-review approve card linking to `/clinician` |
| `2 weekly plan` | `POST /chat` tool `fire_weekly_plan_trigger` (auth) | LLM drafts next week's progression from the active protocol; saves as `pending_review` | "weekly plan → review queue" card with Approve button |
| `3 check-in` | `POST /chat` tool `fire_checkin_trigger` (auth) | LLM drafts a load/volume tweak (or returns the active protocol unchanged with a "no edit needed" summary) | "check-in → review queue" card |
| `4 symptom` | `POST /chat` tool `fire_symptom_trigger` (auth) | LLM drafts a regression / substitution and quotes the patient's words verbatim in the summary | "symptom → review queue" card |

The chain state propagates through Supabase: each `approve` supersedes the
prior active row in a single transaction, and the next flow's drafter reads
the new active protocol as its starting point.

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
- **Patient agents**: Anthropic Claude Sonnet 4.6 — `IntakeAgent`
  (structured 7-field intake) and `PlanGenerationAgent` (loads intake +
  wearables + KB, drafts a protocol, saves as `pending_review`). Wired
  to the frontend through `POST /patient/interact` and the intake +
  plan-gen modals
- **Chat coach**: OpenAI `gpt-4o-mini` via the `coach_chat` module — covers
  ongoing coaching after the plan exists. Fires `fire_symptom_trigger`,
  `fire_checkin_trigger`, `fire_weekly_plan_trigger`, and the
  intake-restart admin escape hatch `fire_intake_trigger`. Each fire_*_trigger
  routes through `chat_protocol_drafter` (Anthropic) → `protocol_repo.save_pending`
- **Form-check (in-browser)**: MediaPipe Pose Landmarker + custom rep
  counter; per-set summaries POST to `/pose/session` (one row per set)
- **Video coach (optional)**: Tavus CVI iframe — narrative layer only,
  not an intake/checkin agent
- **Wearables**: Apple Health via iOS Shortcut → `/health-sync`, with Open
  Wearables as an optional read-only source
- **Frontend**: Vanilla JS, no build step

## Why no PR-bus anymore

The hackathon shipped on a CodingAgent abstraction (`cursor_sdk`,
`cursor_github`, `ag2_agent`, `cached_replay`, `mock`) that dispatched
protocol writes through GitHub PRs. That worked for a 5-hour demo but
broke for real patients:

- the `cursor_sdk` Node sidecar (`orchestrator/`) wasn't bundled into the
  Vercel function, so `AGENT_PROVIDER=cursor_sdk` 502'd in production;
- `cached_replay` was being used to mask live-provider failures silently,
  hiding outages from real users;
- splitting the audit trail across GitHub PRs and Supabase rows made
  diffing the active vs proposed protocol painful for clinicians.

PR #53 introduced direct Supabase writes via `protocol_repo.save_pending`.
PR #62 hardened it (RLS lockdown, auth.users provisioning trigger, no
silent fallback on chat 502s). The cleanup that retired the entire
CodingAgent surface (this README's previous "Provider abstraction" section)
shipped right after — the chat path now writes to Supabase directly,
clinicians approve from `/clinician`, and there is no PR-bus.

## Layout

```
rehab-as-code/
  backend/
    main.py                        FastAPI app
    agents/
      __init__.py                  PatientAgent registry (intake, plan_generation)
      base.py                      ABC + dataclasses (PatientAgent,
                                   PatientRequest, PatientResponse)
      intake_agent.py              PatientAgent — structured 7-field intake
                                   chat surfaced through /patient/interact
      plan_generation_agent.py     PatientAgent — loads intake + wearables +
                                   KB, drafts a protocol, saves as
                                   pending_review via protocol_repo.save_pending
    chat_protocol_drafter.py       LLM-driven drafter for chat-tool fires
                                   (Anthropic claude-sonnet-4-6); writes
                                   pending_review rows
    protocol_repo.py               read/write helpers for the `protocols`
                                   table (save_pending, approve, reject,
                                   get_active, list_pending)
    protocol_loader.py             fetches active protocol from Supabase
                                   (PROTOCOL_SOURCE=supabase) or GitHub fallback
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
    clinician.html / .js / .css    /clinician dashboard (pending queue + diff)
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

# 2. Configure env
cp .env.example .env
# Required for the chat coach (OpenAI tool-calling):
#   OPENAI_API_KEY=sk-...
# Required for the protocol drafter + IntakeAgent + PlanGenerationAgent:
#   ANTHROPIC_API_KEY=sk-ant-...
# Required for /chat, /patient/interact, /protocols/* auth (or 401):
#   SUPABASE_JWT_SECRET=...      # Supabase → Settings → API → JWT Secret
# Required for the supabase write path (pending_review rows):
#   DATABASE_URL=postgresql://...           # transaction pooler in prod
#   PROTOCOL_SOURCE=supabase
#   STORAGE_BACKEND=sqlite                  # or postgres in prod

# 3. Boot
python -m uvicorn main:app --reload --app-dir backend --port 8000
# UI:        http://127.0.0.1:8000
# Clinician: http://127.0.0.1:8000/clinician
# Swagger:   http://127.0.0.1:8000/docs
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
  (transaction pooler URL on port 6543), `SUPABASE_JWT_SECRET`,
  `PROTOCOL_SOURCE=supabase`, `OPENAI_API_KEY`, `ANTHROPIC_API_KEY`. The
  `AGENT_PROVIDER` / `DEMO_LIVE_AGENT` / `CURSOR_API_KEY` env vars from
  the hackathon-era PR-bus are no longer read.
- **DATABASE_URL gotcha**: the direct `db.<ref>.supabase.co` URL is
  IPv6-only on new Supabase projects and unreachable from Vercel.
  Use the **session pooler** (port 5432) for local dev and the
  **transaction pooler** (port 6543) for Vercel.

## Endpoints

| Method | Path | Purpose |
|---|---|---|
| GET | `/protocol` | Active protocol for the auth'd patient (Supabase row WHERE status=active), with GitHub fallback |
| GET | `/health-data` | Today's wearable metrics |
| GET | `/calendar` | Today's calendar events |
| POST | `/chat` | OpenAI tool-calling chat; fires `fire_*_trigger` tools that draft pending_review rows. **Requires `Authorization: Bearer <supabase_jwt>`** |
| POST | `/patient/interact` | Auth-gated. Drives the IntakeAgent → PlanGenerationAgent flow that backs the intake + plan-gen modals. `metadata.force = "plan_generation"` re-runs plan generation |
| GET | `/patient/me/intake-status` | Auth-gated. Returns `state ∈ {needs_intake, needs_plan, ready}` so the frontend can route to the right modal |
| GET | `/protocols/pending` | Auth-gated (clinician). Pending-review queue for the dashboard |
| GET | `/protocols/{id}` | Auth-gated (clinician). One protocol with the patient's currently-active row alongside, for diff rendering |
| POST | `/protocols/{id}/approve` | Auth-gated. Promotes a pending_review row to active in a single transaction (supersedes prior active) |
| POST | `/protocols/{id}/reject` | Auth-gated. Marks a pending_review row as rejected; notes required for the audit trail |
| ~~POST `/agent/invoke`~~ ~~GET `/agent/stream/{id}`~~ ~~POST `/pr/apply`~~ ~~POST `/demo/reset`~~ | removed 2026-05-06 | Replaced by `/chat` + `chat_protocol_drafter` + `/protocols/*/approve` |
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

- `protocols/README.md` — what the (now-retired) coding agents used to read and write
- `AGENTS.md` — instructions for AI coding agents pair-programming on the repo
- `PLAN.md` — historical planning doc from the hackathon (cursor cloud agent path C)
