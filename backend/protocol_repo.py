"""
protocol_repo — read/write helpers for the versioned `protocols` table
(introduced in supabase/migrations/20260506000000_protocols_versioning.sql).

Separate module from user_store.py because the new table has different
semantics: append-only versioning, status state machine, transactional
promotion. Keeping it isolated also makes the eventual deletion of the
GitHub PR-based write path (Phase E) a clean drop of one module.

All helpers raise on missing DATABASE_URL — unlike fetch_protocol_for_user
which falls back to GitHub. The write path is the source of truth; if the
DB is down we want the call to fail loudly, not silently route around it.
"""
from __future__ import annotations

import json
import logging
import os
from typing import Any

logger = logging.getLogger(__name__)


class ProtocolRepoError(RuntimeError):
    """Raised when a protocol_repo operation cannot complete."""


def _conn():
    dsn = os.getenv("DATABASE_URL", "").strip()
    if not dsn:
        raise ProtocolRepoError(
            "protocol_repo requires DATABASE_URL. "
            "Set it to the Supabase Postgres connection string."
        )
    try:
        import psycopg
        from psycopg.rows import dict_row
    except ImportError as exc:
        raise ProtocolRepoError(
            "protocol_repo requires psycopg. Run: pip install 'psycopg[binary]>=3.2'"
        ) from exc
    return psycopg.connect(dsn, row_factory=dict_row, autocommit=False)


def _normalize_payload(value: Any) -> dict:
    """Coerce a JSONB column value into a plain dict (psycopg may return either)."""
    if isinstance(value, str):
        return json.loads(value)
    return value


_VALID_PENDING_STATUSES = ("pending_review", "needs_clinician_review")


def save_pending(
    token: str,
    payload: dict,
    created_by_agent: str,
    *,
    status: str = "pending_review",
    safety_concerns: list[dict] | None = None,
) -> str:
    """Insert a pending row, return its UUID as a string.

    `parent_id` is set to whatever row currently has status='active' for
    this token (or NULL on first protocol). The version chain is implicit
    in the parent_id pointers; the unique partial index keeps "active"
    singular at all times.

    Parameters
    ----------
    status : str
        One of "pending_review" (default — passed safety review or had
        only low/med concerns) or "needs_clinician_review" (high-severity
        safety flag, surfaces at top of clinician queue with red banner).
    safety_concerns : list[dict] | None
        SafetyReviewAgent output. Persisted as JSONB. When provided, the
        clinician dashboard renders the concern list inline above the
        diff so the reviewer sees what the agent flagged.
    """
    if status not in _VALID_PENDING_STATUSES:
        raise ProtocolRepoError(
            f"save_pending status must be one of {_VALID_PENDING_STATUSES}, got {status!r}"
        )

    from psycopg.types.json import Json

    with _conn() as c, c.cursor() as cur:
        cur.execute(
            "SELECT id FROM protocols WHERE token = %s AND status = 'active' LIMIT 1",
            (token,),
        )
        active = cur.fetchone()
        parent_id = active["id"] if active else None

        cur.execute(
            "INSERT INTO protocols "
            "(token, parent_id, payload, status, created_by_agent, safety_concerns) "
            "VALUES (%s, %s, %s, %s, %s, %s) "
            "RETURNING id",
            (
                token,
                parent_id,
                Json(payload),
                status,
                created_by_agent,
                Json(safety_concerns) if safety_concerns else None,
            ),
        )
        row = cur.fetchone()
        c.commit()
        return str(row["id"])


def _normalize_row(row: dict) -> dict:
    """Coerce a protocols-table row dict into JSON-friendly types in place."""
    row["payload"] = _normalize_payload(row["payload"])
    row["id"] = str(row["id"])
    if row.get("parent_id") is not None:
        row["parent_id"] = str(row["parent_id"])
    if "safety_concerns" in row and row["safety_concerns"] is not None:
        row["safety_concerns"] = _normalize_payload(row["safety_concerns"])
    return row


def get(protocol_id: str) -> dict | None:
    """Return one row by id, or None. Payload is parsed to dict."""
    with _conn() as c, c.cursor() as cur:
        cur.execute(
            "SELECT id, token, parent_id, payload, status, created_by_agent, "
            "created_at, reviewed_by, reviewed_at, review_notes, safety_concerns "
            "FROM protocols WHERE id = %s",
            (protocol_id,),
        )
        row = cur.fetchone()
    if not row:
        return None
    return _normalize_row(row)


