"""
session_repo - read/write helpers for the `public.sessions` table.

Mirrors the pattern in protocol_repo.py: thin module isolated to one table so
RLS / schema changes don't bleed into user_store. Writes are server-side
through a service-role DSN; reads honour the patient/clinician RLS already
declared in supabase/migrations/<ts>_sessions_table.sql.

All public helpers raise SessionRepoError on missing DATABASE_URL or psycopg
import failure - no silent fallbacks.
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

logger = logging.getLogger(__name__)


class SessionRepoError(RuntimeError):
    """Raised when a session_repo operation cannot complete."""


def _conn():
    """Yield a pooled connection (autocommit=False; caller commits explicitly).

    Routes through backend.db.get_conn so every read shares the singleton
    pool. Surface DbConfigError as SessionRepoError so callers don't need
    to import db's exception.
    """
    try:
        from db import DbConfigError, get_conn
    except ImportError as exc:
        raise SessionRepoError(
            "session_repo requires backend.db. "
            "Run: pip install 'psycopg[binary]>=3.2' 'psycopg-pool>=3.2'"
        ) from exc
    try:
        return get_conn(autocommit=False)
    except DbConfigError as exc:
        raise SessionRepoError(str(exc)) from exc


def _serialize(row: dict[str, Any]) -> dict[str, Any]:
    """Coerce DB types to JSON-friendly values."""
    out = dict(row)
    if out.get("id") is not None:
        out["id"] = str(out["id"])
    if out.get("protocol_id") is not None:
        out["protocol_id"] = str(out["protocol_id"])
    for key in ("started_at", "completed_at", "created_at"):
        v = out.get(key)
        if v is not None and not isinstance(v, str):
            out[key] = v.isoformat()
    return out


def create_planned(
    token: str,
    exercise_id: str,
    *,
    planned_sets: int | None = None,
    planned_reps: int | None = None,
    protocol_id: str | None = None,
) -> dict[str, Any]:
    """Insert a `planned` session row. Returns the new row serialized.

    `protocol_id` is captured at-the-time the patient added the exercise to
    today's plan so the audit trail shows which protocol was active when
    they staged it - useful when the protocol is later approved/superseded.
    """
    if not exercise_id:
        raise SessionRepoError("exercise_id is required")

    with _conn() as c, c.cursor() as cur:
        cur.execute(
            "INSERT INTO sessions "
            "(token, exercise_id, protocol_id, planned_sets, planned_reps, status) "
            "VALUES (%s, %s, %s, %s, %s, 'planned') "
            "RETURNING id, token, exercise_id, protocol_id, planned_sets, "
            "planned_reps, completed_sets, completed_reps, pose_metrics, "
            "status, started_at, completed_at, created_at",
            (token, exercise_id, protocol_id, planned_sets, planned_reps),
        )
        row = cur.fetchone()
        c.commit()
    return _serialize(row)


def patch(
    session_id: str,
    token: str,
    *,
    status: str | None = None,
    completed_sets: int | None = None,
    completed_reps: int | None = None,
    pose_metrics: dict | None = None,
    started_at: str | None = None,
    completed_at: str | None = None,
) -> dict[str, Any]:
    """Update a session, scoped by (id, token) so a patient cannot mutate
    someone else's row even if RLS were misconfigured.

    Only fields explicitly passed are updated. Returns the updated row
    serialized; raises SessionRepoError if no row matches.
    """
    from psycopg.types.json import Json

    fields: list[str] = []
    values: list[Any] = []
    if status is not None:
        if status not in {"planned", "in_progress", "completed", "skipped"}:
            raise SessionRepoError(f"invalid status: {status!r}")
        fields.append("status = %s")
        values.append(status)
    if completed_sets is not None:
        fields.append("completed_sets = %s")
        values.append(completed_sets)
    if completed_reps is not None:
        fields.append("completed_reps = %s")
        values.append(completed_reps)
    if pose_metrics is not None:
        fields.append("pose_metrics = %s")
        values.append(Json(pose_metrics))
    if started_at is not None:
        fields.append("started_at = %s")
        values.append(started_at)
    if completed_at is not None:
        fields.append("completed_at = %s")
        values.append(completed_at)

    if not fields:
        raise SessionRepoError("patch called with no fields to update")

    sql = (
        "UPDATE sessions SET " + ", ".join(fields)
        + " WHERE id = %s AND token = %s "
        "RETURNING id, token, exercise_id, protocol_id, planned_sets, "
        "planned_reps, completed_sets, completed_reps, pose_metrics, "
        "status, started_at, completed_at, created_at"
    )
    values.extend([session_id, token])

    with _conn() as c, c.cursor() as cur:
        cur.execute(sql, tuple(values))
        row = cur.fetchone()
        c.commit()
    if not row:
        raise SessionRepoError(f"session {session_id} not found for this user")
    return _serialize(row)


def upsert_completed_pose(
    token: str,
    exercise_id: str,
    pose_metrics: dict,
    *,
    started_at: str | None = None,
    completed_at: str | None = None,
    protocol_id: str | None = None,
) -> dict[str, Any]:
    """Insert a session row representing a completed pose-form-check set.

    Called from /pose/session - when the patient finishes a live form-check
    set we capture it as a `completed` session in addition to the existing
    checkins write. Don't try to merge with a planned row here; pose-check
    can fire on any exercise the patient picks, planned or not.
    """
    from psycopg.types.json import Json

    with _conn() as c, c.cursor() as cur:
        cur.execute(
            "INSERT INTO sessions "
            "(token, exercise_id, protocol_id, pose_metrics, status, "
            " started_at, completed_at) "
            "VALUES (%s, %s, %s, %s, 'completed', %s, %s) "
            "RETURNING id, token, exercise_id, protocol_id, planned_sets, "
            "planned_reps, completed_sets, completed_reps, pose_metrics, "
            "status, started_at, completed_at, created_at",
            (
                token,
                exercise_id,
                protocol_id,
                Json(pose_metrics),
                started_at,
                completed_at,
            ),
        )
        row = cur.fetchone()
        c.commit()
    return _serialize(row)


def list_today(token: str, tz_name: str | None = None) -> list[dict[str, Any]]:
    """Return today's sessions (planned + in_progress + completed) for a user.

    "Today" is interpreted in the patient's timezone (passed via the
    X-Timezone header from the browser). Falls back to UTC if the header is
    absent or names a zone we can't resolve.
    """
    tz = _resolve_tz(tz_name)
    now_local = datetime.now(tz)
    start_local = now_local.replace(hour=0, minute=0, second=0, microsecond=0)
    end_local = start_local.replace(hour=23, minute=59, second=59)

    with _conn() as c, c.cursor() as cur:
        cur.execute(
            "SELECT id, token, exercise_id, protocol_id, planned_sets, "
            "planned_reps, completed_sets, completed_reps, pose_metrics, "
            "status, started_at, completed_at, created_at "
            "FROM sessions "
            "WHERE token = %s "
            "AND created_at >= %s AND created_at <= %s "
            "ORDER BY created_at ASC",
            (token, start_local, end_local),
        )
        rows = cur.fetchall() or []
    return [_serialize(r) for r in rows]


def list_recent(token: str, days: int = 7) -> list[dict[str, Any]]:
    """Return all sessions for a patient over the last N days, oldest-first.

    Used by the clinician dashboard's adherence panel. RLS scopes who can
    actually read this row when called via the anon JWT path; this helper
    runs through the service-role DSN so the FastAPI endpoint must do its
    own role check before calling.
    """
    if days <= 0:
        return []
    with _conn() as c, c.cursor() as cur:
        cur.execute(
            "SELECT id, token, exercise_id, protocol_id, planned_sets, "
            "planned_reps, completed_sets, completed_reps, pose_metrics, "
            "status, started_at, completed_at, created_at "
            "FROM sessions "
            "WHERE token = %s "
            "AND created_at >= NOW() - (%s || ' days')::INTERVAL "
            "ORDER BY created_at ASC",
            (token, str(days)),
        )
        rows = cur.fetchall() or []
    return [_serialize(r) for r in rows]


def _resolve_tz(tz_name: str | None):
    if not tz_name:
        return timezone.utc
    try:
        return ZoneInfo(tz_name)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        logger.info("falling back to UTC; could not resolve tz %r: %s", tz_name, exc)
        return timezone.utc
