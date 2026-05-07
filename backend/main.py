from __future__ import annotations
"""
main.py - FastAPI backend for RehabAsCode

Endpoints:
  GET  /health-data                   today's wearable metrics (optional ?token=)
  GET  /calendar                      today's calendar events
  POST /start-session                 build context + create Tavus CVI session
  GET  /context                       pre-built context (cron use case)
  GET  /protocol                      current rehab protocol for this patient
  POST /chat                          SSE Coach Maya chat (drafts protocol revisions)
  POST /patient/interact              structured intake + plan-gen entry point
  GET  /protocols/pending             clinician dashboard queue
  POST /protocols/{id}/approve|reject clinician review actions

Apple Health onboarding:
  POST /connect/apple-health          generate user token + return onboard URL
  GET  /connect/status/{token}        connection status for a user token
  GET  /onboard/{token}               mobile HTML onboarding page (QR + install button)
  GET  /shortcut/{token}              serve .shortcut file for iOS Shortcuts import

The cursor / ag2 / cached_replay PR-bus is gone (PR-after-#62). All chat-tool
fires now write `pending_review` rows to the Supabase `protocols` table via
chat_protocol_drafter.draft_and_save_pending; clinicians approve them on
/clinician.
"""

import asyncio
import io
import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote
from fastapi import Depends, FastAPI, Header, HTTPException, Query
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
from protocol_loader import (
    fetch_protocol,
    fetch_protocol_for_user,
    PROTOCOL_REPO,
)
import chat_protocol_drafter
import user_store
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
from auth import current_user_id, is_clinician, optional_user_id, require_clinician_id
import coach_chat
import qrcode
import qrcode.image.svg

logger = logging.getLogger(__name__)

app = FastAPI(title="RehabAsCode", version="0.1.0")

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


@app.get("/clinician")
def clinician_root():
    """Serve the clinician dashboard. Auth is checked client-side via /me/role
    (the dashboard JS redirects to / if the caller is not a clinician). This
    route just serves the static HTML."""
    page = FRONTEND_DIR / "clinician.html"
    if page.exists():
        return FileResponse(str(page))
    raise HTTPException(status_code=404, detail="clinician dashboard not built")


@app.get("/me/role")
def me_role(user_id: str | None = Depends(optional_user_id)):
    """Return the caller's role for client-side routing.

    role=anonymous   no JWT
    role=patient     authenticated, not in clinicians table
    role=clinician   authenticated, in clinicians table
    """
    if not user_id:
        return {"role": "anonymous", "user_id": None}
    if is_clinician(user_id):
        return {"role": "clinician", "user_id": user_id}
    return {"role": "patient", "user_id": user_id}


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
        "ANTHROPIC_MODEL":     os.getenv("ANTHROPIC_MODEL") or "claude-sonnet-4-6",
        "TAVUS_API_KEY":       mask(os.getenv("TAVUS_API_KEY")),
        "TAVUS_REPLICA_ID":    mask(os.getenv("TAVUS_REPLICA_ID")),
        "TAVUS_PERSONA_ID":    mask(os.getenv("TAVUS_PERSONA_ID")),
        "OPENAI_API_KEY":      mask(os.getenv("OPENAI_API_KEY")),
        "OPENAI_MODEL":        os.getenv("OPENAI_MODEL") or "gpt-4o-mini",
        "PROTOCOL_SOURCE":     os.getenv("PROTOCOL_SOURCE") or "github",
        "DATABASE_URL":        "set" if os.getenv("DATABASE_URL") else "MISSING",
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
def protocol(user_id: str | None = Depends(optional_user_id)):
    """Return the current rehab protocol. Returns pending_intake state as-is so the
    sidebar starts empty until the patient completes intake.

    Optional auth: when called with a valid JWT and PROTOCOL_SOURCE=supabase
    is set, returns the patient's active protocol from the `protocols` table.
    Without auth (or with PROTOCOL_SOURCE=github), falls back to the legacy
    single-tenant GitHub fetch."""
    p = fetch_protocol_for_user(user_id) if user_id else fetch_protocol()
    return {"repo": PROTOCOL_REPO, "protocol": p}


@app.get("/exercises")
def list_exercises(
    phase: str | None = Query(None, description="Filter by phase: acute|subacute|strength"),
    injury_type: str | None = Query(None, description="Optional injury filter"),
    user_id: str | None = Depends(optional_user_id),  # noqa: ARG001
):
    """Return the curated exercise library indexed by exercise_kb.

    The library is public reference data (no PHI), so auth is optional - the
    sign-in overlay is a soft gate that lets unauthed visitors browse what
    the platform offers. PHI lives in `protocols`, not here.

    Filters:
      ?phase=acute|subacute|strength      narrow to a phase
      ?injury_type=knee|ankle|...         further narrow to an injury
    """
    import exercise_kb
    if phase:
        matches = exercise_kb.find_by_phase(phase, injury_type=injury_type)
    elif injury_type:
        # find_by_phase requires phase; do a manual filter when only injury is set.
        matches = [
            ex for ex in exercise_kb.list_all()
            if injury_type.lower().strip() in [
                i.lower() for i in ex.get("injury_types", [])
            ]
        ]
    else:
        matches = exercise_kb.list_all()
    return {"exercises": [exercise_kb.to_card(ex) for ex in matches]}


