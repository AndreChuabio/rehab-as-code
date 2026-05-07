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
        Run the LLM, validate, persist. Returns:
            {
                "pending_protocol_id": str,
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

import protocol_repo

logger = logging.getLogger(__name__)


class ProtocolDraftError(RuntimeError):
    """Raised when a draft protocol cannot be generated or persisted."""


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

You will receive the patient's currently-active protocol (JSON) and a flow-
specific instruction. Produce a NEW protocol revision as a JSON object that
will be saved as `pending_review` and queued for clinician approval.

Hard requirements:
1. Output a single tool call to `propose_protocol` with the new revision.
2. Preserve the patient name; never invent a different patient.
3. Each exercise MUST include `name`, `sets`, `reps`, and either `load` or a
   load-equivalent field. Include `progression_criteria` and
   `regression_criteria` strings when you can; omit when uncertain rather
   than fabricating.
4. The `summary` field is a single sentence (<= 30 words) the chat UI shows
   the patient. Speak like a clinician, not a chatbot.
5. NEVER write protocol.yaml, open a PR, or call any tool other than
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
) -> str:
    instruction = _FLOW_INSTRUCTIONS.get(flow, _FLOW_INSTRUCTIONS["checkin"])
    parts: list[str] = [instruction]

    if prior_protocol:
        # Strip our internal _recent_set sentinel before sending to the model.
        clean = {k: v for k, v in prior_protocol.items() if not k.startswith("_")}
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


def draft_and_save_pending(
    token: str,
    flow: str,
    payload: dict[str, Any],
    prior_protocol: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Draft a protocol revision via Anthropic, persist as pending_review.

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
        regenerating from scratch.

    Returns
    -------
    dict with keys: pending_protocol_id, summary, phase, week.

    Raises
    ------
    ProtocolDraftError on any failure (no Anthropic key, model error, invalid
    JSON, save failure). /chat catches and surfaces as a clear error toast.
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
    user_prompt = _build_user_prompt(flow, payload, prior_protocol)

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

    try:
        protocol_id = protocol_repo.save_pending(
            token=token,
            payload=payload_to_save,
            created_by_agent=f"chat:{flow}",
        )
    except Exception as exc:
        logger.exception("save_pending failed for flow=%s", flow)
        raise ProtocolDraftError(f"could not save draft protocol: {exc}") from exc

    return {
        "pending_protocol_id": protocol_id,
        "summary": summary,
        "phase": payload_to_save.get("phase"),
        "week": payload_to_save.get("week"),
    }
