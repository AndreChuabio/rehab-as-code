"""
main.py - FastAPI backend for RehabAsCode

Endpoints (existing):
  GET  /health-data         today's wearable metrics
  GET  /calendar            today's calendar events
  POST /start-session       build context + create Tavus CVI session
  GET  /context             pre-built context (cron use case)

Endpoints (new for RehabAsCode):
  GET  /protocol            current rehab protocol (from rehab-protocols-andre)
  POST /agent/invoke        kick off a Cursor cloud agent run, return invocation_id
  GET  /agent/stream/{id}   SSE: stream TraceEvents for an invocation

Agent provider is selected via AGENT_PROVIDER env var (cursor_github,
cursor_api, cached_replay, mock). All endpoints below talk to the abstract
CodingAgent interface only — swapping providers is a config change.
"""

import asyncio
import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel
from dotenv import load_dotenv

load_dotenv()

from health_mock import get_health_data, ingest_shortcut_payload
from calendar_fetch import get_calendar_events
from context_builder import build_system_prompt
from tavus_client import create_conversation
from agents import AgentInvocation, InvocationRequest, get_agent
from protocol_loader import fetch_protocol, write_context_files, PROTOCOL_REPO

logger = logging.getLogger(__name__)

app = FastAPI(title="RehabAsCode", version="0.1.0")

# In-memory store of live invocations so /agent/stream/{id} can find them.
# Keyed by invocation_id, value is the agent that produced it.
_INVOCATIONS: dict[str, tuple] = {}  # invocation_id -> (agent, AgentInvocation)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],   # Tighten in production
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

CONTEXT_FILE = Path(__file__).parent.parent / "context.json"
FRONTEND_DIR = Path(__file__).parent.parent / "frontend"

if FRONTEND_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(FRONTEND_DIR)), name="static")


class StartSessionRequest(BaseModel):
    user_name: str = "there"


class HealthSyncPayload(BaseModel):
    hrv_ms: float | None = None
    resting_hr: float | None = None
    sleep_hours: float | None = None
    steps_yesterday: float | None = None
    calories_burned: float | None = None


@app.get("/")
def root():
    index = FRONTEND_DIR / "index.html"
    if index.exists():
        return FileResponse(str(index))
    return {"status": "ok", "app": "Wellness Coach AI"}


@app.get("/debug-env")
def debug_env():
    """Quick check that all env vars are loaded (values masked)."""
    def mask(val):
        if not val: return "❌ MISSING"
        return f"✅ {val[:6]}..."
    return {
        "ANTHROPIC_API_KEY":   mask(os.getenv("ANTHROPIC_API_KEY")),
        "TAVUS_API_KEY":       mask(os.getenv("TAVUS_API_KEY")),
        "TAVUS_REPLICA_ID":    mask(os.getenv("TAVUS_REPLICA_ID")),
        "TAVUS_PERSONA_ID":    mask(os.getenv("TAVUS_PERSONA_ID")),
        "TRANSITION_API_KEY": mask(os.getenv("TRANSITION_API_KEY")),
    }


@app.post("/health-sync")
def health_sync(payload: HealthSyncPayload):
    """
    Ingest Apple Watch metrics posted by the iOS Shortcut.
    Derives sleep_score and recovery_score, appends to rolling 7-day cache.
    """
    try:
        record = ingest_shortcut_payload(payload.model_dump())
        logger.info("Health sync received: hrv=%s resting_hr=%s sleep=%sh",
                    record.get("hrv_ms"), record.get("resting_hr"), record.get("sleep_hours"))
        return {"status": "ok", "synced_at": datetime.now(timezone.utc).isoformat(), "record": record}
    except Exception as e:
        logger.error("Health sync failed: %s", e)
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/health-sync/status")
def health_sync_status():
    """Report when data was last received from the Shortcut."""
    from health_mock import _load_cache
    cache = _load_cache()
    if not cache:
        return {"status": "no_data", "message": "No Apple Watch data received yet — run the iOS Shortcut."}
    return {
        "status":       "ok" if cache.get("source") == "apple_watch" else "mock",
        "source":       cache.get("source"),
        "last_sync":    cache.get("date"),
        "hrv_ms":       cache.get("hrv_ms"),
        "sleep_hours":  cache.get("sleep_hours"),
    }


@app.get("/health-data")
def health_data():
    """Return today's wearable health metrics."""
    return get_health_data()


@app.get("/calendar")
def calendar():
    """Return today's calendar events."""
    return {"events": get_calendar_events()}


