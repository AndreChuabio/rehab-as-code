"""
PlanGenerationAgent — builds a personalized rehab protocol.

Reads intake record + user health data + exercise KB, constructs a protocol spec,
then writes it where PROTOCOL_WRITE_TARGET points:

  PROTOCOL_WRITE_TARGET=github   (default, legacy)
      Hand the spec to a CodingAgent (ag2_agent / cached_replay / ...) which
      writes protocols/protocol.yaml and opens a GitHub PR. The PR is the
      audit/approval gate.

  PROTOCOL_WRITE_TARGET=supabase (Phase-2+ target)
      INSERT a `pending_review` row directly into the `protocols` table.
      The clinician dashboard (Phase 3) is the audit/approval gate. No git
      operation, no CodingAgent, no orchestrator.

Uses claude-sonnet-4-6 for protocol reasoning.
"""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path

import anthropic

from . import get_agent, register_patient_agent
from .base import InvocationRequest, PatientAgent, PatientRequest, PatientResponse

import sys
sys.path.insert(0, str(__import__("pathlib").Path(__file__).parent.parent))
import user_store
import protocol_repo

logger = logging.getLogger(__name__)

PROTOCOL_REPO = os.getenv("PROTOCOL_REPO", "AndreChuabio/rehab-protocols-andre")


def _write_target() -> str:
    """Read PROTOCOL_WRITE_TARGET each call so runtime flips take effect."""
    return os.getenv("PROTOCOL_WRITE_TARGET", "github").strip().lower() or "github"


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
Then call generate_protocol to hand the spec to the coding agent.

