"""
Factory for the configured CodingAgent implementation.

Selection is driven by the AGENT_PROVIDER env var. Defaults to cached replay
(demo-safe). Switching providers is a config change, not a code change.

Supported values:
    cursor_sdk      -- live path: Cursor TypeScript SDK via Node helper
                       (orchestrator/), supports parent + named sub-agents
    cursor_github   -- fallback path: @cursor GitHub mention via gh CLI
    cursor_api      -- stub: direct REST (kept for future use)
    cached_replay   -- demo path: replay pre-captured trace JSON
    mock            -- dev path: hardcoded fake PR
"""

from __future__ import annotations

import os

from .base import (
    AgentInvocation,
    CodingAgent,
    InvocationRequest,
    TraceEvent,
    TraceEventType,
)

__all__ = [
    "AgentInvocation",
    "CodingAgent",
    "InvocationRequest",
    "TraceEvent",
    "TraceEventType",
    "get_agent",
]


def get_agent(provider: str | None = None) -> CodingAgent:
    """Return the configured CodingAgent.

    Parameters
    ----------
    provider : str | None
        Override AGENT_PROVIDER env var. Useful for tests or per-request switching
        (e.g., live in dev path, cached_replay in demo path).
    """
    name = (provider or os.getenv("AGENT_PROVIDER") or "cached_replay").lower()

    if name == "cursor_sdk":
        from .cursor_sdk import CursorSdkAgent
        return CursorSdkAgent()
    if name == "cursor_github":
        from .cursor_github import CursorGitHubAgent
        return CursorGitHubAgent()
    if name == "cursor_api":
        from .cursor_api import CursorApiAgent
        return CursorApiAgent()
    if name == "cached_replay":
        from .cached_replay import CachedReplayAgent
        return CachedReplayAgent()
    if name == "mock":
        from .mock import MockAgent
        return MockAgent()

    raise ValueError(
        f"Unknown AGENT_PROVIDER {name!r}. "
        f"Expected one of: cursor_sdk, cursor_github, cursor_api, "
        f"cached_replay, mock."
    )
