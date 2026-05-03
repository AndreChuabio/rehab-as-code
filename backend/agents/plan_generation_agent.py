"""
PlanGenerationAgent — builds a personalized rehab protocol and triggers the coding pipeline.

Reads intake record + user health data + exercise KB, constructs a protocol spec,
then calls the existing CodingAgent (ag2_agent.py / cached_replay / etc.) to write
protocol.yaml and open a GitHub PR.

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

logger = logging.getLogger(__name__)

PROTOCOL_REPO = os.getenv("PROTOCOL_REPO", "AndreChuabio/rehab-protocols-andre")

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
            return PatientResponse(
                agent_name=self.name,
                message="Plan generation unavailable (ANTHROPIC_API_KEY not set).",
                next_agent=None,
            )

        client = anthropic.Anthropic(api_key=api_key)
        history: list[dict] = [
            {"role": "user", "content": f"Generate a rehab protocol for token {request.user_token}. {request.message}"}
        ]

        pr_url: str | None = None
        pr_branch: str | None = None
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
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": tc.id,
                    "content": json.dumps(result),
                })

            history.append({"role": "user", "content": tool_results})

            if pr_url:
                break

        artifacts = [{"type": "pr", "url": pr_url}] if pr_url else []

        return PatientResponse(
            agent_name=self.name,
            message=final_message or (
                f"Protocol generated. PR is open for clinician review: {pr_url}"
                if pr_url else "Protocol generation in progress."
            ),
            next_agent=None,
            data={"pr_url": pr_url, "branch": pr_branch},
            artifacts=artifacts,
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
            user = user_store.load_user(tok) or {}
            health = user.get("health") or {}

            # Build prompt for the CodingAgent
            spec_json = json.dumps({k: v for k, v in inputs.items() if k != "token"}, indent=2)
            prompt = (
                f"Initialize protocol.yaml for patient {inputs.get('patient_name')}.\n"
                f"Phase: {inputs.get('phase')}, Week: {inputs.get('week')}.\n\n"
                f"Protocol spec:\n{spec_json}\n\n"
                "Create protocol.yaml following the existing YAML structure. "
                "Include a wearable hold rule in session_targets: hold load if HRV drops > 8ms below 7-day avg."
            )

            # Write context files exactly as main.py does
            try:
                import context_builder  # noqa: F401 — may not be available
                from main import write_context_files
                context_files = write_context_files(
                    flow="intake",
                    wearables=health,
                    symptom_log=spec_json,
                )
            except Exception:
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

                user_store.save_protocol_state(tok, {
                    "last_pr_url": invocation.pr_url,
                    "last_branch": invocation.branch,
                    "current_phase": inputs.get("phase"),
                    "current_week": inputs.get("week"),
                })

                return {"ok": True, "pr_url": invocation.pr_url, "branch": invocation.branch}
            except Exception as exc:
                logger.exception("CodingAgent invocation failed")
                return {"error": str(exc)}

        return {"error": f"unknown tool {name}"}
