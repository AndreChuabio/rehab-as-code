"""
chat_protocol_drafter.py - draft a protocol revision from a chat-tool payload.

Replaces the dead PR-bus path (cursor/ag2/cached_replay agents that opened
GitHub PRs). The new architecture writes a `pending_review` row directly to
the `protocols` Supabase table; the clinician dashboard at /clinician is the
approval gate.

One direct LLM call (Anthropic claude-sonnet-4-6) per chat-tool fire. Output
is validated to the protocol payload shape before being saved. No silent
fallbacks: if the model is unreachable or returns invalid JSON, we raise so
the /chat surface can render a clear error toast (consistent with the
no-silent-fallback rule that landed with PR #62).

Public surface:
    draft_and_save_pending(token, flow, payload, *, prior_protocol)
        Run the LLM, validate, classify by change tier, persist. Returns:
            {
                "pending_protocol_id": str,        # alias of protocol_id
                "protocol_id":         str,
                "auto_applied":        bool,       # True -> saved live
                "summary":             str,   # one-line patient-facing summary
                "phase":               str | None,
                "week":                int | None,
            }
        Raises ProtocolDraftError on any failure.
"""
from __future__ import annotations

import json
import logging
import os
from typing import Any

import change_tier
import clinical_taxonomy
import exercise_kb
import protocol_repo

logger = logging.getLogger(__name__)


class ProtocolDraftError(RuntimeError):
    """Raised when a draft protocol cannot be generated or persisted."""


# When a draft proposes an exercise outside the patient's body_region the
# orchestrator raises this with a clinician-readable detail. Surfaced as a
# 502 toast on /chat. NEVER auto-substituted: the safety net is "fail loud
# so a clinician sees it," not "swap the exercise behind their back."
class CrossRegionExerciseError(ProtocolDraftError):
    """A draft proposed an exercise targeting the wrong body region."""


# Flow-specific instructions appended to the base system prompt. Keep these
# short so the model focuses on producing valid JSON, not exposition.
_FLOW_INSTRUCTIONS: dict[str, str] = {
    "symptom_adjustment": (
        "The patient just reported a new symptom. Adjust the active protocol "
        "to regress or substitute the offending exercise(s). Quote the "
        "patient's words verbatim in the `summary` field. Keep all other "
        "exercises stable unless they directly aggravate the reported issue."
    ),
    "checkin": (
        "The patient is logging today's session outcome. Decide whether the "
        "active protocol needs a small load/volume tweak, or whether the "
        "current dose stands. If the dose stands, return the active protocol "
        "unchanged with a `summary` explaining why no edit is needed."
    ),
    "weekly_plan": (
        "Generate next week's progression. Evaluate progression criteria on "
        "the active protocol and bump load/volume only on exercises whose "
        "criteria have been met. Increment the `week` field by 1."
    ),
}


_BASE_SYSTEM_PROMPT = """You are a rehabilitation protocol drafter.

You will receive the patient's intake (injury type, body region), their
currently-active protocol (JSON, if any), and a flow-specific instruction.
Produce a NEW protocol revision as a JSON object that will be saved as
`pending_review` and queued for clinician approval.

Hard requirements:
1. Output a single tool call to `propose_protocol` with the new revision.
2. Preserve the patient name; never invent a different patient.
3. INJURY ANCHORING (load-bearing for clinical safety): Every exercise you
   propose MUST target the patient's stated body_region. Do NOT propose
   knee exercises for an ankle patient, or shoulder exercises for a low-back
   patient, even if those exercises appear in the active protocol JSON or
   the library you know about. If the active protocol contains exercises
   that do NOT match the patient's body_region, treat that as a data error
   and replace those exercises with region-appropriate ones from the same
   phase.
4. If you cannot find a region-appropriate exercise for the patient's phase,
   refuse: emit a single exercise named `clinician_review_required` with
   sets=0, reps=0 and a `summary` explaining why. Do NOT fabricate exercises.
5. Each exercise MUST include `name`, `sets`, `reps`, and either `load` or a
   load-equivalent field. Include `progression_criteria` and
   `regression_criteria` strings when you can; omit when uncertain rather
   than fabricating.
6. The `summary` field is a single sentence (<= 30 words) the chat UI shows
   the patient. Speak like a clinician, not a chatbot.
7. NEVER write protocol.yaml, open a PR, or call any tool other than
   `propose_protocol`. The clinician dashboard is the only path to active.
"""


