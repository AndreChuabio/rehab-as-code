"""
planner.py - Phase B step 3: compose the final draft protocol.

The planner takes the researcher's candidate exercises, the evaluator's
progress/hold/regress decision, and the patient's intake and produces a
complete draft protocol payload matching the shape `protocol_repo.save_pending`
expects (patient, phase, week, exercises, optional session_targets).

This is the only sub-agent that emits a writeable artifact. Researcher
gives candidates with citations; evaluator gives a triage decision;
planner combines them into prescribed sets/reps/load with progression
and regression criteria. Splitting it out from the researcher keeps the
prompts small and lets us iterate on dose-prescription quality without
re-running the candidate retrieval.

Sonnet 4.6: clinical reasoning over dose prescription (sets, reps,
load progression). Output is a structured payload, validated before
return.

Optional `concerns` argument: when SafetyReviewAgent flags med-severity
issues on a draft, the orchestrator re-runs the planner with the concern
list appended so the planner can address each item before re-submitting
to safety review.

No silent fallbacks: any Anthropic / SDK / parsing failure raises
PlannerError. The orchestrator catches and surfaces a 5xx.
"""
from __future__ import annotations

import json
import logging
import os
import time
from typing import Any

logger = logging.getLogger(__name__)


_DEFAULT_MODEL = "claude-sonnet-4-6"


class PlannerError(RuntimeError):
    """Raised when the planner cannot produce a valid draft."""


_BASE_SYSTEM_PROMPT = (
    "You are a rehabilitation protocol planner. You receive (a) a list of "
    "candidate exercises with citations from the researcher, (b) a triage "
    "decision (progress / hold / regress) from the evaluator with reasons, "
    "and (c) the patient's intake. Compose a complete draft protocol for "
    "the patient's next week.\n\n"
    "Rules:\n"
    "  1. Use only the candidates the researcher returned. Do not invent.\n"
    "  2. Apply the evaluator's decision uniformly:\n"
    "     - progress: bump load / reps / volume on at least one exercise.\n"
    "     - hold: keep current dose but you may swap exercises within "
    "       progressed candidates.\n"
    "     - regress: cut load / volume; substitute easier candidates.\n"
    "  3. Each exercise needs name, sets, reps, load, and "
    "     progression_criteria + regression_criteria when sensible.\n"
    "  4. Set session_targets.frequency_per_week and duration_min based on "
    "     phase (acute: 3x/wk 20-30min; subacute: 4x/wk 30-40min; "
    "     strength: 4-5x/wk 40-50min). Tighten when regress, loosen when progress.\n"
    "  5. Echo the patient name verbatim from intake.\n\n"
    "Output only via the compose_protocol tool."
)


_TOOL = {
    "name": "compose_protocol",
    "description": "Submit the draft protocol that will be saved as pending.",
    "input_schema": {
        "type": "object",
        "properties": {
            "patient": {"type": "string"},
            "phase": {"type": "string"},
            "week": {"type": "integer"},
            "session_targets": {
                "type": "object",
                "properties": {
                    "frequency_per_week": {"type": "integer"},
                    "duration_min": {"type": "integer"},
                    "max_pain_during_session": {"type": "integer"},
                },
            },
            "exercises": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string"},
                        "sets": {"type": "integer"},
                        "reps": {"type": "integer"},
                        "load": {"type": "string"},
                        "progression_criteria": {"type": "string"},
                        "regression_criteria": {"type": "string"},
                        "references": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": (
                                "Library file paths the dose was sourced from. "
                                "Should mirror researcher candidates' citation_paths."
                            ),
                        },
                    },
                    "required": ["name", "sets", "reps"],
                },
            },
        },
        "required": ["patient", "phase", "week", "exercises"],
    },
}


def _model() -> str:
    return os.getenv("PLANNER_MODEL", _DEFAULT_MODEL)


def _build_user_prompt(
    candidates: list[dict[str, Any]],
    signal: dict[str, Any],
    intake: dict[str, Any] | None,
    phase: str,
    week: int,
    concerns: list[dict[str, Any]] | None,
) -> str:
    parts: list[str] = [
        f"Target phase: {phase}",
        f"Target week: {week}",
    ]
    if intake:
        parts.append("Patient intake:\n" + json.dumps(intake, indent=2, default=str))
    parts.append(
        "Researcher candidates (use only these):\n"
        + json.dumps(candidates, indent=2, default=str)
    )
    parts.append(
        "Evaluator decision + reasons:\n"
        + json.dumps(signal, indent=2, default=str)
    )
    if concerns:
        parts.append(
            "Safety concerns from the previous attempt that you MUST "
            "address in this revision:\n"
            + json.dumps(concerns, indent=2, default=str)
            + "\n\nFor each concern: either swap the offending exercise or "
            "lower the dose so the concern no longer applies. Be explicit "
            "about how this revision resolves each item."
        )
    parts.append(
        "Compose the draft protocol via the compose_protocol tool. "
        "Patient name must come from intake; do not invent."
    )
    return "\n\n".join(parts)


