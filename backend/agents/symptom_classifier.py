"""
symptom_classifier.py - Phase F: in-chat symptom triage.

Runs inside coach_chat.chat_stream() when the patient's message contains a
pain/symptom keyword. Output is injected into Coach Maya's system prompt so
her natural-language reply incorporates the triage recommendation. Side
effects (writing a needs_clinician_review row, suggesting a regression) are
deterministic in the orchestrator - NOT LLM-routed.

Three severity tiers:

  minor               -> typical post-rehab discomfort. Maya acknowledges
                         and continues normal coaching. No protocol mutation.
  hold-load           -> pain >= 5/10 or worsening but no red flags. Maya
                         suggests the regression_exercise_id directly. No
                         protocol mutation.
  clinician-attention -> red flag (locking, popping, giving-way, severe pain
                         >= 8/10, sudden swelling, numbness, fever, post-op
                         concern). Orchestrator writes a protocols row with
                         status='needs_clinician_review' + safety_concerns.

Haiku 4.5 (claude-haiku-4-5-20251001) - cheap, fires often.

No silent fallbacks: any Anthropic / SDK / parsing failure raises
SymptomClassifierError. coach_chat catches and logs, then proceeds without
the [SYMPTOM_TRIAGE] block. We do NOT swallow as a fake "minor"
classification - that would mask a real outage and mislead Maya.

PHI hygiene: log token (UUID), severity, latency, and token counts only.
Never log message text or reasoning text - those carry patient PHI.
"""
from __future__ import annotations

import json
import logging
import os
import time
from typing import Any, Literal

logger = logging.getLogger(__name__)


_DEFAULT_MODEL = "claude-haiku-4-5-20251001"


class SymptomClassifierError(RuntimeError):
    """Raised when the symptom classifier cannot produce a verdict."""


Severity = Literal["minor", "hold-load", "clinician-attention"]
_VALID_SEVERITIES: tuple[str, ...] = ("minor", "hold-load", "clinician-attention")


_SYSTEM_PROMPT = (
    "You are a physical-therapy triage assistant inside a rehab coach chat. "
    "You are NOT a doctor. You triage a single patient message and any "
    "available context (recent wearables, current protocol, last pose set) "
    "into one of three severity tiers, then suggest what the coach should "
    "say next.\n\n"
    "Severity definitions:\n"
    "  - minor: typical post-rehab discomfort; pain <= 4/10; no red flags. "
    "Continue plan. Reassure and continue normal coaching.\n"
    "  - hold-load: pain >= 5/10 or worsening; movement-specific; no red "
    "flags. Suggest a regression of the offending exercise. Set "
    "regression_exercise_id to a library id when one is obviously safer.\n"
    "  - clinician-attention: red flags (joint locking, popping, giving-way, "
    "severe pain >= 8/10, sudden swelling, numbness, fever, post-op concern, "
    "loss of function). Flag the clinician. The coach should NOT prescribe "
    "anything; just acknowledge and tell the patient the clinician will reach "
    "out.\n\n"
    "Output ONLY via the classify_symptom tool. Be conservative - when in "
    "doubt between hold-load and clinician-attention, escalate to "
    "clinician-attention. Reasoning must fit in one sentence. "
    "suggested_response is a starting line for the coach (the coach will "
    "rewrite it in her own voice)."
)


_TOOL = {
    "name": "classify_symptom",
    "description": "Submit the symptom triage verdict.",
    "input_schema": {
        "type": "object",
        "properties": {
            "severity": {
                "type": "string",
                "enum": list(_VALID_SEVERITIES),
            },
            "reasoning": {
                "type": "string",
                "description": "One sentence: why this severity tier.",
            },
            "suggested_response": {
                "type": "string",
                "description": (
                    "Starting line for the coach to incorporate (under 40 "
                    "words). Acknowledge the symptom + state the next step."
                ),
            },
            "regression_exercise_id": {
                "type": ["string", "null"],
                "description": (
                    "Library exercise id to suggest as a regression. ONLY "
                    "set when severity == 'hold-load'; otherwise null."
                ),
            },
        },
        "required": [
            "severity",
            "reasoning",
            "suggested_response",
            "regression_exercise_id",
        ],
    },
}


def _model() -> str:
    return os.getenv("SYMPTOM_CLASSIFIER_MODEL", _DEFAULT_MODEL)


