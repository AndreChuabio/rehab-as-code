"""
CheckInAgent — post-session feedback collection.

4 targeted questions (shorter than intake):
  1. Pain level during session (0-10)
  2. Fatigue level (0-10)
  3. Any pain, swelling, or compensation to report?
  4. Any exercises skipped or modified?

After collecting, evaluates trend and optionally triggers a symptom
adjustment PR via the CodingAgent pipeline.
"""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone

import anthropic

from . import register_patient_agent
from .base import PatientAgent, PatientRequest, PatientResponse

import sys
sys.path.insert(0, str(__import__("pathlib").Path(__file__).parent.parent))
import user_store

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = """You are a post-session check-in specialist for a rehabilitation app.
Collect the following 4 data points through natural, brief conversation:
  1. pain_level: pain level during the session (0-10)
  2. fatigue_level: fatigue level (0-10)
  3. symptoms: any pain, swelling, tightness, or compensation patterns (list, can be empty)
  4. skipped_exercises: any exercises skipped or modified (list, can be empty)

Ask one question at a time. Be concise — the patient just finished exercising.

When you have all 4, call save_checkin with the collected data.
If symptoms are concerning (pain > 6, new swelling, sharp pain), also call trigger_symptom_adjustment.
"""

CHECKIN_TOOLS = [
    {
        "name": "save_checkin",
        "description": "Save this session's check-in. Call when all 4 data points are collected.",
        "input_schema": {
            "type": "object",
            "properties": {
                "pain_level": {"type": "integer", "description": "0-10"},
                "fatigue_level": {"type": "integer", "description": "0-10"},
                "symptoms": {"type": "array", "items": {"type": "string"}},
                "skipped_exercises": {"type": "array", "items": {"type": "string"}},
                "patient_note": {"type": "string"},
            },
            "required": ["pain_level", "fatigue_level", "symptoms", "skipped_exercises"],
        },
    },
    {
        "name": "trigger_symptom_adjustment",
        "description": "Flag a concerning symptom for protocol adjustment PR.",
        "input_schema": {
            "type": "object",
            "properties": {"symptom_summary": {"type": "string"}},
            "required": ["symptom_summary"],
        },
    },
]


@register_patient_agent
class CheckInAgent(PatientAgent):
    """Collects post-session feedback, evaluates trends, optionally triggers protocol adjustment."""

    name = "checkin"

    def can_handle(self, request: PatientRequest) -> bool:
        keywords = ["check in", "just finished", "done today", "pain level", "session was"]
        return any(k in request.message.lower() for k in keywords)

    async def handle(self, request: PatientRequest) -> PatientResponse:
        api_key = os.getenv("ANTHROPIC_API_KEY")
        if not api_key:
            return PatientResponse(
                agent_name=self.name,
                message="Check-in unavailable (ANTHROPIC_API_KEY not set).",
                next_agent=None,
            )

        client = anthropic.Anthropic(api_key=api_key)
        history: list[dict] = request.metadata.get("conversation_history", [])
        history.append({"role": "user", "content": request.message})

        checkin_data: dict | None = None
        symptom_pr_url: str | None = None
        final_message = ""

        for _ in range(8):  # max turns
            resp = client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=300,
                system=SYSTEM_PROMPT,
                tools=CHECKIN_TOOLS,
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
                if tc.name == "save_checkin":
                    checkin_data = tc.input
                if tc.name == "trigger_symptom_adjustment":
                    symptom_pr_url = result.get("pr_url")
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": tc.id,
                    "content": json.dumps(result),
                })

            history.append({"role": "user", "content": tool_results})

            if checkin_data:
                break

        trend = self._evaluate_trend(request.user_token) if checkin_data else None

        artifacts = []
        if symptom_pr_url:
            artifacts.append({"type": "pr", "url": symptom_pr_url})

        return PatientResponse(
            agent_name=self.name,
            message=final_message or "Check-in recorded. Thanks for logging today's session.",
            next_agent=None,
            data={"checkin": checkin_data, "trend": trend},
            artifacts=artifacts,
        )

    def _dispatch_tool(self, name: str, inputs: dict, token: str) -> dict:
        if name == "save_checkin":
            user_store.save_checkin(token, {
                "pain_level": inputs.get("pain_level"),
                "fatigue_level": inputs.get("fatigue_level"),
                "symptoms": inputs.get("symptoms", []),
                "skipped_exercises": inputs.get("skipped_exercises", []),
                "patient_note": inputs.get("patient_note", ""),
                "completed_exercises": [],
                "type": "checkin",
            })
            return {"ok": True}

        if name == "trigger_symptom_adjustment":
            return {"ok": True, "pr_url": None, "note": "symptom flagged for review"}

        return {"error": f"unknown tool {name}"}

    def _evaluate_trend(self, token: str) -> dict | None:
        history = user_store.get_session_history(token, limit=5)
        if len(history) < 2:
            return None
        pain_values = [s.get("pain_level") for s in history if s.get("pain_level") is not None]
        if len(pain_values) < 2:
            return None
        recent_avg = sum(pain_values[-2:]) / 2
        prior_avg = sum(pain_values[:-2]) / max(len(pain_values) - 2, 1)
        if recent_avg > prior_avg + 2:
            trend = "regress"
        elif recent_avg < prior_avg - 1:
            trend = "progress"
        else:
            trend = "hold"
        return {"trend": trend, "recent_pain_avg": recent_avg, "prior_pain_avg": prior_avg}
