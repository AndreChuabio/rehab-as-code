"""
junction_repo - read/write helpers for the `public.junction_connections` table.

Mirrors tavus_repo.py / session_repo.py: a thin module isolated to one table so
RLS / schema changes don't bleed elsewhere. Writes go through the pooled
connection (autocommit=False; we commit explicitly); reads honour the
patient/clinician RLS declared in
supabase/migrations/20260624170000_junction_connections.sql.

All public helpers raise JunctionRepoError on a missing DATABASE_URL or psycopg
import failure — no silent fallbacks. The get_health_data resolver wraps the
junction-first branch in try/except, so a JunctionRepoError there degrades to
mock (e.g. local sqlite / CI where there is no DATABASE_URL).

PHI hygiene: cached_metrics holds derived wearable scores (PHI) and vital_user_id
is a Junction-side pointer. Never log either at INFO. RLS is the access control.
"""
from __future__ import annotations

import json
import logging
from typing import Any

logger = logging.getLogger(__name__)


class JunctionRepoError(RuntimeError):
    """Raised when a junction_repo operation cannot complete."""


def _conn():
    """Yield a pooled connection (autocommit=False; caller commits explicitly).

    Routes through backend.db.get_conn. Surface DbConfigError as
    JunctionRepoError so callers don't need to import db's exception, and so a
    missing DATABASE_URL degrades cleanly instead of crashing the resolver.
    """
    try:
        from db import DbConfigError, get_conn
    except ImportError as exc:
        raise JunctionRepoError(
            "junction_repo requires backend.db. "
            "Run: pip install 'psycopg[binary]>=3.2' 'psycopg-pool>=3.2'"
        ) from exc
    try:
        return get_conn(autocommit=False)
    except DbConfigError as exc:
        raise JunctionRepoError(str(exc)) from exc


def _serialize(row: dict[str, Any]) -> dict[str, Any]:
    """Coerce DB types to JSON-friendly values."""
    out = dict(row)
    if out.get("id") is not None:
        out["id"] = str(out["id"])
    for key in ("created_at", "last_synced_at"):
        v = out.get(key)
        if v is not None and not isinstance(v, str):
            out[key] = v.isoformat()
    # providers comes back as a list from psycopg; leave as-is. cached_metrics is
    # JSONB -> already a dict via psycopg's json adapter.
    return out


def get_by_token(token: str) -> dict[str, Any] | None:
    """Return the junction_connections row for a patient, or None.

    Raises JunctionRepoError on a missing DATABASE_URL (consistent with the
    other readers; the resolver catches it and falls through to mock).
    """
    if not token:
        return None
    with _conn() as c, c.cursor() as cur:
        cur.execute(
            "SELECT id, token, vital_user_id, providers, status, "
            " last_synced_at, cached_metrics, created_at "
            "FROM junction_connections WHERE token = %s",
            (token,),
        )
        row = cur.fetchone()
    if not row:
        return None
    return _serialize(row)


def upsert_pending(token: str, vital_user_id: str) -> dict[str, Any]:
    """Create-or-update the row in `pending` state with the Junction user id.

    UNIQUE(token) means one row per patient; a re-link updates in place rather
    than accumulating history.
    """
    if not token or not vital_user_id:
        raise JunctionRepoError("token and vital_user_id are required")

    with _conn() as c, c.cursor() as cur:
        cur.execute(
            "INSERT INTO junction_connections (token, vital_user_id, status) "
            "VALUES (%s, %s, 'pending') "
            "ON CONFLICT (token) DO UPDATE SET "
            " vital_user_id = EXCLUDED.vital_user_id, status = 'pending' "
            "RETURNING id, token, vital_user_id, providers, status, "
            " last_synced_at, cached_metrics, created_at",
            (token, vital_user_id),
        )
        row = cur.fetchone()
        c.commit()
    return _serialize(row)


def set_connected(
    token: str, providers: list[str] | None, cached_metrics: dict[str, Any]
) -> dict[str, Any]:
    """Flip the row to `connected`, store providers + the latest mapped metrics."""
    if not token:
        raise JunctionRepoError("token is required")

    providers_arr = list(providers or [])
    metrics_json = json.dumps(cached_metrics or {})
    with _conn() as c, c.cursor() as cur:
        cur.execute(
            "UPDATE junction_connections "
            "SET status = 'connected', providers = %s, "
            " cached_metrics = %s::jsonb, last_synced_at = NOW() "
            "WHERE token = %s "
            "RETURNING id, token, vital_user_id, providers, status, "
            " last_synced_at, cached_metrics, created_at",
            (providers_arr, metrics_json, token),
        )
        row = cur.fetchone()
        c.commit()
    if not row:
        raise JunctionRepoError("not found")
    return _serialize(row)


def set_error(token: str) -> None:
    """Mark the row `error` (a refresh/fetch failed). Non-fatal for the caller."""
    if not token:
        raise JunctionRepoError("token is required")
    with _conn() as c, c.cursor() as cur:
        cur.execute(
            "UPDATE junction_connections SET status = 'error' WHERE token = %s",
            (token,),
        )
        c.commit()