@app.get("/protocol/exercises")
def protocol_exercises(user_id: str | None = Depends(optional_user_id)):
    """Return protocol exercises enriched with KB video data for the Guided Exercise view.
    Falls back to the week-4 demo snapshot when the live protocol has no exercises yet.

    Optional auth: same per-user routing as `/protocol`."""
    import exercise_kb
    import yaml as _yaml
    p = fetch_protocol_for_user(user_id) if user_id else fetch_protocol()
    exercises_raw = p.get("exercises", [])

    # Demo fallback: ONLY for unauthenticated callers (the public landing
    # page demo). Authenticated patients with no active protocol get an
    # empty list - they see the empty-state CTA in the sidebar instead of
    # inheriting Andre's post-ACL knee demo. Without this gate, an ankle
    # patient with no protocol would see knee exercises (the bug Andre
    # caught in the clinician dashboard, 2026-05-06).
    if not exercises_raw and user_id is None:
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
        # Don't echo a hardcoded default - the protocol's `patient` field is
        # a denormalized snapshot. Front-end never displays this; kept in the
        # response for backwards compat. Resolve display names through
        # /me or user_store.get_display_name instead.
        "patient": p.get("patient"),
        "phase": p.get("phase") or "post-ACL reconstruction",
        "week": p.get("week") or 1,
        "exercises": enriched,
    }


# Removed (post-PR-#62 cleanup, 2026-05-06):
#   * AgentInvokeRequest, _invoke_with_fallback, /agent/invoke, /agent/stream
#   * _INVOCATIONS in-memory map and the SSE trace-streaming surface
#   * _build_agent_prompt and the entire CodingAgent abstraction (cursor_sdk,
#     cursor_api, cursor_github, ag2, cached_replay, mock)
#   * /pr/apply and /demo/reset which only existed to merge agent-opened PRs
#
# The new write path: chat-tool fires call chat_protocol_drafter.draft_and_save_pending,
# which writes a pending_review row to the `protocols` table. Clinicians approve
# them via /clinician → /protocols/{id}/approve. /patient/interact still runs
# IntakeAgent + PlanGenerationAgent (which themselves hit the same supabase
# write path under PROTOCOL_WRITE_TARGET=supabase).
#
# Grep history: a parallel removal of /triggers/* unauth endpoints landed in
# PR #57 for the same "real patients now in the loop" reason.


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

    # Mirror the completed pose set into the durable sessions log so the
    # patient sidebar + clinician adherence panel see it immediately. This
    # is in addition to the checkins write above, which feeds Maya's recent-
    # set context. Failures here are logged, not raised: telemetry into
    # checkins is the load-bearing path; sessions is the audit/UX surface
    # and a write failure shouldn't 500 the form-check that just completed.
    try:
        active = None
        try:
            import protocol_repo as _pr
            active = _pr.get_active(user_id)
        except Exception as exc:
            logger.info("get_active failed during pose-session mirror: %s", exc)
        protocol_id = active["id"] if active else None

        import session_repo as _sr
        _sr.upsert_completed_pose(
            token=user_id,
            exercise_id=req.exercise_id,
            pose_metrics={
                "rep_count": rep_count,
                "best_depth": best_depth,
                "worst_status": worst_status,
                "warnings": [w.model_dump() for w in req.warnings],
            },
            started_at=req.started_at,
            completed_at=req.ended_at,
            protocol_id=protocol_id,
        )
    except Exception as exc:
        logger.warning("sessions mirror failed for pose set: %s", exc)

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


# ─── Supabase-backed protocol approval (Phase 2) ───────────────────────────
#
# These endpoints replace /pr/apply for the new write path. PlanGenerationAgent
# under PROTOCOL_WRITE_TARGET=supabase inserts pending_review rows instead of
# opening PRs; clinicians (or, in the demo, the patient self-approving) flip
# them active here.
#
# Auth posture: require any authenticated user. The clinician role gate lands
# in Phase 3 when the dashboard ships — at that point this becomes "must be
# clinician OR self-approving in demo mode". Today the patient clicks an
# Approve button on their own pending row, identical to today's /pr/apply UX.


class ApproveProtocolRequest(BaseModel):
    notes: str | None = None


class RejectProtocolRequest(BaseModel):
    notes: str


