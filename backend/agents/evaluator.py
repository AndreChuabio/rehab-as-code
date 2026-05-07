"""
evaluator.py - Phase B step 2: progress / hold / regress decision.

The evaluator reads the patient's wearable health metrics, recent
check-ins (pain, RPE, symptom text), recent session quality (pose
metrics, completion rate), and the trend analyst's pattern summary,
then emits a single triage decision: progress, hold, or regress.

It does NOT pick exercises (that is the researcher's job) and does NOT
draft the protocol (that is the planner's job). Splitting the decision
out lets the clinician see exactly which signal flipped a plan -
useful when a draft looks wrong and someone has to figure out which
sub-agent's output was off.

Sonnet 4.6: clinical reasoning over noisy multi-source signals.

No silent fallbacks: any Anthropic / SDK / parsing failure raises
EvaluatorError. The orchestrator catches it and surfaces a 5xx so the
patient gets a clear error rather than a plan built on a guess.
"""
from __future__ import annotations

import json
import logging
import os
import time
from typing import Any, Literal

logger = logging.getLogger(__name__)


_DEFAULT_MODEL = "claude-sonnet-4-6"


class EvaluatorError(RuntimeError):
    """Raised when the evaluator cannot produce a decision."""


Decision = Literal["progress", "hold", "regress"]


_SYSTEM_PROMPT = (
    "You are a rehabilitation evaluator. You receive a patient's wearable "
    "health metrics, recent symptom check-ins, recent session quality "
    "data, and a pre-computed trend pattern. Your job is to emit ONE "
    "triage decision for the next protocol week:\n\n"
    "  progress - patient is tolerating current dose; advance load/volume.\n"
    "  hold     - tolerating but no clear signal to advance; repeat current dose.\n"
    "  regress  - signs of overload, pain trending up, or recovery dropping; "
    "             reduce load/volume.\n\n"
    "Rules of thumb:\n"
    "  - Pain trending up over 2+ check-ins -> regress.\n"
    "  - Recovery score dropping 10+ points week-over-week -> hold or regress.\n"
    "  - Sleep score under 60 for 3+ days -> do not progress.\n"
    "  - Two missed sessions in last 7 days -> hold.\n"
    "  - All metrics stable + completion >= 80% + trend.pattern == 'breakthrough' "
    "    -> progress.\n\n"
    "Cite each reason in the `reasons` array, grounded in the input data. "
    "Confidence is 0-1, calibrated to how clear the signal is. Output only "
    "via the propose_decision tool."
)


_TOOL = {
    "name": "propose_decision",
    "description": "Submit the triage decision for the next protocol week.",
    "input_schema": {
        "type": "object",
        "properties": {
            "decision": {
                "type": "string",
                "enum": ["progress", "hold", "regress"],
            },
            "reasons": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "One short bullet per signal that drove the decision. "
                    "Cite specific numbers (e.g. 'pain 3 -> 5 over 4 days')."
                ),
            },
            "confidence": {
                "type": "number",
                "minimum": 0,
                "maximum": 1,
                "description": (
                    "Calibrated 0-1. <0.5 means the signals conflict; the "
                    "clinician should review more carefully."
                ),
            },
        },
        "required": ["decision", "reasons", "confidence"],
    },
}


def _model() -> str:
    return os.getenv("EVALUATOR_MODEL", _DEFAULT_MODEL)


def _build_user_prompt(
    intake: dict[str, Any] | None,
    health: dict[str, Any] | None,
    history: list[dict[str, Any]] | None,
    trend_summary: dict[str, Any] | None,
) -> str:
    parts: list[str] = []
    if intake:
        parts.append("Patient intake:\n" + json.dumps(intake, indent=2, default=str))
    if health:
        parts.append("Recent wearable metrics:\n" + json.dumps(health, indent=2, default=str))
    if history:
        # history is a mix of checkins and set_completion entries.
        # The model can sort it out from the `kind` field; we don't
        # trim here because clinical context matters.
        parts.append(
            "Recent session history (oldest first):\n"
            + json.dumps(history, indent=2, default=str)
        )
    if trend_summary:
        parts.append(
            "Pre-computed trend analysis:\n"
            + json.dumps(trend_summary, indent=2, default=str)
        )
    if not parts:
        parts.append("No patient data available; default to hold with low confidence.")

    parts.append(
        "Emit the decision via the propose_decision tool. Be concrete in "
        "`reasons`; vague rationales blow the clinician's trust budget."
    )
    return "\n\n".join(parts)


