"""
SessionManagerAgent — entry point for every patient interaction.

Responsibilities:
1. Resolve the patient's stable userID (Slack ID → UUID, or create new user)
2. Load the user's current record
3. Classify intent and return next_agent for routing

Uses Claude Haiku for fast, cheap intent classification. Does NOT respond
directly to the patient — it routes.
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

logger = logging.getLogger(__name__)

ROUTING_PROMPT = """You are a routing classifier for a rehabilitation app.
Given a patient message and their record state, classify the intent and choose the next agent.

Agent options:
- intake         → patient has no intake on record, OR explicitly wants to redo intake
- plan_generation → patient explicitly asks to generate/update their plan or protocol
- guided_video   → patient wants to start a session, see exercises, or do a workout
- checkin        → patient just finished a session, reports pain/progress, or wants to log today

Rules:
- If no intake exists: ALWAYS return "intake" regardless of message
- If message contains "check in", "just finished", "done today", "pain level", "session was": return "checkin"
- If message contains "generate plan", "new protocol", "update my plan", "next week": return "plan_generation"
- Default for returning patients: "guided_video"

Respond with JSON only: {"next_agent": "...", "reason": "one sentence"}"""


@register_patient_agent
class SessionManagerAgent(PatientAgent):
    """Authenticates user and routes to the correct domain agent."""

    name = "session_manager"

    def can_handle(self, request: PatientRequest) -> bool:
        return True  # always the first agent

    async def handle(self, request: PatientRequest) -> PatientResponse:
        # 1. Resolve token
        token = request.user_token or ""

        if not token and request.slack_user_id:
            token = user_store.lookup_by_slack_id(request.slack_user_id) or ""

        if not token:
            token = user_store.create_user(slack_user_id=request.slack_user_id)
            logger.info("SessionManager: created new user %s", token)
        elif request.slack_user_id:
            rec = user_store.load_user(token)
            if rec and not rec.get("slack_user_id"):
                user_store.link_slack_id(token, request.slack_user_id)

        # 2. Load record
        record = user_store.load_user(token) or {}
        has_intake = record.get("intake") is not None

        # 3. Classify intent
        next_agent = self._classify(request.message, has_intake, record)

        return PatientResponse(
            agent_name=self.name,
            message="",
            next_agent=next_agent,
            data={"user_token": token, "user_record": record, "has_intake": has_intake},
        )

    def _classify(self, message: str, has_intake: bool, record: dict) -> str:
        if not has_intake:
            return "intake"

        api_key = os.getenv("ANTHROPIC_API_KEY")
        if not api_key:
            return "guided_video"

        try:
            client = anthropic.Anthropic(api_key=api_key)
            context = json.dumps({
                "has_intake": has_intake,
                "current_phase": (record.get("protocol_state") or {}).get("current_phase"),
                "session_count": len(record.get("session_history", [])),
            })
            resp = client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=100,
                system=ROUTING_PROMPT,
                messages=[{"role": "user", "content": f"Message: {message}\nState: {context}"}],
            )
            block = resp.content[0]
            raw = block.text.strip() if hasattr(block, "text") else "{}"
            parsed = json.loads(raw)
            return parsed.get("next_agent", "guided_video")
        except Exception as exc:
            logger.warning("SessionManager classification failed (%s); defaulting to guided_video", exc)
            return "guided_video"
