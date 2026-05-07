"""
Abstract interfaces for patient-facing journey agents.

The CodingAgent / TraceEvent / AgentInvocation surface that used to live
here was retired post-PR-#62 along with the cursor / ag2 / cached_replay
agent providers. Protocol writes now go straight to Supabase
(chat_protocol_drafter + protocol_repo.save_pending), so there is no need
for the abstract "open a PR somewhere" interface.

What remains: PatientAgent — owns a phase of the patient conversation
(intake, plan_generation), persists state to user_store, and signals
routing via PatientResponse.next_agent.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any


# ── Patient Journey Agents ────────────────────────────────────────────────────

@dataclass
class PatientRequest:
    """Input to any PatientAgent."""
    user_token: str               # internal UUID; empty string on first contact
    message: str                  # patient's raw message or trigger payload
    slack_user_id: str | None     # present when called from Slack
    metadata: dict[str, Any] = field(default_factory=dict)
    # metadata keys: conversation_history, image_url, completed_exercises, etc.


@dataclass
class PatientResponse:
    """Output from any PatientAgent."""
    agent_name: str
    message: str                  # text to surface to the patient
    next_agent: str | None        # routing signal; None = this agent is terminal
    data: dict[str, Any] = field(default_factory=dict)
    artifacts: list[dict] = field(default_factory=list)
    # artifact shapes: {"type": "pending_protocol", "id": ...}
    #                  {"type": "video", "url": ...}
    #                  {"type": "video_card", "card": ...}


class PatientAgent(ABC):
    """Abstract base for domain-specific patient interaction agents.

    PatientAgent owns a phase of the patient journey. Implementations handle
    conversation, persist state to user_store, and signal routing via
    PatientResponse.next_agent.
    """

    name: str = "abstract_patient"

    @abstractmethod
    async def handle(self, request: PatientRequest) -> PatientResponse:
        """Process a patient interaction. Save state, return response + routing."""
        ...

    @abstractmethod
    def can_handle(self, request: PatientRequest) -> bool:
        """Return True if this agent should handle the given request."""
        ...
