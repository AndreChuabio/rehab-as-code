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
the cursor / ag2 / cached_replay PR-bus shipped right after).

The 2026-05-07 productionization sprint replaced the single-LLM drafter
with a deterministic multi-agent pipeline (researcher + trend_analyst in
parallel → evaluator → planner → safety_reviewer), added a `DiffNarrator`
and `SymptomClassifier` for the trust loop, made today's-session a
DB-backed training log, added a `start_intake_tool` so Maya can run intake
conversationally, hardened Tavus video calls with auth + persistence, and
swapped the dull palette for the Clinical Twilight color system. Now in
production: deployed on Vercel, backed by Supabase Postgres + Supabase
Auth (HS256 / ES256 JWT, magic-link or email+password sign-in). Live at
https://rehab-as-code-five.vercel.app.

The 2026-06-24 sprint made the video coach a real two-way agent. The Tavus
avatar now runs Coach Maya's own brain via a **bring-your-own-LLM** proxy
(`/tavus/llm/chat/completions`) instead of a generic hosted model; it counts
the patient's real calf-raise reps live (MediaPipe pose on the call's camera
→ the avatar speaks each *detected* rep via the Tavus echo interaction); and
it wires real wearable data through **Junction** (the Vital rebrand) behind
`get_health_data`, with the existing mock as the documented fallback. All
three shipped to production. See `docs/ARCHITECTURE.md` for the system shape.

## How it works

```
  patient
     │
     ▼
  Coach Maya chat (OpenAI gpt-4o-mini, /chat SSE)
     │  pre-flight: symptom_classifier (Haiku 4.5) when pain keywords
     │  tools: start_intake_tool, fire_symptom_trigger,
     │         fire_checkin_trigger, fire_weekly_plan_trigger,
     │         fire_intake_trigger (admin), recommend_exercise,
     │         list_phase_exercises
     ▼
  Plan generation pipeline (deterministic orchestration):
     │
     │   ┌─────────────────────┐    ┌─────────────────────┐
     │   │ researcher          │    │ trend_analyst       │
     │   │ (Sonnet 4.6)        │    │ (Sonnet 4.6)        │
     │   │ candidates from KB  │    │ multi-week patterns │
     │   └──────────┬──────────┘    └──────────┬──────────┘
     │              │   asyncio.gather         │
     │              └────────────┬─────────────┘
     │                           ▼
     │              ┌─────────────────────────┐
     │              │ evaluator (Sonnet 4.6)  │
     │              │ progress | hold | regress
     │              └────────────┬────────────┘
     │                           ▼
     │              ┌─────────────────────────┐
     │              │ planner (Sonnet 4.6)    │
     │              │ composes protocol YAML  │
     │              └────────────┬────────────┘
     │                           ▼
     │              ┌─────────────────────────┐
     │              │ safety_reviewer         │
     │              │ (Sonnet 4.6)            │
     │              │ pain ceiling / contra-  │
     │              │ indication / hold rules │
     │              └────────────┬────────────┘
     ▼                           ▼
  protocol_repo.save_pending(token, payload, source_metadata,
                             status=pending_review|needs_clinician_review,
                             safety_concerns=[...])
     │
     ▼                       ┌──────────────────────────────────┐
  Clinician dashboard ─────► │ DiffNarrator (Haiku 4.5)         │
                             │ 2-3 sentence plain-English diff   │
                             │ Patient-at-a-glance card          │
                             │ Safety concerns banner (red)      │
                             │ Region-mismatch banner (amber)    │
                             │ Last-7-days adherence panel       │
                             │ POST /protocols/{id}/approve      │
                             └──────────────┬───────────────────┘
                                            ▼
                                  status=active (transactional;
                                  prior active becomes superseded)
                                            │
                                            ▼
                             Patient sees review_status pill flip
                             from pending → approved + "Start
                             today's session" CTA appears
```

The Supabase `protocols` table is the message bus. Every protocol revision
is auditable (token, parent_id, created_by_agent, reviewed_by, reviewed_at,
review_notes, safety_concerns) and the `(token) WHERE status='active'`
partial unique index keeps "active" singular per patient.

**Supabase is the canonical source of truth.** Patient name, intake fields,
protocol exercises, sessions, checkins, and Tavus conversations all live
in Postgres tables with RLS. The frontend never reads patient identity
from denormalized JSON; it resolves through `get_display_name(token)` which
walks `intake_records → auth.users.full_name → email local-part → "the
patient"`.

