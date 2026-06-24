"""
api/tavus_proxy.py - BYO-LLM OpenAI-compatible proxy for Tavus CVI.

Tavus's persona "custom LLM" layer calls an OpenAI-compatible
/chat/completions endpoint for every spoken turn. This module exposes that
endpoint and routes it into Coach Maya's single brain (coach_chat.chat_stream)
so the avatar IS Maya: same tools, same clinician-review trust loop, same live
Supabase reads. No brain is duplicated here.

Flow per call:
  1. Authenticate a shared secret (Tavus persona llm.api_key == TAVUS_PROXY_SECRET).
     This is NOT a patient JWT - Tavus is not a logged-in user. The request
     body never carries the patient token; the patient is recovered server-side
     after the secret check (the documented exception to "never trust the body").
  2. Recover the patient token:
       - PRIMARY: a conversation_id forwarded by Tavus (body or header) ->
         tavus_repo.get_token_by_conversation_id.
       - FALLBACK: the opaque [RAC_SESSION_REF] sentinel we embedded in
         conversational_context at create-conversation time ->
         tavus_repo.get_token_by_session_ref.
  3. STRIP every incoming system-role message. coach_chat prepends its own
     freshly-built system prompt; a forwarded conversational_context would
     double-prompt, is stale relative to the fresh Supabase read, and carries
     the session_ref - which must never reach the model. Only user/assistant
     turns are passed into chat_stream.
  4. Re-read {health, protocol, display_name} fresh from Supabase for the
     recovered patient and drive coach_chat.chat_stream.
  5. Translate Maya's custom event protocol into OpenAI SSE chunks: only
     {'type':'token'} deltas reach TTS; card/tool_call/tool_result/triage_alert
     run their side effects inside chat_stream but are consumed silently here.

DOCUMENTED ASSUMPTION (open question): it is NOT confirmed that Tavus CVI
forwards conversation_id to the custom-LLM call, nor the exact request/header
shape. The embedded session_ref is the robust required fallback; conversation_id
is preferred-if-present. Verify against Tavus docs / a live call before prod.

Body params stream / temperature / max_tokens are IGNORED: chat_stream owns the
upstream OpenAI create() call (hard-capped max_tokens=350, temperature=0.4) and
the proxy always streams. This is intentional, not a bug.

PHI hygiene: log only token + conversation_id/session_ref-present bool + status
codes. Never log message bodies, display_name, the system prompt, or deltas.
"""
from __future__ import annotations

import json
import logging
import os
import re
import secrets
import time
from typing import Any
from uuid import uuid4

from fastapi import APIRouter, Header, HTTPException, Request
from fastapi.responses import StreamingResponse

import coach_chat
from health_mock import get_health_data
from patient_context import (
    _chat_trigger_executor_factory,
    _clinician_attention_writer_factory,
    _last_pose_metrics,
)
from protocol_loader import fetch_protocol_for_user
from user_store import ensure_user, get_display_name, get_last_set_completion

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/tavus/llm", tags=["tavus-proxy"])

# Sentinel the proxy regex-extracts from a stripped system message as the
# fallback patient key. Must match tavus_client.create_conversation.
_SESSION_REF_RE = re.compile(r"\[RAC_SESSION_REF\]:\s*(\S+)")


def _require_shared_secret(authorization: str | None) -> None:
    """Gate the proxy on TAVUS_PROXY_SECRET.

    Empty env -> 503 (feature configured-out). Missing/wrong header -> 401.
    Constant-time compare so a wrong secret leaks no timing signal.
    """
    expected = (os.getenv("TAVUS_PROXY_SECRET") or "").strip()
    if not expected:
        raise HTTPException(status_code=503, detail="Proxy not configured.")
    provided = ""
    if authorization:
        parts = authorization.split(" ", 1)
        provided = parts[1].strip() if len(parts) == 2 else authorization.strip()
    if not provided or not secrets.compare_digest(provided, expected):
        raise HTTPException(status_code=401, detail="Unauthorized.")


def _extract_conversation_id(body: dict[str, Any], request: Request) -> str | None:
    """Best-effort pull of a Tavus-forwarded conversation_id.

    Shape is unconfirmed; check the common spots without trusting any of them
    as a patient identifier (they only key a lookup against an active row).
    """
    for key in ("conversation_id", "conversationId"):
        val = body.get(key)
        if isinstance(val, str) and val.strip():
            return val.strip()
    meta = body.get("metadata")
    if isinstance(meta, dict):
        val = meta.get("conversation_id") or meta.get("conversationId")
        if isinstance(val, str) and val.strip():
            return val.strip()
    for hdr in ("x-tavus-conversation-id", "x-conversation-id"):
        val = request.headers.get(hdr)
        if val and val.strip():
            return val.strip()
    return None


def _extract_session_ref(messages: list[dict[str, Any]]) -> str | None:
    """Regex the [RAC_SESSION_REF] sentinel out of any system message.

    Run BEFORE stripping system messages from the turns sent to the brain.
    The ref is the fallback patient key; it never reaches chat_stream.
    """
    for m in messages:
        if m.get("role") != "system":
            continue
        content = m.get("content")
        if isinstance(content, list):
            # Tavus may send the conversational_context as structured
            # content parts; flatten text-only so the sentinel regex still
            # runs. Mirrors _strip_system_messages's flattening.
            content = "".join(
                p.get("text", "")
                for p in content
                if isinstance(p, dict) and p.get("type") == "text"
            )
        if not isinstance(content, str):
            continue
        match = _SESSION_REF_RE.search(content)
        if match:
            return match.group(1)
    return None


