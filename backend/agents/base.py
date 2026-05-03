"""
Abstract interface for any coding agent that updates the rehab protocol repo.

Concrete implementations (CursorGitHubAgent, CursorApiAgent, CachedReplayAgent,
MockAgent) live in sibling modules. The rest of the backend talks ONLY to this
interface, so the provider can be swapped via the AGENT_PROVIDER env var without
touching FastAPI handlers, frontend, or any caller code.

Why modular: the Cursor cloud agent integration is the riskiest unknown in the
build. The invocation path may pivot (GitHub @cursor mention vs Cursor API vs
cached replay vs mocked dev runs) and we don't want a pivot to cascade.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Literal

TraceEventType = Literal[
    "agent_started",
    "tool_call",
    "file_read",
    "file_edit",
    "branch_created",
    "commit_created",
    "pr_opened",
    "agent_completed",
    "agent_failed",
]


@dataclass
class TraceEvent:
    """A single event in an agent's execution trace.

    Stable across all agent implementations — frontend renders these uniformly.
    """
    type: TraceEventType
    timestamp: float          # unix seconds since invocation start
    label: str                # human-readable line for the trace panel
    payload: dict = field(default_factory=dict)


@dataclass
class AgentInvocation:
    """Result of invoking an agent."""
    invocation_id: str
    pr_url: str | None
    branch: str | None
    artifacts: list[dict] = field(default_factory=list)


@dataclass
class InvocationRequest:
    """Inputs handed to a CodingAgent.

    Provider-agnostic — concrete implementations decide HOW to deliver these
    to the underlying agent (issue body, API payload, etc.).
    """
    repo: str                          # e.g., "AndreChuabio/rehab-protocols-andre"
    prompt: str                        # natural-language task for the agent
    context_files: dict[str, str]      # path -> content (written to repo before invocation)
    flow: Literal["weekly_plan", "symptom_adjustment"] = "weekly_plan"


class CodingAgent(ABC):
    """Abstract base for any coding agent that updates the protocol repo.

    Two responsibilities:
      1. invoke()       — trigger the agent, return when PR is ready
      2. stream_trace() — yield TraceEvents (live or replayed) for the UI

    Implementations are obtained from the factory in agents/__init__.py.
    """

    name: str = "abstract"

    @abstractmethod
    async def invoke(self, request: InvocationRequest) -> AgentInvocation:
        """Run the agent end-to-end. Returns the invocation handle once a PR exists."""
        ...

    @abstractmethod
    async def stream_trace(self, invocation_id: str) -> AsyncIterator[TraceEvent]:
        """Yield trace events for the given invocation.

        Live agents poll their backing system; replay agents read cached JSON
        and pace events by their captured timestamps. Either way, the consumer
        sees the same shape.
        """
        ...
        # `yield` keeps this an async generator at the type level
        if False:  # pragma: no cover
            yield  # type: ignore[unreachable]


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
    # artifact shapes: {"type": "pr", "url": ...} | {"type": "video", "url": ...}
    #                  {"type": "video_card", "card": ...}


class PatientAgent(ABC):
    """Abstract base for domain-specific patient interaction agents.

    Unlike CodingAgent (which edits repos), PatientAgent owns a phase of
    the patient journey. Implementations handle conversation, persist state
    to user_store, and signal routing via PatientResponse.next_agent.
    """

    name: str = "abstract_patient"

    @abstractmethod
    async def handle(self, request: PatientRequest) -> PatientResponse:
        """Process a patient interaction. Save state, return response + routing."""
        ...

    @abstractmethod
    def can_handle(self, request: PatientRequest) -> bool:
        """Return True if this agent should handle the given request.

        Used by SessionManagerAgent for dynamic routing.
        """
        ...
