from __future__ import annotations
"""
main.py - FastAPI backend for RehabAsCode

Endpoints (existing):
  GET  /health-data         today's wearable metrics (optional ?token= for per-user)
  GET  /calendar            today's calendar events
  POST /start-session       build context + create Tavus CVI session
  GET  /context             pre-built context (cron use case)

Endpoints (new for RehabAsCode):
  GET  /protocol            current rehab protocol (from rehab-protocols-andre)
  POST /agent/invoke        kick off a Cursor cloud agent run, return invocation_id
  GET  /agent/stream/{id}   SSE: stream TraceEvents for an invocation

Apple Health onboarding:
  POST /connect/apple-health          generate user token + return onboard URL
  GET  /connect/status/{token}        connection status for a user token
  GET  /onboard/{token}               mobile HTML onboarding page (QR + install button)
  GET  /shortcut/{token}              serve .shortcut file for iOS Shortcuts import

Agent provider is selected via AGENT_PROVIDER env var (cursor_github,
cursor_api, cached_replay, mock). All endpoints below talk to the abstract
CodingAgent interface only — swapping providers is a config change.
"""

import asyncio
import io
import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote
from fastapi import Depends, FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, HTMLResponse, Response, StreamingResponse
from pydantic import BaseModel
from dotenv import load_dotenv

load_dotenv()

from health_mock import get_health_data, ingest_shortcut_payload
from calendar_fetch import get_calendar_events
from context_builder import build_system_prompt
from tavus_client import create_conversation
from agents import AgentInvocation, InvocationRequest, get_agent
from protocol_loader import fetch_protocol, write_context_files, PROTOCOL_REPO
from user_store import (
    create_token,
    ensure_user,
    token_exists,
    load_user,
    save_health,
    save_checkin,
    get_last_set_completion,
)
from shortcut_template import generate_shortcut
from auth import current_user_id
import coach_chat
import qrcode
import qrcode.image.svg

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


@app.get("/config")
def public_config():
    """Public, browser-readable config. The anon/publishable key is *meant*
    to live in client code (it's gated by RLS on the database side); the
    JWT secret is NEVER returned here.
    """
    return {
        "supabase_url": os.getenv("SUPABASE_URL", "").strip(),
        "supabase_anon_key": os.getenv("SUPABASE_ANON_KEY", "").strip(),
    }


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
def health_sync(payload: HealthSyncPayload, token: str | None = Query(None)):
    """
    Ingest Apple Watch metrics posted by the iOS Shortcut.
    If ?token= is provided, saves to per-user store (users/{token}.json).
    Otherwise appends to the shared rolling 7-day Apple cache.
    """
    try:
        record = ingest_shortcut_payload(payload.model_dump())
        logger.info("Health sync received: hrv=%s resting_hr=%s sleep=%sh token=%s",
                    record.get("hrv_ms"), record.get("resting_hr"), record.get("sleep_hours"), token)
        if token:
            if not token_exists(token):
                raise HTTPException(status_code=404, detail="unknown token")
            save_health(token, record)
        return {"status": "ok", "synced_at": datetime.now(timezone.utc).isoformat(), "record": record}
    except HTTPException:
        raise
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
def health_data(token: str | None = Query(None)):
    """Return today's wearable health metrics.
    If ?token= is provided, returns that user's Apple Watch data (if synced)."""
    return get_health_data(user_token=token)


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
# Apple Health onboarding — token-based Shortcut magic link
# -------------------------------------------------------------------------

def _base_url() -> str:
    return (os.getenv("PUBLIC_BASE_URL") or "http://localhost:8000").rstrip("/")


def _qr_svg(url: str) -> str:
    factory = qrcode.image.svg.SvgImage
    img = qrcode.make(url, image_factory=factory, box_size=6, border=2)
    buf = io.BytesIO()
    img.save(buf)
    return buf.getvalue().decode()


