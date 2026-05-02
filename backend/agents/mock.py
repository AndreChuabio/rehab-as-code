"""
MockAgent — deterministic fake for dev work without hitting any external API.

Use AGENT_PROVIDER=mock when iterating on the frontend, the SSE plumbing, or
the FastAPI handlers. Returns a fixed PR URL and emits a short scripted trace.
"""

from __future__ import annotations

import asyncio
import uuid
from typing import AsyncIterator

from .base import AgentInvocation, CodingAgent, InvocationRequest, TraceEvent

_SCRIPT: list[tuple[str, str]] = [
    ("agent_started",   "mock agent started"),
    ("file_read",       "read protocol.yaml"),
    ("file_read",       "read protocol-library/post-acl-week-4.yaml"),
    ("branch_created",  "git checkout -b mock-week-4"),
    ("file_edit",       "edit protocol.yaml"),
    ("commit_created",  "Week 4 progression: load tolerance + neuro"),
    ("pr_opened",       "PR #99 opened"),
    ("agent_completed", "mock invocation complete"),
]


class MockAgent(CodingAgent):
    """Fixed-script fake agent for local development."""

    name = "mock"

    def __init__(self, step_delay_sec: float = 0.5) -> None:
        self.step_delay_sec = step_delay_sec
        self._invocations: dict[str, InvocationRequest] = {}

    async def invoke(self, request: InvocationRequest) -> AgentInvocation:
        invocation_id = str(uuid.uuid4())
        self._invocations[invocation_id] = request
        return AgentInvocation(
            invocation_id=invocation_id,
            pr_url="https://github.com/example/rehab-protocols-andre/pull/99",
            branch="mock-week-4",
            artifacts=[{"type": "mock", "note": "no real PR was created"}],
        )

    async def stream_trace(self, invocation_id: str) -> AsyncIterator[TraceEvent]:
        if invocation_id not in self._invocations:
            raise KeyError(invocation_id)
        for i, (event_type, label) in enumerate(_SCRIPT):
            await asyncio.sleep(self.step_delay_sec)
            yield TraceEvent(
                type=event_type,  # type: ignore[arg-type]
                timestamp=i * self.step_delay_sec,
                label=label,
            )