@app.get("/protocols/pending")
def list_pending_protocols(
    limit: int = Query(50, ge=1, le=200),
    user_id: str = Depends(require_clinician_id),
):
    """Clinician dashboard queue. Returns pending_review rows newest-first
    with the patient's display name (looked up from `users.patient_name`)
    so the dashboard can render the list without a second roundtrip."""
    import protocol_repo
    import user_store

    rows = protocol_repo.list_pending(limit=limit)
    patient_lookup: dict[str, str | None] = {}
    out = []
    for row in rows:
        token = row.get("token")
        if token and token not in patient_lookup:
            user = user_store.load_user(token) or {}
            patient_lookup[token] = user.get("patient_name") or (
                user.get("intake") or {}
            ).get("name")
        out.append({
            "id": row["id"],
            "token": token,
            "patient_name": patient_lookup.get(token),
            "phase": (row.get("payload") or {}).get("phase"),
            "week": (row.get("payload") or {}).get("week"),
            "created_by_agent": row.get("created_by_agent"),
            "created_at": row["created_at"].isoformat()
            if row.get("created_at") else None,
            # status is `pending_review` for normal drafts and
            # `needs_clinician_review` when SafetyReviewAgent flagged
            # high severity. The frontend uses this to show the SAFETY
            # badge and sort flagged rows to the top of the queue.
            "status": row.get("status"),
            "safety_concerns": row.get("safety_concerns"),
        })
    return {"pending": out}


@app.get("/protocols/{protocol_id}")
def get_protocol_detail(
    protocol_id: str,
    user_id: str = Depends(current_user_id),
):
    """Single protocol with the patient's currently-active row alongside,
    for diff rendering. Returns 404 if not found.

    Access:
      * Clinicians (rows in `clinicians` table) can fetch any protocol —
        the dashboard needs cross-patient read for review.
      * A patient may fetch their own protocol (target.token == user_id);
        anyone else gets 403.

    Response shape adds `narrator_summary` only for clinicians. Patients
    see the protocol data but not the AI-generated diff narration —
    that's a clinician-facing review tool, and skipping the call saves
    Haiku cost on patient self-fetch.
    """
    import protocol_repo
    import user_store

    target = protocol_repo.get(protocol_id)
    if not target:
        raise HTTPException(status_code=404, detail="protocol not found")

    requester_is_clinician = is_clinician(user_id)
    if not requester_is_clinician and target["token"] != user_id:
        raise HTTPException(status_code=403, detail="not authorized for this protocol")

    active = protocol_repo.get_active(target["token"])
    if active and active["id"] == target["id"]:
        # The target is itself the active row — no separate "previous active"
        active_for_diff = None
    else:
        active_for_diff = active

    user = user_store.load_user(target["token"]) or {}
    intake = user.get("intake")
    recent_sessions = user_store.get_session_history(target["token"], limit=20)

    response: dict = {
        "target": _serialize_protocol_row(target),
        "active": _serialize_protocol_row(active_for_diff) if active_for_diff else None,
        "patient": {
            "token": target["token"],
            "patient_name": user.get("patient_name") or (intake or {}).get("name"),
            "intake": intake,
            "recent_sessions": recent_sessions[-5:],
        },
    }

    # AI-generated diff narration: clinician-only. Patients self-fetching
    # don't need the meta-explanation, and skipping the call saves cost.
    # We expose BOTH narrator_summary (the text) and narrator_status (an
    # enum: no_diff | no_api_key | sdk_error | empty_response | ok) so
    # the dashboard can render a disambiguated micro-state per failure
    # mode instead of one generic "Summary unavailable" string.
    if requester_is_clinician:
        import diff_narrator
        # Filter session_history down to actual symptom / pain check-ins
        # (the same store mixes set-completion entries and check-ins).
        checkins = [
            s for s in recent_sessions
            if s.get("kind") == "checkin"
            or s.get("pain_level") is not None
            or s.get("symptom_text")
            or s.get("checkin_text")
        ][-5:]
        proposed_id = str(target.get("id") or protocol_id)
        active_id = str(active_for_diff["id"]) if active_for_diff else None
        narration, narrator_status = diff_narrator.summarize(
            active_payload=(active_for_diff or {}).get("payload") if active_for_diff else None,
            proposed_payload=target.get("payload"),
            intake_payload=intake,
            last_5_checkins=checkins,
            recent_sessions=recent_sessions[-7:],
            active_id=active_id,
            proposed_id=proposed_id,
            protocol_id=proposed_id,
            clinician_id=user_id,
        )
        response["narrator_summary"] = narration
        response["narrator_status"] = narrator_status

        # Patient-at-a-glance: structured summary + last-5 pain trend.
        # Computed server-side so the frontend doesn't have to denormalize.
        # Clinician-only: this collates PHI (name, age, injury_type) into
        # a single payload, so it follows the same gate as the narrator.
        response["patient_summary"] = _build_patient_summary(
            intake=intake,
            target_payload=target.get("payload") or {},
        )
        response["pain_trend"] = _build_pain_trend(recent_sessions)

    return response


