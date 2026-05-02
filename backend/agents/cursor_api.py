"""
Stub: direct Cursor cloud agent invocation via Cursor's API.

Activate this provider once API access + auth details are confirmed at the
sponsor table. Implements the same CodingAgent interface as cursor_github,
so flipping AGENT_PROVIDER=cursor_api is the only change needed.

Until then, raises NotImplementedError on invoke() so misconfiguration fails loud.
"""

from __future__ import annotations

import logging
import os
from typing import AsyncIterator

from .base import AgentInvocation, CodingAgent, InvocationRequest, TraceEvent

logger = logging.getLogger(__name__)


class CursorApiAgent(CodingAgent):
    """Direct Cursor API client. Stub until access confirmed."""

    name = "cursor_api"

    def __init__(self) -> None:
        self.api_key = os.getenv("CURSOR_API_KEY")
        self.base_url = os.getenv("CURSOR_API_BASE", "https://api.cursor.com")

    async def invoke(self, request: InvocationRequest) -> AgentInvocation:
        # TODO: replace with real API call once endpoints + auth are documented.
        # Expected shape (subject to change):
        #   POST {base_url}/v1/agents/runs
        #   { "repo": request.repo, "prompt": request.prompt,
        #     "context_files": request.context_files }
        #   -> { "run_id": "...", "pr_url": "...", "branch": "..." }
        raise NotImplementedError(
            "CursorApiAgent: API access not yet confirmed. "
            "Set AGENT_PROVIDER=cursor_github to use the GitHub @cursor path instead."
        )

    async def stream_trace(self, invocation_id: str) -> AsyncIterator[TraceEvent]:
        raise NotImplementedError
        if False:  # pragma: no cover
            yield  # type: ignore[unreachable]
