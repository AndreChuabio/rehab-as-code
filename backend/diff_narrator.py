"""
diff_narrator.py — plain-English summary of a protocol diff for clinicians.

When a clinician opens a pending protocol, the dashboard already shows a
side-by-side JSON diff. That tells them WHAT changed but not WHY. This
module calls Anthropic Haiku 4.5 to produce a 2-3 sentence narration that
cites the numerical changes (sets, reps, minutes, ROM degrees) AND ties
them back to the patient's recent wearables / symptoms / session
completion. Goal: clinician scans the summary in 5 seconds, then drills
into the diff only if the summary doesn't satisfy them.

Inputs are sourced server-side from Supabase by the caller (main.py). The
narrator is patient-PHI-aware:
  * `protocol_id` and `clinician_id` are logged.
  * The narration text and any patient identifiers are NEVER logged.

Caching:
  * Narration is deterministic per (active_id, proposed_id) pair within a
    function instance lifetime. We memoize in-process to avoid repeat
    Haiku spend when a clinician toggles between queue items.
  * Cache lifetime == Vercel function lifetime. On cold start it's empty;
    that's fine — the call is ~$0.001 and ~1s.

Failure mode:
  * Any error (no API key, SDK missing, Anthropic 5xx, malformed/empty
    output, output >500 chars) returns None. The caller renders a muted
    "Summary unavailable, see diff below" fallback. We do NOT silently
    swap in a stale cache or invent text — clinicians need to know when
    AI assistance failed.
"""
from __future__ import annotations

import logging
import os
import time
from typing import Any

logger = logging.getLogger(__name__)


# Model ID is pinned. Production-grade prompts target a specific model
# version; "claude-haiku-4-5" without a date suffix is not a production
# identifier. Override via env only for eval / staging.
_DEFAULT_MODEL = "claude-haiku-4-5-20251001"

# Output guardrails. The clinician is supposed to scan in seconds, not
# read a paragraph. We hard-cap and fall back rather than truncate so
# they never see a half-sentence.
_MAX_CHARS = 500
_MAX_TOKENS = 220  # generous for ~3 sentences; trims pathological cases

_SYSTEM_PROMPT = (
    "You write clinical summaries for physical therapists. Be specific, "
    "cite numerical changes (sets, reps, minutes, ROM degrees), and "
    "explain the WHY drawing on wearables, recent symptom logs, and "
    "session completion. Do not invent values not in the input. Do not "
    "give medical advice. 2-3 sentences max."
)


# Module-level cache: (active_id, proposed_id) -> narration string.
# Bounded loosely; Vercel functions recycle long before this matters.
_CACHE: dict[tuple[str, str], str] = {}


def _model() -> str:
    return os.getenv("DIFF_NARRATOR_MODEL", _DEFAULT_MODEL)


def _build_user_prompt(
    active_payload: dict[str, Any] | None,
    proposed_payload: dict[str, Any],
    intake_payload: dict[str, Any] | None,
    last_5_checkins: list[dict[str, Any]],
    recent_sessions: list[dict[str, Any]],
) -> str:
    """Assemble the structured input for Haiku.

    We ship the JSON verbatim — Haiku is good at structured compare —
    and let the model surface what's changed. Everything is tagged so
    the model knows what each block is.
    """
    import json

    parts: list[str] = []
    parts.append("Summarize the change from the active protocol to the proposed protocol.")
    if active_payload:
        parts.append("ACTIVE PROTOCOL (currently in effect):\n" + json.dumps(active_payload, indent=2, default=str))
    else:
        parts.append("ACTIVE PROTOCOL: none — this is the first protocol for this patient.")
    parts.append("PROPOSED PROTOCOL (under clinician review):\n" + json.dumps(proposed_payload, indent=2, default=str))

    if intake_payload:
        parts.append("PATIENT INTAKE (baseline):\n" + json.dumps(intake_payload, indent=2, default=str))
    if last_5_checkins:
        parts.append("LAST 5 CHECK-INS (most recent first):\n" + json.dumps(last_5_checkins, indent=2, default=str))
    if recent_sessions:
        parts.append("RECENT SESSIONS (last 7 days):\n" + json.dumps(recent_sessions, indent=2, default=str))

    parts.append(
        "Output: a single paragraph of 2-3 sentences. Cite specific "
        "numerical changes and the patient evidence that justifies them. "
        "No greeting, no sign-off, no markdown."
    )
    return "\n\n".join(parts)


def _has_meaningful_diff(
    active_payload: dict[str, Any] | None,
    proposed_payload: dict[str, Any] | None,
) -> bool:
    """Skip the LLM call when there's nothing to narrate.

    Cases:
      * proposed missing entirely -> no diff
      * active missing AND proposed empty -> no diff
      * active and proposed identical -> no diff
    """
    if not proposed_payload:
        return False
    if active_payload is None:
        # Initial protocol: narrate only if it has actual content.
        return bool(proposed_payload.get("exercises") or proposed_payload.get("session_targets"))
    return active_payload != proposed_payload


