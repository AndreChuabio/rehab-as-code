"""
tavus_client.py - Create a Tavus CVI (Conversational Video Interface) session.

Docs: https://docs.tavus.io/api-reference/conversations/create-conversation

Posture (post PR-P): no silent mock fallback. Missing keys -> raise
TavusConfigError; provider errors -> raise TavusAPIError. Callers (FastAPI
endpoints) translate these into 503 / 502 responses so the patient sees a
real error toast instead of a broken iframe.
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta, timezone

import requests
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)

TAVUS_BASE_URL = "https://tavusapi.com/v2"
MAX_SESSION_MINUTES = 5  # Keep it snappy — this is a coaching check-in, not therapy.
# Tavus terminates idle calls server-side; we add a small buffer to MAX_SESSION_MINUTES
# when computing the row's expires_at so the UI doesn't show "expired" while the
# call is still legitimately wrapping up.
EXPIRES_BUFFER_SECONDS = 60


SESSION_RULES = """
--- SESSION RULES ---
Objectives (follow strictly in this exact order):
1. Greetings + review relevant yesterday's metrics + health trends -> provide overall advice.
2. Face / Tongue reading based on traditional Chinese medicine -> provide nutritional advice based on observations.
3. Goal Setting: Look at the user's calendar and ask them about their goals for the day.
4. Guided affirmations + visualizations relevant to their stated goals -> motivate them.
5. Quick relaxation exercise (maximum 20 seconds) - reference the salamander exercise or similar quick resets.
6. End with a relevant motivational quote tailored to the user's day, then wish them a great day and close the session.

