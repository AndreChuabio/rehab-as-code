"""
tavus_repo - read/write helpers for the `public.tavus_sessions` table.

Mirrors the pattern in session_repo.py / protocol_repo.py: thin module
isolated to one table so RLS / schema changes don't bleed into user_store.
Writes go through a service-role DSN; reads honour the patient/clinician RLS
declared in supabase/migrations/<ts>_tavus_sessions.sql.

All public helpers raise TavusRepoError on missing DATABASE_URL or psycopg
import failure - no silent fallbacks.

PHI hygiene: rows hold only conversation handles + status enums + timestamps.
No transcript, no greeting text, no patient-entered content.
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger(__name__)


class TavusRepoError(RuntimeError):
    """Raised when a tavus_repo operation cannot complete."""


def _conn():
    """Yield a pooled connection (autocommit=False; caller commits explicitly).

    Routes through backend.db.get_conn. Surface DbConfigError as
    TavusRepoError so callers don't need to import db's exception.
    """
    try:
        from db import DbConfigError, get_conn
    except ImportError as exc:
        raise TavusRepoError(
            "tavus_repo requires backend.db. "
            "Run: pip install 'psycopg[binary]>=3.2' 'psycopg-pool>=3.2'"
        ) from exc
    try:
        return get_conn(autocommit=False)
    except DbConfigError as exc:
        raise TavusRepoError(str(exc)) from exc


def _serialize(row: dict[str, Any]) -> dict[str, Any]:
    """Coerce DB types to JSON-friendly values."""
    out = dict(row)
    if out.get("id") is not None:
        out["id"] = str(out["id"])
    for key in ("created_at", "expires_at", "ended_at"):
        v = out.get(key)
        if v is not None and not isinstance(v, str):
            out[key] = v.isoformat()
    return out


def insert_active(
    *,
    token: str,
    conversation_id: str,
    conversation_url: str | None,
    replica_id: str | None,
    persona_id: str | None,
    expires_at: str | None,
    session_ref: str | None = None,
) -> dict[str, Any]:
    """Insert a new tavus_sessions row in `active` state. Returns the row.

    `session_ref` is the opaque per-conversation reference the BYO-LLM proxy
    uses to recover this patient; it is persisted NULLable (legacy rows and
    callers that don't mint one stay valid).
    """
    if not token or not conversation_id:
        raise TavusRepoError("token and conversation_id are required")

    with _conn() as c, c.cursor() as cur:
        cur.execute(
            "INSERT INTO tavus_sessions "
            "(token, conversation_id, conversation_url, replica_id, persona_id, "
            " status, expires_at, session_ref) "
            "VALUES (%s, %s, %s, %s, %s, 'active', %s, %s) "
            "RETURNING id, token, conversation_id, conversation_url, "
            " replica_id, persona_id, status, created_at, expires_at, ended_at, "
            " session_ref",
            (token, conversation_id, conversation_url, replica_id, persona_id,
             expires_at, session_ref),
        )
        row = cur.fetchone()
        c.commit()
    return _serialize(row)


def get_token_by_session_ref(session_ref: str) -> str | None:
    """Return the patient token for an ACTIVE row matching session_ref.

    Used by the BYO-LLM proxy to map a custom-LLM call back to its patient.
    Returns None when no active row matches. Raises TavusRepoError on a
    missing DATABASE_URL (consistent with the other readers).
    """
    if not session_ref:
        return None
    with _conn() as c, c.cursor() as cur:
        cur.execute(
            "SELECT token FROM tavus_sessions "
            "WHERE session_ref = %s AND status = 'active' "
            "ORDER BY created_at DESC LIMIT 1",
            (session_ref,),
        )
        row = cur.fetchone()
    if not row:
        return None
    return row["token"] if isinstance(row, dict) else row[0]


def get_token_by_conversation_id(conversation_id: str) -> str | None:
    """Return the patient token for an ACTIVE row matching conversation_id.

    Preferred proxy lookup when Tavus forwards the conversation_id to the
    custom-LLM call. Returns None when no active row matches. Raises
    TavusRepoError on a missing DATABASE_URL.
    """
    if not conversation_id:
        return None
    with _conn() as c, c.cursor() as cur:
        cur.execute(
            "SELECT token FROM tavus_sessions "
            "WHERE conversation_id = %s AND status = 'active' "
            "ORDER BY created_at DESC LIMIT 1",
            (conversation_id,),
        )
        row = cur.fetchone()
    if not row:
        return None
    return row["token"] if isinstance(row, dict) else row[0]


def list_recent(token: str, *, limit: int = 5) -> list[dict[str, Any]]:
    """Most-recent-first list of tavus_sessions for a patient."""
    if not token:
        raise TavusRepoError("token is required")
    if limit < 1:
        limit = 1
    if limit > 50:
        limit = 50
    with _conn() as c, c.cursor() as cur:
        cur.execute(
            "SELECT id, token, conversation_id, conversation_url, replica_id, "
            " persona_id, status, created_at, expires_at, ended_at "
            "FROM tavus_sessions "
            "WHERE token = %s "
            "ORDER BY created_at DESC "
            "LIMIT %s",
            (token, limit),
        )
        rows = cur.fetchall() or []
    return [_serialize(r) for r in rows]


def end_session(*, session_id: str, token: str) -> dict[str, Any]:
    """Mark a tavus_sessions row as `ended`. Scoped by (id, token).

    Idempotent on repeat calls: a row already in `ended` returns its current
    state without raising. Returns the row.

    Raises TavusRepoError("not found") if the row doesn't exist or doesn't
    belong to this patient.
    """
    if not session_id or not token:
        raise TavusRepoError("session_id and token are required")

    with _conn() as c, c.cursor() as cur:
        cur.execute(
            "UPDATE tavus_sessions "
            "SET status = 'ended', ended_at = COALESCE(ended_at, NOW()) "
            "WHERE id = %s AND token = %s "
            "RETURNING id, token, conversation_id, conversation_url, replica_id, "
            " persona_id, status, created_at, expires_at, ended_at",
            (session_id, token),
        )
        row = cur.fetchone()
        c.commit()
    if not row:
        raise TavusRepoError("not found")
    return _serialize(row)


def is_active(row: dict[str, Any]) -> bool:
    """Helper for the frontend "Continue last" affordance.

    Active means status='active' AND (expires_at IS NULL OR expires_at > now).
    Used to decide whether to show "Continue last session" vs hide it.
    """
    if (row.get("status") or "").lower() != "active":
        return False
    expires_raw = row.get("expires_at")
    if not expires_raw:
        return True
    try:
        if isinstance(expires_raw, str):
            expires = datetime.fromisoformat(expires_raw.replace("Z", "+00:00"))
        else:
            expires = expires_raw
        if expires.tzinfo is None:
            expires = expires.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return False
    return expires > datetime.now(timezone.utc)
