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
from typing import AsyncIterator, Literal

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