## The four-flow workflow chain

| Trigger | Endpoint | What happens | UI artifact |
|---|---|---|---|
| Start intake | `POST /chat` tool `start_intake_tool` (auth) — or modal-driven `POST /patient/interact` for new patients | Maya runs the 7-field intake conversationally; on completion the multi-agent plan pipeline fires, writing a `pending_review` row | "intake → captured" card → review queue card linking to `/clinician` |
| Draft next week | `POST /chat` tool `fire_weekly_plan_trigger` (auth) | Multi-agent pipeline drafts next week's progression from the active protocol; safety_reviewer attaches concerns; saves as `pending_review` (or `needs_clinician_review` on high severity) | "weekly plan → review queue" card |
| Log a check-in | `POST /chat` tool `fire_checkin_trigger` (auth) | Multi-agent pipeline drafts a load/volume tweak (or returns the active protocol unchanged with a "no edit needed" summary) | "check-in → review queue" card |
| Report a symptom | `POST /chat` tool `fire_symptom_trigger` (auth) — or pre-flight `symptom_classifier` (Haiku 4.5) when patient mentions pain keywords | LLM drafts a regression / substitution and quotes the patient's words verbatim. High-severity classifier output writes a `needs_clinician_review` row directly. | "symptom → review queue" card; high severity gets red banner on clinician queue |