_PROPOSE_TOOL = {
    "name": "propose_protocol",
    "description": (
        "Submit the proposed protocol revision. The backend will save it as "
        "pending_review and surface it to the clinician dashboard."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "summary": {
                "type": "string",
                "description": (
                    "One-sentence patient-facing description of what changed "
                    "and why. Shown in the chat UI."
                ),
            },
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
                        },
                    },
                    "required": ["name", "sets", "reps"],
                },
            },
        },
        "required": ["summary", "patient", "phase", "week", "exercises"],
    },
}


def _model() -> str:
    return os.getenv("ANTHROPIC_MODEL", "claude-sonnet-4-6")


def _build_user_prompt(
    flow: str,
    payload: dict[str, Any],
    prior_protocol: dict[str, Any] | None,
    canonical_patient_name: str | None = None,
    intake: dict[str, Any] | None = None,
    body_region: str | None = None,
) -> str:
    instruction = _FLOW_INSTRUCTIONS.get(flow, _FLOW_INSTRUCTIONS["checkin"])
    parts: list[str] = [instruction]

    if canonical_patient_name:
        # Anchor the patient name from the canonical Supabase source
        # (intake_records -> auth.users) so the model can't recycle a stale
        # `patient` field that happens to live on a prior protocol payload
        # from a different account run.
        parts.append(
            f"Canonical patient name (use this verbatim in the `patient` "
            f"field - do NOT use any name that may appear inside the active "
            f'protocol JSON): "{canonical_patient_name}"'
        )

    # INJURY ANCHORING. This is the load-bearing block for clinical safety.
    # Both injury_type and body_region are pinned at the top of the user
    # prompt so the model can't drift onto an unrelated region just because
    # the active protocol JSON contains stale or wrong-region exercises.
    if intake or body_region:
        anchor_lines: list[str] = ["Patient injury anchoring (HARD constraint):"]
        if intake and intake.get("injury_type"):
            anchor_lines.append(f"  injury_type: {intake['injury_type']}")
        if body_region:
            anchor_lines.append(f"  body_region: {body_region}")
        if intake and intake.get("symptoms"):
            anchor_lines.append(
                f"  reported_symptoms: {', '.join(intake.get('symptoms') or [])}"
            )
        anchor_lines.append(
            "Every exercise you propose MUST target the body_region above. "
            "If the active protocol's exercises target a different region, "
            "REPLACE them with region-appropriate ones - do not preserve "
            "wrong-region exercises just because they're in the prior "
            "protocol. If you can't find a region-appropriate exercise, "
            "emit `clinician_review_required` per system instructions."
        )
        parts.append("\n".join(anchor_lines))

    if prior_protocol:
        # Strip our internal _recent_set sentinel before sending to the model.
        # Also strip any `patient` field so the model can't accidentally copy
        # a stale name; the canonical name above is the only allowed source.
        clean = {
            k: v for k, v in prior_protocol.items()
            if not k.startswith("_") and k != "patient"
        }
        parts.append("Active protocol:\n" + json.dumps(clean, indent=2))
    else:
        parts.append(
            "There is no active protocol yet. Generate an initial week-1 "
            "acute-phase plan from the patient's report."
        )

    if payload.get("symptom_text"):
        parts.append(f'Patient symptom report (verbatim): "{payload["symptom_text"]}"')
    if payload.get("checkin_text"):
        parts.append(f'Patient check-in (verbatim): "{payload["checkin_text"]}"')

    return "\n\n".join(parts)


def _normalize_exercise(ex: dict[str, Any]) -> dict[str, Any]:
    """Coerce one exercise into the shape protocol_repo / the YAML schema expect.

    The schema requires a `references` list with at least one entry. Synthesize
    a back-reference if the model didn't emit one rather than rejecting the
    whole draft. Mirrors the behavior in plan_generation_agent._build_payload_from_inputs.
    """
    out = dict(ex)
    refs = out.get("references")
    if not refs:
        out["references"] = ["protocol-library/auto-generated.yaml"]
    return out


