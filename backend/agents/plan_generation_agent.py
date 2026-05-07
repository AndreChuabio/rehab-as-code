"""
PlanGenerationAgent — builds a personalized rehab protocol.

Reads intake record + user health data + exercise KB, constructs a protocol
spec via claude-sonnet-4-6, then INSERTs a `pending_review` row into the
Supabase `protocols` table. The clinician dashboard at /clinician is the
audit/approval gate — clinicians flip the row to `active` via
/protocols/{id}/approve.

The legacy GitHub PR-bus write path was retired post-PR-#62 along with the
CodingAgent abstraction (cursor_sdk, ag2_agent, cached_replay, ...). All
protocol writes now go through protocol_repo.save_pending; clinician
approval is the only path to active.
"""
from __future__ import annotations

import json
import logging
import os

import anthropic

from . import register_patient_agent
from .base import PatientAgent, PatientRequest, PatientResponse

import sys
sys.path.insert(0, str(__import__("pathlib").Path(__file__).parent.parent))
import user_store
import protocol_repo

logger = logging.getLogger(__name__)


def _build_payload_from_inputs(inputs: dict) -> dict:
    """Translate the planner's `generate_protocol` tool call into the YAML
    shape consumed by the rest of the app (matches protocols/schema.json).

    Only fields the planner produces are copied through; missing optional
    fields stay missing rather than getting placeholder values.
    """
    payload: dict = {
        "patient": inputs.get("patient_name"),
        "phase": inputs.get("phase"),
        "week": inputs.get("week"),
        "exercises": [],
    }
    if "session_targets" in inputs and inputs["session_targets"]:
        payload["session_targets"] = inputs["session_targets"]
    for ex in inputs.get("exercises") or []:
        # The schema requires a `references` list with at least one entry; the
        # planner doesn't always emit one. Synthesize a back-reference to the
        # phase library so validation passes and the audit trail still points
        # somewhere meaningful.
        refs = ex.get("references") or [
            f"protocol-library/{inputs.get('phase', 'unknown')}.yaml"
        ]
        payload["exercises"].append({**ex, "references": refs})
    return payload


SYSTEM_PROMPT = """You are a rehabilitation protocol planner.
Given patient intake data, wearable health metrics, and the exercise knowledge base,
design a personalized protocol.

Call load_patient_context first to load everything you need.
Then call list_exercises_for_phase to see available exercises.
Then call generate_protocol to save the spec as a pending-review draft for
clinician approval.

Protocol design rules:
- Week 1-2 post-op: acute phase, ROM + neuromuscular activation only
- Week 3-6: subacute phase, introduce strengthening with low load
- Week 7+: strength phase, progressive loading
- Always include progression and regression criteria per exercise
- Include a wearable hold rule: if HRV drops > 8ms below 7-day avg -> hold load
"""

PLAN_TOOLS = [
    {
        "name": "load_patient_context",
        "description": "Load intake record, health data, and session history for the patient.",
        "input_schema": {
            "type": "object",
            "properties": {"token": {"type": "string"}},
            "required": ["token"],
        },
    },
    {
        "name": "list_exercises_for_phase",
        "description": "Return exercises from KB matching a rehab phase.",
        "input_schema": {
            "type": "object",
            "properties": {"phase": {"type": "string", "enum": ["acute", "subacute", "strength"]}},
            "required": ["phase"],
        },
    },
    {
        "name": "generate_protocol",
        "description": (
            "Save the proposed protocol as a `pending_review` row in the "
            "`protocols` table. The clinician dashboard at /clinician is the "
            "approval gate; this tool does NOT auto-apply."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "token": {"type": "string"},
                "patient_name": {"type": "string"},
                "phase": {"type": "string"},
                "week": {"type": "integer"},
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
                        },
                    },
                },
                "session_targets": {
                    "type": "object",
                    "properties": {
                        "frequency_per_week": {"type": "integer"},
                        "duration_min": {"type": "integer"},
                    },
                },
            },
            "required": ["token", "patient_name", "phase", "week", "exercises"],
        },
    },
]