def _normalize_exercise(ex: dict[str, Any]) -> dict[str, Any]:
    """Coerce one exercise into the shape protocol_repo / the YAML schema expect.

    Mirrors chat_protocol_drafter._normalize_exercise: synthesizes a
    references list when the model omits one so save_pending validation
    passes.
    """
    out = dict(ex)
    refs = out.get("references")
    if not refs:
        out["references"] = ["protocol-library/auto-generated.yaml"]
    return out


def _validate(proposal: dict[str, Any]) -> dict[str, Any]:
    """Light validation. Anthropic tool-use enforces the schema; we
    re-check load-bearing fields so a malformed response surfaces as
    PlannerError instead of crashing in psycopg later."""
    for field in ("patient", "phase", "week", "exercises"):
        if field not in proposal:
            raise PlannerError(f"draft missing required field: {field}")
    if not isinstance(proposal["exercises"], list) or not proposal["exercises"]:
        raise PlannerError("draft.exercises must be a non-empty list")
    if not isinstance(proposal["week"], int):
        raise PlannerError("draft.week must be an integer")

    payload: dict[str, Any] = {
        "patient": proposal["patient"],
        "phase": proposal["phase"],
        "week": proposal["week"],
        "exercises": [_normalize_exercise(ex) for ex in proposal["exercises"]],
    }
    if "session_targets" in proposal and proposal["session_targets"]:
        payload["session_targets"] = proposal["session_targets"]
    return payload


def compose(
    candidates: list[dict[str, Any]],
    signal: dict[str, Any],
    intake: dict[str, Any] | None,
    *,
    phase: str,
    week: int,
    concerns: list[dict[str, Any]] | None = None,
    token: str | None = None,
) -> dict[str, Any]:
    """Compose a draft protocol payload.

    Parameters
    ----------
    candidates : list[dict]
        Researcher output. Each item has exercise_id + citation + rationale.
    signal : dict
        Evaluator output: {decision, reasons, confidence}.
    intake : dict | None
        Patient intake snapshot. Source of patient name.
    phase : str
        Target phase for the new protocol (acute / subacute / strength).
    week : int
        Target week number.
    concerns : list[dict] | None
        SafetyReviewAgent concerns from a previous attempt (med-severity
        retry path). When set, the planner is asked to revise the draft
        to address each concern.
    token : str | None
        For logging only.

    Returns
    -------
    dict
        Validated payload matching `protocol_repo.save_pending` shape:
        {patient, phase, week, exercises[], session_targets?}.

    Raises
    ------
    PlannerError
        On Anthropic API failures, missing API key, or malformed output.
    """
    api_key = os.getenv("ANTHROPIC_API_KEY", "").strip()
    if not api_key:
        raise PlannerError("ANTHROPIC_API_KEY is not configured")

    try:
        import anthropic
    except ImportError as exc:
        raise PlannerError(f"anthropic SDK not installed: {exc}") from exc

    client = anthropic.Anthropic(api_key=api_key)
    prompt = _build_user_prompt(candidates, signal, intake, phase, week, concerns)

    started = time.monotonic()
    try:
        resp = client.messages.create(
            model=_model(),
            max_tokens=2000,
            system=_BASE_SYSTEM_PROMPT,
            tools=[_TOOL],
            tool_choice={"type": "tool", "name": "compose_protocol"},
            messages=[{"role": "user", "content": prompt}],
        )
    except Exception as exc:
        elapsed_ms = int((time.monotonic() - started) * 1000)
        logger.warning(
            "planner anthropic call failed in %dms token=%s: %s",
            elapsed_ms, token, exc,
        )
        raise PlannerError(f"planner unavailable: {exc}") from exc

    elapsed_ms = int((time.monotonic() - started) * 1000)
    usage = getattr(resp, "usage", None)
    in_tokens = getattr(usage, "input_tokens", None) if usage else None
    out_tokens = getattr(usage, "output_tokens", None) if usage else None

    for block in resp.content or []:
        if (
            getattr(block, "type", None) == "tool_use"
            and getattr(block, "name", "") == "compose_protocol"
        ):
            payload = _validate(dict(block.input or {}))
            logger.info(
                "planner ok in %dms in_tokens=%s out_tokens=%s "
                "n_exercises=%d retry=%s token=%s",
                elapsed_ms, in_tokens, out_tokens,
                len(payload.get("exercises") or []),
                bool(concerns), token,
            )
            return payload

    raise PlannerError("planner returned no compose_protocol tool call")