def _build_patient_summary(
    *,
    intake: dict | None,
    target_payload: dict,
) -> dict:
    """Compose the at-a-glance card for the clinician dashboard.

    Pulls from the patient's intake (display_name, age, injury_type,
    surgery_date, symptoms, goals) plus the proposed protocol payload
    (phase, week) plus the clinical_taxonomy resolver (body_region).
    Computes post_op_days from surgery_date when present.

    PHI note: this dict is returned over the wire, so the same gate
    (clinician role) that protects narrator_summary protects it. Never
    write the contents to logs.
    """
    import clinical_taxonomy

    intake = intake or {}
    display_name = intake.get("name")
    age = intake.get("age")
    injury_type = intake.get("injury_type")
    surgery_date = intake.get("surgery_date")
    symptoms = intake.get("symptoms") or []
    goals = intake.get("goals") or []

    # body_region uses the deterministic-first map; classify_freetext()
    # is intentionally NOT used here so the at-a-glance card stays
    # synchronous + free. A null badge is fine; the clinician sees the
    # raw injury_type underneath.
    try:
        body_region = clinical_taxonomy.body_region(injury_type)
    except Exception:  # noqa: BLE001 — taxonomy is best-effort here
        body_region = None

    post_op_days = None
    if surgery_date:
        try:
            # surgery_date is "YYYY-MM-DD" from the intake schema. If a
            # patient or agent typed something weirder, just leave it
            # blank rather than crash the whole detail call.
            sd = datetime.strptime(str(surgery_date)[:10], "%Y-%m-%d").date()
            today = datetime.now(timezone.utc).date()
            post_op_days = max(0, (today - sd).days)
        except (ValueError, TypeError):
            post_op_days = None

    return {
        "display_name": display_name,
        "age": age,
        "injury_type": injury_type,
        "body_region": body_region,
        "phase": target_payload.get("phase"),
        "week": target_payload.get("week"),
        "post_op_days": post_op_days,
        "symptoms": symptoms,
        "goals": goals,
    }


def _build_pain_trend(recent_sessions: list[dict]) -> list[dict]:
    """Return up to 5 most-recent pain-level check-ins, oldest-first.

    The session_history store mixes pain check-ins, symptom check-ins,
    and set-completion entries. We keep only entries with a numeric
    pain_level and surface (date, level) pairs for the dashboard's
    sparkline-equivalent.
    """
    if not recent_sessions:
        return []
    pain_entries: list[dict] = []
    for entry in recent_sessions:
        level = entry.get("pain_level")
        if level is None:
            continue
        try:
            level_int = int(level)
        except (TypeError, ValueError):
            continue
        date = entry.get("recorded_at") or entry.get("created_at") or ""
        # Keep just the date prefix (YYYY-MM-DD) for the chip render.
        date_str = str(date)[:10] if date else ""
        pain_entries.append({"date": date_str, "level": level_int})
    # Last 5, oldest -> newest.
    return pain_entries[-5:]


def _serialize_protocol_row(row: dict | None) -> dict | None:
    if not row:
        return None
    out = dict(row)
    if out.get("created_at") and not isinstance(out["created_at"], str):
        out["created_at"] = out["created_at"].isoformat()
    if out.get("reviewed_at") and not isinstance(out["reviewed_at"], str):
        out["reviewed_at"] = out["reviewed_at"].isoformat()
    return out


@app.post("/protocols/{protocol_id}/approve")
def approve_protocol(
    protocol_id: str,
    req: ApproveProtocolRequest,
    user_id: str = Depends(current_user_id),
):
    """Promote a pending_review protocol to active. Transactional — supersedes
    the previous active row in the same statement. Idempotency: re-calling
    on an already-active row returns 409 (the unique partial index also
    catches concurrent approvals)."""
    import protocol_repo
    try:
        result = protocol_repo.approve(
            protocol_id=protocol_id,
            reviewed_by=user_id,
            notes=req.notes,
        )
    except protocol_repo.ProtocolRepoError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    except Exception as exc:
        logger.exception("approve_protocol failed")
        raise HTTPException(status_code=500, detail=str(exc))

    return {
        "approved": True,
        "protocol_id": result["id"],
        "status": result["status"],
        "reviewed_at": result["reviewed_at"].isoformat()
        if result.get("reviewed_at") else None,
    }


@app.post("/protocols/{protocol_id}/reject")
def reject_protocol(
    protocol_id: str,
    req: RejectProtocolRequest,
    user_id: str = Depends(current_user_id),
):
    """Mark a pending_review protocol as rejected. Active row unchanged.
    Notes required for the audit trail."""
    import protocol_repo
    try:
        result = protocol_repo.reject(
            protocol_id=protocol_id,
            reviewed_by=user_id,
            notes=req.notes,
        )
    except protocol_repo.ProtocolRepoError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    except Exception as exc:
        logger.exception("reject_protocol failed")
        raise HTTPException(status_code=500, detail=str(exc))

    return {
        "rejected": True,
        "protocol_id": result["id"],
        "status": result["status"],
        "reviewed_at": result["reviewed_at"].isoformat()
        if result.get("reviewed_at") else None,
    }


class RawContextRevealedRequest(BaseModel):
    target_token: str


