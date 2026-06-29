"""
api/tavus_tools.py - Tavus LLM-tool `delivery.api` callback for Supabase reads.

Coach Maya runs on a BYO/custom LLM (our proxy in api/tavus_proxy.py). Tavus's
tool registry (POST /v2/tools, attached to the PAL) lets the avatar's LLM call a
named tool during a conversation; with `delivery.api` Tavus dispatches that call
as an HTTPS POST to THIS endpoint, uses our 2xx body as the tool result, and
feeds it back to the LLM (`on_resolve: generate_response`) so the avatar speaks a
summary. This module is the read-only Supabase side of that contract: protocol,
recent history, and the approved exercise library.

Security (the whole boundary - there is no patient JWT here):
  - Tavus signs the EXACT raw request body with the shared secret configured in
    the tool's `delivery.api.auth.secret` and sends it as `X-Tavus-Signature`
    (HMAC-SHA256, lowercase hex, no timestamp/prefix). We verify against the raw
    bytes received BEFORE JSON parsing - re-serialized JSON reorders keys and
    breaks the signature. Verified against Tavus's own sample on
    docs.tavus.io pal/llm-tool-delivery ("Verifying API signatures").
  - The patient is recovered server-side from the callback's `conversation_id`
    (tavus_repo.get_token_by_conversation_id) - the body is never trusted as an
    identity, it only keys a lookup against an active tavus_sessions row. Same
    "never trust the body" rule as the proxy.

Failure modes map onto Tavus's retry table: 503 (secret unset) / 401 (bad
signature) / 400 (malformed) / 404 (unmapped conversation) are NOT retried by
Tavus for hmac auth, so the avatar acknowledges a graceful failure instead of
hanging. 5xx (a genuine read error) gets one Tavus retry; the reads are
idempotent so that is safe.

PHI: every tool response egresses to Tavus (protocol exercise names, body
region, session history are PHI; conversation_id is not). Same BAA-gated,
test-data-only posture as the proxy and notification email paths. Log only
{conversation_id-present bool, tool name, status, token}; never log arguments,
exercise names, pain levels, or the response body.
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import os
from typing import Any

from fastapi import APIRouter, Header, HTTPException, Request

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/tavus/tools", tags=["tavus-tools"])

# History window + response-size caps. Responses are injected into the model's
# context, so keep them tight (Tavus doc guidance + token cost).
_HISTORY_DEFAULT_DAYS = 7
_HISTORY_MAX_DAYS = 14
_HISTORY_MAX_ROWS = 20
_PROTOCOL_MAX_EXERCISES = 12
_EXERCISES_MAX = 30


class _UnknownTool(Exception):
    """Raised when the callback's `name` matches no registered tool."""


def _verify_signature(raw_body: bytes, signature: str | None) -> None:
    """Gate on the HMAC-SHA256 signature Tavus sends in `X-Tavus-Signature`.

    Tavus computes `hmac_sha256(secret, raw_body).hexdigest()` (lowercase hex,
    no timestamp/prefix) over the exact bytes it sends, using the secret we set
    in the tool's `delivery.api.auth.secret`. We MUST hash the raw bytes we
    received - a re-serialized body can reorder keys and break the match.

    Empty secret -> 503 (feature configured-out, mirrors the proxy's 503).
    Missing/wrong signature -> 401 (constant-time compare; Tavus does not retry
    a 401 under hmac auth, so the avatar fails gracefully).
    """
    secret = (os.getenv("TAVUS_TOOL_HMAC_SECRET") or "").strip()
    if not secret:
        raise HTTPException(status_code=503, detail="Tavus tools not configured.")
    expected = hmac.new(secret.encode("utf-8"), raw_body, hashlib.sha256).hexdigest()
    provided = (signature or "").strip()
    if not provided or not hmac.compare_digest(provided, expected):
        raise HTTPException(status_code=401, detail="Bad signature.")


def _parse_arguments(raw: Any) -> dict[str, Any]:
    """Decode the callback's `arguments` field.

    Tavus sends `arguments` as a JSON-ENCODED STRING (e.g. "{\"days\":7}"), not
    an object. Tolerate an already-decoded dict and an empty/missing value so a
    no-arg tool (get_active_protocol) never 400s on a "{}" or "".
    """
    if raw is None or raw == "":
        return {}
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
        except (ValueError, TypeError):
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def _compact(d: dict[str, Any]) -> dict[str, Any]:
    """Drop keys whose value is None/empty so the injected result stays tiny."""
    return {k: v for k, v in d.items() if v is not None and v != ""}


def _resolve_token(conversation_id: str) -> str | None:
    """Map a Tavus conversation_id to the patient token, or None.

    Mirrors the proxy's recovery path. Repo unavailable (no DATABASE_URL) ->
    None, never a leak.
    """
    try:
        import tavus_repo

        return tavus_repo.get_token_by_conversation_id(conversation_id)
    except Exception as exc:  # repo import / DSN failure - degrade, don't leak
        logger.warning("tavus_tools repo lookup failed: %s", exc)
        return None


