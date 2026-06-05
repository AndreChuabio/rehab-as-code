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
import sys
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


_DEFAULT_MODEL = "claude-sonnet-4-6"


class PlannerError(RuntimeError):
    """Raised when the planner cannot produce a valid draft."""


_BASE_SYSTEM_PROMPT = (
    "You are a rehabilitation protocol planner. You receive (a) a list of "
    "candidate exercises with citations from the researcher, (b) a triage "
    "decision (progress / hold / regress) from the evaluator with reasons, "
    "(c) the patient's intake, and (d) the patient's body_region.\n\n"
    "INJURY ANCHORING (load-bearing for clinical safety): every exercise "
    "you compose MUST target the patient's body_region. Candidates from "
    "the researcher are already region-filtered; do not introduce any "
    "exercise outside that list. If the candidate list is empty, refuse: "
    "emit a single exercise named `clinician_review_required` with sets=0, "
    "reps=0 and explain why in the patient name field's adjacent context. "
    "Never fabricate cross-region exercises (no knee work for an ankle "
    "patient, etc.) even if you know they exist.\n\n"
    "GUIDED FORM-CHECK PREFERENCE: when two candidate exercises serve the "
    "same therapeutic purpose, prefer the one with `form_check_supported`="
    "true. Only prescribe exercises with `form_check_supported`=false when "
    "no equivalent guided alternative exists in the candidate list. This "
    "affects patient adherence — exercises without form-check fall back to "
    "mark-done-yourself, which has lower compliance.\n\n"
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
    "  5. Echo the patient name verbatim from intake.\n"
    "  6. Emit 2-4 payer-aware rehab goals (see GOAL-WRITING MODE below).\n\n"
    "GOAL-WRITING MODE (payer-aware — the patient's payer_model is given in "
    "the user message; goal language is payer-model-dependent and getting it "
    "wrong gets the goal DENIED or reads as nonsense):\n"
    "  NEVER mix modes. A cash goal written in insurance/medical-necessity "
    "language — or an insurance goal written as a personal-training goal — is "
    "a billing error, not a style choice.\n"
    "    - insurance / medicare: every goal MUST be measurable, time-bound, "
    "and tied to a functional ADL or fall-risk / medical-necessity rationale "
    "(independent ambulation, ADL performance, fall-risk reduction, return to "
    "prior level of function). measurable_target is a concrete number "
    "(degrees, reps, distance, seconds); tied_to is `adl` or `fall_risk`.\n"
    "    - cash / concierge: goals are load-management / performance goals in "
    "personal-training framing (mileage, load, pain ceiling during activity). "
    "measurable_target is the performance milestone; tied_to is `performance` "
    "or `load_mgmt`.\n"
    "  Base each goal on the patient's stated intake goals, reframed into the "
    "active mode. Set payer_mode to the patient's payer_model. Cite at least "
    "one exercise reference path per goal.\n\n"
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
            "goals": {
                "type": "array",
                "description": (
                    "2-4 payer-aware rehab goals. Language MUST match the "
                    "patient's payer_model: insurance/medicare = measurable "
                    "medical-necessity/ADL/fall-risk; cash = load-management/"
                    "performance. See GOAL-WRITING MODE in the system prompt."
                ),
                "items": {
                    "type": "object",
                    "properties": {
                        "text": {"type": "string"},
                        "measurable_target": {
                            "type": "string",
                            "description": (
                                "Concrete number the goal is judged against "
                                "(degrees, reps, distance, seconds, mileage)."
                            ),
                        },
                        "tied_to": {
                            "type": "string",
                            "enum": ["adl", "fall_risk", "performance", "load_mgmt"],
                        },
                        "payer_mode": {
                            "type": "string",
                            "enum": ["insurance", "medicare", "cash"],
                        },
                        "references": {
                            "type": "array",
                            "items": {"type": "string"},
                        },
                    },
                    "required": ["text", "tied_to", "payer_mode"],
                },
            },
        },
        "required": ["patient", "phase", "week", "exercises"],
    },
}


# tied_to values that are valid for each payer mode. Insurance/Medicare goals
# are anchored to functional/medical-necessity buckets; cash goals to
# performance/load-management. Enforced deterministically in _normalize_goals
# so the LLM cannot conflate the two modes (the load-bearing guardrail).
_GOAL_TIED_TO_BY_MODE: dict[str, tuple[str, ...]] = {
    "insurance": ("adl", "fall_risk"),
    "medicare": ("adl", "fall_risk"),
    "cash": ("performance", "load_mgmt"),
}

_VALID_PAYER_MODELS = ("insurance", "medicare", "cash")
_DEFAULT_PAYER_MODEL = "cash"

