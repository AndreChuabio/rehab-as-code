"""Async decorator + writer for pipeline_runs.

Wraps an agent's entry function. Writes one row per call with timing,
status, model, token usage, decision, and a PHI-redacted output summary.

Design choices:
  * Fire-and-forget INSERT via asyncio.create_task — observability MUST
    NOT block /patient/interact. A failing writer logs to stdout and
    moves on; the user-facing path never sees an exception from here.
  * Gated by OBSERVABILITY_ENABLED env flag (default off). Lets us land
    the decorator wired into every agent without flipping logging on
    until we've smoked the writer in preview.
  * PHI rule: the caller decides what makes it into output_summary via
    the `summarize` callback (kwarg of trace_agent). Default = nothing.
    Never accept raw prompts or raw responses.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
from datetime import datetime, timezone
from functools import wraps
from typing import Any, Callable

from .run_context import RunContext, get_run_context, next_step

logger = logging.getLogger(__name__)

OBSERVABILITY_ENABLED_ENV = "OBSERVABILITY_ENABLED"
_PII_REGEX = re.compile(
    r"(?:[\w._%+-]+@[\w.-]+\.[A-Za-z]{2,}"        # email
    r"|\+?\d{3,}[-.\s]?\d{3,}[-.\s]?\d{3,})",      # phone-ish
)
_ERROR_MESSAGE_MAX = 500


def _enabled() -> bool:
    return os.environ.get(OBSERVABILITY_ENABLED_ENV, "0") == "1"


def _redact(text: str | None) -> str | None:
    if not text:
        return text
    truncated = text[:_ERROR_MESSAGE_MAX]
    return _PII_REGEX.sub("[redacted]", truncated)


def _extract_anthropic_tokens(result: Any) -> tuple[int | None, int | None]:
    """Anthropic SDK responses carry .usage.input_tokens / .output_tokens.
    Best-effort extraction — silently falls back to (None, None) for
    anything else (raw dicts, plain strings, classifier results)."""
    try:
        usage = getattr(result, "usage", None)
        if usage is None and isinstance(result, dict):
            usage = result.get("usage")
        if usage is None:
            return None, None
        if isinstance(usage, dict):
            return usage.get("input_tokens"), usage.get("output_tokens")
        return getattr(usage, "input_tokens", None), getattr(usage, "output_tokens", None)
    except Exception:
        return None, None


async def _write_run_row(payload: dict[str, Any]) -> None:
    """Write one pipeline_runs row. All exceptions swallowed + logged.

    Sync DB call wrapped in run_in_executor so we don't block the loop
    waiting on Postgres. The caller already wrapped THIS coroutine in
    asyncio.create_task, so even this whole function is fire-and-forget.
    """
    try:
        from db import DbConfigError, get_conn
    except ImportError:
        return
    try:
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, _insert_sync, payload)
    except DbConfigError:
        # No DATABASE_URL — local dev without Supabase. Quietly skip.
        return
    except Exception as exc:
        logger.warning("pipeline_runs insert failed: %s", exc)


def _insert_sync(payload: dict[str, Any]) -> None:
    from db import get_conn

    cols = [
        "request_id", "patient_uid", "agent", "step_index", "status",
        "started_at", "duration_ms", "model", "tokens_in", "tokens_out",
        "decision", "output_summary", "error_class", "error_message",
        "protocol_id",
    ]
    placeholders = ", ".join(["%s"] * len(cols))
    values = [
        payload["request_id"], payload["patient_uid"], payload["agent"],
        payload["step_index"], payload["status"], payload["started_at"],
        payload["duration_ms"], payload.get("model"),
        payload.get("tokens_in"), payload.get("tokens_out"),
        payload.get("decision"),
        json.dumps(payload["output_summary"]) if payload.get("output_summary") is not None else None,
        payload.get("error_class"), payload.get("error_message"),
        payload.get("protocol_id"),
    ]
    sql = (
        f"INSERT INTO pipeline_runs ({', '.join(cols)}) "
        f"VALUES ({placeholders})"
    )
    with get_conn(autocommit=True) as conn, conn.cursor() as cur:
        cur.execute(sql, values)


def _insert_sync_safe(payload: dict[str, Any]) -> None:
    """Sync sibling of _write_run_row for sync agents (called via
    asyncio.to_thread from the orchestrator). Already on a worker thread
    so we just call the DB without going back through the event loop.
    All exceptions swallowed."""
    try:
        _insert_sync(payload)
    except Exception as exc:
        logger.warning("pipeline_runs sync insert failed: %s", exc)


def trace_sync(
    agent_name: str,
    model: str | None = None,
    *,
    summarize: Callable[[Any], dict[str, Any] | None] | None = None,
    decision_from: Callable[[Any], str | None] | None = None,
):
    """Decorator for SYNC agent entry functions.

    Same contract as trace_agent (the async one), but for the agents that
    are still sync and called via asyncio.to_thread from the orchestrator
    (researcher, trend_analyst, evaluator, planner, safety_reviewer,
    diff_narrator, symptom_classifier).

    The agent function runs on a worker thread; the DB write also goes
    via the same thread (sync), so we never block the event loop. The
    write is best-effort — exceptions log + swallow, never propagate
    back to the user-facing path.
    """
    def deco(fn: Callable[..., Any]):
        @wraps(fn)
        def wrapped(*args, **kwargs):
            if not _enabled():
                return fn(*args, **kwargs)
            ctx: RunContext | None = get_run_context()
            if ctx is None:
                return fn(*args, **kwargs)
            step = next_step()
            t0 = time.monotonic()
            started_at = datetime.now(timezone.utc).isoformat()
            row: dict[str, Any] = {
                "request_id": ctx.request_id,
                "patient_uid": ctx.patient_uid,
                "agent": agent_name,
                "step_index": step,
                "started_at": started_at,
                "model": model,
            }
            try:
                result = fn(*args, **kwargs)
                row["duration_ms"] = int((time.monotonic() - t0) * 1000)
                row["status"] = "ok"
                tokens_in, tokens_out = _extract_anthropic_tokens(result)
                row["tokens_in"] = tokens_in
                row["tokens_out"] = tokens_out
                if summarize is not None:
                    try:
                        row["output_summary"] = summarize(result)
                    except Exception as exc:
                        row["output_summary"] = {"_summary_error": str(exc)[:120]}
                if decision_from is not None:
                    try:
                        row["decision"] = decision_from(result)
                    except Exception:
                        row["decision"] = None
                _insert_sync_safe(row)
                return result
            except Exception as exc:
                row["duration_ms"] = int((time.monotonic() - t0) * 1000)
                row["status"] = "error"
                row["error_class"] = type(exc).__name__
                row["error_message"] = _redact(str(exc))
                _insert_sync_safe(row)
                raise
        return wrapped
    return deco


def trace_agent(
    agent_name: str,
    model: str | None = None,
    *,
    summarize: Callable[[Any], dict[str, Any] | None] | None = None,
    decision_from: Callable[[Any], str | None] | None = None,
):
    """Async decorator that records one pipeline_runs row per call.

    Usage:
        @trace_agent("planner", model="claude-sonnet-4-6",
                     summarize=lambda res: {"exercise_count": len(res.get("exercises", []))},
                     decision_from=lambda res: res.get("evaluator_decision"))
        async def run(...):
            ...

    summarize:    callable that returns the redacted output summary jsonb.
                  Returning None = nothing logged to output_summary.
    decision_from: optional callable that returns 'progress' | 'hold' |
                  'regress' (or any short string) for the decision column.
    """
    def deco(fn: Callable[..., Any]):
        @wraps(fn)
        async def wrapped(*args, **kwargs):
            if not _enabled():
                return await fn(*args, **kwargs)
            ctx: RunContext | None = get_run_context()
            if ctx is None:
                # No request context (e.g., direct test invocation).
                # Run the agent normally; just don't log.
                return await fn(*args, **kwargs)
            step = next_step()
            t0 = time.monotonic()
            started_at = datetime.now(timezone.utc).isoformat()
            row: dict[str, Any] = {
                "request_id": ctx.request_id,
                "patient_uid": ctx.patient_uid,
                "agent": agent_name,
                "step_index": step,
                "started_at": started_at,
                "model": model,
            }
            try:
                result = await fn(*args, **kwargs)
                row["duration_ms"] = int((time.monotonic() - t0) * 1000)
                row["status"] = "ok"
                tokens_in, tokens_out = _extract_anthropic_tokens(result)
                row["tokens_in"] = tokens_in
                row["tokens_out"] = tokens_out
                if summarize is not None:
                    try:
                        row["output_summary"] = summarize(result)
                    except Exception as exc:
                        row["output_summary"] = {"_summary_error": str(exc)[:120]}
                if decision_from is not None:
                    try:
                        row["decision"] = decision_from(result)
                    except Exception:
                        row["decision"] = None
                # Fire and forget. The decorator returns the original
                # result before the writer finishes — observability MUST
                # NOT slow the user-facing path.
                asyncio.create_task(_write_run_row(row))
                return result
            except Exception as exc:
                row["duration_ms"] = int((time.monotonic() - t0) * 1000)
                row["status"] = "error"
                row["error_class"] = type(exc).__name__
                row["error_message"] = _redact(str(exc))
                asyncio.create_task(_write_run_row(row))
                raise
        return wrapped
    return deco
