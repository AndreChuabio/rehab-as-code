# RehabAsCode

Rehab protocols as code, updated weekly by Cursor cloud agents, delivered by a
live video coach you can talk to.

Built at Slop Con NYC 2026-05-02. Forked from
[wellness-coach](https://github.com/AndreChuabio/wellness-coach).

## What it does

Andre is week 3 post-ACL reconstruction. His rehab protocol lives in
`AndreChuabio/rehab-protocols-andre/protocol.yaml`. Each week (or whenever a
symptom report comes in mid-session), a Cursor cloud agent:

1. reads the patient's wearable data (HRV, sleep, recovery from Apple Watch)
2. reads the patient's symptom log
3. consults `protocol-library/` for evidence-based progressions
4. opens a PR updating `protocol.yaml` with reasoning + citations
5. a Tavus video avatar (Coach Maya) walks the patient through the new plan

The repo is the message bus. The agent has no other channel back.

## Demo moments

1. **Personalized greeting.** Coach Maya appears, knows you are week 3
   post-ACL, mentions HRV + ROM specifically.
2. **Live agent generates next week's plan.** Click "Generate this week's
   plan." Cursor agent panel streams tool calls, opens a PR, the diff slides
   in.
3. **Live re-plan from voice.** Patient says "knee felt tweaky on single-leg
   squats." Avatar relays it. New Cursor agent task fires. Revised PR opens
   with the regression. Avatar walks the swap.

## Layout

```
rehab-as-code/
  backend/                     FastAPI server
    agents/                    modular CodingAgent abstraction
      base.py                  ABC + dataclasses
      cursor_github.py         primary: @cursor mention via gh CLI
      cursor_api.py            fallback: direct Cursor API (stub)
      cached_replay.py         demo: replay captured trace JSON
      mock.py                  dev: scripted fake
      __init__.py              factory keyed on AGENT_PROVIDER env var
    cached_runs/               pre-captured demo traces
      weekly_plan.json
      symptom_adjustment.json
    main.py                    /protocol /agent/invoke /agent/stream/{id}
    context_builder.py         Tavus persona context (Coach Maya)
    protocol_loader.py         fetch + write to rehab-protocols-andre
    health_mock.py             Apple Watch ingest (reused from wellness-coach)
    calendar_fetch.py          Google Calendar (reused)
    tavus_client.py            Tavus CVI client (reused)
    scripts/
      smoke_test_agents.py     exercise the modular agent layer
  frontend/                    vanilla JS + HTML + CSS
    index.html                 dashboard
    app.js                     SSE consumer for /agent/stream
    style.css                  dark theme
  protocol-repo/               TARGET REPO content (push to GitHub separately)
    protocol.yaml
    protocol-library/
    .cursorrules
    schema.json
    .github/ISSUE_TEMPLATE/rehab-update.md
    README.md
```

## Modular agent layer

`AGENT_PROVIDER` env var selects the implementation:

| Provider         | What it does                                              |
|------------------|-----------------------------------------------------------|
| `cursor_github`  | primary: `@cursor` GitHub mention via gh CLI              |
| `cursor_api`     | fallback: direct Cursor API (stub until access confirmed) |
| `cached_replay`  | demo: replay pre-captured trace from `cached_runs/`       |
| `mock`           | dev: scripted fake (no external calls)                    |

Swapping providers is a config change, not a code change. The rest of the
backend talks only to the abstract `CodingAgent` interface in `agents/base.py`.

## Run locally

```bash
cd backend
pip install -r ../requirements.txt
cp ../.env.example .env  # fill in TAVUS / ANTHROPIC keys
# Backend targets Python 3.11+ (install e.g. Python 3.11 and use `python3.11` if `python3` is older).
AGENT_PROVIDER=cached_replay python3.11 -m uvicorn main:app --reload
```

Open `frontend/index.html` (or `cd frontend && python3 -m http.server 3000`).

Smoke test the modular agent layer:

```bash
cd backend && python3 -m scripts.smoke_test_agents
```

## Open Wearables data source (optional)

`backend/health_mock.py` supports [Open Wearables](https://github.com/the-momentum/open-wearables) as a **read-only** source for normalized wearable data (cloud OAuth providers plus Apple Health-style sync via their stack), while preserving the Apple Shortcut cache and mock fallbacks.

### What you need

1. **Python 3.11+** for this backend (type hints and tooling expect 3.11+).
2. **A running Open Wearables instance** (self-hosted per [their quickstart](https://openwearables.io/docs/quickstart)). Connect your devices there so summaries exist for your user.
3. **An Open Wearables user UUID** — create a user in their developer portal or API; this integration does not create users.
4. **API key** — from the Open Wearables developer portal; server calls use header `X-Open-Wearables-API-Key` (see their [backend integration guide](https://openwearables.io/docs/dev-guides/integration-guide)).
5. **Correct base URL** — `OPEN_WEARABLES_API_URL` must point at **Open Wearables’** API, not RehabAsCode. Both default to port `8000`; run one stack on another port (e.g. `uvicorn main:app --port 8001` for this app) or host OW on a different URL.

### Env vars (in `.env`)

```bash
OPEN_WEARABLES_API_URL=http://localhost:8000
OPEN_WEARABLES_API_KEY=sk-your-open-wearables-key
OPEN_WEARABLES_USER_ID=your-open-wearables-user-uuid
HEALTH_DATA_SOURCE=auto
```

### Source modes

- `auto` (default): try Open Wearables when all three `OPEN_WEARABLES_*` vars are set; else Apple cache/mock
- `open_wearables`: always try Open Wearables first; fallback to cache/mock if the request fails
- `apple_cache`: never call Open Wearables; cache/mock only

Check `GET /health-data`: on success, `source` is `open_wearables`.

## Publish the protocol target repo

The `protocol-repo/` directory is the seed for `AndreChuabio/rehab-protocols-andre`.
Push it once Cursor's GitHub app has the right permissions:

```bash
cd protocol-repo
git init && git add . && git commit -m "seed rehab-protocols-andre"
gh repo create AndreChuabio/rehab-protocols-andre --public --push --source=.
```

Then install the Cursor GitHub App on the new repo so `@cursor` mentions
trigger cloud agent runs.
