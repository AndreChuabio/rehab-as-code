"""
GuidedVideoAgent — guides the patient through their current protocol session.

Fetches the live protocol, enriches with exercise KB video data, optionally
creates a Tavus coaching session. Does NOT write to the repo.
Routes to checkin when the session completes or a symptom is flagged.
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

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = """You are Coach Maya's guided exercise assistant.
You have the patient's current protocol and the exercise video library.

Guide the patient through their session:
1. Call get_current_protocol to load today's exercises
2. For each exercise, present the name and dose (sets × reps × load)
3. Call show_exercise_video to surface the video card
4. Guide them through verbal cues, ask if they feel pain or compensation
5. If pain reported: call flag_symptom
6. When done: call complete_session with the list of completed exercises

Keep responses short and clinical. If the patient asks to skip, accept it and log it.
"""

GUIDED_VIDEO_TOOLS = [
    {
        "name": "get_current_protocol",
        "description": "Fetch the patient's current protocol enriched with video KB data.",
        "input_schema": {
            "type": "object",
            "properties": {"token": {"type": "string"}},
            "required": ["token"],
        },
    },
    {
        "name": "show_exercise_video",
        "description": "Return a video card for an exercise to display to the patient.",
        "input_schema": {
            "type": "object",
            "properties": {"exercise_id": {"type": "string"}},
            "required": ["exercise_id"],
        },
    },
    {
        "name": "create_coaching_session",
        "description": "Create a Tavus video coaching session for this exercise session.",
        "input_schema": {
            "type": "object",
            "properties": {
                "patient_name": {"type": "string"},
                "exercise_names": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["patient_name", "exercise_names"],
        },
    },
    {
        "name": "flag_symptom",
        "description": "Flag a mid-session symptom. Routes the patient to check-in.",
        "input_schema": {
            "type": "object",
            "properties": {
                "symptom_text": {"type": "string"},
                "exercise_id": {"type": "string"},
            },
            "required": ["symptom_text"],
        },
    },
    {
        "name": "complete_session",
        "description": "Mark session complete and route to check-in.",
        "input_schema": {
            "type": "object",
            "properties": {
                "completed_exercises": {"type": "array", "items": {"type": "string"}},
                "skipped_exercises": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["completed_exercises"],
        },
    },
]


@register_patient_agent
class GuidedVideoAgent(PatientAgent):
    """Guides the patient through their protocol session with video coaching."""

    name = "guided_video"

    def can_handle(self, request: PatientRequest) -> bool:
        keywords = ["start session", "show me", "exercises", "workout", "session"]
        return any(k in request.message.lower() for k in keywords)

    async def handle(self, request: PatientRequest) -> PatientResponse:
        api_key = os.getenv("ANTHROPIC_API_KEY")
        if not api_key:
            return PatientResponse(
                agent_name=self.name,
                message="Guided video unavailable (ANTHROPIC_API_KEY not set).",
                next_agent=None,
            )

        client = anthropic.Anthropic(api_key=api_key)
        history: list[dict] = request.metadata.get("conversation_history", [])
        history.append({"role": "user", "content": request.message})

        next_agent: str | None = None
        final_message = ""
        artifacts: list[dict] = []
        session_data: dict = {}

        for _ in range(12):
            resp = client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=400,
                system=SYSTEM_PROMPT,
                tools=GUIDED_VIDEO_TOOLS,
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
                result, extra_artifacts, should_route = self._dispatch_tool(
                    tc.name, tc.input, request.user_token
                )
                artifacts.extend(extra_artifacts)
                if should_route:
                    next_agent = should_route
                    session_data.update(tc.input)
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": tc.id,
                    "content": json.dumps(result),
                })

            history.append({"role": "user", "content": tool_results})

            if next_agent:
                break

        return PatientResponse(
            agent_name=self.name,
            message=final_message or "Ready to start your session.",
            next_agent=next_agent,
            data=session_data,
            artifacts=artifacts,
        )

    def _dispatch_tool(
        self, name: str, inputs: dict, token: str
    ) -> tuple[dict, list[dict], str | None]:
        """Returns (result, extra_artifacts, next_agent_or_None)."""
        if name == "get_current_protocol":
            try:
                import protocol_loader
                import exercise_kb as kb
                protocol = protocol_loader.fetch_protocol()
                exercises = protocol.get("exercises", [])
                enriched = []
                for ex in exercises:
                    ex_id = ex.get("id") or ex.get("name", "")
                    entry = kb.find_by_id(ex_id)
                    card = kb.to_card(entry) if entry else {}
                    enriched.append({
                        "id": ex_id,
                        "name": ex.get("name", ex_id),
                        "spec": f"{ex.get('sets','?')}×{ex.get('reps','?')} {ex.get('load','')}".strip(),
                        **card,
                    })
                return {"exercises": enriched, "phase": protocol.get("phase"), "week": protocol.get("week")}, [], None
            except Exception as exc:
                return {"error": str(exc)}, [], None

        if name == "show_exercise_video":
            try:
                import exercise_kb as kb
                entry = kb.find_by_id(inputs.get("exercise_id", ""))
                if not entry:
                    return {"error": "exercise not found"}, [], None
                card = kb.to_card(entry)
                return {"ok": True}, [{"type": "video_card", "card": card}], None
            except Exception as exc:
                return {"error": str(exc)}, [], None

        if name == "create_coaching_session":
            try:
                import asyncio
                import tavus_client
                result = asyncio.get_event_loop().run_in_executor(
                    None,
                    tavus_client.create_conversation,
                    f"Guided exercise session: {', '.join(inputs.get('exercise_names', []))}",
                    f"Hi {inputs.get('patient_name', 'there')}, let's begin your session.",
                    inputs.get("patient_name", "there"),
                )
                return {"ok": True}, [{"type": "tavus_session_pending"}], None
            except Exception as exc:
                return {"error": str(exc)}, [], None

        if name == "flag_symptom":
            return (
                {"ok": True, "flagged": True},
                [],
                "checkin",
            )

        if name == "complete_session":
            return (
                {"ok": True, "completed": inputs.get("completed_exercises", [])},
                [],
                "checkin",
            )

        return {"error": f"unknown tool {name}"}, [], None
