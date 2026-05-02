# AGENTS.md — RehabAsCode

Instructions for AI coding agents (Cursor, Copilot, Claude, Codex) pair
programming on this repo. The cloud agent's clinical guardrails live in
`protocols/.cursorrules`; this file is for harness-level concerns.

## What this project is

A FastAPI app where a Cursor cloud agent updates `protocols/protocol.yaml`
each time the patient hits a trigger (intake / weekly plan / check-in /
symptom). The agent reads wearables + library + the current protocol, opens
a draft PR with reasoning + cited library entries, and a clinician approves
it from the UI.

The repo IS the message bus. Don't add side channels.

**Stack**: FastAPI · `@cursor/sdk` (TypeScript, wrapped by a Node helper) ·
OpenAI gpt-4o-mini (chat) · Anthropic (context) · Tavus CVI (optional
video) · vanilla JS frontend.

## Repo layout

See the root `README.md` for the full tree. Quick orientation:

```
backend/
  main.py                   FastAPI app + 4 trigger endpoints + /pr/apply
  agents/                   provider abstraction (cursor_sdk live primary)
  cached_runs/              JSON traces for cached_replay fallback
  coach_chat.py             OpenAI chat with fire_*_trigger tools
  protocol_loader.py        fetches protocol.yaml via GitHub API (no CDN cache)
orchestrator/
  src/orchestrator.ts       @cursor/sdk wrapper (subprocess from Python)
  configs/care-plan.yaml    parent prompt + sub-agent roster
protocols/
  protocol.yaml             the patient's current program
  protocol-library/         evidence base (read-only for the agent)
  .cursorrules              clinical guardrails
frontend/
  app.js                    SSE consumer + Approve / Reset / chat
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
| `cursor_sdk` | Live primary. Spawns `tsx orchestrator/src/orchestrator.ts` via subprocess. Requires `CURSOR_API_KEY` and `cd orchestrator && npm install`. |
| `cached_replay` | Replays JSON from `backend/cached_runs/{flow}.json`. Default fallback. |
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
| POST | `/start-session` | Needs cleared API keys for mock fallback |
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

- New endpoint: add to `backend/main.py`, match existing pattern
- New trigger flow: add config in `orchestrator/configs/care-plan.yaml`,
  new `_build_agent_prompt` branch in `main.py`, new `/triggers/X`
  endpoint that funnels through `_invoke_with_fallback`
- New provider: add a class implementing `CodingAgent` (see `agents/base.py`)
  and register in `agents/__init__.py:get_agent`
- New chat tool: add in `coach_chat.py` tool registry; if it fires an
  agent, follow the `fire_*_trigger` naming so the frontend lights up
  the team-mini strip in the Cloud Agent card
- Touching the trace UI: `streamTrace` in `frontend/app.js`, consumes the
  SSE stream and renders inline as a chat bubble

## Hackathon context

- Built at Slop Con NYC 2026-05-02 (single day)
- Two-person team (Andre + Nikki Hu)
- Priority order: working demo > clean code > full features
- `cursor_sdk` is the live primary path; `cached_replay` is the silent
  safety net that catches anything that breaks live
- The Approve and apply button is the visible clinician-gate gesture
  for the pitch story; Cursor cloud agents auto-merge their own PRs
  in seconds, so the Approve handler treats "already merged" as success