Guardrails:
- CRITICAL: Never suggest lengthy box breathing or long meditations. Only use the 20-second quick relaxation exercise.
- Keep the entire session under 5 minutes. If it has gone on for 4+ minutes, wrap up immediately.
- Keep all responses to 2-3 sentences max unless actively guiding an exercise or visualization.
- Never give medical diagnoses, prescribe medication, or present health data as medical advice.
- Never discuss topics unrelated to wellness, health, or the user's day ahead.
- If the user expresses a mental health crisis or emergency, gently refer them to a professional and end the session.
--- END SESSION RULES ---"""


class TavusError(RuntimeError):
    """Base class for tavus_client errors."""


class TavusConfigError(TavusError):
    """Raised when required Tavus env vars are missing.

    Surfaces at the FastAPI layer as a 503 — the feature is configured-out,
    not broken; retrying won't help until ops sets the keys.
    """


class TavusAPIError(TavusError):
    """Raised when the Tavus API returned an error or a malformed response.

    Surfaces at the FastAPI layer as a 502 — the upstream call failed and the
    patient should see "video call temporarily unavailable, please retry".
    """


def _get_keys() -> tuple[str, str, str]:
    """Read keys at call time so .env edits during dev are picked up.

    Raises TavusConfigError if any of the three required env vars is missing.
    """
    api_key = (os.getenv("TAVUS_API_KEY") or "").strip()
    replica_id = (os.getenv("TAVUS_REPLICA_ID") or "").strip()
    persona_id = (os.getenv("TAVUS_PERSONA_ID") or "").strip()
    missing = [
        name for name, val in (
            ("TAVUS_API_KEY", api_key),
            ("TAVUS_REPLICA_ID", replica_id),
            ("TAVUS_PERSONA_ID", persona_id),
        )
        if not val
    ]
    if missing:
        raise TavusConfigError(
            f"Tavus is not configured: missing env vars {missing}"
        )
    return api_key, replica_id, persona_id


def create_conversation(
    system_prompt: str,
    greeting: str,
    user_name: str = "there",
) -> dict:
    """Start a Tavus CVI session with injected coaching context.

    Returns:
        {
          "conversation_url":  str  (Daily.co room url for the iframe),
          "conversation_id":   str  (Tavus-side conversation handle),
          "status":            str  (Tavus-reported status, typically "active"),
          "replica_id":        str,
          "persona_id":        str,
          "expires_at":        ISO8601 str  (best-effort TTL the caller persists),
        }

    Raises:
        TavusConfigError  if required env vars are missing.
        TavusAPIError     if the upstream HTTP call fails or returns garbage.
    """
    api_key, replica_id, persona_id = _get_keys()

    headers = {
        "x-api-key": api_key,
        "Content-Type": "application/json",
    }

    # Tavus CVI payload — conversational_context injects our coach-specific
    # system prompt; custom_greeting is spoken by the avatar on join.
    payload = {
        "replica_id": replica_id,
        "persona_id": persona_id,
        "conversation_name": f"Coach Maya session ({user_name})",
        "conversational_context": system_prompt + "\n\n" + SESSION_RULES,
        "custom_greeting": greeting,
        "properties": {
            "max_call_duration": MAX_SESSION_MINUTES * 60,
            "participant_left_timeout": 60,
            "participant_absent_timeout": 300,
            "enable_recording": False,
            "apply_greenscreen": False,
            "language": "english",
            "enable_closed_captions": True,
        },
    }

    # PHI hygiene: log the call but NOT the patient's name, system prompt,
    # or greeting text. replica_id + persona_id are config IDs, not PHI, but
    # we keep the log lean.
    logger.info("tavus.create_conversation start replica=%s", replica_id)

    # Manual Langfuse span - Tavus is HTTP, not Anthropic, so the OTel
    # auto-instrumentation doesn't capture it. We emit a span so the
    # admin / Langfuse dashboard can see Tavus latency + failure rate
    # alongside the Anthropic agents. PHI: we metadata only replica_id
    # (config), not user_name or prompt.
    import langfuse_client
    with langfuse_client.span("tavus.create_conversation", replica_id=replica_id):
        try:
            response = requests.post(
                f"{TAVUS_BASE_URL}/conversations",
                headers=headers,
                json=payload,
                timeout=15,
            )
        except requests.RequestException as exc:
            logger.warning("tavus.create_conversation transport_error=%s", exc)
            raise TavusAPIError(f"Tavus transport error: {exc}") from exc

    if response.status_code >= 400:
        # Don't echo Tavus's body verbatim back to the user; log it for ops
        # and surface a generic message at the FastAPI layer.
        logger.warning(
            "tavus.create_conversation status=%s body=%s",
            response.status_code, response.text[:300],
        )
        raise TavusAPIError(
            f"Tavus returned HTTP {response.status_code}"
        )

    try:
        data = response.json()
    except ValueError as exc:
        logger.warning("tavus.create_conversation invalid_json body=%s", response.text[:200])
        raise TavusAPIError("Tavus returned non-JSON response") from exc

    conversation_url = data.get("conversation_url")
    conversation_id = data.get("conversation_id")
    if not conversation_url or not conversation_id:
        logger.warning(
            "tavus.create_conversation missing_fields keys=%s",
            list(data.keys()),
        )
        raise TavusAPIError("Tavus response missing conversation_id/url")

    expires_at = (
        datetime.now(timezone.utc)
        + timedelta(seconds=MAX_SESSION_MINUTES * 60 + EXPIRES_BUFFER_SECONDS)
    ).isoformat()

    logger.info(
        "tavus.create_conversation ok conversation_id=%s status=%s",
        conversation_id, data.get("status"),
    )
    return {
        "conversation_url": conversation_url,
        "conversation_id": conversation_id,
        "status": data.get("status", "active"),
        "replica_id": replica_id,
        "persona_id": persona_id,
        "expires_at": expires_at,
    }


def end_conversation(conversation_id: str) -> bool:
    """Best-effort end of an active Tavus conversation.

    Returns True on a 200 from Tavus, False otherwise. Caller should still
    flip the row's status=ended regardless of the return value — the patient
    is done with the session even if Tavus's deletion call hiccups.
    """
    if not conversation_id:
        return False
    try:
        api_key, _, _ = _get_keys()
    except TavusConfigError:
        # Nothing to talk to; treat as a no-op so the row can still be ended.
        return False
    try:
        response = requests.delete(
            f"{TAVUS_BASE_URL}/conversations/{conversation_id}",
            headers={"x-api-key": api_key},
            timeout=10,
        )
        return response.status_code == 200
    except requests.RequestException as exc:
        logger.info("tavus.end_conversation transport_error=%s", exc)
        return False