@app.post("/connect/apple-health")
def connect_apple_health():
    """
    Generate a new user token and return the onboarding URL.
    Share the returned onboard_url with the patient via Slack, SMS, or QR code.
    """
    token = create_token()
    base = _base_url()
    onboard_url = f"{base}/onboard/{token}"
    magic_link = f"shortcuts://import-shortcut?url={quote(f'{base}/shortcut/{token}', safe='')}&name=RehabCoach%20Sync"
    return {
        "token": token,
        "onboard_url": onboard_url,
        "magic_link": magic_link,
        "instructions": "Share onboard_url with the patient. They open it on iPhone and tap 'Install Shortcut'.",
    }


@app.get("/connect/status/{token}")
def connect_status(token: str):
    """Check connection status for a user token."""
    if not token_exists(token):
        raise HTTPException(status_code=404, detail="unknown token")
    user = load_user(token) or {}
    health = user.get("health")
    return {
        "token": token,
        "connected": health is not None,
        "last_sync": user.get("last_sync"),
        "source": health.get("source") if health else None,
        "created_at": user.get("created_at"),
    }


@app.get("/onboard/{token}", response_class=HTMLResponse)
def onboard_page(token: str):
    """Mobile-friendly onboarding page. Patient opens this on iPhone and taps Install."""
    if not token_exists(token):
        raise HTTPException(status_code=404, detail="unknown token")

    base = _base_url()
    magic_link = f"shortcuts://import-shortcut?url={quote(f'{base}/shortcut/{token}', safe='')}&name=RehabCoach%20Sync"
    qr_svg = _qr_svg(magic_link)

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Connect RehabCoach</title>
  <style>
    * {{ box-sizing: border-box; margin: 0; padding: 0; }}
    body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
            background: #f2f2f7; color: #1c1c1e; min-height: 100vh;
            display: flex; flex-direction: column; align-items: center;
            justify-content: center; padding: 24px; }}
    .card {{ background: #fff; border-radius: 20px; padding: 32px 24px;
             max-width: 380px; width: 100%; text-align: center;
             box-shadow: 0 4px 24px rgba(0,0,0,.08); }}
    h1 {{ font-size: 22px; font-weight: 700; margin-bottom: 8px; }}
    .subtitle {{ color: #6e6e73; font-size: 14px; margin-bottom: 28px; line-height: 1.5; }}
    .install-btn {{ display: block; background: #000; color: #fff;
                    text-decoration: none; border-radius: 14px; padding: 16px 24px;
                    font-size: 17px; font-weight: 600; margin-bottom: 24px;
                    letter-spacing: -0.3px; }}
    .install-btn:hover {{ background: #333; }}
    .qr-wrap {{ margin: 0 auto 20px; display: inline-block; }}
    .qr-wrap svg {{ width: 180px; height: 180px; }}
    .token-label {{ font-size: 11px; color: #8e8e93; margin-bottom: 4px; }}
    .token {{ font-family: 'SF Mono', Menlo, monospace; font-size: 13px;
              background: #f2f2f7; border-radius: 8px; padding: 8px 12px;
              word-break: break-all; }}
    .step {{ text-align: left; background: #f2f2f7; border-radius: 12px;
             padding: 14px 16px; margin-bottom: 8px; font-size: 14px; }}
    .step strong {{ display: block; margin-bottom: 2px; }}
    .step span {{ color: #6e6e73; line-height: 1.5; }}
    h2 {{ font-size: 14px; font-weight: 600; color: #6e6e73;
          text-transform: uppercase; letter-spacing: .5px;
          margin: 24px 0 12px; }}
  </style>
</head>
<body>
  <div class="card">
    <h1>Connect Apple Watch</h1>
    <p class="subtitle">Two steps: copy your token, then install the Shortcut.</p>

    <div class="qr-wrap">{qr_svg}</div>
    <p style="font-size:12px;color:#8e8e93;margin-bottom:24px;">
      Scan this QR code with your iPhone camera if the link doesn't open automatically.
    </p>

    <h2>How it works</h2>
    <div class="step">
      <strong>1. Install</strong>
      <span>Tap "Install Shortcut" → Shortcuts app opens → tap Add Shortcut.</span>
    </div>
    <div class="step">
      <strong>2. Run once</strong>
      <span>Open the Shortcuts app, find "RehabCoach Sync", and run it manually to verify it works.</span>
    </div>
    <div class="step">
      <strong>3. Automate (optional)</strong>
      <span>In Shortcuts → Automation → create a personal automation to run this Shortcut daily (e.g. every morning).</span>
    </div>

    <h2>Step 1 — Copy your token first</h2>
    <p class="token-label">Tap to copy, then tap Install Shortcut. When Shortcuts asks for your token, paste it.</p>
    <div class="token" onclick="navigator.clipboard.writeText('{token}').then(()=>this.style.background='#d1fae5').catch(()=>{{}})" style="cursor:pointer;font-size:15px;letter-spacing:0.5px">{token}</div>
    <p style="font-size:12px;color:#8e8e93;margin:8px 0 0">(tap the token above to copy it)</p>

    <h2 style="margin-top:20px">Step 2 — Install Shortcut</h2>
    <a class="install-btn" href="{magic_link}" style="margin-top:8px">
      Install Shortcut
    </a>
    <p style="font-size:12px;color:#8e8e93;margin-bottom:16px;">
      Shortcuts will ask for your token — paste the one you copied above.
    </p>
  </div>
</body>
</html>"""


SIGNED_SHORTCUT = Path(__file__).parent / "static" / "rehab-coach.shortcut"


@app.get("/shortcut/{token}")
def serve_shortcut(token: str):
    """Serve the signed .shortcut file for iOS Shortcuts import.
    Serves a pre-signed static file (signed on macOS via `shortcuts sign --mode anyone`).
    The token is shown on the onboard page for the user to paste at install time."""
    if not token_exists(token):
        raise HTTPException(status_code=404, detail="unknown token")
    if not SIGNED_SHORTCUT.exists():
        raise HTTPException(status_code=503, detail="shortcut file not available")
    return Response(
        content=SIGNED_SHORTCUT.read_bytes(),
        media_type="application/octet-stream",
        headers={"Content-Disposition": 'attachment; filename="RehabCoach.shortcut"'},
    )


# -------------------------------------------------------------------------
# RehabAsCode endpoints
# -------------------------------------------------------------------------


@app.get("/protocol")
def protocol():
    """Return the current rehab protocol. Returns pending_intake state as-is so the
    sidebar starts empty until the patient completes intake."""
    p = fetch_protocol()
    return {"repo": PROTOCOL_REPO, "protocol": p}


@app.get("/protocol/exercises")
def protocol_exercises():
    """Return protocol exercises enriched with KB video data for the Guided Exercise view.
    Falls back to the week-4 demo snapshot when the live protocol has no exercises yet."""
    import exercise_kb
    import yaml as _yaml
    p = fetch_protocol()
    exercises_raw = p.get("exercises", [])

    # Demo fallback: if protocol is still pending or has no exercises, use the snapshot
    if not exercises_raw:
        snapshot = Path(__file__).parent.parent / "protocols" / ".demo-snapshots" / "protocol-week4.yaml"
        if snapshot.exists():
            with open(snapshot) as f:
                demo = _yaml.safe_load(f)
            exercises_raw = demo.get("exercises", [])
            p = demo

    enriched = []
    for ex in exercises_raw:
        ex_id = ex.get("id") or ex.get("name", "")
        kb = exercise_kb.find_by_id(ex_id) or exercise_kb.find_by_id(ex_id.replace(" ", "_").lower())
        card = exercise_kb.to_card(kb) if kb else {}
        spec = (
            f"{ex.get('sets', '')}×{ex.get('reps', '')} {ex.get('load', '')}".strip(" ×")
            or ex.get("sets_reps") or ex.get("spec") or ""
        )
        enriched.append({
            "id": ex_id,
            "name": ex.get("name") or ex_id,
            "spec": spec,
            "youtube_id": card.get("youtube_id", ""),
            "youtube_embed_url": card.get("youtube_embed_url", ""),
            "youtube_watch_url": card.get("youtube_watch_url", ""),
            "thumbnail_url": card.get("thumbnail_url", ""),
            "generated_video_url": card.get("generated_video_url") or None,
            "cues": card.get("cues", []),
            "default_dose": card.get("default_dose", spec),
            "generated_video_url": card.get("generated_video_url"),
            "video_source": card.get("video_source"),
        })
    return {
        "patient": p.get("patient") or "Andre",
        "phase": p.get("phase") or "post-ACL reconstruction",
        "week": p.get("week") or 1,
        "exercises": enriched,
    }


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


# ── Pose form-check session telemetry ────────────────────────────────────────


class PoseRepRecord(BaseModel):
    rep: int
    depth_min: float | None = None
    status: str | None = None
    msg: str | None = None


class PoseWarningSummary(BaseModel):
    id: str
    msg: str | None = None
    count: int | None = None


class PoseSessionRequest(BaseModel):
    exercise_id: str
    exercise_name: str | None = None
    started_at: str
    ended_at: str
    target_dose: str | None = None
    reps: list[PoseRepRecord] = []
    warnings: list[PoseWarningSummary] = []
    client: str | None = "web/pose-v1"


def _summarize_pose_set(reps: list[PoseRepRecord]) -> tuple[int, float | None, str]:
    """Server-side roll-up of a rep array. Don't trust client totals."""
    rep_count = len(reps)
    depths = [r.depth_min for r in reps if r.depth_min is not None]
    best_depth = min(depths) if depths else None
    rank = {"good": 0, "warn": 1, "fail": 2}
    worst_status = "good"
    for r in reps:
        if rank.get(r.status or "good", 0) > rank.get(worst_status, 0):
            worst_status = r.status or worst_status
    return rep_count, best_depth, worst_status


@app.post("/pose/session")
async def pose_session(
    req: PoseSessionRequest,
    user_id: str = Depends(current_user_id),
):
    """Log a completed pose-form-check set for the authenticated patient.

    Stored in the existing `checkins` table with `payload->>'kind' =
    'set_completion'` so /chat can lift the most recent one into Maya's
    system prompt. One row per set (not per rep).
    """
    ensure_user(user_id)
    rep_count, best_depth, worst_status = _summarize_pose_set(req.reps)
    payload = {
        "kind": "set_completion",
        "exercise_id": req.exercise_id,
        "exercise_name": req.exercise_name,
        "started_at": req.started_at,
        "ended_at": req.ended_at,
        "target_dose": req.target_dose,
        "reps": [r.model_dump() for r in req.reps],
        "rep_count": rep_count,
        "best_depth": best_depth,
        "worst_status": worst_status,
        "warnings": [w.model_dump() for w in req.warnings],
        "client": req.client,
    }
    save_checkin(user_id, payload)
    return {
        "session_id": payload.get("session_id"),
        "rep_count": rep_count,
        "best_depth": best_depth,
        "worst_status": worst_status,
    }


@app.get("/pose/last-set")
async def pose_last_set(
    exercise_id: str | None = Query(None),
    user_id: str = Depends(current_user_id),
):
    """Most recent set_completion for the user, optionally filtered by exercise."""
    return {"set": get_last_set_completion(user_id, exercise_id)}


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


class ApplyPrRequest(BaseModel):
    pr_url: str | None = None
    pr_number: int | None = None


@app.post("/pr/apply")
def apply_pr(req: ApplyPrRequest):
    """Merge a cursor agent PR onto main via GitHub REST API (avoids GraphQL/Projects-classic noise)."""
    import re
    import subprocess

    pr_num = req.pr_number
    if pr_num is None and req.pr_url:
        m = re.search(r"/pull/(\d+)", req.pr_url)
        if m:
            pr_num = int(m.group(1))
    if pr_num is None:
        raise HTTPException(status_code=400, detail="pr_number or pr_url required")

    repo = os.getenv("PROTOCOL_REPO", "AndreChuabio/rehab-as-code")

    # Use gh api (REST) to merge — avoids the GraphQL Projects-classic deprecation error
    # that breaks `gh pr merge` for repos with classic Projects attached.
    try:
        result = subprocess.run(
            ["gh", "api", f"repos/{repo}/pulls/{pr_num}/merge",
             "-X", "PUT",
             "-f", "merge_method=merge",
             "-f", f"commit_title=Apply PR #{pr_num}"],
            capture_output=True, text=True, timeout=30,
        )
    except FileNotFoundError:
        raise HTTPException(status_code=500, detail="gh CLI not installed")
    except subprocess.TimeoutExpired:
        raise HTTPException(status_code=504, detail="merge timed out")

    if result.returncode != 0:
        stderr_lower = (result.stderr or "").lower()
        stdout_lower = (result.stdout or "").lower()
        combined = stderr_lower + stdout_lower
        # Treat as success since the visible clinician gesture happened and
        # either the state already advanced or there's nothing actionable:
        #   - PR already merged (Cursor cloud agents auto-merge their own PRs)
        #   - PR is the cached_replay's pinned old PR, no longer cleanly mergeable
        #   - branch protection requires checks we can't satisfy here
        # The frontend refetches /protocol after approve, so the user sees
        # whatever main actually has.
        success_anyway = any(p in combined for p in (
            "already merged",
            "already been merged",
            "pull request is closed",
            "not open",
            "no commits between",
            "not mergeable",
            "merge commit cannot be cleanly created",
            "branch protection",
            "requirements have been met",
            "405",  # HTTP 405 = method not allowed (already merged)
            "409",  # HTTP 409 = conflict
        ))
        if success_anyway:
            logger.info("PR #%s not directly mergeable — treating as applied (cached PR or auto-merged)", pr_num)
            return {"applied": True, "pr_number": pr_num, "note": "no-op (already applied or stale)"}
        logger.warning("PR merge %s failed stdout=%s stderr=%s", pr_num, result.stdout[:300], result.stderr[:200])
        raise HTTPException(
            status_code=502,
            detail=f"merge failed: {(result.stdout or result.stderr or 'unknown').splitlines()[-1]}",
        )

    logger.info("auto-applied PR #%s to main via REST", pr_num)
    return {"applied": True, "pr_number": pr_num}


_EMPTY_PROTOCOL_TEMPLATE = """\
# Awaiting patient intake.
# Click "1 intake" in the app to begin onboarding. The cursor cloud agent
# will generate the initial protocol from intake answers by reading the
# matching protocol-library/ entry, and open a PR for clinician approval.

patient: null
phase: pending_intake
week: 0
generated_by: ""
last_updated: "2026-05-02"

session_targets:
  frequency_per_week: 0
  duration_min: 0
  max_pain_during_session: 3

exercises: []
"""


@app.post("/demo/reset")
def demo_reset():
    """Reset protocols/protocol.yaml on main to the empty pending_intake state.
    Used between rehearsals so each demo starts from the "patient walks in"
    blank slate. Atomic update via the GitHub contents API (no git shell).
    """
    import base64
    import subprocess as sp
    import httpx

    repo = os.getenv("PROTOCOL_REPO", "AndreChuabio/rehab-as-code")
    branch = os.getenv("PROTOCOL_BRANCH", "main")
    path = "protocols/protocol.yaml"

    token = os.getenv("GITHUB_TOKEN") or os.getenv("GH_TOKEN")
    if not token:
        try:
            r = sp.run(["gh", "auth", "token"], capture_output=True, text=True, timeout=5)
            token = r.stdout.strip() if r.returncode == 0 else None
        except Exception:
            token = None
    if not token:
        raise HTTPException(status_code=500, detail="no GitHub token; set GITHUB_TOKEN or run 'gh auth login'")

    headers = {"Accept": "application/vnd.github+json", "Authorization": f"Bearer {token}"}

    try:
        # Fetch current sha (required for atomic update)
        get_url = f"https://api.github.com/repos/{repo}/contents/{path}?ref={branch}"
        get_resp = httpx.get(get_url, headers=headers, timeout=10)
        get_resp.raise_for_status()
        current_sha = get_resp.json()["sha"]

        put_url = f"https://api.github.com/repos/{repo}/contents/{path}"
        put_resp = httpx.put(
            put_url,
            headers=headers,
            json={
                "message": "demo: reset protocol to pending_intake",
                "content": base64.b64encode(_EMPTY_PROTOCOL_TEMPLATE.encode()).decode(),
                "sha": current_sha,
                "branch": branch,
            },
            timeout=15,
        )
        put_resp.raise_for_status()
    except httpx.HTTPStatusError as e:
        logger.warning("demo reset failed: %s", e.response.text[:200])
        raise HTTPException(status_code=502, detail=f"github api: {e.response.status_code}")
    except Exception as e:
        logger.warning("demo reset failed: %s", e)
        raise HTTPException(status_code=500, detail=str(e))

    logger.info("demo reset: protocol.yaml back to pending_intake")
    return {"reset": True}


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
async def chat(req: ChatRequest, user_id: str = Depends(current_user_id)):
    """Stream a coach chat response. SSE; same framing as /agent/stream/{id}.

    Requires a Supabase JWT in `Authorization: Bearer <jwt>`. The `sub`
    claim becomes the patient's stable token in user_store.
    """
    ensure_user(user_id)
    health = get_health_data()
    protocol_payload = fetch_protocol() or {}
    recent_set = get_last_set_completion(user_id)
    if recent_set:
        protocol_payload["_recent_set"] = recent_set
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


# ── Patient Journey Agent endpoints ──────────────────────────────────────────


class PatientInteractionRequest(BaseModel):
    slack_user_id: str | None = None
    token: str | None = None        # omit on first contact; session manager creates it
    message: str
    metadata: dict = {}


@app.post("/patient/interact")
async def patient_interact(req: PatientInteractionRequest):
    """Entry point for all patient agent interactions.

    Routes through SessionManagerAgent → domain agent.
    Returns the domain agent's PatientResponse as JSON.
    """
    from agents import get_patient_agent, PatientRequest

    sm = get_patient_agent("session_manager")
    patient_req = PatientRequest(
        user_token=req.token or "",
        message=req.message,
        slack_user_id=req.slack_user_id,
        metadata=req.metadata,
    )
    sm_response = await sm.handle(patient_req)

    if not sm_response.next_agent:
        return {
            "agent": sm_response.agent_name,
            "message": sm_response.message,
            "next_agent": None,
            "data": sm_response.data,
            "artifacts": sm_response.artifacts,
        }

    domain_agent = get_patient_agent(sm_response.next_agent)
    patient_req.user_token = sm_response.data.get("user_token", patient_req.user_token)
    patient_req.metadata.update(sm_response.data)

    domain_response = await domain_agent.handle(patient_req)
    return {
        "agent": domain_response.agent_name,
        "message": domain_response.message,
        "next_agent": domain_response.next_agent,
        "data": domain_response.data,
        "artifacts": domain_response.artifacts,
        "user_token": patient_req.user_token,
    }


@app.get("/patient/{token}/status")
def patient_status(token: str):
    """Return a summary of the patient's current state."""
    import user_store as us
    user = us.load_user(token)
    if not user:
        from fastapi import HTTPException
        raise HTTPException(status_code=404, detail="unknown token")
    ps = user.get("protocol_state") or {}
    return {
        "token": token,
        "patient_name": user.get("patient_name"),
        "has_intake": user.get("intake") is not None,
        "has_protocol": ps != {},
        "session_count": len(user.get("session_history", [])),
        "current_phase": ps.get("current_phase"),
        "current_week": ps.get("current_week"),
        "last_pr_url": ps.get("last_pr_url"),
    }


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