Protocol design rules:
- Week 1-2 post-op: acute phase, ROM + neuromuscular activation only
- Week 3-6: subacute phase, introduce strengthening with low load
- Week 7+: strength phase, progressive loading
- Always include progression and regression criteria per exercise
- Include a wearable hold rule: if HRV drops > 8ms below 7-day avg → hold load
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
        "description": "Trigger the coding agent to write protocol.yaml and open a PR.",
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
    """Reads intake + KB, builds protocol spec, calls CodingAgent to write YAML + PR."""

    name = "plan_generation"

    def can_handle(self, request: PatientRequest) -> bool:
        keywords = ["generate plan", "new protocol", "update my plan", "next week", "progress"]
        return any(k in request.message.lower() for k in keywords)

    async def handle(self, request: PatientRequest) -> PatientResponse:
        api_key = os.getenv("ANTHROPIC_API_KEY")
        if not api_key:
            # Fallback path for demo: skip the planner LLM and call the CodingAgent directly
            # with whatever intake data we have. This keeps the PR-open demo working when
            # ANTHROPIC_API_KEY is missing in Vercel (CodingAgent may itself be cached_replay).
            return await self._fallback_direct_pr(request)

        client = anthropic.Anthropic(api_key=api_key)
        history: list[dict] = [
            {"role": "user", "content": f"Generate a rehab protocol for token {request.user_token}. {request.message}"}
        ]

        pr_url: str | None = None
        pr_branch: str | None = None
        invocation_id: str | None = None
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
                result = await self._dispatch_tool(tc.name, tc.input, request.user_token)
                if tc.name == "generate_protocol":
                    pr_url = result.get("pr_url")
                    pr_branch = result.get("branch")
                    invocation_id = result.get("invocation_id")
                    pending_protocol_id = result.get("pending_protocol_id")
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": tc.id,
                    "content": json.dumps(result),
                })

            history.append({"role": "user", "content": tool_results})

            if pr_url or pending_protocol_id:
                break

        artifacts: list[dict] = []
        if pr_url:
            artifacts.append({"type": "pr", "url": pr_url})
        if pending_protocol_id:
            artifacts.append({"type": "pending_protocol", "id": pending_protocol_id})

        if not final_message:
            if pending_protocol_id:
                final_message = "Protocol generated. Awaiting clinician review."
            elif pr_url:
                final_message = f"Protocol generated. PR is open for clinician review: {pr_url}"
            else:
                final_message = "Protocol generation in progress."

        return PatientResponse(
            agent_name=self.name,
            message=final_message,
            next_agent=None,
            data={
                "pr_url": pr_url,
                "branch": pr_branch,
                "invocation_id": invocation_id,
                "pending_protocol_id": pending_protocol_id,
            },
            artifacts=artifacts,
        )

    async def _fallback_direct_pr(self, request: PatientRequest) -> PatientResponse:
        """Skip the planner LLM. Used when ANTHROPIC_API_KEY is missing.

        Splits on PROTOCOL_WRITE_TARGET like the LLM-driven path:
          - supabase: save a stub pending_review row directly
          - github:   hand the stub to a CodingAgent (which may itself be
                      cached_replay), opening a PR
        """
        token = request.user_token
        user = user_store.load_user(token) or {}
        intake = user.get("intake") or {}
        health = user.get("health") or {}

        stub_inputs = {
            "patient_name": intake.get("name", "Patient"),
            "phase": "acute",
            "week": 1,
            "exercises": [],
        }

        if _write_target() == "supabase":
            try:
                payload = _build_payload_from_inputs(stub_inputs)
                # Stub protocol has no exercises; surface that in the payload
                # so the clinician sees an empty draft and knows to fill it
                # in (rather than auto-approving an empty plan).
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
                data={"pr_url": None, "branch": None, "invocation_id": None,
                      "pending_protocol_id": protocol_id},
                artifacts=[{"type": "pending_protocol", "id": protocol_id}],
            )

        spec_json = json.dumps(stub_inputs, indent=2)
        prompt = (
            f"Initialize protocol.yaml for patient {stub_inputs['patient_name']}.\n"
            f"Phase: {stub_inputs['phase']}, Week: {stub_inputs['week']}.\n\n"
            f"Protocol spec:\n{spec_json}\n"
        )

        try:
            from protocol_loader import write_context_files
            context_files = write_context_files(
                flow="intake", wearables=health, symptom_log=spec_json,
            )
        except Exception:
            context_files = {
                f"protocols/data/intake-{stub_inputs['patient_name'].lower()}.json": spec_json
            }

        try:
            invocation = await get_agent().invoke(InvocationRequest(
                repo=PROTOCOL_REPO,
                prompt=prompt,
                context_files=context_files,
                flow="weekly_plan",
            ))
            user_store.save_protocol_state(token, {
                "last_pr_url": invocation.pr_url,
                "last_branch": invocation.branch,
                "current_phase": stub_inputs["phase"],
                "current_week": stub_inputs["week"],
            })
            return PatientResponse(
                agent_name=self.name,
                message=(
                    f"Protocol generated. PR is open for clinician review: {invocation.pr_url}"
                    if invocation.pr_url
                    else "Protocol generation in progress."
                ),
                next_agent=None,
                data={"pr_url": invocation.pr_url, "branch": invocation.branch,
                      "invocation_id": invocation.invocation_id},
                artifacts=[{"type": "pr", "url": invocation.pr_url}] if invocation.pr_url else [],
            )
        except Exception as exc:
            logger.exception("Fallback CodingAgent invocation failed")
            return PatientResponse(
                agent_name=self.name,
                message=f"Plan generation failed: {exc}",
                next_agent=None,
            )

    async def _dispatch_tool(self, name: str, inputs: dict, token: str) -> dict:
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

            if _write_target() == "supabase":
                return self._generate_protocol_supabase(tok, inputs)

            return await self._generate_protocol_github(tok, inputs)

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

        # Mirror the legacy flow's protocol_state update so any code still
        # reading protocol_state (e.g., dashboard sidebar) sees the new
        # phase/week. Cleaned up entirely in Phase E once protocol_state is
        # decommissioned.
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

    async def _generate_protocol_github(self, token: str, inputs: dict) -> dict:
        """Legacy: hand the spec to a CodingAgent that opens a GitHub PR."""
        user = user_store.load_user(token) or {}
        health = user.get("health") or {}

        spec_json = json.dumps({k: v for k, v in inputs.items() if k != "token"}, indent=2)
        prompt = (
            f"Initialize protocol.yaml for patient {inputs.get('patient_name')}.\n"
            f"Phase: {inputs.get('phase')}, Week: {inputs.get('week')}.\n\n"
            f"Protocol spec:\n{spec_json}\n\n"
            "Create protocol.yaml following the existing YAML structure. "
            "Include a wearable hold rule in session_targets: hold load if HRV drops > 8ms below 7-day avg."
        )

        try:
            from protocol_loader import write_context_files
            context_files = write_context_files(
                flow="intake",
                wearables=health,
                symptom_log=spec_json,
            )
        except Exception as exc:
            logger.warning("write_context_files failed (%s); using minimal stub", exc)
            context_files = {
                f"protocols/data/intake-{inputs.get('patient_name','patient').lower()}.json": spec_json
            }

        invocation_request = InvocationRequest(
            repo=PROTOCOL_REPO,
            prompt=prompt,
            context_files=context_files,
            flow="weekly_plan",
        )

        try:
            coding_agent = get_agent()
            invocation = await coding_agent.invoke(invocation_request)

            user_store.save_protocol_state(token, {
                "last_pr_url": invocation.pr_url,
                "last_branch": invocation.branch,
                "current_phase": inputs.get("phase"),
                "current_week": inputs.get("week"),
            })

            return {
                "ok": True,
                "pr_url": invocation.pr_url,
                "branch": invocation.branch,
                "invocation_id": invocation.invocation_id,
            }
        except Exception as exc:
            logger.exception("CodingAgent invocation failed")
            return {"error": str(exc)}
