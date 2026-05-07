"""Observability surface for the multi-agent pipeline.

Public API:
  set_run_context(request_id, patient_uid)   — middleware sets this per request
  get_run_context()                          — agents pull current context
  trace_agent(name, model)                   — async decorator that logs to pipeline_runs
"""

from .run_context import (
    attach_patient,
    clear_run_context,
    get_run_context,
    set_run_context,
)
from .trace import OBSERVABILITY_ENABLED_ENV, trace_agent, trace_sync

__all__ = [
    "OBSERVABILITY_ENABLED_ENV",
    "attach_patient",
    "clear_run_context",
    "get_run_context",
    "set_run_context",
    "trace_agent",
    "trace_sync",
]