def _build_user_prompt(
    message: str,
    wearables: dict[str, Any] | None,
    protocol: dict[str, Any] | None,
    last_pose_metrics: dict[str, Any] | None,
) -> str:
    parts: list[str] = [f"Patient message:\n{message}"]
    if wearables:
        parts.append(
            "Wearables (last 24h):\n"
            + json.dumps(wearables, indent=2, default=str)
        )
    if protocol:
        # Keep the protocol payload but prune the verbose recent_set bag.
        slim = {
            k: v for k, v in protocol.items()
            if k not in ("_recent_set",)
        }
        parts.append(
            "Current protocol:\n"
            + json.dumps(slim, indent=2, default=str)
        )
    if last_pose_metrics:
        parts.append(
            "Last completed pose-check metrics:\n"
            + json.dumps(last_pose_metrics, indent=2, default=str)
        )
    parts.append(
        "Triage the message. Submit your verdict via classify_symptom. Be "
        "conservative on red flags."
    )
    return "\n\n".join(parts)


def classify(
    message: str,
    wearables: dict[str, Any] | None = None,
    protocol: dict[str, Any] | None = None,
    last_pose_metrics: dict[str, Any] | None = None,
    *,
    token: str | None = None,
) -> dict[str, Any]:
    """Run Haiku symptom triage on a single patient message.

    Parameters
    ----------
    message : str
        Raw patient chat message. Treated as PHI - not logged.
    wearables : dict | None
        Output of get_health_data() for the patient.
    protocol : dict | None
        Current active protocol payload (fetch_protocol_for_user).
    last_pose_metrics : dict | None
        pose_metrics from the last completed session row.
    token : str | None
        Patient UUID for logging. Never log the message itself.

    Returns
    -------
    dict
        {
          "severity": "minor" | "hold-load" | "clinician-attention",
          "reasoning": str,
          "suggested_response": str,
          "regression_exercise_id": str | None,
        }

    Raises
    ------
    SymptomClassifierError
        On Anthropic API failures, missing API key, or malformed output.
        coach_chat catches this, logs, and skips the [SYMPTOM_TRIAGE] block
        for this turn. We do NOT silently downgrade to "minor".
    """
    api_key = os.getenv("ANTHROPIC_API_KEY", "").strip()
    if not api_key:
        raise SymptomClassifierError("ANTHROPIC_API_KEY is not configured")

    try:
        import anthropic
    except ImportError as exc:
        raise SymptomClassifierError(
            f"anthropic SDK not installed: {exc}"
        ) from exc

    client = anthropic.Anthropic(api_key=api_key)
    prompt = _build_user_prompt(message, wearables, protocol, last_pose_metrics)

    started = time.monotonic()
    try:
        resp = client.messages.create(
            model=_model(),
            max_tokens=400,
            system=_SYSTEM_PROMPT,
            tools=[_TOOL],
            tool_choice={"type": "tool", "name": "classify_symptom"},
            messages=[{"role": "user", "content": prompt}],
        )
    except Exception as exc:
        elapsed_ms = int((time.monotonic() - started) * 1000)
        logger.warning(
            "symptom_classifier anthropic call failed in %dms token=%s: %s",
            elapsed_ms, token, exc,
        )
        raise SymptomClassifierError(
            f"symptom classifier unavailable: {exc}"
        ) from exc

    elapsed_ms = int((time.monotonic() - started) * 1000)
    usage = getattr(resp, "usage", None)
    in_tokens = getattr(usage, "input_tokens", None) if usage else None
    out_tokens = getattr(usage, "output_tokens", None) if usage else None

    for block in resp.content or []:
        if (
            getattr(block, "type", None) == "tool_use"
            and getattr(block, "name", "") == "classify_symptom"
        ):
            data = dict(block.input or {})
            severity = data.get("severity")
            if severity not in _VALID_SEVERITIES:
                raise SymptomClassifierError(
                    f"symptom classifier returned invalid severity: {severity!r}"
                )
            reasoning = (data.get("reasoning") or "").strip()
            suggested = (data.get("suggested_response") or "").strip()
            if not reasoning or not suggested:
                raise SymptomClassifierError(
                    "symptom classifier returned empty reasoning or suggested_response"
                )
            regression = data.get("regression_exercise_id") or None
            if isinstance(regression, str) and not regression.strip():
                regression = None
            # Discipline: only hold-load may carry a regression suggestion.
            if severity != "hold-load":
                regression = None

            out = {
                "severity": severity,
                "reasoning": reasoning,
                "suggested_response": suggested,
                "regression_exercise_id": regression,
            }
            # PHI hygiene: do NOT log message, reasoning, or suggested_response.
            logger.info(
                "symptom_classifier ok in %dms in_tokens=%s out_tokens=%s "
                "severity=%s has_regression=%s token=%s",
                elapsed_ms, in_tokens, out_tokens,
                severity, bool(regression), token,
            )
            return out

    raise SymptomClassifierError(
        "symptom classifier returned no classify_symptom tool call"
    )
