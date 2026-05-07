"""
Factory for patient-facing journey agents (intake, plan generation).

What lived here before: a `CodingAgent` factory keyed on AGENT_PROVIDER
(cursor_sdk / cursor_api / cursor_github / ag2 / cached_replay / mock) that
dispatched protocol writes through GitHub PRs. That whole surface is gone
post-PR-#62 — the chat path now writes directly to the Supabase `protocols`
table via chat_protocol_drafter, and PlanGenerationAgent does the same via
protocol_repo.save_pending. Clinicians approve drafts on /clinician.

What remains: PatientAgent — domain-specific agents that own a phase of
the patient journey (currently `intake` and `plan_generation`).
"""

from __future__ import annotations

from .base import (
    PatientAgent,
    PatientRequest,
    PatientResponse,
)

__all__ = [
    "PatientAgent",
    "PatientRequest",
    "PatientResponse",
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
      * session_manager -> Supabase JWT auth (backend/auth.py)
      * guided_video    -> in-browser MediaPipe form-check + /pose/session
      * checkin         -> /pose/session writes set_completion rows;
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
