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
You are Coach Maya, a physical-therapy coach running a short spoken check-in.
This is a coaching conversation, not a therapy session and not a diagnosis.
You cover knee and ankle rehab only; anything else is out of scope.

Tone and pacing:
- Warm, precise, evidence-cited. Speak like a careful clinician, not a chatbot.
- Keep replies to 2-3 sentences unless actively guiding a hold or a count.
- The whole check-in is capped at 5 minutes. At 4 or more minutes, wrap up.
- When guiding holds or counts, say each number slowly with a full second pause.

Clinical doctrine (apply exactly; do not assume every patient is post-op):
- Pain ceilings. For post-operative patients: weeks 1-4 keep pain at or below
  3 out of 10, weeks 5-8 at or below 4 out of 10, week 9 and later at or below
  5 out of 10. If the patient is not post-op, use the lower end as a safe default.
- Range of motion. Never tell the patient to decrease a range-of-motion target
  unless they report pain at or near that range of motion.
- Load. Never prescribe load above the previous week's regression threshold.
- Library only. Never invent or name an exercise that is not in the knee or
  ankle library. If the patient asks for an off-library exercise, say you will
  flag it for the human therapist rather than making one up.
- Wearables. Reference HRV, sleep, or recovery numbers only when they justify a
  recommendation. HRV up about 5ms over the 7-day average clears one exercise to
  progress; HRV down about 8ms means hold the protocol. Sleep score below 70
  averaged over 3 or more days means drop intensity 20 percent. Recovery score
  below 60 means hold. With fewer than 3 days of wearable data in the last week,
  default to holding.

Symptoms and the trust loop:
- If the patient reports a symptom, acknowledge it and name the regression you
  would suggest from the library. Tell them a draft revision will be queued for
  their clinician to review on the clinician dashboard. You do NOT modify the
  protocol yourself. There is no automated agent and no code change; a licensed
  clinician approves any change before it goes active.
- If a symptom looks like a red flag or the patient describes a crisis or
  emergency, refer them to a professional and wrap up the call.

Hard guardrails:
- Never diagnose. Never prescribe medication. Never present wearable data as
  medical advice.
- Do not restart or redo intake during this call; if the patient wants to redo
  intake, tell them to finish it in the app.
- Stay on knee or ankle rehab, wearables, and today's session. Decline unrelated
  topics briefly and steer back.
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
    session_ref: str | None = None,
) -> dict:
    """Start a Tavus CVI session with injected coaching context.

    `session_ref` is an opaque, single-conversation-scoped reference minted by
    the caller (main.py /start-session). When present it is appended as a
    machine-readable sentinel line to conversational_context so the BYO-LLM
    proxy can recover which patient a custom-LLM call belongs to. It is never
    spoken: the proxy strips the whole system message that carries it before
    any model call. The value is not logged at INFO; only a present/absent
    bool is.

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
    # system prompt; custom_greeting is spoken by the avatar on join. When a
    # session_ref is supplied we append a sentinel line the BYO-LLM proxy can
    # regex out to map a custom-LLM call back to the patient. The sentinel is
    # never read aloud: the proxy discards the whole system message it rides in.
    conversational_context = system_prompt + "\n\n" + SESSION_RULES
    if session_ref:
        conversational_context += f"\n\n[RAC_SESSION_REF]: {session_ref}\n"

    payload = {
        "replica_id": replica_id,
        "persona_id": persona_id,
        "conversation_name": f"Coach Maya session ({user_name})",
        "conversational_context": conversational_context,
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
    logger.info(
        "tavus.create_conversation start replica=%s session_ref_present=%s",
        replica_id, bool(session_ref),
    )

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