def _get_active_protocol(token: str) -> dict[str, Any]:
    """Tool: the patient's current approved protocol (this week's exercises)."""
    from protocol_loader import fetch_protocol_for_user

    payload = fetch_protocol_for_user(token) or {}
    exercises = payload.get("exercises") or []
    if not exercises:
        return {
            "status": "no_active_protocol",
            "message": "No active protocol on file for this patient yet.",
        }
    items = []
    for ex in exercises[:_PROTOCOL_MAX_EXERCISES]:
        items.append(
            _compact(
                {
                    "id": ex.get("id") or ex.get("library_id") or ex.get("exercise_id"),
                    "name": ex.get("name"),
                    "sets": ex.get("sets"),
                    "reps": ex.get("reps"),
                    "rom_target_deg": ex.get("ROM_target_deg") or ex.get("rom_target_deg"),
                }
            )
        )
    return _compact(
        {
            "status": "ok",
            "week": payload.get("week"),
            "phase": payload.get("phase"),
            "body_region": payload.get("body_region"),
            "exercises": items,
        }
    )


def _get_recent_history(token: str, args: dict[str, Any]) -> dict[str, Any]:
    """Tool: the patient's logged sessions over the last N days (1-14, default 7)."""
    import session_repo

    days = args.get("days", _HISTORY_DEFAULT_DAYS)
    try:
        days = int(days)
    except (TypeError, ValueError):
        days = _HISTORY_DEFAULT_DAYS
    days = max(1, min(_HISTORY_MAX_DAYS, days))

    rows = session_repo.list_recent(token, days=days)
    # list_recent is oldest-first; surface the most recent rows under the cap.
    rows = rows[-_HISTORY_MAX_ROWS:]
    sessions = []
    for r in rows:
        created = r.get("created_at") or ""
        sessions.append(
            _compact(
                {
                    "date": created[:10] if isinstance(created, str) else None,
                    "exercise": r.get("exercise_id"),
                    "completed_reps": r.get("completed_reps"),
                    "planned_reps": r.get("planned_reps"),
                    "status": r.get("status"),
                    "pain_level": r.get("checkin_pain_level"),
                }
            )
        )
    return {"status": "ok", "days": days, "count": len(sessions), "sessions": sessions}


def _list_approved_exercises(args: dict[str, Any]) -> dict[str, Any]:
    """Tool: the in-scope, library-approved exercises (knee + ankle)."""
    import exercise_kb
    from agents.researcher import IN_SCOPE_REGIONS

    phase = args.get("phase")
    region = args.get("body_region")
    source = exercise_kb.find_by_phase(str(phase)) if phase else exercise_kb.list_all()

    items = []
    for ex in source:
        ex_region = ex.get("body_region")
        if ex_region not in IN_SCOPE_REGIONS:
            continue
        if region and ex_region != region:
            continue
        items.append(_compact({"id": ex.get("id"), "name": ex.get("name"), "region": ex_region}))
        if len(items) >= _EXERCISES_MAX:
            break
    return {"status": "ok", "count": len(items), "exercises": items}


def _run_tool(name: str, args: dict[str, Any], token: str) -> dict[str, Any]:
    """Switch on the registered tool `name`. Raises _UnknownTool otherwise.

    `get_patient_protocols` is the name Andre registered in the Tavus dashboard;
    `get_active_protocol` is accepted as an alias so either registration works.
    """
    if name in ("get_patient_protocols", "get_active_protocol"):
        return _get_active_protocol(token)
    if name == "get_recent_history":
        return _get_recent_history(token, args)
    if name == "list_approved_exercises":
        return _list_approved_exercises(args)
    raise _UnknownTool(name)


@router.post("/dispatch")
async def dispatch(
    request: Request,
    x_tavus_signature: str | None = Header(default=None),
):
    """Tavus `delivery.api` callback: verify -> map patient -> read -> tiny JSON.

    Returns a small JSON object that Tavus feeds back to the avatar's LLM as the
    tool result. All blocking DB work runs in a worker thread so the request is
    fully anchored (no fire-and-forget; safe under Fluid Compute).
    """
    raw = await request.body()
    _verify_signature(raw, x_tavus_signature)

    try:
        body = json.loads(raw)
    except (ValueError, TypeError):
        raise HTTPException(status_code=400, detail="Invalid JSON body.")
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="Invalid request body.")

    name = body.get("name")
    conversation_id = body.get("conversation_id")
    if not isinstance(name, str) or not name.strip():
        raise HTTPException(status_code=400, detail="name is required.")
    if not isinstance(conversation_id, str) or not conversation_id.strip():
        raise HTTPException(status_code=400, detail="conversation_id is required.")
    name = name.strip()
    conversation_id = conversation_id.strip()
    args = _parse_arguments(body.get("arguments"))

    token = await asyncio.to_thread(_resolve_token, conversation_id)
    if not token:
        logger.info("tavus_tools unresolved patient name=%s conversation_id_present=True", name)
        raise HTTPException(status_code=404, detail="Conversation not found.")

    try:
        result = await asyncio.to_thread(_run_tool, name, args, token)
    except _UnknownTool:
        logger.info("tavus_tools unknown tool token=%s name=%s", token, name)
        raise HTTPException(status_code=400, detail="Unknown tool.")
    except Exception:
        logger.exception("tavus_tools tool failed token=%s name=%s", token, name)
        raise HTTPException(status_code=500, detail="Tool failed.")

    logger.info("tavus_tools ok token=%s name=%s", token, name)
    return result