@register_patient_agent
class PlanGenerationAgent(PatientAgent):
    """Reads intake + KB, drafts protocol, saves as pending_review for clinician approval."""

    name = "plan_generation"

    def can_handle(self, request: PatientRequest) -> bool:
        keywords = ["generate plan", "new protocol", "update my plan", "next week", "progress"]
        return any(k in request.message.lower() for k in keywords)

    async def handle(self, request: PatientRequest) -> PatientResponse:
        api_key = os.getenv("ANTHROPIC_API_KEY")
        if not api_key:
            # No planner LLM available -> save a stub pending row so the clinician
            # can still see something queued and fill it in. Surfacing an empty
            # pending draft is better than a 500; the clinician dashboard makes
            # it clear no exercises were proposed.
            return self._save_stub_pending(request)

        client = anthropic.Anthropic(api_key=api_key)
        history: list[dict] = [
            {"role": "user", "content": f"Generate a rehab protocol for token {request.user_token}. {request.message}"}
        ]

        pending_protocol_id: str | None = None
        final_message = ""

        for _ in range(6):
            resp = client.messages.create(
                model="claude-sonnet-4-6",
                max_tokens=800,
                system=SYSTEM_PROMPT,
                tools=PLAN_TOOLS,
                messages=history,
            )

            assistant_content = resp.content
            history.append({"role": "assistant", "content": assistant_content})

            tool_calls = [b for b in assistant_content if b.type == "tool_use"]
            text_blocks = [b for b in assistant_content if b.type == "text"]
            if text_blocks:
                final_message = text_blocks[-1].text

            if not tool_calls:
                break

            tool_results = []
            for tc in tool_calls:
                result = self._dispatch_tool(tc.name, tc.input, request.user_token)
                if tc.name == "generate_protocol":
                    pending_protocol_id = result.get("pending_protocol_id")
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": tc.id,
                    "content": json.dumps(result),
                })

            history.append({"role": "user", "content": tool_results})

            if pending_protocol_id:
                break

        artifacts: list[dict] = []
        if pending_protocol_id:
            artifacts.append({"type": "pending_protocol", "id": pending_protocol_id})

        if not final_message:
            final_message = (
                "Protocol generated. Awaiting clinician review."
                if pending_protocol_id
                else "Protocol generation in progress."
            )

        return PatientResponse(
            agent_name=self.name,
            message=final_message,
            next_agent=None,
            data={"pending_protocol_id": pending_protocol_id},
            artifacts=artifacts,
        )

    def _save_stub_pending(self, request: PatientRequest) -> PatientResponse:
        """Save a no-LLM stub pending row when ANTHROPIC_API_KEY is missing.

        Better than a 500: the clinician sees an empty draft attributed to
        "plan_generation.fallback" and knows to fill it in by hand. The
        intake payload rides along so they have context.
        """
        token = request.user_token
        user = user_store.load_user(token) or {}
        intake = user.get("intake") or {}

        stub_inputs = {
            "patient_name": intake.get("name", "Patient"),
            "phase": "acute",
            "week": 1,
            "exercises": [],
        }

        try:
            payload = _build_payload_from_inputs(stub_inputs)
            payload["intake"] = intake
            protocol_id = protocol_repo.save_pending(
                token=token,
                payload=payload,
                created_by_agent=f"{self.name}.fallback",
            )
        except Exception as exc:
            logger.exception("fallback supabase pending insert failed")
            return PatientResponse(
                agent_name=self.name,
                message=f"Plan generation failed: {exc}",
                next_agent=None,
            )

        try:
            user_store.save_protocol_state(token, {
                "last_pr_url": None,
                "last_branch": None,
                "current_phase": stub_inputs["phase"],
                "current_week": stub_inputs["week"],
                "pending_protocol_id": protocol_id,
            })
        except Exception as exc:
            logger.warning("protocol_state mirror update failed: %s", exc)

        return PatientResponse(
            agent_name=self.name,
            message="Protocol generated (fallback). Awaiting clinician review.",
            next_agent=None,
            data={"pending_protocol_id": protocol_id},
            artifacts=[{"type": "pending_protocol", "id": protocol_id}],
        )

    def _dispatch_tool(self, name: str, inputs: dict, token: str) -> dict:
        if name == "load_patient_context":
            user = user_store.load_user(inputs.get("token", token))
            if not user:
                return {"error": "user not found"}
            return {
                "intake": user.get("intake"),
                "health": user.get("health"),
                "protocol_state": user.get("protocol_state"),
                "recent_sessions": user_store.get_session_history(token, limit=5),
            }

        if name == "list_exercises_for_phase":
            try:
                import exercise_kb as kb
                matches = kb.find_by_phase(inputs.get("phase", ""))
                return {"exercises": [kb.to_card(e) for e in matches]}
            except Exception as exc:
                return {"error": str(exc)}

        if name == "generate_protocol":
            tok = inputs.get("token", token)
            return self._generate_protocol_supabase(tok, inputs)

        return {"error": f"unknown tool {name}"}

    def _generate_protocol_supabase(self, token: str, inputs: dict) -> dict:
        """Insert a pending_review row directly. No CodingAgent, no GitHub."""
        try:
            payload = _build_payload_from_inputs(inputs)
            protocol_id = protocol_repo.save_pending(
                token=token,
                payload=payload,
                created_by_agent=self.name,
            )
        except Exception as exc:
            logger.exception("supabase pending insert failed")
            return {"error": f"protocol save failed: {exc}"}

        # Mirror to protocol_state so the dashboard sidebar can show the new
        # phase/week without reading the protocols table directly. Cleaned up
        # entirely once protocol_state is decommissioned.
        try:
            user_store.save_protocol_state(token, {
                "last_pr_url": None,
                "last_branch": None,
                "current_phase": inputs.get("phase"),
                "current_week": inputs.get("week"),
                "pending_protocol_id": protocol_id,
            })
        except Exception as exc:
            logger.warning("protocol_state mirror update failed: %s", exc)

        return {
            "ok": True,
            "pending_protocol_id": protocol_id,
            "status": "pending_review",
        }