def get_active(token: str) -> dict | None:
    """Return the patient's currently-active protocol row, or None."""
    with _conn() as c, c.cursor() as cur:
        cur.execute(
            "SELECT id, token, parent_id, payload, status, created_by_agent, "
            "created_at, reviewed_by, reviewed_at, review_notes, safety_concerns "
            "FROM protocols WHERE token = %s AND status = 'active' LIMIT 1",
            (token,),
        )
        row = cur.fetchone()
    if not row:
        return None
    return _normalize_row(row)


def list_pending(limit: int = 50) -> list[dict]:
    """List pending rows newest-first for the clinician dashboard.

    Returns BOTH `pending_review` and `needs_clinician_review` rows.
    `needs_clinician_review` rows are sorted to the top of each created_at
    bucket — the dashboard already shows them with a red banner, but this
    server-side ordering means a newly-flagged high-severity row jumps the
    queue without the frontend having to re-sort.
    """
    with _conn() as c, c.cursor() as cur:
        cur.execute(
            "SELECT id, token, parent_id, payload, status, created_by_agent, "
            "created_at, safety_concerns "
            "FROM protocols "
            "WHERE status IN ('pending_review', 'needs_clinician_review') "
            "ORDER BY "
            "  CASE status "
            "    WHEN 'needs_clinician_review' THEN 0 "
            "    ELSE 1 "
            "  END, "
            "  created_at DESC "
            "LIMIT %s",
            (limit,),
        )
        rows = cur.fetchall() or []
    return [_normalize_row(row) for row in rows]


def approve(
    protocol_id: str,
    reviewed_by: str,
    notes: str | None = None,
) -> dict:
    """Promote a pending_review row to active in a single transaction.

    Steps:
      1. Look up the pending row + its token.
      2. UPDATE the prior active row (if any) to 'superseded'.
      3. UPDATE the pending row to 'active' with reviewer + timestamp.

    The unique partial index on (token) WHERE status='active' enforces
    that there's never more than one active row per patient — if step 3
    runs before step 2 the index trips and the transaction rolls back.

    Raises ProtocolRepoError if the row doesn't exist or isn't pending.
    """
    with _conn() as c, c.cursor() as cur:
        cur.execute(
            "SELECT token, status FROM protocols WHERE id = %s FOR UPDATE",
            (protocol_id,),
        )
        target = cur.fetchone()
        if not target:
            raise ProtocolRepoError(f"protocol {protocol_id} not found")
        if target["status"] not in _VALID_PENDING_STATUSES:
            raise ProtocolRepoError(
                f"protocol {protocol_id} is {target['status']}, "
                f"not in {_VALID_PENDING_STATUSES}"
            )

        token = target["token"]

        cur.execute(
            "UPDATE protocols SET status = 'superseded' "
            "WHERE token = %s AND status = 'active'",
            (token,),
        )
        cur.execute(
            "UPDATE protocols "
            "SET status = 'active', reviewed_by = %s, reviewed_at = NOW(), "
            "    review_notes = %s "
            "WHERE id = %s "
            "RETURNING id, token, status, reviewed_at",
            (reviewed_by, notes, protocol_id),
        )
        row = cur.fetchone()
        c.commit()
    row["id"] = str(row["id"])
    return row


def reject(
    protocol_id: str,
    reviewed_by: str,
    notes: str,
) -> dict:
    """Mark a pending_review row as rejected. Notes required (clinician must
    explain why for the audit trail). Active row is unchanged."""
    if not notes or not notes.strip():
        raise ProtocolRepoError("review_notes required when rejecting")

    with _conn() as c, c.cursor() as cur:
        cur.execute(
            "UPDATE protocols "
            "SET status = 'rejected', reviewed_by = %s, reviewed_at = NOW(), "
            "    review_notes = %s "
            "WHERE id = %s "
            "  AND status IN ('pending_review', 'needs_clinician_review') "
            "RETURNING id, token, status, reviewed_at",
            (reviewed_by, notes, protocol_id),
        )
        row = cur.fetchone()
        c.commit()
    if not row:
        raise ProtocolRepoError(
            f"protocol {protocol_id} not found or not in a pending state"
        )
    row["id"] = str(row["id"])
    return row
