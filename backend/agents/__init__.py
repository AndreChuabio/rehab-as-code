"""
Factory for the configured CodingAgent implementation.

Selection is driven by the AGENT_PROVIDER env var. Defaults to cached replay
(demo-safe). Switching providers is a config change, not a code change.

Supported values:
    ag2             -- live path: AG2 (AutoGen) multi-agent framework + Claude
                       pure Python, no Node/TypeScript dependency
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
    PatientAgent,
    PatientRequest,
    PatientResponse,
    TraceEvent,
    TraceEventType,
)

__all__ = [
    "AgentInvocation",
    "CodingAgent",
    "InvocationRequest",
    "PatientAgent",
    "PatientRequest",
    "PatientResponse",
    "TraceEvent",
    "TraceEventType",
    "get_agent",
    "get_patient_agent",
    "register_patient_agent",
]

# ── Patient agent registry ────────────────────────────────────────────────────

_PATIENT_REGISTRY: dict[str, type[PatientAgent]] = {}


def register_patient_agent(cls: type[PatientAgent]) -> type[PatientAgent]:
    """Class decorator that registers a PatientAgent in the factory."""
    _PATIENT_REGISTRY[cls.name] = cls
    return cls


def get_patient_agent(role: str) -> PatientAgent:
    """Return a PatientAgent instance by role name.

    Roles: intake, plan_generation.

    Lazy-imported on first call. Three roles from PR #34 (session_manager,
    guided_video, checkin) were removed when their responsibilities were
    absorbed by other systems:
      * session_manager → Supabase JWT auth (backend/auth.py)
      * guided_video    → in-browser MediaPipe form-check + /pose/session
      * checkin         → /pose/session writes set_completion rows;
                          coach_chat.fire_checkin_trigger covers narrative
    """
    if not _PATIENT_REGISTRY:
        from .intake_agent import IntakeAgent  # noqa: F401
        from .plan_generation_agent import PlanGenerationAgent  # noqa: F401

    if role not in _PATIENT_REGISTRY:
        raise ValueError(
            f"Unknown patient agent role {role!r}. "
            f"Expected one of: {', '.join(sorted(_PATIENT_REGISTRY.keys()))}"
        )
    return _PATIENT_REGISTRY[role]()


def get_agent(provider: str | None = None) -> CodingAgent:
    """Return the configured CodingAgent.

    Parameters
    ----------
    provider : str | None
        Override AGENT_PROVIDER env var. Useful for tests or per-request switching
        (e.g., live in dev path, cached_replay in demo path).
    """
    name = (provider or os.getenv("AGENT_PROVIDER") or "cached_replay").lower()

    if name == "ag2":
        from .ag2_agent import AG2Agent
        return AG2Agent()
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
        f"Expected one of: ag2, cursor_sdk, cursor_github, cursor_api, "
        f"cached_replay, mock."
    )