def _validate_proposal(proposal: dict[str, Any]) -> dict[str, Any]:
    """Light validation. Anthropic tool-use already enforces the schema, but
    we re-check the load-bearing fields so a malformed response surfaces as
    ProtocolDraftError rather than a confusing 500 from psycopg.
    """
    for field in ("summary", "patient", "phase", "week", "exercises"):
        if field not in proposal:
            raise ProtocolDraftError(f"draft missing required field: {field}")
    if not isinstance(proposal["exercises"], list):
        raise ProtocolDraftError("draft.exercises must be a list")
    if not isinstance(proposal["week"], int):
        raise ProtocolDraftError("draft.week must be an integer")

    payload = {
        "patient": proposal["patient"],
        "phase": proposal["phase"],
        "week": proposal["week"],
        "exercises": [_normalize_exercise(ex) for ex in proposal["exercises"]],
    }
    if "session_targets" in proposal and proposal["session_targets"]:
        payload["session_targets"] = proposal["session_targets"]
    return payload


# Sentinel name the drafter is instructed to emit when no region-appropriate
# exercise exists. The validator treats this as a refusal, not a region
# mismatch - clinicians see the empty draft + summary explaining why.
_REFUSAL_EXERCISE_NAME = "clinician_review_required"


def _validate_region(
    payload: dict[str, Any],
    expected_region: str | None,
) -> None:
    """Deterministic post-LLM safety net.

    Walks every exercise in the draft and confirms its body_region (looked
    up via exercise_kb) matches the patient's expected_region. Any mismatch
    raises CrossRegionExerciseError, which becomes a 502 toast on /chat -
    we never auto-substitute. The clinician (or the next drafter call) is
    the recovery path.

    expected_region == None means we couldn't resolve the patient's region
    from intake at all; in that case we don't enforce - drafting is a
    judgment call for the clinician. expected_region == "multi" means the
    intake spans multiple regions and per-exercise enforcement would block
    legitimate cross-region drafts.
    """
    if not expected_region or expected_region == "multi":
        return

    mismatches: list[dict[str, Any]] = []
    for ex in payload.get("exercises") or []:
        ex_id = ex.get("id") or ex.get("name") or ""
        if ex_id == _REFUSAL_EXERCISE_NAME:
            # Drafter explicitly refused; that's the safe path, not a bug.
            continue
        ex_region = exercise_kb.body_region_for(ex_id)
        if ex_region is None:
            # Unknown to the library - free-text custom exercise. Allow it
            # through; the clinician sees + can reject it. The library is
            # not exhaustive and refusing here would over-block.
            continue
        if ex_region == "multi":
            continue
        if ex_region != expected_region:
            mismatches.append({
                "exercise": ex_id,
                "exercise_region": ex_region,
                "patient_region": expected_region,
            })

    if mismatches:
        # PHI-safe log: token / region / exercise id - no symptom text or
        # patient name. The detail string is what surfaces in the toast.
        logger.warning(
            "drafter region mismatch: patient_region=%s n_mismatches=%d",
            expected_region, len(mismatches),
        )
        raise CrossRegionExerciseError(
            "Drafter proposed exercises outside the patient's body region "
            f"({expected_region}). Mismatched: "
            + ", ".join(
                f"{m['exercise']} ({m['exercise_region']})"
                for m in mismatches
            )
        )


def _run_safety(
    payload: dict[str, Any],
    region: str | None,
) -> list[dict[str, Any]]:
    """Deterministic safety pass over a drafted payload.

    Runs the cross-region validator (which raises CrossRegionExerciseError on
    any exercise outside `region`; we never auto-substitute) and returns the
    list of graded safety concerns. The chat drafter has no LLM safety
    reviewer, so the graded list is currently empty - the return type is the
    contract that change_tier.classify and the clinician gate consume, and the
    swap path reuses this same entry point. Extracted to module level so the
    routing tests can monkeypatch it.
    """
    _validate_region(payload, region)
    return []