@app.post("/audit/raw-context-revealed")
def audit_raw_context_revealed(
    req: RawContextRevealedRequest,
    user_id: str = Depends(current_user_id),
):
    """Audit log: a clinician opened the raw-JSON context disclosure.

    Triggered by the <details> toggle on the clinician dashboard. Writes
    a single server-side log line containing the reviewer + target UUIDs
    only. Patient name is intentionally NOT logged.

    Clinician-only — patients have no UI path that hits this endpoint.
    The role check 403s non-clinicians so accidental hits don't pollute
    the audit log.
    """
    if not is_clinician(user_id):
        raise HTTPException(status_code=403, detail="clinician role required")
    logger.info(
        "audit clinician_revealed_raw_context reviewer_id=%s target_token=%s",
        user_id, req.target_token,
    )
    return {"logged": True}


# -------------------------------------------------------------------------
# Sessions — DB-backed today's-session log
# -------------------------------------------------------------------------
#
# Replaces the in-memory `todaySession` array on the patient frontend. Sessions
# are clinical state: every exercise the patient stages, starts, completes, or
# skips lands in public.sessions, RLS-scoped to (auth.uid()::text = token).
# Pose-form-check sets are mirrored here from /pose/session above.


class CreateSessionRequest(BaseModel):
    exercise_id: str
    planned_sets: int | None = None
    planned_reps: int | None = None


class PatchSessionRequest(BaseModel):
    status: str | None = None
    completed_sets: int | None = None
    completed_reps: int | None = None
    pose_metrics: dict | None = None
    started_at: str | None = None
    completed_at: str | None = None


@app.post("/sessions")
def create_session(
    req: CreateSessionRequest,
    user_id: str = Depends(current_user_id),
):
    """Stage an exercise into the patient's session log.

    Captures the patient's currently-active protocol_id at create-time so the
    audit trail shows which protocol was in force when the patient added
    the exercise (the protocol may be superseded before they complete it).
    """
    ensure_user(user_id)
    import session_repo as _sr
    import protocol_repo as _pr

    protocol_id = None
    try:
        active = _pr.get_active(user_id)
        if active:
            protocol_id = active["id"]
    except Exception as exc:
        logger.info("get_active failed during create_session: %s", exc)

    try:
        row = _sr.create_planned(
            token=user_id,
            exercise_id=req.exercise_id,
            planned_sets=req.planned_sets,
            planned_reps=req.planned_reps,
            protocol_id=protocol_id,
        )
    except _sr.SessionRepoError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:
        logger.exception("create_session failed")
        raise HTTPException(status_code=500, detail=str(exc))
    return row


@app.patch("/sessions/{session_id}")
def patch_session(
    session_id: str,
    req: PatchSessionRequest,
    user_id: str = Depends(current_user_id),
):
    """Patient mutates their own session row. Scoped by (id, token) so a
    cross-patient PATCH attempt 404s rather than corrupting another patient's
    record - belt + suspenders alongside the RLS policy."""
    import session_repo as _sr
    try:
        row = _sr.patch(
            session_id=session_id,
            token=user_id,
            status=req.status,
            completed_sets=req.completed_sets,
            completed_reps=req.completed_reps,
            pose_metrics=req.pose_metrics,
            started_at=req.started_at,
            completed_at=req.completed_at,
        )
    except _sr.SessionRepoError as exc:
        # Both "no fields" and "not found" surface here; treat the latter as
        # 404 for clearer semantics.
        msg = str(exc)
        if "not found" in msg:
            raise HTTPException(status_code=404, detail=msg)
        raise HTTPException(status_code=400, detail=msg)
    except Exception as exc:
        logger.exception("patch_session failed")
        raise HTTPException(status_code=500, detail=str(exc))
    return row


@app.get("/sessions/today")
def list_today_sessions(
    x_timezone: str | None = Header(None, alias="X-Timezone"),
    user_id: str = Depends(current_user_id),
):
    """Today's session list (planned + in_progress + completed) for the user.

    The patient's local timezone arrives via the X-Timezone header (the
    frontend reads Intl.DateTimeFormat().resolvedOptions().timeZone). Falls
    back to UTC if the header is absent or unresolvable.
    """
    ensure_user(user_id)
    import session_repo as _sr
    try:
        rows = _sr.list_today(token=user_id, tz_name=x_timezone)
    except Exception as exc:
        logger.exception("list_today failed")
        raise HTTPException(status_code=500, detail=str(exc))
    return {"sessions": rows}


@app.get("/sessions/recent")
def list_recent_sessions(
    days: int = Query(7, ge=1, le=30),
    token: str | None = Query(None, description="Clinician-only: read for a specific patient"),
    user_id: str = Depends(current_user_id),
):
    """Recent sessions for adherence tracking.

    Patient self-fetch: omit `token` -> reads the caller's own sessions.
    Clinician fetch: pass `?token=<patient_uuid>` -> reads that patient's
    sessions (gated by is_clinician check; rejects with 403 otherwise).
    """
    target = user_id
    if token and token != user_id:
        if not is_clinician(user_id):
            raise HTTPException(status_code=403, detail="clinician role required")
        target = token

    import session_repo as _sr
    try:
        rows = _sr.list_recent(token=target, days=days)
    except Exception as exc:
        logger.exception("list_recent failed")
        raise HTTPException(status_code=500, detail=str(exc))
    return {"sessions": rows, "token": target, "days": days}


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


