"""
langfuse_client.py - process-singleton Langfuse client + Anthropic OTel
auto-instrumentation for rehab-as-code.

Layered intentionally on top of `backend/observability/` (which writes
structured pipeline_runs rows for the admin dashboard). Langfuse adds:
  * raw prompt/response inspection (PHI-redacted via mask callback)
  * cross-agent trace hierarchy
  * latency + cost dashboards out-of-the-box

Both layers run in parallel - they answer different questions.

Kill-switch: LANGFUSE_ENABLED != "true" -> the whole module is a no-op.
The patient-facing path NEVER blocks on Langfuse: client init failures,
network errors, and flush failures all log + drop per the production-mode
no-silent-fallback contract (errors are surfaced via WARNING logs, not
silently swallowed - operators see them but users don't).

PHI handling: the `_mask` callback is registered with the Langfuse
client. It rewrites user-role message content to a redacted summary
(`[redacted user content, 412 chars]`) so prompt SIZE is observable but
the actual freetext (intake, injury description, pain notes) never
leaves the function. system prompts and tool inputs/outputs pass
through (versioned, non-PHI by design).

Trace identity: the existing observability_request_context middleware
sets a per-request UUID via `RunContext.request_id`. We attach Langfuse
spans under a session_id derived from a SHA-256 hash of patient_uid so
Langfuse's session view stays PHI-safe. request_id is also added as
metadata so admin dashboard rows + Langfuse traces are joinable.

Vercel Fluid Compute: client is process-singleton. flush() is called
in the FastAPI middleware finally-block per request so traces land
even if the function instance freezes between requests.
"""
from __future__ import annotations

import contextlib
import hashlib
import logging
import os
import threading
from typing import Any, Iterator

logger = logging.getLogger(__name__)


LANGFUSE_ENABLED_ENV = "LANGFUSE_ENABLED"
LANGFUSE_HOST_ENV = "LANGFUSE_HOST"
LANGFUSE_PUBLIC_KEY_ENV = "LANGFUSE_PUBLIC_KEY"
LANGFUSE_SECRET_KEY_ENV = "LANGFUSE_SECRET_KEY"


_client: Any = None
_init_lock = threading.Lock()
_init_attempted = False
_anthropic_instrumented = False


def is_enabled() -> bool:
    """True iff LANGFUSE_ENABLED env is set to "true" (case-insensitive).

    Cheaper than calling get_client() when the caller just wants a gate.
    """
    return os.environ.get(LANGFUSE_ENABLED_ENV, "false").lower() == "true"


def _hash_uid(uid: str | None) -> str | None:
    """Stable PHI-safe hash for use as session_id / user_id in Langfuse.

    Same shortened-SHA256 convention as backend/agents observability logs
    so traces and DB rows can be joined by hash without exposing the raw
    auth.uid().
    """
    if not uid:
        return None
    return hashlib.sha256(uid.encode("utf-8")).hexdigest()[:16]


def _mask(*, data: Any) -> Any:
    """Langfuse PHI redaction callback.

    Walks the input recursively. Rewrites any dict whose `role == "user"`
    to replace `content` with a length-only summary. Leaves system
    prompts, assistant outputs, and tool_use inputs intact - those are
    structured / versioned and don't carry freetext PHI by current schema.

    Always returns SOMETHING (never raises). On unexpected shapes we
    return the input untouched and log once.
    """
    try:
        return _mask_walk(data)
    except Exception as exc:
        logger.warning("langfuse mask failed, dropping data: %s", exc)
        return {"_mask_error": str(exc)[:120]}


def _mask_walk(value: Any) -> Any:
    if isinstance(value, list):
        return [_mask_walk(item) for item in value]
    if isinstance(value, dict):
        if value.get("role") == "user":
            return _redact_user_message(value)
        return {k: _mask_walk(v) for k, v in value.items()}
    return value


def _redact_user_message(msg: dict) -> dict:
    """Replace freetext user content with a length-only marker."""
    content = msg.get("content")
    if isinstance(content, str):
        return {**msg, "content": f"[redacted user content, {len(content)} chars]"}
    if isinstance(content, list):
        masked = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                text = block.get("text") or ""
                masked.append({**block, "text": f"[redacted, {len(text)} chars]"})
            else:
                masked.append(block)
        return {**msg, "content": masked}
    return msg


