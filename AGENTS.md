# AGENTS.md — Wellness Coach AI

> Instructions for any AI coding agent (Copilot, Cursor, Claude, Codex) pair programming on this project.

---

## What This Project Is

A conversational AI wellness coach that:
- Reads your wearable health data (sleep, HRV, recovery)
- Reads your Google Calendar for the day
- Builds a personalized Claude system prompt (Healthmaxx persona)
- Starts a **live, interactive Tavus CVI video session** — the user speaks back and forth with the AI character
- Proactively suggests wellness practices (breathing, meditation, movement)

**Stack:** FastAPI (Python) · Tavus CVI · Claude (Anthropic) · Vanilla JS frontend · gog CLI (Google Cal)

---

## Repo Structure

```
wellness-coach/
├── backend/
│   ├── main.py              ← FastAPI app (start here)
│   ├── context_builder.py   ← Health + calendar → Claude system prompt
│   ├── health_mock.py       ← Mock wearable data + real API stubs
│   ├── calendar_fetch.py    ← gog CLI integration + mock fallback
│   ├── tavus_client.py      ← Tavus CVI session creation
│   └── requirements.txt
├── frontend/
│   ├── index.html           ← Main UI
│   ├── app.js               ← API calls + Tavus iframe logic
│   └── style.css            ← Dark wellness theme
├── cron/
│   └── morning_context.py   ← Standalone 6:30AM pre-build script
├── .env.example             ← Copy to .env, fill in keys
└── README.md
```

---

## Current Status (as of scaffold)

### ✅ Done
- Full project scaffold pushed to GitHub
- FastAPI backend with 4 working endpoints
- Health mock data + real API stubs (Oura, Fitbit, Apple Health)
- Calendar fetch via `gog` CLI with mock fallback
- Context builder: Claude generates Healthmaxx system prompt + greeting + wellness recs
- Tavus CVI session creation (graceful mock if no keys)
- Frontend: dark UI, Tavus iframe, recommendations cards, health stats sidebar
- Cron script for morning pre-build

### 🔧 TODO / Open Work
- [ ] Wire real wearable API (see `health_mock.py` stubs)
- [ ] Set up Tavus Persona + Replica → add to `.env`
- [ ] Test full end-to-end with real Tavus keys
- [ ] Add ElevenLabs voice layer (optional — Tavus has native voice)
- [ ] Polish frontend (animations, better mobile layout)
- [ ] Add a "wellness history" endpoint to track recs over time
- [ ] OpenClaw cron job for morning context pre-build

---

## Key Design Decisions

### Why Tavus CVI (not just audio)?
Real-time video avatar = way more engaging demo. The character lip-syncs live to Claude's responses. Users feel like they're talking to someone, not a chatbot.

### Why pre-build context at 6:30 AM?
So the session starts *instantly* when the user opens the app — no 3-second wait for Claude to generate the system prompt. The `cron/morning_context.py` saves to `context.json` which the backend serves from `/context`.

### Why Healthmaxx?
Warm, caring, non-judgmental, health-obsessed. Perfect wellness coach energy. The Claude prompt instructs the AI to stay in character.

### Why mock data first?
Wearable APIs vary wildly (Oura vs Fitbit vs Apple Health). Mock lets us build + demo the full pipeline without being blocked on OAuth flows. Easy to swap in real data later.

---

## Environment Variables

```bash
cp .env.example .env
```

| Variable | Required | Description |
|---|---|---|
| `ANTHROPIC_API_KEY` | Yes | Claude context generation |
| `TAVUS_API_KEY` | For live video | Tavus CVI sessions |
| `TAVUS_REPLICA_ID` | For live video | Your avatar (create at platform.tavus.io) |
| `TAVUS_PERSONA_ID` | For live video | Your character persona |
| `ELEVENLABS_API_KEY` | Optional | Custom voice (Tavus has native voice) |

> **Without Tavus keys:** the app still runs — it uses mock mode and shows the greeting as text.
> **Without Anthropic key:** context builder falls back to a hardcoded Healthmaxx prompt.

---

## Running Locally

```bash
# Backend
cd backend
pip install -r requirements.txt
cp ../.env.example ../.env   # fill in keys
uvicorn main:app --reload
# → http://localhost:8000
# → http://localhost:8000/docs  (Swagger UI)

# Frontend (no build needed)
open frontend/index.html
# or: cd frontend && python3 -m http.server 3000
```

---

## API Endpoints

| Method | Path | Description |
|---|---|---|
| `GET` | `/health-data` | Today's wearable metrics |
| `GET` | `/calendar` | Today's calendar events |
| `POST` | `/start-session` | Full pipeline → Tavus CVI URL + recs |
| `GET` | `/context` | Pre-built context (from cron) |

Test with Swagger: `http://localhost:8000/docs`

---

## Coding Guidelines

- **Python 3.11+**, type hints preferred
- **No secrets in code** — all keys via `.env` / `os.getenv()`
- **Graceful degradation** — every external API call falls back to mock if keys are missing
- **Keep it hackathon-clean** — readable > clever. Comment the non-obvious stuff.
- **Don't break the mock fallbacks** — demo must work without any API keys

---

## Pair Programming Tips for the Agent

- If you're adding a new endpoint, add it to `main.py` and match the pattern already there
- If you're adding a new wellness recommendation rule, add it to `analyze_health()` in `context_builder.py`
- If you're wiring a real wearable, implement the stub in `health_mock.py` and update `get_health_data()` to call it
- If you're touching the frontend, keep it vanilla JS — no framework, no build step
- The `context.json` file is gitignored (runtime artifact) — don't hardcode paths, use `Path(__file__)` relative resolution
- Run `uvicorn main:app --reload` from the `backend/` directory, not the root

---

## Hackathon Context

- Built at a hackathon, April 2026
- Two-person team (Andre + teammate)
- Time constraint: ship a working demo
- Priority order: **working demo > clean code > full features**
- The Tavus CVI integration is the money shot — prioritize getting that live