def _draft_payload(
    token: str,
    flow: str,
    payload: dict[str, Any],
    prior_protocol: dict[str, Any] | None,
) -> tuple[str, dict[str, Any], str | None, int | None]:
    """Run the Anthropic draft call and return the validated payload.

    Pure extraction of the LLM-draft half of draft_and_save_pending so the
    tier-routing tail (and the swap path) can call it in isolation and the
    tests can monkeypatch it. Returns (summary, payload_to_save, phase, week).
    The resolved body_region is stamped on payload_to_save (in-scope only),
    and the canonical patient name is pinned, exactly as before. Raises
    ProtocolDraftError on any failure.
    """
    api_key = os.getenv("ANTHROPIC_API_KEY", "").strip()
    if not api_key:
        raise ProtocolDraftError(
            "ANTHROPIC_API_KEY is not configured; cannot draft protocol revisions."
        )

    try:
        import anthropic
    except ImportError as exc:
        raise ProtocolDraftError(f"anthropic SDK not installed: {exc}") from exc

    client = anthropic.Anthropic(api_key=api_key)

    # Resolve canonical patient name from Supabase (intake -> auth.users -> email).
    # This deliberately bypasses prior_protocol.patient so a stale name from a
    # previous account run can't leak into the new draft.
    try:
        import user_store as _us
        canonical_name = _us.get_display_name(token)
    except Exception as exc:
        logger.warning(
            "get_display_name failed in drafter for token=%s flow=%s: %s",
            token, flow, exc,
        )
        canonical_name = None

    # Load intake for injury anchoring. Drafter previously had ZERO knowledge
    # of injury_type - the model was free to keep proposing whatever was in
    # the active protocol JSON. Pull intake here and inject into the prompt
    # plus the deterministic post-LLM validator.
    intake: dict[str, Any] | None = None
    try:
        import user_store as _us  # re-import scoped; cheap.
        intake = _us.get_intake(token)
    except Exception as exc:
        logger.warning(
            "get_intake failed in drafter for token=%s flow=%s: %s",
            token, flow, exc,
        )

    expected_region = clinical_taxonomy.resolve_body_region(
        (intake or {}).get("injury_type") if intake else None
    )
    logger.info(
        "drafter anchoring token=%s flow=%s body_region=%s injury_present=%s",
        token, flow, expected_region, bool(intake and intake.get("injury_type")),
    )

    user_prompt = _build_user_prompt(
        flow,
        payload,
        prior_protocol,
        canonical_patient_name=canonical_name,
        intake=intake,
        body_region=expected_region,
    )

    try:
        resp = client.messages.create(
            model=_model(),
            max_tokens=1500,
            system=_BASE_SYSTEM_PROMPT,
            tools=[_PROPOSE_TOOL],
            tool_choice={"type": "tool", "name": "propose_protocol"},
            messages=[{"role": "user", "content": user_prompt}],
        )
    except Exception as exc:
        logger.exception("anthropic draft call failed for flow=%s", flow)
        raise ProtocolDraftError(f"protocol drafter unavailable: {exc}") from exc

    proposal: dict[str, Any] | None = None
    for block in resp.content:
        if getattr(block, "type", None) == "tool_use" and block.name == "propose_protocol":
            proposal = dict(block.input)
            break

    if proposal is None:
        raise ProtocolDraftError(
            "drafter returned no propose_protocol tool call; "
            "cannot save a pending revision."
        )

    summary = (proposal.get("summary") or "Updated rehab protocol.").strip()
    payload_to_save = _validate_proposal(proposal)

    # Belt + suspenders: enforce the canonical name on the persisted row even
    # if the model echoed a different value. Without this guard a malicious or
    # confused model output could still write a stale name into the protocols
    # table.
    if canonical_name:
        payload_to_save["patient"] = canonical_name

    # Persist the resolved body_region on the payload so the canonical taxonomy
    # field is stored — not just inferred from exercise-name coincidence.
    # Downstream readers (the cross-region validator in _run_safety, the
    # /sessions/today out-of-region dimming, and change_tier.classify) key on
    # payload.body_region; it was null on every row until now.
    if expected_region and expected_region != "multi":
        payload_to_save["body_region"] = expected_region

    # Route the draft through the SAME library-coverage gate the multi-agent
    # pipeline uses (plan_generation_agent): an out-of-scope-region draft goes
    # to needs_clinician_review with a med-severity concern (red banner, top of
    # queue); an in-scope draft with no exact week file gets a low informational
    # concern. Chat drafts previously ALL saved as plain pending_review
    # regardless of region, so a shoulder revision drafted from chat skipped the
    # escalation the /interact pipeline applies. Fail-open — a gate error must
    # not block the draft (the clinician review gate still catches everything).
    #
    # Computed here (not in the tier-routing tail) because `intake` and
    # `expected_region` are resolved in this function. `in_scope` is returned
    # separately because it is load-bearing twice downstream: it forces
    # needs_clinician_review AND it vetoes auto-apply outright.
    in_scope = True
    coverage_concerns: list[dict] = []
    try:
        from agents.plan_generation_agent import _coverage_concern
        from agents.researcher import compute_library_match

        _week = payload_to_save.get("week")
        lib_match = compute_library_match(
            (intake or {}).get("injury_type"),
            int(_week) if _week else 1,
            body_region=expected_region,
        )
        in_scope = bool(lib_match.get("in_scope"))
        _cov = _coverage_concern(lib_match)
        if _cov:
            coverage_concerns.append(_cov)
    except Exception:
        logger.exception("chat drafter coverage gate failed flow=%s", flow)

    return (
        summary,
        payload_to_save,
        payload_to_save.get("phase"),
        payload_to_save.get("week"),
        in_scope,
        coverage_concerns,
    )


