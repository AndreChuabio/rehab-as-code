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

Failure modes are disambiguated via the returned status enum so the
clinician UI can render the right micro-state instead of one generic
"Summary unavailable" string. Statuses:
  * "no_diff"        — active == proposed (no LLM call made)
  * "no_api_key"     — ANTHROPIC_API_KEY unset
  * "sdk_error"      — anthropic SDK raised (5xx, rate limit, timeout) or
                       SDK module missing
  * "empty_response" — model returned empty text or text >500 chars
  * "ok"             — narration is valid model output

We never silently swap in a stale cache or invent text — clinicians need
to know when (and why) AI assistance is offline.
"""
from __future__ import annotations

import logging
import os
import time
from typing import Any, Literal

logger = logging.getLogger(__name__)

# Status enum for summarize(). Exposed so callers (main.py, tests) can
# match on it without re-declaring the literal.
NarratorStatus = Literal["no_diff", "no_api_key", "sdk_error", "empty_response", "ok"]


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

# Default retry count for empty-text responses from Haiku. Empty / blank
# is a transient failure mode on small diffs; one retry clears the bulk
# of cases without paying noticeably more in latency or cost. Override
# via DIFF_NARRATOR_RETRY_ON_EMPTY for staging eval. sdk_error and
# overlong are NOT retried — those are persistent failure modes.
_DEFAULT_RETRY_ON_EMPTY = 1


def _model() -> str:
    return os.getenv("DIFF_NARRATOR_MODEL", _DEFAULT_MODEL)


def _retry_on_empty() -> int:
    raw = os.getenv("DIFF_NARRATOR_RETRY_ON_EMPTY", "").strip()
    if not raw:
        return _DEFAULT_RETRY_ON_EMPTY
    try:
        return max(0, int(raw))
    except ValueError:
        logger.warning(
            "invalid DIFF_NARRATOR_RETRY_ON_EMPTY=%r, using default %d",
            raw, _DEFAULT_RETRY_ON_EMPTY,
        )
        return _DEFAULT_RETRY_ON_EMPTY


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


def _summarize_narrator(result: tuple[str | None, "NarratorStatus"]) -> dict[str, Any]:
    """PHI-safe summary: status enum only — never the prose itself.
    The narration text already lives in protocols.diff_summary on the
    happy path; duplicating into pipeline_runs would be redundant +
    adds a second PHI surface to govern."""
    if not isinstance(result, tuple) or len(result) < 2:
        return {"_unknown_shape": str(type(result))}
    text, status = result[0], result[1]
    return {
        "narrator_status": str(status) if status is not None else None,
        "text_len": len(text) if isinstance(text, str) else 0,
    }


def _decision_from_narrator(result: tuple[str | None, "NarratorStatus"]) -> str | None:
    if not isinstance(result, tuple) or len(result) < 2:
        return None
    return str(result[1]) if result[1] is not None else None


from observability import trace_sync


@trace_sync(
    "diff_narrator",
    model="claude-haiku-4-5",
    summarize=_summarize_narrator,
    decision_from=_decision_from_narrator,
)
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
) -> tuple[str | None, NarratorStatus]:
    """Produce a 2-3 sentence narration of the protocol diff plus a status.

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
    tuple[str | None, NarratorStatus]
        Pair of (narration_text, status). Narration is a non-empty string
        only when status == "ok". For all other statuses, the narration
        is None and the status disambiguates which failure mode produced
        the gap so the clinician dashboard can render the right micro-
        state (hide the block, show "key not configured", offer retry,
        etc.). We never raise, never silently substitute; the clinician
        always knows when (and why) AI assistance is offline.
    """
    if not _has_meaningful_diff(active_payload, proposed_payload):
        # No work to do. Don't log — happens on every patient self-fetch
        # path that funnels through here, plus on stale drafts whose
        # active row already matches.
        return None, "no_diff"

    assert proposed_payload is not None  # _has_meaningful_diff guarantees this

    cache_key = (str(active_id or ""), str(proposed_id or ""))
    if cache_key[1] and cache_key in _CACHE:
        # Hit. Skip the cost. Don't log the text.
        logger.info(
            "diff_narrator cache_hit protocol_id=%s clinician_id=%s",
            protocol_id, clinician_id,
        )
        return _CACHE[cache_key], "ok"

    api_key = os.getenv("ANTHROPIC_API_KEY", "").strip()
    if not api_key:
        logger.warning(
            "diff_narrator skipped: ANTHROPIC_API_KEY not set "
            "protocol_id=%s clinician_id=%s",
            protocol_id, clinician_id,
        )
        return None, "no_api_key"

    try:
        import anthropic
    except ImportError as exc:
        # SDK missing is functionally an "sdk_error" — the model couldn't
        # be reached. Surface it the same way so the UI offers retry.
        logger.warning(
            "diff_narrator skipped: anthropic SDK missing: %s "
            "protocol_id=%s clinician_id=%s",
            exc, protocol_id, clinician_id,
        )
        return None, "sdk_error"

    client = anthropic.Anthropic(api_key=api_key)
    user_prompt = _build_user_prompt(
        active_payload,
        proposed_payload,
        intake_payload,
        last_5_checkins or [],
        recent_sessions or [],
    )

    # Retry loop: only the empty-response branch retries. sdk_error and
    # overlong are persistent failure modes — retrying them masks real
    # problems (network, rate limit, runaway prompt) and the UI's Retry
    # pill is the right surface for clinician-driven re-attempts.
    total_attempts = _retry_on_empty() + 1
    for attempt in range(total_attempts):
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
            # APIConnectionError, etc. Surface as sdk_error without retry.
            elapsed_ms = int((time.monotonic() - started) * 1000)
            logger.warning(
                "diff_narrator anthropic call failed in %dms: %s "
                "protocol_id=%s clinician_id=%s",
                elapsed_ms, exc, protocol_id, clinician_id,
            )
            return None, "sdk_error"

        elapsed_ms = int((time.monotonic() - started) * 1000)

        # Pull the text block. Haiku may return tool calls or thinking
        # blocks in future SDK versions; defensively extract the first
        # text block.
        text = ""
        for block in resp.content or []:
            if getattr(block, "type", None) == "text":
                text = (getattr(block, "text", "") or "").strip()
                if text:
                    break

        if not text:
            # Empty / blank text is the retryable case. If we have
            # attempts left, log + continue. If we've exhausted, return
            # empty_response so the UI shows the right micro-state.
            if attempt < total_attempts - 1:
                logger.warning(
                    "diff_narrator retry_on_empty attempt=%d/%d "
                    "protocol_id=%s clinician_id=%s",
                    attempt + 1, total_attempts, protocol_id, clinician_id,
                )
                continue
            logger.warning(
                "diff_narrator empty response after %d attempts in %dms "
                "protocol_id=%s clinician_id=%s",
                total_attempts, elapsed_ms, protocol_id, clinician_id,
            )
            return None, "empty_response"

        if len(text) > _MAX_CHARS:
            # Don't truncate — clinicians shouldn't see a sentence cut in
            # half. Overlong is a different failure mode than empty (the
            # model produced text, just too much) and isn't transient, so
            # no retry. Surfaced as empty_response to keep the UI states
            # consolidated; consider splitting if we want a distinct
            # micro-copy for "model rambled."
            logger.warning(
                "diff_narrator overlong response (%d chars) in %dms "
                "attempt=%d/%d protocol_id=%s clinician_id=%s",
                len(text), elapsed_ms, attempt + 1, total_attempts,
                protocol_id, clinician_id,
            )
            return None, "empty_response"

        if cache_key[1]:
            _CACHE[cache_key] = text

        # Success. Log timing + token counts + attempt number ONLY. Never
        # the narration text. attempts=N lets us watch retry rate via
        # Vercel logs without PHI exposure.
        usage = getattr(resp, "usage", None)
        in_tokens = getattr(usage, "input_tokens", None) if usage else None
        out_tokens = getattr(usage, "output_tokens", None) if usage else None
        logger.info(
            "diff_narrator ok in %dms attempts=%d in_tokens=%s out_tokens=%s "
            "protocol_id=%s clinician_id=%s",
            elapsed_ms, attempt + 1, in_tokens, out_tokens,
            protocol_id, clinician_id,
        )
        return text, "ok"

    # Defensive: the loop should always return inside its body. If we
    # reach here, the retry budget was 0 and the first attempt fell
    # through somehow — treat as empty_response.
    return None, "empty_response"