def get_client() -> Any:
    """Return the Langfuse singleton, or None if disabled / unconfigured.

    Lazy init - first call wires up the client and the OTel Anthropic
    instrumentor. Init failures log + return None; subsequent calls
    short-circuit to None without re-attempting (avoids log spam).
    """
    global _client, _init_attempted, _anthropic_instrumented

    if not is_enabled():
        return None
    if _init_attempted:
        return _client

    with _init_lock:
        if _init_attempted:
            return _client
        _init_attempted = True

        try:
            from langfuse import Langfuse  # type: ignore[import-not-found]
        except ImportError as exc:
            logger.warning(
                "LANGFUSE_ENABLED=true but `langfuse` package missing: %s. "
                "Install: pip install 'langfuse>=3'",
                exc,
            )
            _client = None
            return None

        host = os.environ.get(LANGFUSE_HOST_ENV, "").strip()
        public_key = os.environ.get(LANGFUSE_PUBLIC_KEY_ENV, "").strip()
        secret_key = os.environ.get(LANGFUSE_SECRET_KEY_ENV, "").strip()
        if not (host and public_key and secret_key):
            logger.warning(
                "LANGFUSE_ENABLED=true but %s/%s/%s incomplete; client disabled.",
                LANGFUSE_HOST_ENV, LANGFUSE_PUBLIC_KEY_ENV, LANGFUSE_SECRET_KEY_ENV,
            )
            _client = None
            return None

        try:
            _client = Langfuse(
                host=host,
                public_key=public_key,
                secret_key=secret_key,
                mask=_mask,
            )
        except Exception as exc:
            logger.warning("langfuse client init failed: %s", exc)
            _client = None
            return None

        if not _anthropic_instrumented:
            try:
                from opentelemetry.instrumentation.anthropic import (  # type: ignore[import-not-found]
                    AnthropicInstrumentor,
                )
                AnthropicInstrumentor().instrument()
                _anthropic_instrumented = True
                logger.info("langfuse: anthropic OTel instrumentation enabled")
            except ImportError:
                logger.warning(
                    "opentelemetry-instrumentation-anthropic not installed; "
                    "Langfuse will get only manual spans, no auto-captured "
                    "messages.create generations."
                )
            except Exception as exc:
                logger.warning("anthropic OTel instrumentation failed: %s", exc)

        logger.info("langfuse client ready (host=%s)", host)
        return _client


def reset_for_tests() -> None:
    """Reset the singleton so tests can re-init with different env vars.

    Test-only escape hatch. Production code must not call this.
    """
    global _client, _init_attempted, _anthropic_instrumented
    with _init_lock:
        _client = None
        _init_attempted = False
        _anthropic_instrumented = False


@contextlib.contextmanager
def request_span(
    name: str,
    *,
    request_id: str | None = None,
    patient_uid: str | None = None,
    **metadata: Any,
) -> Iterator[Any]:
    """Wrap a request handler in a Langfuse trace span.

    Yields the span (or None when Langfuse is disabled / fails) so callers
    can attach more attributes if they want. Either way the surrounding
    code path runs identically; observability is purely additive.

    The session_id and user_id we attach to the trace are SHA-256 hashes
    of patient_uid (matching the existing log convention). request_id is
    stored as metadata so admin dashboard rows are joinable.
    """
    client = get_client()
    if client is None:
        yield None
        return

    span = None
    try:
        span_cm = client.start_as_current_span(name=name)
    except Exception as exc:
        logger.warning("langfuse start_as_current_span failed: %s", exc)
        yield None
        return

    try:
        with span_cm as span:
            try:
                if hasattr(span, "update_trace"):
                    span.update_trace(
                        session_id=_hash_uid(patient_uid),
                        user_id=_hash_uid(patient_uid),
                        metadata={"request_id": request_id, **metadata},
                    )
                elif hasattr(client, "update_current_trace"):
                    client.update_current_trace(
                        session_id=_hash_uid(patient_uid),
                        user_id=_hash_uid(patient_uid),
                        metadata={"request_id": request_id, **metadata},
                    )
            except Exception as exc:
                logger.warning("langfuse update_trace failed: %s", exc)
            yield span
    except Exception as exc:
        logger.warning("langfuse request_span body failed: %s", exc)
        yield None


@contextlib.contextmanager
def span(name: str, **metadata: Any) -> Iterator[Any]:
    """Generic child span context manager (chat/draft/tavus surfaces).

    No-op when Langfuse is disabled.
    """
    client = get_client()
    if client is None:
        yield None
        return
    try:
        with client.start_as_current_span(name=name) as s:
            try:
                if metadata and hasattr(s, "update"):
                    s.update(metadata=metadata)
            except Exception as exc:
                logger.warning("langfuse span update failed: %s", exc)
            yield s
    except Exception as exc:
        logger.warning("langfuse span %s failed: %s", name, exc)
        yield None


def flush() -> None:
    """Flush buffered events. Safe + no-op when client is None."""
    client = get_client()
    if client is None:
        return
    try:
        client.flush()
    except Exception as exc:
        logger.warning("langfuse flush failed: %s", exc)