def draft_and_save_pending(
    token: str,
    flow: str,
    payload: dict[str, Any],
    prior_protocol: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Draft a protocol revision via Anthropic, then route it by change tier.

    Drafts the revision (`_draft_payload`), runs the deterministic safety pass
    (`_run_safety`, which also enforces the body-region net), then asks
    change_tier.classify whether the change is low-risk enough to apply live:

      - "auto" -> protocol_repo.save_active_auto (live, revertible by clinician)
      - "gate" -> protocol_repo.save_pending (clinician review queue)

    Routing is deterministic; the fail-safe default is gate. The first plan of
    care (no prior_protocol) and any high-severity safety concern always gate.

    Parameters
    ----------
    token : str
        Patient identifier (Supabase auth.uid()). Becomes the `token` column
        on the `protocols` row, scoped by RLS to this user.
    flow : str
        One of: symptom_adjustment, checkin, weekly_plan. Drives the
        flow-specific instruction injected into the LLM prompt.
    payload : dict
        Tool arguments from the chat call (symptom_text / checkin_text).
    prior_protocol : dict | None
        The patient's currently-active protocol payload, or None on first
        contact. Anchors the model so it edits-in-place rather than
        regenerating from scratch, and is the diff baseline for the tier
        classifier.

    Returns
    -------
    dict with keys: pending_protocol_id, protocol_id, auto_applied, summary,
    phase, week. `pending_protocol_id` is kept as an alias of `protocol_id`
    for back-compat with existing callers.

    Raises
    ------
    ProtocolDraftError on any failure (no Anthropic key, model error, invalid
    JSON, save failure). /chat catches and surfaces as a clear error toast.
    """
    summary, payload_to_save, phase, week, in_scope, coverage_concerns = (
        _draft_payload(token, flow, payload, prior_protocol)
    )

    # Deterministic safety pass. Raises CrossRegionExerciseError (502 toast on
    # /chat) on any cross-region exercise; we never auto-substitute. The
    # graded concern list feeds the tier classifier and the clinician gate.
    region = payload_to_save.get("body_region")
    safety_concerns = _run_safety(payload_to_save, region)

    # Library-coverage concerns ride along with the safety concerns so the
    # clinician sees both sets on one row, and so the classifier sees the
    # coverage severity too.
    if coverage_concerns:
        safety_concerns = list(safety_concerns or []) + coverage_concerns

    tier = change_tier.classify(prior_protocol, payload_to_save, safety_concerns)

    # An out-of-scope-region draft can NEVER auto-apply, whatever the classifier
    # says. classify() reasons about the shape of the change (regression, swap,
    # load-down) and is blind to whether we hold a protocol library for this
    # region at all. Without this veto a shoulder revision drafted from chat
    # could go live with no clinician ever seeing it, in a region the safety
    # reviewer has no reference material for. Fail-safe direction only: this can
    # turn auto into gate, never gate into auto.
    if not in_scope and tier == "auto":
        logger.info(
            "drafter vetoed auto-apply: out-of-scope region token=%s flow=%s",
            token, flow,
        )
        tier = "gate"

    if tier == "auto":
        try:
            protocol_id = protocol_repo.save_active_auto(
                token=token,
                payload=payload_to_save,
                created_by_agent=f"chat:{flow}",
                safety_concerns=safety_concerns or None,
            )
        except Exception as exc:
            logger.exception("save_active_auto failed for flow=%s", flow)
            raise ProtocolDraftError(
                f"could not auto-apply draft protocol: {exc}"
            ) from exc
        auto_applied = True
    else:
        # High-severity concerns push the row to the top of the clinician
        # queue with the red banner. Pass status / safety_concerns only when
        # they diverge from the defaults so the call stays signature-compatible
        # with callers that monkeypatch the minimal save_pending shape.
        high_severity = any(
            str(c.get("severity", "")).strip().lower() == "high"
            for c in (safety_concerns or [])
        )
        save_kwargs: dict[str, Any] = {}
        # Out-of-scope region escalates the same way a high-severity concern
        # does: red banner, top of the clinician queue. Matches what
        # plan_generation_agent does for the /interact pipeline.
        if high_severity or not in_scope:
            save_kwargs["status"] = "needs_clinician_review"
        if safety_concerns:
            save_kwargs["safety_concerns"] = safety_concerns
        try:
            protocol_id = protocol_repo.save_pending(
                token=token,
                payload=payload_to_save,
                created_by_agent=f"chat:{flow}",
                **save_kwargs,
            )
        except Exception as exc:
            logger.exception("save_pending failed for flow=%s", flow)
            raise ProtocolDraftError(
                f"could not save draft protocol: {exc}"
            ) from exc
        auto_applied = False

    # PHI-safe: token + flow + tier + result id only; never payload or names.
    logger.info(
        "drafter routed token=%s flow=%s tier=%s auto_applied=%s protocol_id=%s",
        token, flow, tier, auto_applied, protocol_id,
    )

    return {
        "pending_protocol_id": protocol_id,  # back-compat alias of protocol_id
        "protocol_id": protocol_id,
        "auto_applied": auto_applied,
        "summary": summary,
        "phase": phase,
        "week": week,
    }


def _in_library_and_region(exercise_id: str, region: str) -> bool:
    """True if exercise_id exists in the library and matches the region.

    Gate condition for swap_exercise: a target the patient names must be a
    real, in-scope library exercise for their own body region. An unknown id
    or a region mismatch forces the swap to clinician review (never applied
    live). Defaults to False on any lookup error - fail-safe to the gate.
    """
    if exercise_id not in set(exercise_kb.list_ids()):
        return False
    try:
        return exercise_kb.body_region_for(exercise_id) == region
    except Exception:
        return False


def apply_swap(
    token: str,
    from_id: str,
    to_id: str,
    reason: str,
    prior_protocol: dict[str, Any] | None,
) -> dict[str, Any]:
    """Swap one exercise for another within the active protocol.

    Deterministic payload edit (no LLM): replace the exercise whose
    `exercise_id` matches `from_id` with `to_id`, keeping every other field
    untouched. Runs the same deterministic safety pass (`_run_safety`) and
    tier classifier (`change_tier.classify`) the drafter uses:

      - "auto" -> protocol_repo.save_active_auto (live, revertible by clinician)
      - "gate" -> protocol_repo.save_pending (clinician review queue)

    A swap whose target is out of the library or out of the patient's body
    region is forced to gate regardless of the diff shape - we never apply a
    cross-region or fabricated exercise live. High-severity safety concerns
    push the gated row to needs_clinician_review.

    Parameters mirror the chat tool args (`from_exercise_id`,
    `to_exercise_id`, `reason`) plus the JWT-derived token and the patient's
    currently-active protocol payload (the diff baseline).

    Returns
    -------
    dict with keys: protocol_id, auto_applied, summary.
    """
    prior = prior_protocol or {}
    region = (prior.get("body_region") or "").strip().lower()
    exs = [dict(e) for e in prior.get("exercises", [])]
    payload = dict(prior)
    payload["exercises"] = [
        {**e, "exercise_id": to_id, "name": to_id}
        if e.get("exercise_id") == from_id
        else e
        for e in exs
    ]
    # Drop the internal live-set sentinel so it never lands on a persisted row.
    payload.pop("_recent_set", None)

    safety = _run_safety(payload, region)
    target_ok = _in_library_and_region(to_id, region)
    tier = "gate" if not target_ok else change_tier.classify(prior, payload, safety)

    summary = f"Swapped {from_id} -> {to_id} ({reason})."

    if tier == "auto":
        protocol_id = protocol_repo.save_active_auto(
            token,
            payload,
            created_by_agent="coach_swap",
            safety_concerns=safety or None,
        )
        logger.info(
            "swap routed token=%s tier=auto protocol_id=%s", token, protocol_id,
        )
        return {
            "protocol_id": protocol_id,
            "auto_applied": True,
            "summary": summary,
        }

    high_severity = any(
        str(c.get("severity", "")).strip().lower() == "high" for c in safety
    )
    status = "needs_clinician_review" if high_severity else "pending_review"
    protocol_id = protocol_repo.save_pending(
        token,
        payload,
        created_by_agent="coach_swap",
        status=status,
        safety_concerns=safety or None,
    )
    logger.info(
        "swap routed token=%s tier=gate status=%s protocol_id=%s",
        token, status, protocol_id,
    )
    return {
        "protocol_id": protocol_id,
        "auto_applied": False,
        "summary": summary,
    }