# Lexical lint: vocabulary that signals a goal TEXT is in the wrong payer
# register even when its structural fields (tied_to / payer_mode) are correct.
# A soft clinician-facing flag — never an auto-rewrite, never authoritative.
# Whether a goal reads in the correct register is the clinician's call (Kendell);
# code only surfaces the suspicion so it isn't silently lent false confidence.
_PERFORMANCE_VOCAB = ("mile", "mileage", "pace", "1rm", " pr ", "personal record", " load ")
_NECESSITY_VOCAB = ("adl", "fall", "ambulat", "prior level of function", "activities of daily")


def _text_register_mismatch(text: str, payer_model: str) -> bool:
    """True when goal text uses vocabulary of the OTHER payer register.

    insurance/medicare goal carrying performance vocab (mileage, 1RM), or a
    cash goal carrying medical-necessity vocab (ADL, fall, ambulation). Soft
    signal only.
    """
    t = f" {text.lower()} "
    if payer_model in ("insurance", "medicare"):
        return any(k in t for k in _PERFORMANCE_VOCAB)
    return any(k in t for k in _NECESSITY_VOCAB)


def _model() -> str:
    return os.getenv("PLANNER_MODEL", _DEFAULT_MODEL)


def _build_user_prompt(
    candidates: list[dict[str, Any]],
    signal: dict[str, Any],
    intake: dict[str, Any] | None,
    phase: str,
    week: int,
    concerns: list[dict[str, Any]] | None,
    body_region: str | None = None,
    payer_model: str = _DEFAULT_PAYER_MODEL,
) -> str:
    parts: list[str] = [
        f"Target phase: {phase}",
        f"Target week: {week}",
        f"Body region (HARD constraint - all exercises must target this): "
        f"{body_region or 'unspecified'}",
        f"Payer model (HARD constraint - drives goal language, do not mix "
        f"modes): {payer_model}",
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
    # Carry the raw goals through; compose() normalizes + enforces payer-mode
    # consistency once the resolved payer_model is in scope.
    if proposal.get("goals"):
        payload["goals"] = proposal["goals"]
    return payload


def _normalize_goals(
    goals: Any,
    payer_model: str,
    *,
    token: str | None = None,
) -> list[dict[str, Any]]:
    """Coerce planner goals into the stored shape + enforce payer-mode consistency.

    Deterministic guard against the mode-conflation BLOCKER: `payer_mode` is
    overwritten with the resolved payer_model (the LLM's self-report is not
    trusted), and `tied_to` is constrained to the bucket set valid for that
    mode. A goal whose tied_to is out-of-mode is coerced to the mode's first
    bucket and logged — it signals the planner drifted toward the wrong payer
    framing, which the clinician should eyeball at review time.

    The goal *text* language correctness is what the clinician verifies; we
    can only deterministically enforce the structural fields here.
    """
    if not isinstance(goals, list):
        return []
    allowed = _GOAL_TIED_TO_BY_MODE.get(
        payer_model, _GOAL_TIED_TO_BY_MODE[_DEFAULT_PAYER_MODEL]
    )
    out: list[dict[str, Any]] = []
    for g in goals:
        if not isinstance(g, dict):
            continue
        text = str(g.get("text") or "").strip()
        if not text:
            continue
        tied = str(g.get("tied_to") or "").strip().lower()
        tied_coerced = tied not in allowed
        if tied_coerced:
            logger.warning(
                "planner goal tied_to=%r out of payer_mode=%s — coercing token=%s",
                tied, payer_model, token,
            )
            tied = allowed[0]
        refs = g.get("references")
        citation_missing = not (isinstance(refs, list) and refs)
        refs_out = [str(r) for r in refs] if not citation_missing else []
        goal: dict[str, Any] = {
            "text": text,
            "measurable_target": str(g.get("measurable_target") or "").strip(),
            "tied_to": tied,
            "payer_mode": payer_model,  # deterministic truth, not the LLM claim
            "references": refs_out,
        }
        # Surface the soft signals as clinician-review flags rather than burying
        # them in server logs — a coerced anchor or a wrong-register text claims
        # validity it doesn't have, which is worse than no guard if hidden.
        if tied_coerced:
            goal["tied_to_coerced"] = True
            goal["needs_clinician_review"] = True
        if citation_missing:
            # Do NOT fabricate a citation path — flag the gap so it's visible.
            goal["citation_missing"] = True
        if _text_register_mismatch(text, payer_model):
            goal["text_register_warning"] = True
            goal["needs_clinician_review"] = True
        out.append(goal)
    return out


def _summarize_protocol(result: dict[str, Any]) -> dict[str, Any]:
    """PHI-safe summary of the planner's draft payload. Counts + structural
    facts only — the full YAML lives in protocols.payload, no need to
    duplicate."""
    if not isinstance(result, dict):
        return {"_unknown_shape": str(type(result))}
    exercises = result.get("exercises", []) or []
    goals = result.get("goals", []) or []
    return {
        "phase": result.get("phase"),
        "week": result.get("week"),
        "body_region": result.get("body_region"),
        "n_exercises": len(exercises),
        "n_goals": len(goals),
        "exercise_ids": [str(e.get("id", ""))[:80] for e in exercises[:12]],
    }


from observability import trace_sync


@trace_sync(
    "planner",
    model="claude-sonnet-4-6",
    summarize=_summarize_protocol,
)
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

    # Resolve body_region for prompt anchoring + post-LLM validation.
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
        import clinical_taxonomy as _ct
        injury_type = (intake or {}).get("injury_type") if intake else None
        resolved_region = _ct.resolve_body_region(injury_type)
    except Exception as exc:
        logger.warning("planner: body_region resolve failed: %s", exc)
        resolved_region = None

    # Payer model drives goal language. Canonical source is intake (set by the
    # clinician, defaults to cash — the insurance-lapse-bridge GTM is cash-pay
    # first). Never denormalized onto the protocol payload; resolve it here.
    payer_model = str((intake or {}).get("payer_model") or "").strip().lower()
    if payer_model not in _VALID_PAYER_MODELS:
        payer_model = _DEFAULT_PAYER_MODEL

    client = anthropic.Anthropic(api_key=api_key)
    prompt = _build_user_prompt(
        candidates, signal, intake, phase, week, concerns,
        body_region=resolved_region,
        payer_model=payer_model,
    )

    started = time.monotonic()
    try:
        resp = client.messages.create(
            model=_model(),
            max_tokens=2600,
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
            _validate_region(payload, resolved_region, token=token)
            # Normalize goals + enforce payer-mode consistency now that the
            # resolved payer_model is in scope. Drop the key if no goals so
            # downstream readers can treat absence uniformly.
            normalized_goals = _normalize_goals(
                payload.get("goals"), payer_model, token=token
            )
            if normalized_goals:
                payload["goals"] = normalized_goals
            else:
                payload.pop("goals", None)
                if payer_model in ("insurance", "medicare"):
                    # An insurance/medicare protocol with zero goals undercuts
                    # medical-necessity documentation — flag for a clinician.
                    logger.warning(
                        "planner produced zero goals for payer=%s token=%s — "
                        "medical-necessity documentation will be incomplete",
                        payer_model, token,
                    )
            logger.info(
                "planner ok in %dms in_tokens=%s out_tokens=%s "
                "n_exercises=%d n_goals=%d payer=%s retry=%s body_region=%s token=%s",
                elapsed_ms, in_tokens, out_tokens,
                len(payload.get("exercises") or []),
                len(payload.get("goals") or []),
                payer_model, bool(concerns), resolved_region, token,
            )
            return payload

    raise PlannerError("planner returned no compose_protocol tool call")


# Sentinel name the planner is instructed to emit when no candidate fits.
# The validator allows this through so the orchestrator can save the empty
# refusal draft for clinician review.
_REFUSAL_EXERCISE_NAME = "clinician_review_required"


def _validate_region(
    payload: dict[str, Any],
    expected_region: str | None,
    *,
    token: str | None = None,
) -> None:
    """Deterministic post-LLM safety net mirroring chat_protocol_drafter.

    Walks every exercise and confirms its body_region (looked up via
    exercise_kb) matches expected_region. Any mismatch raises PlannerError;
    the orchestrator propagates it as PlanGenerationError so the patient
    sees a "plan generation failed - try again" toast and the clinician
    queue is not polluted with cross-region drafts.

    Skips enforcement when expected_region is None (couldn't resolve from
    intake) or "multi" (legitimately multi-region).
    """
    if not expected_region or expected_region == "multi":
        return

    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
        import exercise_kb as _kb
    except Exception as exc:
        logger.warning("planner: exercise_kb import failed: %s", exc)
        return

    mismatches: list[dict[str, Any]] = []
    for ex in payload.get("exercises") or []:
        ex_id = ex.get("id") or ex.get("name") or ""
        if ex_id == _REFUSAL_EXERCISE_NAME:
            continue
        ex_region = _kb.body_region_for(ex_id)
        if ex_region is None or ex_region == "multi":
            continue
        if ex_region != expected_region:
            mismatches.append({
                "exercise": ex_id,
                "exercise_region": ex_region,
                "patient_region": expected_region,
            })

    if mismatches:
        logger.warning(
            "planner region mismatch token=%s patient_region=%s n_mismatches=%d",
            token, expected_region, len(mismatches),
        )
        raise PlannerError(
            "Planner proposed exercises outside the patient's body region "
            f"({expected_region}). Mismatched: "
            + ", ".join(
                f"{m['exercise']} ({m['exercise_region']})"
                for m in mismatches
            )
        )