def _last_pose_metrics(user_id: str) -> dict | None:
    """Return the most-recent completed session's pose_metrics, or None.

    Used as Phase F context for the symptom classifier so it can correlate
    a complaint ("knee buckled on lunges") with the most recent observed
    form quality. Best-effort: any DB error returns None; we don't want a
    failed pose-metrics lookup to block the chat path.
    """
    try:
        import session_repo as _sr
        rows = _sr.list_recent(token=user_id, days=2)
    except Exception as exc:
        logger.info("last_pose_metrics lookup failed user=%s: %s", user_id, exc)
        return None
    completed = [
        r for r in rows
        if r.get("status") == "completed" and r.get("pose_metrics")
    ]
    if not completed:
        return None
    return completed[-1].get("pose_metrics")


def _clinician_attention_writer_factory(user_id: str):
    """Build a coach_chat.ClinicianAttentionWriter bound to this patient.

    On a clinician-attention symptom verdict, clone the patient's current
    active protocol payload (if any) and persist a needs_clinician_review
    row with safety_concerns set to the classifier output. The clinician
    dashboard already shows these rows at the top of the queue with a red
    banner (see PR-C). Returns the new pending row id.

    Cloning rather than synthesizing a fresh payload means the diff view
    on /clinician renders "no exercise change, but this needs your eyes" -
    which is the right framing: the agent isn't proposing a regression,
    it's escalating a red flag.
    """
    async def _writer(triage: dict, message_text: str) -> str:
        active = fetch_protocol_for_user(user_id) or {}
        # Drop the in-memory _recent_set bag so it doesn't leak into the
        # persisted payload (it's a runtime overlay, not protocol state).
        payload = {k: v for k, v in active.items() if not k.startswith("_")}
        if not payload:
            payload = {
                "patient": "unknown",
                "phase": "rehab",
                "week": 0,
                "exercises": [],
                "_synthetic": True,
            }
        concerns = [{
            "check": "symptom-classifier",
            "severity": "high",
            "detail": (
                f"Patient message: {message_text}\n\n"
                f"Classifier reasoning: {triage.get('reasoning', '')}"
            ),
        }]
        loop = asyncio.get_running_loop()
        from protocol_repo import save_pending
        pending_id = await loop.run_in_executor(
            None,
            lambda: save_pending(
                user_id,
                payload,
                created_by_agent="symptom_classifier",
                status="needs_clinician_review",
                safety_concerns=concerns,
            ),
        )
        # Log only the id, severity, and that we wrote — never the message.
        logger.info(
            "clinician_attention row written user=%s pending_id=%s",
            user_id, pending_id,
        )
        return pending_id

    return _writer


def _chat_trigger_executor_factory(user_id: str):
    """Bind a chat-tool trigger executor to the authenticated patient.

    The executor signature `(flow, payload) -> dict` matches what
    coach_chat.chat_stream expects. We close over `user_id` here so the
    drafter row is attributed to the JWT-derived patient (never client-
    provided), mirroring the auth boundary used by /protocols/*/approve.

    Each fire_*_trigger ultimately runs chat_protocol_drafter.draft_and_save_pending,
    which writes a `pending_review` row to the `protocols` table. Returns
    {pending_protocol_id, summary, phase, week, flow} on success; raises on
    failure so coach_chat._dispatch_tool can render an error tool_result.
    """
    async def _executor(flow: str, payload: dict) -> dict:
        prior_protocol = fetch_protocol_for_user(user_id) or None
        # draft_and_save_pending is sync (blocks on Anthropic + psycopg). Run
        # in the default executor so the SSE stream stays responsive.
        loop = asyncio.get_running_loop()
        result = await loop.run_in_executor(
            None,
            chat_protocol_drafter.draft_and_save_pending,
            user_id,
            flow,
            payload,
            prior_protocol,
        )
        return {
            "pending_protocol_id": result["pending_protocol_id"],
            "summary": result["summary"],
            "phase": result.get("phase"),
            "week": result.get("week"),
            "flow": flow,
        }

    return _executor