def signal(
    intake: dict[str, Any] | None,
    health: dict[str, Any] | None,
    history: list[dict[str, Any]] | None,
    trend_summary: dict[str, Any] | None = None,
    *,
    token: str | None = None,
) -> dict[str, Any]:
    """Return a triage decision for the next protocol week.

    Parameters
    ----------
    intake : dict | None
        Patient intake snapshot. Provides baseline pain / ROM / surgery
        date the model anchors against.
    health : dict | None
        Recent wearable metrics (HRV, sleep, recovery). Latest snapshot
        is fine; trend is captured separately.
    history : list[dict] | None
        Recent check-ins + set-completions, oldest-first. The same shape
        user_store.get_session_history returns.
    trend_summary : dict | None
        Output from trend_analyst.analyze() if available. Optional - the
        evaluator can produce a decision without it, but accuracy goes
        up materially when supplied.
    token : str | None
        For logging only - the patient's auth.uid().

    Returns
    -------
    dict
        {"decision": "progress" | "hold" | "regress",
         "reasons": [str, ...],
         "confidence": float}

    Raises
    ------
    EvaluatorError
        On Anthropic API failures, missing API key, or malformed model
        output. The orchestrator catches and surfaces a 5xx.
    """
    api_key = os.getenv("ANTHROPIC_API_KEY", "").strip()
    if not api_key:
        raise EvaluatorError("ANTHROPIC_API_KEY is not configured")

    try:
        import anthropic
    except ImportError as exc:
        raise EvaluatorError(f"anthropic SDK not installed: {exc}") from exc

    client = anthropic.Anthropic(api_key=api_key)
    prompt = _build_user_prompt(intake, health, history, trend_summary)

    started = time.monotonic()
    try:
        resp = client.messages.create(
            model=_model(),
            max_tokens=600,
            system=_SYSTEM_PROMPT,
            tools=[_TOOL],
            tool_choice={"type": "tool", "name": "propose_decision"},
            messages=[{"role": "user", "content": prompt}],
        )
    except Exception as exc:
        elapsed_ms = int((time.monotonic() - started) * 1000)
        logger.warning(
            "evaluator anthropic call failed in %dms token=%s: %s",
            elapsed_ms, token, exc,
        )
        raise EvaluatorError(f"evaluator unavailable: {exc}") from exc

    elapsed_ms = int((time.monotonic() - started) * 1000)
    usage = getattr(resp, "usage", None)
    in_tokens = getattr(usage, "input_tokens", None) if usage else None
    out_tokens = getattr(usage, "output_tokens", None) if usage else None

    for block in resp.content or []:
        if (
            getattr(block, "type", None) == "tool_use"
            and getattr(block, "name", "") == "propose_decision"
        ):
            data = dict(block.input or {})
            decision = data.get("decision")
            if decision not in ("progress", "hold", "regress"):
                raise EvaluatorError(
                    f"evaluator returned invalid decision: {decision!r}"
                )
            out = {
                "decision": decision,
                "reasons": list(data.get("reasons") or []),
                "confidence": float(data.get("confidence") or 0.0),
            }
            logger.info(
                "evaluator ok in %dms in_tokens=%s out_tokens=%s "
                "decision=%s confidence=%.2f token=%s",
                elapsed_ms, in_tokens, out_tokens,
                out["decision"], out["confidence"], token,
            )
            return out

    raise EvaluatorError("evaluator returned no propose_decision tool call")