Pre-flight `symptom_classifier` runs before Maya generates her response
whenever the patient's message contains pain keywords. It reads the
patient's message + last 24h of wearables + current protocol + last
session's pose metrics, and emits one of `minor | hold-load |
clinician-attention`. `clinician-attention` writes a `needs_clinician_review`
row and Maya tells the patient she has flagged it for review.

The chain state propagates through Supabase: each `approve` supersedes the
prior active row in a single transaction, the next flow's pipeline reads
the new active protocol as its starting point, and the patient surface
flips its `review_status` pill from `pending` → `approved` and surfaces a
"Start today's session" CTA.

The frontend asks `GET /patient/me/intake-status` on auth-ready and routes
to the right modal based on server-derived state (`needs_intake` →
intake modal or conversational intake via Maya, `needs_plan` → plan-gen
modal, `ready` → main UI). The endpoint also returns `display_name`,
`last_active`, and `review_status` so the header pill and Maya's greeting
stay in sync. The `intake_records` row + `protocol_state.last_pr_url` are
the source of truth; no localStorage flag drives the gating for authed
users.

### State-aware Maya greeting

On auth-resolve, Maya's opening line is selected from four branches keyed
on `intake_status.state`:

- `needs_intake` (new patient) → "Hi, I'm Maya. I'll ask a few quick
  questions to set up your plan…"
- `needs_plan` (intake done, awaiting plan) → "Welcome back. Let me draft
  your first week…"
- `ready & last_active < 48h` → "Good to see you again. Anything new
  since yesterday?"
- `ready & last_active ≥ 48h` → "Welcome back — it's been a few days.
  How's the recovery going?"

This is computed server-side and rendered before the patient types.

Flows 2-4 can also be fired through chat (Coach Maya parses natural
language and routes via `fire_*_trigger` tools). `fire_intake_trigger` is now
a narrow admin escape hatch — it deletes the patient's intake row so the
modal re-opens on next reload, and is only invoked when the patient explicitly
asks to restart their intake.

## Stack

- **Hosting**: Vercel serverless (`api/index.py` re-exports `backend/main.py`)
- **Database**: Supabase Postgres in production with RLS on every public
  table; SQLite locally; flat-file legacy. Selected via `STORAGE_BACKEND`
  env var. Schema lives in `supabase/migrations/` (append-only) and
  auto-applies on push to main via Supabase's GitHub integration
- **Auth**: Supabase Auth on every patient-scoped surface — HS256 + ES256
  JWT verified via JWKS in `backend/auth.py`, `auth.uid()` becomes the
  patient identifier server-side. Magic-link and email+password sign-in
  are both supported; self-service sign-up + password set are wired
- **Backend**: FastAPI (Python 3.11+), serves both API and frontend
- **Plan generation pipeline** (Anthropic Claude Sonnet 4.6 throughout):
  `researcher` → reads `protocols/protocol-library/` and pulls
  evidence-based candidates with citations; `trend_analyst` → looks at the
  last 4-8 weeks of checkins / sessions / wearables and emits
  `plateau | breakthrough | regression | steady`; `evaluator` → consumes
  both fans and emits `progress | hold | regress` with confidence;
  `planner` → composes the protocol YAML; `safety_reviewer` → enforces
  pain ceilings, contraindication, hold rules, frequency limits.
  Orchestrated deterministically via `asyncio.gather` + sequential
  composition. Never LLM-routed.
- **Chat coach**: OpenAI `gpt-4o-mini` via the `coach_chat` module. Tools:
  `start_intake_tool` (conversational intake), `fire_symptom_trigger`,
  `fire_checkin_trigger`, `fire_weekly_plan_trigger`,
  `fire_intake_trigger` (admin escape hatch), `recommend_exercise`,
  `list_phase_exercises`. Pre-flight `symptom_classifier` (Haiku 4.5)
  fires when the patient mentions pain keywords
- **DiffNarrator** (Anthropic Haiku 4.5): generates a 2-3 sentence
  plain-English summary of pending protocol diffs for clinicians,
  returned via `narrator_status` enum (`ok | no_diff | no_api_key |
  sdk_error | empty_response`) so the UI can distinguish "summary
  unavailable" failure modes
- **Body-region anchoring**: `clinical_taxonomy.resolve_body_region(injury_type)`
  + `exercise_kb.body_region_for(exercise_id)` ensure drafter and planner
  refuse cross-region exercises. Post-LLM validator catches stragglers.
- **Form-check (in-browser)**: MediaPipe Pose Landmarker + custom rep
  counter; per-set summaries POST to `/pose/session` (one row per set).
  Guided exercise mode (PR-J) speaks set / rep cues and real-time form
  corrections via the Web Speech API. Also runs **live inside the Tavus
  video call** (fed the call's local camera track) so the avatar coaches
  real reps — see "Video coach" below
- **Video coach (Tavus CVI, bring-your-own-LLM)**: `/start-session`
  (`Depends(current_user_id)`) creates a conversation against a custom
  persona and persists to `tavus_sessions` (now with a per-conversation
  `session_ref`). The persona runs in **BYO-LLM mode** — its LLM layer
  points at our OpenAI-compatible proxy `backend/api/tavus_proxy.py`
  (`POST /tavus/llm/chat/completions`, shared-secret auth) which drives the
  **same `coach_chat` brain as the text chat** (no duplication, same tools +
  clinician-review safety loop). Patient identity on a static persona is
  recovered via the opaque `session_ref` (or Tavus `conversation_id`) and
  stripped before the model. The call embeds via the **Daily JS SDK** so the
  client can send Tavus interactions. **Live rep-counting**: during a
  calf-raise set, `pose.js` runs MediaPipe on the call's camera and, on each
  *detected* rep, fires a `conversation.echo` so Maya speaks the count + a
  form cue — counting is gated strictly on real movement
- **Wearables**: real multi-device data via **Junction** (the Vital rebrand;
  300+ devices — Oura, Garmin, Fitbit, Withings, Whoop…) behind the single
  `get_health_data` seam (`backend/junction_client.py` + `backend/api/junction.py`
  + the `junction_connections` table); **fail-opens to the mock defaults**
  when not connected so the pipeline + avatar never break. Apple Health via
  iOS Shortcut → `/health-sync` and Open Wearables remain as additional
  sources
- **Frontend**: Vanilla JS, no build step. Clinical Twilight palette
  (cool navy-slate dark mode, teal CTA, sage success, warm tan AI-accent).
  See `frontend/DESIGN_SYSTEM.md`.

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
      plan_generation_agent.py     thin orchestrator: gathers researcher +
                                   trend_analyst, runs evaluator, planner,
                                   safety_reviewer; writes via protocol_repo
      researcher.py                Sonnet 4.6 — KB-grounded candidate exercises
      trend_analyst.py             Sonnet 4.6 — 4-8 week pattern detection
      evaluator.py                 Sonnet 4.6 — progress | hold | regress
      planner.py                   Sonnet 4.6 — composes protocol YAML
      safety_reviewer.py           Sonnet 4.6 — pain / contraindication /
                                   hold rules; sets needs_clinician_review
      symptom_classifier.py        Haiku 4.5 — pre-flight pain triage on /chat
    chat_protocol_drafter.py       legacy single-call drafter for chat-tool
                                   fires; preserved for fire_checkin_trigger /
                                   fire_symptom_trigger short paths
    diff_narrator.py               Haiku 4.5 — 2-3 sentence plain-English
                                   protocol diff summary for clinicians;
                                   returns narrator_status enum
    clinical_taxonomy.py           resolve_body_region(injury_type) + region
                                   guards used by drafter / planner
    exercise_kb.py                 exercise library lookup; body_region_for()
    protocol_repo.py               read/write helpers for `protocols` table
                                   (save_pending, approve, reject, get_active,
                                   list_pending, attach_safety_concerns)
    protocol_loader.py             fetches active protocol from Supabase
    session_repo.py                read/write helpers for `sessions` (training
                                   log) — plan / start / complete an exercise
    tavus_repo.py                  read/write helpers for `tavus_sessions`
    coach_chat.py                  OpenAI chat with start_intake_tool +
                                   fire_*_trigger tools + state-aware greeting
    health_mock.py                 wearable data + Apple Health ingest
    open_wearables_client.py       optional read-only Open Wearables source
    user_store.py                  per-user records (3-way pluggable);
                                   owns get_display_name(token) resolver
    auth.py                        Supabase JWT verification (HS256 + ES256
                                   via JWKS) → current_user_id, is_clinician
    shortcut_template.py           iOS Shortcut binary plist generator
    calendar_fetch.py              Google Calendar
    context_builder.py             Tavus persona context (live per-patient block)
    tavus_client.py                Tavus CVI conversation client (+ session_ref)
    patient_context.py             per-patient factories shared by /chat + the proxy
    api/tavus_proxy.py             BYO-LLM proxy: /tavus/llm -> coach_chat (avatar brain)
    api/junction.py                Junction wearable connect / refresh / status routes
    junction_client.py             Junction (Vital) API client + health-schema mapping
    junction_repo.py               read/write helpers for junction_connections
  protocols/
    protocol.yaml                  patient's current program (starts empty)
    protocol-library/              evidence-based progressions (read-only)
    schema.json                    protocol.yaml schema
    log.yaml                       check-in log
  knowledge/
    exercise-library.json          full exercise library (publicly readable)
  frontend/
    index.html                     dashboard + intake modal + plan-gen modal
                                   + auth overlay
    app.js                         SSE consumer + tool calls + Approve +
                                   patient state machine + sessions integration
                                   + state-aware Maya greeting render
    style.css                      Clinical Twilight palette + components
    pose.js                        in-browser MediaPipe form-check; guided
                                   exercise mode with spoken cues / corrections
    clinician.html / .js / .css    /clinician dashboard (pending queue + diff
                                   + narrator summary + safety banner +
                                   region-mismatch banner + adherence panel)
    DESIGN_SYSTEM.md               token + component + Figma → code rules
  api/
    index.py                       Vercel entrypoint (re-exports backend/main.py)
  supabase/
    migrations/                    SQL files auto-applied on push to main via
                                   Supabase GitHub integration. Latest (2026-06-24):
                                   tavus_sessions.session_ref + junction_connections
                                   (RLS: patient-self + clinician-read)
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
# Video coach (Tavus CVI + BYO-LLM avatar):
#   TAVUS_API_KEY=...  TAVUS_REPLICA_ID=...  TAVUS_PERSONA_ID=...
#   TAVUS_PROXY_SECRET=...                   # shared secret == persona llm.api_key
# Real wearables (Junction / Vital):
#   VITAL_API_KEY=sk_us_...  JUNCTION_ENV=sandbox  JUNCTION_REGION=us
#   JUNCTION_REDIRECT_URL=https://<your-app>/

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
| GET | `/protocol` | Active protocol for the auth'd patient (Supabase row WHERE status=active) |
| GET | `/health-data` | Today's wearable metrics |
| GET | `/calendar` | Today's calendar events |
| GET | `/exercises` | Public exercise library (full catalog, optional `?phase=` filter) |
| POST | `/chat` | OpenAI tool-calling chat; pre-flight `symptom_classifier`; fires `start_intake_tool`, `fire_*_trigger` tools that draft pending_review rows. State-aware greeting based on intake-status. **Auth required.** |
| POST | `/patient/interact` | Auth-gated. Drives the IntakeAgent → multi-agent plan pipeline that backs the intake + plan-gen modals. `metadata.force = "plan_generation"` re-runs plan generation |
| GET | `/patient/me/intake-status` | Auth-gated. Returns `{state, display_name, last_active, review_status, has_intake, has_protocol, ...}` so the frontend can route to the right modal and render the header pill + state-aware greeting |
| GET | `/protocols/pending` | Auth-gated (clinician). Pending-review queue, sorted with `needs_clinician_review` first |
| GET | `/protocols/{id}` | Auth-gated (clinician). One protocol + active alongside + `narrator_summary` (DiffNarrator output) + `narrator_status` enum + `safety_concerns` |
| POST | `/protocols/{id}/approve` | Auth-gated. Promotes a pending_review row to active transactionally |
| POST | `/protocols/{id}/reject` | Auth-gated. Marks a pending_review row as rejected; notes required |
| GET | `/sessions/today` | Auth-gated (patient). Today's planned + completed exercises |
| POST | `/sessions` | Auth-gated. Plan an exercise into today |
| PATCH | `/sessions/{id}` | Auth-gated. Mark completed, attach pose_metrics |
| GET | `/sessions/last7?token=...` | Auth-gated (clinician). Adherence panel data |
| POST | `/pose/session` | Auth-gated. One row per set into `checkins` and (if attached to a planned session) updates `sessions` |
| POST | `/checkins` | Auth-gated. Manual narrative check-ins (auto-fired card after pose session) |
| POST | `/start-session` | Auth-gated. Create a Tavus CVI conversation (custom BYO-LLM persona); mints a `session_ref`, persists to `tavus_sessions` |
| GET | `/tavus/sessions/recent` | Auth-gated. Patient's recent Tavus sessions (powers "Continue last session") |
| POST | `/tavus/sessions/{id}/end` | Auth-gated. End an active Tavus session |
| POST | `/tavus/llm/chat/completions` | **Shared-secret, not patient JWT.** OpenAI-compatible SSE proxy that Tavus CVI calls as its custom LLM; recovers the patient (session_ref / conversation_id) and streams Coach Maya's response |
| POST | `/api/junction/link` | Auth-gated. Create-or-get the patient's Junction user + return a hosted-Link URL |
| POST | `/api/junction/refresh` | Auth-gated. Pull + cache latest sleep / HRV / recovery from Junction |
| GET | `/api/junction/status` | Auth-gated. Wearable connection state for the panel |
| POST | `/api/junction/demo-connect` | Auth-gated, **sandbox only**. Connect a synthetic device to test without a real wearable |
| POST | `/health-sync` | Ingest Apple Watch metrics from iOS Shortcut |
| POST | `/connect/apple-health` | Generate per-user token + onboard URL (QR flow) |
| GET | `/onboard/{token}` | Mobile HTML onboarding page |
| GET | `/shortcut/{token}` | Serve `.shortcut` file for iOS import |
| ~~POST `/agent/invoke`~~ ~~GET `/agent/stream/{id}`~~ ~~POST `/pr/apply`~~ ~~POST `/demo/reset`~~ ~~POST `/triggers/*`~~ | removed 2026-05-06 | Replaced by `/chat` + multi-agent pipeline + `/protocols/*/approve` |

## Wearables (Junction / Vital)

`backend/junction_client.py` + `backend/api/junction.py` integrate
[Junction](https://docs.junction.com) (the rebrand of Vital) as the real
wearable source — one API for 300+ devices (Oura, Garmin, Fitbit, Withings,
Whoop…). It slots in behind the single `get_health_data(token)` seam and
**fail-opens to the mock defaults** on not-connected / stale / error, so the
clinical pipeline and the avatar always have data. Flow: the patient clicks
"Connect health data" → `POST /api/junction/link` → Junction's hosted Link →
on return, `POST /api/junction/refresh` pulls + caches the mapped metrics into
the `junction_connections` table and the panel badge flips to "Live".

```bash
VITAL_API_KEY=sk_us_...          # x-vital-api-key, server-side only, never client
JUNCTION_ENV=sandbox             # sandbox | production
JUNCTION_REGION=us               # us | eu  -> https://api.{sandbox.}{region}.junction.com
JUNCTION_REDIRECT_URL=https://rehab-as-code-five.vercel.app/
```

Sandbox supports **synthetic-device** connections for testing without a real
wearable (`POST /api/junction/demo-connect`). Notes: Apple Health is
iOS-SDK-only (not in the web Link flow); a production key + real patient PHI
require Junction's **BAA** (sandbox/test patients only until then). On success
`GET /health-data` returns `source: junction` and `isLive: true`.

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

- `frontend/DESIGN_SYSTEM.md` — Clinical Twilight palette, components, Figma → code translation rules
- `AGENTS.md` — instructions for AI coding agents pair-programming on the repo
- `protocols/README.md` — historical context on the (retired) PR-bus
- `PLAN.md` — historical planning doc from the hackathon
