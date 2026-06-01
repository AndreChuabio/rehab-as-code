"""
safety_reviewer.py - Phase D: post-planner safety gate.

Runs AFTER planner.compose() produces a draft and BEFORE
protocol_repo.save_pending() persists it. The orchestrator branches on
the agent's overall_severity to decide whether to:

  ok        -> save as pending_review (normal queue).
  med       -> re-call planner.compose(..., concerns=...) up to 2 times.
               If still med, save as pending_review with safety_concerns
               attached so the clinician sees what the agent flagged.
  high      -> save as needs_clinician_review with safety_concerns
               attached. Surfaces top of clinician queue with red banner.

Branching is deterministic in the orchestrator - NOT in this prompt.
The agent's job is to evaluate each safety check and emit a structured
verdict. The orchestrator decides what to do with that verdict.

Sonnet 4.6: clinical reasoning over each safety check.

No silent fallbacks: any Anthropic / SDK / parsing failure raises
SafetyReviewError. The orchestrator catches and surfaces a 5xx so a
broken safety gate fails closed - we'd rather block a draft from
saving than let an unreviewed protocol slip through.
"""
from __future__ import annotations

import json
import logging
import os
import time
from typing import Any, Literal

logger = logging.getLogger(__name__)


_DEFAULT_MODEL = "claude-sonnet-4-6"


class SafetyReviewError(RuntimeError):
    """Raised when the safety reviewer cannot produce a verdict."""


Severity = Literal["low", "med", "high"]


_SYSTEM_PROMPT = (
    "You are a rehabilitation safety reviewer. You receive a draft "
    "protocol, the patient's intake, and the trend pattern from the "
    "trend analyst. Evaluate the draft against the following safety "
    "checks and emit a verdict for each:\n\n"
    "  1. pain_ceiling: max pain ceiling appropriate for the phase. "
    "     (post-op week 1-4: pain <= 3/10 for any prescribed exercise; "
    "      week 5-8: pain <= 4/10; week 9+: pain <= 5/10)\n"
    "  2. contraindication: no exercises contraindicated for the surgery "
    "     date or phase (e.g., closed-chain loading week 1 post-ACL; "
    "     overhead pressing on a torn rotator cuff).\n"
    "  3. frequency_limit: frequency_per_week within evidence-based bounds "
    "     (loaded strength <= 5x/wk; pain-controlled mobility can be daily).\n"
    "  4. hold_rule: respect the trend pattern. If trend.pattern == "
    "     'regression' the draft must NOT progress load/volume.\n"
    "  5. intake_consistency: no exercises directly aggravating the "
    "     reported injury (no overhead pressing on a shoulder injury; "
    "     no deep squats post-knee surgery week 1; etc.).\n\n"
    "For each check, decide ok or concern. When concern, set severity:\n"
    "  low  - cosmetic / clinician should review but not blocking.\n"
    "  med  - draft should be revised; clinician sees concerns inline.\n"
    "  high - draft is unsafe; needs explicit clinician sign-off.\n\n"
    "overall_severity is the highest individual severity across concerns. "
    "If no concerns: ok=true, overall_severity='low'. Output only via the "
    "submit_verdict tool."
)


_TOOL = {
    "name": "submit_verdict",
    "description": "Submit the safety review verdict for the draft protocol.",
    "input_schema": {
        "type": "object",
        "properties": {
            "ok": {"type": "boolean"},
            "concerns": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "check": {
                            "type": "string",
                            "description": (
                                "Which check fired: pain_ceiling, "
                                "contraindication, frequency_limit, "
                                "hold_rule, intake_consistency."
                            ),
                        },
                        "severity": {
                            "type": "string",
                            "enum": ["low", "med", "high"],
                        },
                        "detail": {
                            "type": "string",
                            "description": (
                                "Specific exercise / dose / phase that "
                                "tripped the check, plus what the clinician "
                                "needs to see to decide."
                            ),
                        },
                    },
                    "required": ["check", "severity", "detail"],
                },
            },
            "overall_severity": {
                "type": "string",
                "enum": ["low", "med", "high"],
            },
        },
        "required": ["ok", "concerns", "overall_severity"],
    },
}


def _model() -> str:
    return os.getenv("SAFETY_REVIEWER_MODEL", _DEFAULT_MODEL)


def _build_user_prompt(
    draft: dict[str, Any],
    intake: dict[str, Any] | None,
    trend_summary: dict[str, Any] | None,
) -> str:
    parts: list[str] = []
    parts.append("Draft protocol under review:\n" + json.dumps(draft, indent=2, default=str))
    if intake:
        parts.append("Patient intake:\n" + json.dumps(intake, indent=2, default=str))
    if trend_summary:
        parts.append("Trend analysis:\n" + json.dumps(trend_summary, indent=2, default=str))
    parts.append(
        "Evaluate each of the five safety checks. Submit the verdict via "
        "submit_verdict. Be specific in `detail` - vague flags waste the "
        "clinician's review budget."
    )
    return "\n\n".join(parts)


def _summarize_review(result: dict[str, Any]) -> dict[str, Any]:
    """PHI-safe summary: severity + rule codes only. Never the patient
    narrative; safety_concerns can echo back symptom descriptions."""
    if not isinstance(result, dict):
        return {"_unknown_shape": str(type(result))}
    concerns = result.get("safety_concerns", []) or []
    return {
        "verdict": result.get("verdict"),
        "n_concerns": len(concerns),
        "severities": [c.get("severity") for c in concerns if isinstance(c, dict)][:6],
        "rules": [c.get("rule") for c in concerns if isinstance(c, dict) and c.get("rule")][:6],
    }