@app.post("/start-session")
def start_session(req: StartSessionRequest = StartSessionRequest()):
    """
    Full pipeline:
    1. Fetch health + calendar data
    2. Build Claude-generated system prompt + greeting
    3. Create Tavus CVI session
    4. Return conversation URL + recommendations
    """
    try:
        health = get_health_data()
        events = get_calendar_events()
        context = build_system_prompt(health, events)

        conversation = create_conversation(
            system_prompt=context["system_prompt"],
            greeting=context["greeting"],
            user_name=req.user_name
        )

        return {
            "conversation_url": conversation["conversation_url"],
            "conversation_id": conversation["conversation_id"],
            "status": conversation["status"],
            "greeting": context["greeting"],
            "recommendations": context["recommendations"],
            "health_summary": {
                "sleep_score": health["sleep_score"],
                "hrv_ms": health["hrv_ms"],
                "recovery_score": health["recovery_score"],
            },
            "event_count": len(events)
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/context")
def get_context():
    """Return the pre-built context from the morning cron job (if available)."""
    if CONTEXT_FILE.exists():
        with open(CONTEXT_FILE) as f:
            return json.load(f)
    # Fall back to building live
    health = get_health_data()
    events = get_calendar_events()
    return build_system_prompt(health, events)


# -------------------------------------------------------------------------
# RehabAsCode endpoints
# -------------------------------------------------------------------------


@app.get("/protocol")
def protocol():
    """Return the current rehab protocol from the rehab-protocols-andre repo."""
    return {"repo": PROTOCOL_REPO, "protocol": fetch_protocol()}


class AgentInvokeRequest(BaseModel):
    flow: str = "weekly_plan"               # weekly_plan | symptom_adjustment
    symptom_text: str = ""                  # used for symptom_adjustment
    provider: str | None = None             # override AGENT_PROVIDER per call


@app.post("/agent/invoke")
async def agent_invoke(req: AgentInvokeRequest):
    """Kick off a Cursor cloud agent run.

    Provider is resolved by the factory in agents/__init__.py — this endpoint
    is provider-agnostic. The same code path works for live Cursor runs and
    cached-replay demo playback.
    """
    agent = get_agent(req.provider)
    health = get_health_data()
    context_files = write_context_files(
        flow=req.flow,
        wearables=health,
        symptom_log=req.symptom_text or "(no symptom report attached)",
    )
    prompt = _build_agent_prompt(req.flow, health, req.symptom_text)

    invocation_request = InvocationRequest(
        repo=PROTOCOL_REPO,
        prompt=prompt,
        context_files=context_files,
        flow=req.flow,  # type: ignore[arg-type]
    )
    invocation = await agent.invoke(invocation_request)
    _INVOCATIONS[invocation.invocation_id] = (agent, invocation)
    return {
        "invocation_id": invocation.invocation_id,
        "pr_url": invocation.pr_url,
        "branch": invocation.branch,
        "provider": agent.name,
    }


@app.get("/agent/stream/{invocation_id}")
async def agent_stream(invocation_id: str):
    """SSE stream of TraceEvents for an invocation."""
    entry = _INVOCATIONS.get(invocation_id)
    if entry is None:
        raise HTTPException(status_code=404, detail="unknown invocation_id")
    agent, _ = entry

    async def gen():
        async for event in agent.stream_trace(invocation_id):
            payload = {
                "type": event.type,
                "timestamp": event.timestamp,
                "label": event.label,
                "payload": event.payload,
            }
            yield f"data: {json.dumps(payload)}\n\n"
        yield "event: done\ndata: {}\n\n"

    return StreamingResponse(gen(), media_type="text/event-stream")


def _build_agent_prompt(flow: str, health: dict, symptom_text: str) -> str:
    """Compose the natural-language task we'll hand to the cloud agent."""
    if flow == "symptom_adjustment":
        return (
            "Patient reported a symptom mid-session. "
            "Adjust the current week's protocol minimally to accommodate.\n\n"
            f"Symptom report: {symptom_text}\n\n"
            "Cite a regression entry from protocol-library/regressions/ in the PR body."
        )
    # Default: weekly plan generation
    return (
        "Generate the next week's protocol for the patient. "
        "Read the just-pushed wearables snapshot, evaluate progression criteria "
        "from the current protocol.yaml, and consult protocol-library/ for the "
        "appropriate next-phase progressions.\n\n"
        f"HRV trend visible in data/wearables-*.json. Current sleep score: "
        f"{health.get('sleep_score', 'n/a')}.\n\n"
        "Open a PR following .cursorrules conventions."
    )


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
