from __future__ import annotations
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
import coach_chat

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
        if not val:
            return "MISSING"
        return f"set ({val[:6]}...)"
    return {
        "ANTHROPIC_API_KEY":   mask(os.getenv("ANTHROPIC_API_KEY")),
        "TAVUS_API_KEY":       mask(os.getenv("TAVUS_API_KEY")),
        "TAVUS_REPLICA_ID":    mask(os.getenv("TAVUS_REPLICA_ID")),
        "TAVUS_PERSONA_ID":    mask(os.getenv("TAVUS_PERSONA_ID")),
        "CURSOR_API_KEY":      mask(os.getenv("CURSOR_API_KEY")),
        "OPENAI_API_KEY":      mask(os.getenv("OPENAI_API_KEY")),
        "OPENAI_MODEL":        os.getenv("OPENAI_MODEL") or "gpt-4o-mini",
        "AGENT_PROVIDER":      os.getenv("AGENT_PROVIDER") or "cached_replay",
        "DEMO_LIVE_AGENT":     os.getenv("DEMO_LIVE_AGENT") or "0",
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
    flow: str = "weekly_plan"               # weekly_plan | symptom_adjustment | intake | checkin
    symptom_text: str = ""                  # used for symptom_adjustment
    intake_text: str = ""                   # used for intake
    checkin_text: str = ""                  # used for checkin
    provider: str | None = None             # override AGENT_PROVIDER per call


async def _invoke_with_fallback(
    req: AgentInvokeRequest,
) -> tuple[object, "AgentInvocation"]:
    """Run the agent. On any live-provider failure, fall back to cached_replay.

    The demo path is always cached_replay unless DEMO_LIVE_AGENT=1. This keeps
    the stage deterministic while still letting us flip to the live orchestrator
    for real captures and sponsor-table demos.
    """
    demo_live = os.getenv("DEMO_LIVE_AGENT", "0") == "1"
    provider = req.provider
    if provider is None:
        provider = (
            os.getenv("AGENT_PROVIDER", "cached_replay") if demo_live else "cached_replay"
        )

    health = get_health_data()
    symptom_or_note = req.symptom_text or req.intake_text or req.checkin_text
    context_files = write_context_files(
        flow=req.flow,
        wearables=health,
        symptom_log=symptom_or_note or "(no patient note attached)",
    )
    prompt = _build_agent_prompt(
        flow=req.flow,
        health=health,
        symptom_text=req.symptom_text,
        intake_text=req.intake_text,
        checkin_text=req.checkin_text,
    )
    invocation_request = InvocationRequest(
        repo=PROTOCOL_REPO,
        prompt=prompt,
        context_files=context_files,
        flow=req.flow,  # type: ignore[arg-type]
    )

    agent = get_agent(provider)
    try:
        invocation = await agent.invoke(invocation_request)
        return agent, invocation
    except Exception as exc:
        logger.warning(
            "live agent provider %s failed (%s); falling back to cached_replay",
            provider,
            exc,
        )
        fallback = get_agent("cached_replay")
        invocation = await fallback.invoke(invocation_request)
        return fallback, invocation


@app.post("/agent/invoke")
async def agent_invoke(req: AgentInvokeRequest):
    """Kick off a cloud-agent orchestrated run.

    Provider selection, live/replay gating, and auto-fallback live in
    _invoke_with_fallback(). Every trigger endpoint below funnels through it.
    """
    agent, invocation = await _invoke_with_fallback(req)
    _INVOCATIONS[invocation.invocation_id] = (agent, invocation)
    return {
        "invocation_id": invocation.invocation_id,
        "pr_url": invocation.pr_url,
        "branch": invocation.branch,
        "provider": agent.name,
    }


# ---- Patient-journey trigger endpoints ----------------------------------
# All four trigger endpoints funnel through the same orchestrator. Only the
# `flow` and body fields change. This is the "5 triggers, 1 orchestrator"
# story for the pitch. Apple Health sync stays on /health-sync above.


class IntakeRequest(BaseModel):
    intake_text: str


class CheckinRequest(BaseModel):
    checkin_text: str


class SymptomRequest(BaseModel):
    symptom_text: str


@app.post("/triggers/intake")
async def trigger_intake(body: IntakeRequest):
    """Patient intake form submitted -> initialize protocol."""
    req = AgentInvokeRequest(flow="intake", intake_text=body.intake_text)
    agent, invocation = await _invoke_with_fallback(req)
    _INVOCATIONS[invocation.invocation_id] = (agent, invocation)
    return {
        "trigger": "intake",
        "invocation_id": invocation.invocation_id,
        "pr_url": invocation.pr_url,
        "branch": invocation.branch,
        "provider": agent.name,
    }


@app.post("/triggers/weekly-cron")
async def trigger_weekly_cron():
    """Weekly schedule -> generate next week's protocol."""
    req = AgentInvokeRequest(flow="weekly_plan")
    agent, invocation = await _invoke_with_fallback(req)
    _INVOCATIONS[invocation.invocation_id] = (agent, invocation)
    return {
        "trigger": "weekly-cron",
        "invocation_id": invocation.invocation_id,
        "pr_url": invocation.pr_url,
        "branch": invocation.branch,
        "provider": agent.name,
    }


@app.post("/triggers/checkin")
async def trigger_checkin(body: CheckinRequest):
    """Daily check-in logged -> append to log, evaluate trend."""
    req = AgentInvokeRequest(flow="checkin", checkin_text=body.checkin_text)
    agent, invocation = await _invoke_with_fallback(req)
    _INVOCATIONS[invocation.invocation_id] = (agent, invocation)
    return {
        "trigger": "checkin",
        "invocation_id": invocation.invocation_id,
        "pr_url": invocation.pr_url,
        "branch": invocation.branch,
        "provider": agent.name,
    }


@app.post("/triggers/symptom")
async def trigger_symptom(body: SymptomRequest):
    """Mid-session symptom report -> patch protocol."""
    req = AgentInvokeRequest(flow="symptom_adjustment", symptom_text=body.symptom_text)
    agent, invocation = await _invoke_with_fallback(req)
    _INVOCATIONS[invocation.invocation_id] = (agent, invocation)
    return {
        "trigger": "symptom",
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


def _build_agent_prompt(
    flow: str,
    health: dict,
    symptom_text: str = "",
    intake_text: str = "",
    checkin_text: str = "",
) -> str:
    """Compose the natural-language task addendum for a given flow.

    The orchestrator config's `parent.prompt` + the flow's `addon` already
    carry the role + objective. This function supplies only the
    per-invocation context (wearables summary, patient note) that changes
    between runs.
    """
    summary = (
        f"Wearables snapshot: sleep_score={health.get('sleep_score', 'n/a')}, "
        f"hrv_ms={health.get('hrv_ms', 'n/a')}, "
        f"recovery_score={health.get('recovery_score', 'n/a')}."
    )

    if flow == "symptom_adjustment":
        return (
            f"{summary}\n\n"
            f"Patient symptom report: {symptom_text or '(none)'}\n\n"
            "Cite a regression entry from protocol-library/regressions/ in the PR body."
        )
    if flow == "intake":
        return (
            f"{summary}\n\n"
            f"Intake form: {intake_text or '(none — initialize with phase defaults)'}\n\n"
            "Initialize protocol.yaml from the matching protocol-library/ entry."
        )
    if flow == "checkin":
        return (
            f"{summary}\n\n"
            f"Today's check-in: {checkin_text or '(no note)'}\n\n"
            "Append to log.yaml. Flag any trend that should trigger a follow-up PR."
        )
    # Default: weekly_plan
    return (
        f"{summary}\n\n"
        "Generate the next week's protocol. Evaluate progression criteria on the "
        "current protocol.yaml and consult protocol-library/ for phase-appropriate "
        "progressions."
    )


# -------------------------------------------------------------------------
# Coach chat co-pilot (OpenAI) - /chat SSE endpoint
# -------------------------------------------------------------------------


class ChatTurn(BaseModel):
    role: str                                # "user" | "assistant"
    content: str


class ChatRequest(BaseModel):
    session_id: str = "default"
    message: str
    history: list[ChatTurn] = []


async def _chat_trigger_executor(flow: str, payload: dict) -> dict:
    """Run a /triggers/* equivalent from inside the chat tool dispatch.

    Funnels through the SAME _invoke_with_fallback() as the four buttons,
    so the trace and PR cards on the frontend behave identically. Registers
    the invocation in _INVOCATIONS so the existing /agent/stream/{id}
    consumer can attach.
    """
    req = AgentInvokeRequest(
        flow=flow,
        symptom_text=payload.get("symptom_text", ""),
        intake_text=payload.get("intake_text", ""),
        checkin_text=payload.get("checkin_text", ""),
    )
    agent, invocation = await _invoke_with_fallback(req)
    _INVOCATIONS[invocation.invocation_id] = (agent, invocation)
    return {
        "invocation_id": invocation.invocation_id,
        "pr_url": invocation.pr_url,
        "branch": invocation.branch,
        "provider": agent.name,
        "flow": flow,
    }


@app.post("/chat")
async def chat(req: ChatRequest):
    """Stream a coach chat response. SSE; same framing as /agent/stream/{id}."""
    health = get_health_data()
    protocol_payload = fetch_protocol() or {}
    messages = [
        {"role": turn.role, "content": turn.content} for turn in req.history
    ]
    messages.append({"role": "user", "content": req.message})

    async def gen():
        try:
            async for event in coach_chat.chat_stream(
                messages=messages,
                health=health,
                protocol=protocol_payload,
                trigger_executor=_chat_trigger_executor,
            ):
                yield f"data: {json.dumps(event)}\n\n"
        except Exception as exc:
            logger.exception("chat stream failed")
            err = {"type": "error", "message": str(exc)}
            yield f"data: {json.dumps(err)}\n\n"
        yield "event: done\ndata: {}\n\n"

    return StreamingResponse(gen(), media_type="text/event-stream")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