def _decision_from_review(result: dict[str, Any]) -> str | None:
    return (result or {}).get("verdict") if isinstance(result, dict) else None


_SEVERITY_RANK = {"low": 1, "med": 2, "high": 3}


def _derive_overall_severity(concerns: list[Any]) -> str:
    """overall_severity is, by definition, the highest concern severity.

    Anthropic tool-use does not enforce `required`, so the model sometimes
    calls submit_verdict and omits overall_severity (commonly a no-concern
    first protocol where it set ok=true but left the field null). Rather than
    502 the whole plan run, derive it from the concerns the model DID return:
    no concerns -> 'low'; otherwise the worst concern severity. An
    unrecognized concern severity counts as 'med' (conservative — it won't
    auto-progress). Only used when overall_severity is absent/blank; a value
    that is present but not in the enum (e.g. 'critical') still fails closed.
    """
    worst = 0
    for c in concerns:
        sev = c.get("severity") if isinstance(c, dict) else None
        worst = max(worst, _SEVERITY_RANK.get(sev, 2))
    return {0: "low", 1: "low", 2: "med", 3: "high"}[worst]


from observability import trace_sync


@trace_sync(
    "safety_reviewer",
    model="claude-sonnet-4-6",
    summarize=_summarize_review,
    decision_from=_decision_from_review,
)
def review(
    draft: dict[str, Any],
    intake: dict[str, Any] | None,
    trend_summary: dict[str, Any] | None = None,
    *,
    token: str | None = None,
) -> dict[str, Any]:
    """Run the safety review on a draft protocol.

    Parameters
    ----------
    draft : dict
        The protocol payload from planner.compose() (patient, phase,
        week, exercises, optional session_targets).
    intake : dict | None
        Patient intake snapshot - injury, surgery date, baseline.
    trend_summary : dict | None
        Trend analyst output if available. Used to enforce hold_rule
        when pattern == 'regression'.
    token : str | None
        For logging only.

    Returns
    -------
    dict
        {
          "ok": bool,
          "concerns": [{"check": str, "severity": "low"|"med"|"high",
                        "detail": str}, ...],
          "overall_severity": "low"|"med"|"high"
        }

    Raises
    ------
    SafetyReviewError
        On Anthropic API failures, missing API key, or malformed output.
        We fail closed - no silent OK on a broken safety gate.
    """
    api_key = os.getenv("ANTHROPIC_API_KEY", "").strip()
    if not api_key:
        raise SafetyReviewError("ANTHROPIC_API_KEY is not configured")

    try:
        import anthropic
    except ImportError as exc:
        raise SafetyReviewError(f"anthropic SDK not installed: {exc}") from exc

    client = anthropic.Anthropic(api_key=api_key)
    prompt = _build_user_prompt(draft, intake, trend_summary)

    started = time.monotonic()
    try:
        resp = client.messages.create(
            model=_model(),
            max_tokens=900,
            system=_SYSTEM_PROMPT,
            tools=[_TOOL],
            tool_choice={"type": "tool", "name": "submit_verdict"},
            messages=[{"role": "user", "content": prompt}],
        )
    except Exception as exc:
        elapsed_ms = int((time.monotonic() - started) * 1000)
        logger.warning(
            "safety_reviewer anthropic call failed in %dms token=%s: %s",
            elapsed_ms, token, exc,
        )
        raise SafetyReviewError(f"safety reviewer unavailable: {exc}") from exc

    elapsed_ms = int((time.monotonic() - started) * 1000)
    usage = getattr(resp, "usage", None)
    in_tokens = getattr(usage, "input_tokens", None) if usage else None
    out_tokens = getattr(usage, "output_tokens", None) if usage else None

    for block in resp.content or []:
        if (
            getattr(block, "type", None) == "tool_use"
            and getattr(block, "name", "") == "submit_verdict"
        ):
            data = dict(block.input or {})
            concerns = list(data.get("concerns") or [])
            severity = data.get("overall_severity")
            if severity is None or (isinstance(severity, str) and not severity.strip()):
                # Model called submit_verdict but omitted the summary field
                # (tool-use does not enforce `required`). Derive it from the
                # concerns it returned instead of 502-ing the whole plan run.
                derived = _derive_overall_severity(concerns)
                logger.warning(
                    "safety_reviewer overall_severity missing (got %r); "
                    "derived %s from %d concerns token=%s",
                    severity, derived, len(concerns), token,
                )
                severity = derived
            elif severity not in ("low", "med", "high"):
                # Present but not a recognized value -> malformed; fail closed.
                raise SafetyReviewError(
                    f"safety reviewer returned invalid overall_severity: {severity!r}"
                )
            # Reconcile: if concerns is empty, ok must be True.
            ok = bool(data.get("ok"))
            if not concerns and not ok:
                # Trust the absence of concerns over the model's `ok` flag.
                ok = True
            out = {
                "ok": ok,
                "concerns": concerns,
                "overall_severity": severity,
            }
            logger.info(
                "safety_reviewer ok in %dms in_tokens=%s out_tokens=%s "
                "verdict_ok=%s overall_severity=%s n_concerns=%d token=%s",
                elapsed_ms, in_tokens, out_tokens,
                out["ok"], out["overall_severity"],
                len(concerns), token,
            )
            return out

    raise SafetyReviewError("safety reviewer returned no submit_verdict tool call")