@app.post("/chat")
async def chat(req: ChatRequest, user_id: str = Depends(current_user_id)):
    """Stream a coach chat response. SSE; same framing as /agent/stream/{id}.

    Requires a Supabase JWT in `Authorization: Bearer <jwt>`. The `sub`
    claim becomes the patient's stable token in user_store.
    """
    ensure_user(user_id)
    health = get_health_data()
    protocol_payload = fetch_protocol_for_user(user_id) or {}
    recent_set = get_last_set_completion(user_id)
    if recent_set:
        protocol_payload["_recent_set"] = recent_set
    # Resolve the patient's display name fresh on every /chat call. Reading
    # from the protocol payload would re-introduce the stale-name bug
    # ("Christian" greeted as Andre) - see user_store.get_display_name docstring.
    display_name = user_store.get_display_name(user_id)
    messages = [
        {"role": turn.role, "content": turn.content} for turn in req.history
    ]
    messages.append({"role": "user", "content": req.message})

    trigger_executor = _chat_trigger_executor_factory(user_id)
    clinician_attention_writer = _clinician_attention_writer_factory(user_id)
    last_pose_metrics = _last_pose_metrics(user_id)

    async def gen():
        try:
            async for event in coach_chat.chat_stream(
                messages=messages,
                health=health,
                protocol=protocol_payload,
                trigger_executor=trigger_executor,
                user_token=user_id,
                display_name=display_name,
                session_id=req.session_id,
                last_pose_metrics=last_pose_metrics,
                clinician_attention_writer=clinician_attention_writer,
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
    """Body for /patient/interact.

    Auth identifies the patient via Supabase JWT (Depends(current_user_id)),
    so no token field is needed. `history` is the in-modal conversation
    transcript replayed back to the agent on every turn (the modal is
    stateful only on the client; the server stays request/response).
    """
    message: str
    history: list[dict] = []  # [{role: "user"|"assistant", content: str}]
    metadata: dict = {}


@app.post("/patient/interact")
async def patient_interact(
    req: PatientInteractionRequest,
    user_id: str = Depends(current_user_id),
):
    """Single entry point for the structured intake + plan-gen flow.

    Two states, server-resolved from intake_records (no client trust):
      1. No intake row yet      → run IntakeAgent, return next question.
                                   When IntakeAgent saves and signals
                                   plan_generation, immediately kick PlanGenAgent.
      2. metadata.force == "plan_generation" → re-run plan generation
                                                 (admin escape hatch / retries).
      3. Otherwise (intake exists, no force) → 409, client should use /chat.
    """
    from agents import get_patient_agent, PatientRequest

    ensure_user(user_id)

    intake = user_store.get_intake(user_id)
    force = (req.metadata or {}).get("force")

    if intake is None:
        intake_agent = get_patient_agent("intake")
        patient_req = PatientRequest(
            user_token=user_id,
            message=req.message,
            slack_user_id=None,
            metadata={**(req.metadata or {}), "conversation_history": list(req.history or [])},
        )
        resp = await intake_agent.handle(patient_req)

        # IntakeAgent finished and called trigger_plan_generation → fire plan gen now.
        if resp.next_agent == "plan_generation":
            plan_resp = await _kick_plan_generation(user_id, "Generate my rehab plan.")
            return {
                "agent": "plan_generation",
                "message": plan_resp["message"],
                "next_agent": None,
                "data": plan_resp["data"],
                "artifacts": plan_resp["artifacts"],
                "intake_complete": True,
            }

        return {
            "agent": resp.agent_name,
            "message": resp.message,
            "next_agent": resp.next_agent,
            "data": resp.data,
            "artifacts": resp.artifacts,
            "intake_complete": False,
        }

    if force == "plan_generation":
        plan_resp = await _kick_plan_generation(user_id, req.message or "Generate my rehab plan.")
        return {
            "agent": "plan_generation",
            "message": plan_resp["message"],
            "next_agent": None,
            "data": plan_resp["data"],
            "artifacts": plan_resp["artifacts"],
            "intake_complete": True,
        }

    raise HTTPException(
        status_code=409,
        detail="intake already complete; use /chat for ongoing coaching.",
    )


async def _kick_plan_generation(user_id: str, message: str) -> dict:
    """Run PlanGenerationAgent and return a serializable summary dict.

    Translates PlanGenerationError into a 502-style HTTPException so the
    caller (/patient/interact) surfaces a clear toast detail instead of
    a generic 500 stacktrace. Sub-agent failures (Anthropic 5xx, etc.)
    propagate up as PlanGenerationError; we treat them as upstream-AI
    outage and return the underlying message verbatim so the toast can
    say something like "researcher unavailable: ..." instead of a
    cryptic FastAPI internal error.
    """
    from agents import get_patient_agent, PatientRequest
    from agents.plan_generation_agent import PlanGenerationError

    plan_agent = get_patient_agent("plan_generation")
    patient_req = PatientRequest(
        user_token=user_id,
        message=message,
        slack_user_id=None,
        metadata={},
    )
    try:
        resp = await plan_agent.handle(patient_req)
    except PlanGenerationError as exc:
        logger.warning(
            "plan_generation failed for user_id=%s: %s", user_id, exc,
        )
        raise HTTPException(
            status_code=502,
            detail=f"plan generation failed: {exc}",
        ) from exc
    return {
        "agent": resp.agent_name,
        "message": resp.message,
        "data": resp.data,
        "artifacts": resp.artifacts,
    }


@app.get("/patient/me/intake-status")
def patient_intake_status(user_id: str = Depends(current_user_id)):
    """Server-derived patient state for the frontend state machine.

    Frontend calls this on auth-ready and after every modal close. Drives whether
    the intake modal opens, the plan-gen modal opens, or the main UI loads.

    States:
      - "needs_intake"     no intake_records row
      - "needs_plan"       intake exists, protocol_state has no last_pr_url
      - "ready"            intake + protocol_state.last_pr_url both present

    Also returns `review_status` (PR-H trust loop): a small dict telling the
    frontend whether the patient has a draft awaiting clinician review, was
    just approved/rejected, or has nothing in flight. None when the helper
    errors — frontend renders no pill in that case (no silent fallback to a
    fake state).
    """
    ensure_user(user_id)
    user = user_store.load_user(user_id) or {}
    intake = user.get("intake")
    ps = user.get("protocol_state") or {}
    has_intake = intake is not None
    has_pr = bool(ps.get("last_pr_url"))

    if not has_intake:
        state = "needs_intake"
    elif not has_pr:
        state = "needs_plan"
    else:
        state = "ready"

    # Review status (PR-H). Failures here log + return None; the call must
    # not 5xx the intake-status endpoint just because the trust pill query
    # missed.
    review_status = None
    try:
        import protocol_repo
        review_status = protocol_repo.get_review_status(user_id)
        # PHI hygiene: log only the state enum + token, never reviewer name
        # or notes_excerpt.
        if review_status:
            logger.info(
                "review_status token=%s state=%s",
                user_id, review_status.get("state"),
            )
    except protocol_repo.ProtocolRepoError as exc:
        # DATABASE_URL not configured (local dev / sqlite). Frontend renders
        # no pill; this is the documented graceful degrade.
        logger.warning("review_status unavailable (config): %s", exc)
        review_status = None
    except Exception as exc:
        logger.exception("review_status fetch failed token=%s: %s", user_id, exc)
        review_status = None

    return {
        "state": state,
        "patient_name": user.get("patient_name"),
        "has_intake": has_intake,
        "has_protocol": has_pr,
        "current_phase": ps.get("current_phase"),
        "current_week": ps.get("current_week"),
        "last_pr_url": ps.get("last_pr_url"),
        "session_count": len(user.get("session_history", [])),
        "review_status": review_status,
    }


# ─────────────────────────────────────────────────────────────────────────────
# PR-N: Direct check-in endpoint (auto check-in card after pose session)
#
# Lives at the bottom of main.py to stay clear of PR-I's edit zone around
# /protocols/{id}. Writes go through user_store.save_checkin -> the existing
# `checkins` table (RLS already in place; no migration needed).
#
# PHI hygiene: log token + pain_level (an integer is not PHI on its own) +
# returned checkin_id. Never log `notes` content.
# ─────────────────────────────────────────────────────────────────────────────


class CheckinCreate(BaseModel):
    """Auto-checkin payload posted right after a pose-form-check session.

    pain_level is the only required field. rpe + notes + associated_session_id
    are all optional — the patient can dismiss the card without filling them.
    """
    pain_level: int
    rpe: int | None = None
    notes: str | None = None
    associated_session_id: str | None = None


def _sanitize_checkin_notes(raw: str | None) -> str | None:
    """Strip control chars, trim whitespace, truncate to 500.

    Spec calls for >500 chars to truncate silently and 201-succeed (the card
    is mid-flow UX; rejecting on a stray paste would lose the patient's whole
    pain/rpe input). Truncation is logged at the call site without content.
    """
    if raw is None:
        return None
    cleaned = "".join(ch for ch in raw if ch == "\n" or ch == "\t" or ord(ch) >= 0x20)
    cleaned = cleaned.strip()
    if not cleaned:
        return None
    return cleaned[:500]


@app.post("/checkins", status_code=201)
def create_checkin(
    req: CheckinCreate,
    user_id: str = Depends(current_user_id),
):
    """Persist a patient self-reported check-in.

    Validates pain_level [0, 10] and rpe [1, 10] if provided. Sanitizes notes
    and truncates >500 chars. Returns {checkin_id, created_at}.
    """
    if req.pain_level < 0 or req.pain_level > 10:
        raise HTTPException(
            status_code=422,
            detail="pain_level must be between 0 and 10 inclusive",
        )
    if req.rpe is not None and (req.rpe < 1 or req.rpe > 10):
        raise HTTPException(
            status_code=422,
            detail="rpe must be between 1 and 10 inclusive",
        )

    notes_in_len = len(req.notes) if req.notes else 0
    sanitized_notes = _sanitize_checkin_notes(req.notes)
    truncated = notes_in_len > 500

    ensure_user(user_id)

    recorded_at = datetime.now(timezone.utc).isoformat()
    payload: dict = {
        "kind": "auto_checkin",
        "pain_level": req.pain_level,
        "rpe": req.rpe,
        "notes": sanitized_notes,
        "associated_session_id": req.associated_session_id,
        "recorded_at": recorded_at,
        "client": "web/auto-checkin-v1",
    }

    save_checkin(user_id, payload)

    checkin_id = payload.get("session_id")
    logger.info(
        "auto_checkin saved token=%s pain_level=%s rpe=%s notes_truncated=%s checkin_id=%s",
        user_id, req.pain_level, req.rpe, truncated, checkin_id,
    )

    return {
        "checkin_id": checkin_id,
        "created_at": payload.get("recorded_at"),
    }


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