def _strip_system_messages(
    messages: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Keep only user/assistant turns. System messages are dropped entirely.

    Defense in depth: coach_chat prepends its own system prompt, so forwarding
    a system message would double-prompt; dropping it also guarantees the
    session_ref can never be echoed into spoken output.
    """
    out: list[dict[str, Any]] = []
    for m in messages:
        role = m.get("role")
        if role not in ("user", "assistant"):
            continue
        content = m.get("content")
        if not isinstance(content, str):
            # Tavus may send structured content parts; flatten text-only.
            if isinstance(content, list):
                content = "".join(
                    p.get("text", "")
                    for p in content
                    if isinstance(p, dict) and p.get("type") == "text"
                )
            else:
                content = "" if content is None else str(content)
        out.append({"role": role, "content": content})
    return out


def _chunk(
    *,
    chunk_id: str,
    model: str,
    created: int,
    delta: dict[str, Any],
    finish_reason: str | None,
) -> str:
    """Render one OpenAI chat.completion.chunk SSE frame."""
    payload = {
        "id": chunk_id,
        "object": "chat.completion.chunk",
        "created": created,
        "model": model,
        "choices": [
            {"index": 0, "delta": delta, "finish_reason": finish_reason}
        ],
    }
    return f"data: {json.dumps(payload)}\n\n"


@router.post("/chat/completions")
async def chat_completions(
    request: Request,
    authorization: str | None = Header(default=None),
):
    """OpenAI-compatible, SSE-streaming custom-LLM endpoint for Tavus CVI.

    Returns text/event-stream of chat.completion.chunk frames terminated by
    `data: [DONE]`. Always streams regardless of the request's `stream` flag.
    """
    _require_shared_secret(authorization)

    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON body.")
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="Invalid request body.")

    raw_messages = body.get("messages")
    if not isinstance(raw_messages, list):
        raise HTTPException(status_code=400, detail="messages is required.")

    # Recover the patient: conversation_id primary, session_ref fallback.
    conversation_id = _extract_conversation_id(body, request)
    session_ref = _extract_session_ref(raw_messages)

    token: str | None = None
    try:
        import tavus_repo
        if conversation_id:
            token = tavus_repo.get_token_by_conversation_id(conversation_id)
        if not token and session_ref:
            token = tavus_repo.get_token_by_session_ref(session_ref)
    except Exception as exc:
        # Repo unavailable (e.g. no DATABASE_URL) - degrade, don't leak.
        logger.warning("tavus_proxy repo lookup failed: %s", exc)
        token = None

    if not token:
        logger.info(
            "tavus_proxy unresolved patient conversation_id_present=%s "
            "session_ref_present=%s",
            bool(conversation_id), bool(session_ref),
        )
        raise HTTPException(status_code=404, detail="Conversation not found.")

    # Strip system messages BEFORE handing turns to the brain.
    stripped = _strip_system_messages(raw_messages)

    # Re-read live patient state server-side. Never trust anything in the body.
    ensure_user(token)
    health = get_health_data(user_token=token)
    protocol = fetch_protocol_for_user(token) or {}
    recent_set = get_last_set_completion(token)
    if recent_set:
        protocol["_recent_set"] = recent_set
    display_name = get_display_name(token)
    last_pose_metrics = _last_pose_metrics(token)
    trigger_executor = _chat_trigger_executor_factory(token)
    clinician_attention_writer = _clinician_attention_writer_factory(token)

    model = body.get("model") or coach_chat._model()
    session_id = conversation_id or session_ref or "tavus"

    logger.info(
        "tavus_proxy chat token=%s conversation_id_present=%s session_ref_present=%s",
        token, bool(conversation_id), bool(session_ref),
    )

    async def gen():
        chunk_id = f"chatcmpl-{uuid4().hex}"
        created = int(time.time())
        first = True
        try:
            async for event in coach_chat.chat_stream(
                messages=stripped,
                health=health,
                protocol=protocol,
                trigger_executor=trigger_executor,
                user_token=token,
                display_name=display_name,
                session_id=session_id,
                last_pose_metrics=last_pose_metrics,
                clinician_attention_writer=clinician_attention_writer,
            ):
                etype = event.get("type")
                if etype == "token":
                    delta: dict[str, Any] = {"content": event.get("delta", "")}
                    if first:
                        delta["role"] = "assistant"
                        first = False
                    yield _chunk(
                        chunk_id=chunk_id, model=model, created=created,
                        delta=delta, finish_reason=None,
                    )
                elif etype == "error":
                    # PHI-safe: don't echo the brain's error into TTS; just
                    # close the stream cleanly so the avatar doesn't hang.
                    logger.warning(
                        "tavus_proxy brain error token=%s class=%s",
                        token, type(event.get("message")).__name__,
                    )
                    yield _chunk(
                        chunk_id=chunk_id, model=model, created=created,
                        delta={}, finish_reason="stop",
                    )
                    yield "data: [DONE]\n\n"
                    return
                elif etype == "done":
                    yield _chunk(
                        chunk_id=chunk_id, model=model, created=created,
                        delta={}, finish_reason="stop",
                    )
                    yield "data: [DONE]\n\n"
                    return
                # card / tool_call / tool_result / triage_alert: side effects
                # already ran inside chat_stream; consume silently (no TTS,
                # no UI surface in a voice call).
        except Exception as exc:
            logger.exception("tavus_proxy stream failed token=%s", token)
            yield _chunk(
                chunk_id=chunk_id, model=model, created=created,
                delta={}, finish_reason="stop",
            )
            yield "data: [DONE]\n\n"
            return
        # chat_stream always ends with a 'done' event, but guard the path.
        yield _chunk(
            chunk_id=chunk_id, model=model, created=created,
            delta={}, finish_reason="stop",
        )
        yield "data: [DONE]\n\n"

    return StreamingResponse(gen(), media_type="text/event-stream")