def summarize(
    active_payload: dict[str, Any] | None,
    proposed_payload: dict[str, Any] | None,
    intake_payload: dict[str, Any] | None,
    last_5_checkins: list[dict[str, Any]] | None,
    recent_sessions: list[dict[str, Any]] | None,
    *,
    active_id: str | None = None,
    proposed_id: str | None = None,
    protocol_id: str | None = None,
    clinician_id: str | None = None,
) -> str | None:
    """Produce a 2-3 sentence narration of the protocol diff.

    Parameters
    ----------
    active_payload : dict | None
        The patient's currently-active protocol payload (the YAML/JSON
        content — exercises, phase, week, etc.), or None if this is the
        first protocol for the patient.
    proposed_payload : dict
        The pending-review protocol payload.
    intake_payload : dict | None
        Patient intake snapshot — baseline injury, ROM, surgery date.
    last_5_checkins : list[dict] | None
        Recent symptom / pain-level check-ins, oldest-to-newest from the
        caller (we don't reorder).
    recent_sessions : list[dict] | None
        Last 7 days of session rows from public.sessions.
    active_id : str | None
        Active protocol row id; used as half of the cache key. When
        omitted, the call still runs but the result is not cached.
    proposed_id : str | None
        Proposed protocol row id; used as half of the cache key.
    protocol_id : str | None
        For logging only. Usually equals proposed_id.
    clinician_id : str | None
        For logging only. The Supabase auth.uid() of the clinician
        viewing the diff.

    Returns
    -------
    str | None
        The narration text on success, or None on:
          * no meaningful diff between active and proposed
          * missing ANTHROPIC_API_KEY
          * Anthropic SDK error / 5xx / rate limit
          * model returned empty or >500 chars

        On None the frontend renders "Summary unavailable, see diff below"
        in muted gray. We never raise, never silently substitute; the
        clinician knows when AI assistance is offline.
    """
    if not _has_meaningful_diff(active_payload, proposed_payload):
        return None

    assert proposed_payload is not None  # _has_meaningful_diff guarantees this

    cache_key = (str(active_id or ""), str(proposed_id or ""))
    if cache_key[1] and cache_key in _CACHE:
        # Hit. Skip the cost. Don't log the text.
        logger.info(
            "diff_narrator cache_hit protocol_id=%s clinician_id=%s",
            protocol_id, clinician_id,
        )
        return _CACHE[cache_key]

    api_key = os.getenv("ANTHROPIC_API_KEY", "").strip()
    if not api_key:
        logger.warning(
            "diff_narrator skipped: ANTHROPIC_API_KEY not set "
            "protocol_id=%s clinician_id=%s",
            protocol_id, clinician_id,
        )
        return None

    try:
        import anthropic
    except ImportError as exc:
        logger.warning(
            "diff_narrator skipped: anthropic SDK missing: %s "
            "protocol_id=%s clinician_id=%s",
            exc, protocol_id, clinician_id,
        )
        return None

    client = anthropic.Anthropic(api_key=api_key)
    user_prompt = _build_user_prompt(
        active_payload,
        proposed_payload,
        intake_payload,
        last_5_checkins or [],
        recent_sessions or [],
    )

    started = time.monotonic()
    try:
        resp = client.messages.create(
            model=_model(),
            max_tokens=_MAX_TOKENS,
            system=_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_prompt}],
        )
    except Exception as exc:
        # Anthropic SDK raises a hierarchy: APIError, RateLimitError,
        # APIConnectionError, etc. We don't care to discriminate here —
        # the surface behavior is the same: fall back, log the error
        # without leaking PHI.
        elapsed_ms = int((time.monotonic() - started) * 1000)
        logger.warning(
            "diff_narrator anthropic call failed in %dms: %s "
            "protocol_id=%s clinician_id=%s",
            elapsed_ms, exc, protocol_id, clinician_id,
        )
        return None

    elapsed_ms = int((time.monotonic() - started) * 1000)

    # Pull the text block. Haiku may return tool calls or thinking blocks
    # in future SDK versions; defensively extract the first text block.
    text = ""
    for block in resp.content or []:
        if getattr(block, "type", None) == "text":
            text = (getattr(block, "text", "") or "").strip()
            if text:
                break

    if not text:
        logger.warning(
            "diff_narrator empty response in %dms "
            "protocol_id=%s clinician_id=%s",
            elapsed_ms, protocol_id, clinician_id,
        )
        return None

    if len(text) > _MAX_CHARS:
        # Don't truncate — clinicians shouldn't see a sentence cut in half.
        logger.warning(
            "diff_narrator overlong response (%d chars) in %dms "
            "protocol_id=%s clinician_id=%s",
            len(text), elapsed_ms, protocol_id, clinician_id,
        )
        return None

    if cache_key[1]:
        _CACHE[cache_key] = text

    # Success. Log timing + token counts ONLY. Never the narration text.
    usage = getattr(resp, "usage", None)
    in_tokens = getattr(usage, "input_tokens", None) if usage else None
    out_tokens = getattr(usage, "output_tokens", None) if usage else None
    logger.info(
        "diff_narrator ok in %dms in_tokens=%s out_tokens=%s "
        "protocol_id=%s clinician_id=%s",
        elapsed_ms, in_tokens, out_tokens, protocol_id, clinician_id,
    )
    return text
