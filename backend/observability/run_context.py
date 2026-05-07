"""Per-request context for pipeline observability.

A single request_id correlates one /patient/interact (or /chat) call
across the multiple agents that fan out from it. ContextVars carry the
id through async boundaries without requiring every agent function
signature to grow a `request_id` parameter.

Set at FastAPI middleware (per request, alongside auth.uid resolution).
Read by the @trace_agent decorator when each agent runs.
"""

from __future__ import annotations

import uuid
from contextvars import ContextVar
from dataclasses import dataclass

_request_id: ContextVar[str | None] = ContextVar("rehab_request_id", default=None)
_patient_uid: ContextVar[str | None] = ContextVar("rehab_patient_uid", default=None)
_step_counter: ContextVar[int] = ContextVar("rehab_step_counter", default=0)


@dataclass(frozen=True)
class RunContext:
    request_id: str
    patient_uid: str | None
    step_index: int


def set_run_context(
    request_id: str | None = None,
    patient_uid: str | None = None,
) -> str:
    """Initialize the run context for a request. Returns the request_id.

    request_id auto-generated when caller passes None. patient_uid may be
    None for unauthenticated paths; trace_agent skips logging in that case.
    """
    rid = request_id or str(uuid.uuid4())
    _request_id.set(rid)
    _patient_uid.set(patient_uid)
    _step_counter.set(0)
    return rid


def clear_run_context() -> None:
    _request_id.set(None)
    _patient_uid.set(None)
    _step_counter.set(0)


def attach_patient(patient_uid: str | None) -> None:
    """Late-bind the resolved auth.uid() to the current request context.

    Middleware initializes the context before Depends(current_user_id)
    runs; handlers call this once the JWT has resolved so subsequent
    @trace_agent invocations get patient_uid in their pipeline_runs row.
    """
    _patient_uid.set(patient_uid)


def get_run_context() -> RunContext | None:
    rid = _request_id.get()
    if not rid:
        return None
    return RunContext(
        request_id=rid,
        patient_uid=_patient_uid.get(),
        step_index=_step_counter.get(),
    )


def next_step() -> int:
    """Bump and return the per-request step counter. Decorator uses this
    so agents are ordered as they execute (researcher=0, trend=1, etc.)."""
    n = _step_counter.get() + 1
    _step_counter.set(n)
    return n
