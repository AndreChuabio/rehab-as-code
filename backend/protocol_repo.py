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
    """Yield a pooled connection (autocommit=False; caller commits explicitly).

    Routes through backend.db.get_conn. Tests monkeypatch this name (see
    test_review_status.py) so the import stays local to keep the patch
    surface narrow.
    """
    try:
        from db import DbConfigError, get_conn
    except ImportError as exc:
        raise ProtocolRepoError(
            "protocol_repo requires backend.db. "
            "Run: pip install 'psycopg[binary]>=3.2' 'psycopg-pool>=3.2'"
        ) from exc
    try:
        return get_conn(autocommit=False)
    except DbConfigError as exc:
        raise ProtocolRepoError(str(exc)) from exc


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


def list_by_token(token: str) -> list[dict]:
    """Every protocol row for one patient, newest first, all statuses.

    Powers the clinician patient-history timeline (active / superseded /
    rejected / pending_review / needs_clinician_review).
    """
    with _conn() as c, c.cursor() as cur:
        cur.execute(
            "SELECT id, token, parent_id, payload, status, created_by_agent, "
            "created_at, reviewed_by, reviewed_at, review_notes, safety_concerns "
            "FROM protocols WHERE token = %s "
            "ORDER BY created_at DESC",
            (token,),
        )
        rows = cur.fetchall() or []
    return [_normalize_row(row) for row in rows]


def list_patient_tokens(limit: int = 200) -> list[dict]:
    """One row per patient token for the clinician roster: the latest
    protocol's status + created_at + region/phase/week. Newest activity first.

    DISTINCT ON (token) picks each token's most-recent row; the outer query
    re-orders the roster by recency and caps it.
    """
    with _conn() as c, c.cursor() as cur:
        cur.execute(
            "SELECT * FROM ("
            "  SELECT DISTINCT ON (token) "
            "    token, status, created_at, "
            "    payload->>'body_region' AS body_region, "
            "    payload->>'phase' AS phase, "
            "    payload->>'week' AS week "
            "  FROM protocols "
            "  ORDER BY token, created_at DESC"
            ") latest "
            "ORDER BY created_at DESC "
            "LIMIT %s",
            (limit,),
        )
        rows = cur.fetchall() or []
    return [
        {
            "token": r["token"],
            "latest_status": r["status"],
            "latest_created_at": r["created_at"],
            "body_region": r.get("body_region"),
            "phase": r.get("phase"),
            "week": r.get("week"),
        }
        for r in rows
    ]


def list_active_tokens(limit: int = 1000) -> list[str]:
    """Return the tokens of every patient with an active protocol.

    The "due patient" set for scheduled reminders: a patient with an active
    plan is in care and may want session / check-in nudges. Per-pref gating
    (and per-patient timezone, when we add it) happens at the send site — this
    is just the candidate roster. Raises ProtocolRepoError if the DB is
    unavailable (the cron caller swallows it and returns a degraded result).
    """
    with _conn() as c, c.cursor() as cur:
        cur.execute(
            "SELECT DISTINCT token FROM protocols WHERE status = 'active' "
            "LIMIT %s",
            (limit,),
        )
        rows = cur.fetchall() or []
    return [r["token"] for r in rows if r.get("token")]


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


# 72h trust-loop window. Rationale: a clinician approval/rejection ages out
# of "recently_xxx" after 3 days so the patient header stops nagging once
# they've had a reasonable chance to read it. Tunable from one place.
RECENT_REVIEW_WINDOW_HOURS = 72


def _initials_from_name(full_name: str | None) -> str | None:
    """First letter of each whitespace-separated token, max two chars, upper.

    Returns None on falsy / whitespace input. Single-name inputs ("Nikki")
    return one initial ("N"). Empty/None -> None lets the caller decide
    whether to fallback to the generic "PT" placeholder.
    """
    if not full_name:
        return None
    parts = [p for p in str(full_name).strip().split() if p]
    if not parts:
        return None
    initials = "".join(p[0] for p in parts[:2]).upper()
    return initials or None


def _resolve_reviewer_initials(cur, reviewed_by: str | None) -> str | None:
    """Look up auth.users.raw_user_meta_data.full_name -> initials.

    Returns None on any failure (caller decides whether to fall back to
    "PT"). Logs but does NOT raise — the trust-loop pill is informational
    and a missing reviewer name should never 500 the intake-status call.
    `auth.users` is in a separate schema; if the role lacks SELECT we
    swallow the permission error the same way user_store.get_display_name
    does.
    """
    if not reviewed_by:
        return None
    try:
        cur.execute(
            "SELECT raw_user_meta_data->>'full_name' AS full_name, "
            "email FROM auth.users WHERE id::text = %s",
            (str(reviewed_by),),
        )
        row = cur.fetchone()
    except Exception as exc:  # pragma: no cover (depends on PG perms)
        logger.warning("reviewer name lookup failed: %s", exc)
        return None
    if not row:
        return None
    initials = _initials_from_name(row.get("full_name"))
    if initials:
        return initials
    # Email fallback: first letter of local-part, uppercased. Single char
    # because we don't want to invent a surname from "andre@x.com".
    email = (row.get("email") or "").strip()
    if email and "@" in email:
        local = email.split("@", 1)[0].strip()
        if local:
            return local[0].upper()
    return None


def get_review_status(token: str) -> dict | None:
    """Return the patient's most-recent review state for the trust-loop pill.

    Five states (str enum, see UX plan v2 / PR-H scope):

      "pending_review"            -> latest row is `pending_review`
      "needs_clinician_review"    -> latest row is high-severity flag
      "recently_approved"         -> latest row is `active` AND was reviewed
                                      within the last RECENT_REVIEW_WINDOW_HOURS
      "recently_rejected"         -> latest row is `rejected` AND reviewed
                                      within the same window
      "none"                      -> nothing draft-state, no recent decision

    Returns None ONLY on infra failure (DB error). The caller surfaces None
    as "no pill" - same render as "none". Empty token -> None as well, so
    callers don't accidentally query for the unauthenticated case.

    PHI hygiene: reviewer_initials and notes_excerpt go in the response,
    but never in logs. The caller (main.py) logs only the state enum.
    """
    if not token:
        return None
    try:
        with _conn() as c, c.cursor() as cur:
            cur.execute(
                "SELECT id, status, created_at, reviewed_at, reviewed_by, "
                "review_notes "
                "FROM protocols WHERE token = %s "
                "ORDER BY created_at DESC LIMIT 1",
                (token,),
            )
            latest = cur.fetchone()
            if not latest:
                return {
                    "state": "none",
                    "protocol_id": None,
                    "submitted_at": None,
                    "reviewed_at": None,
                    "reviewer_initials": None,
                    "notes_excerpt": None,
                }

            status = latest["status"]
            protocol_id = str(latest["id"])

            if status in ("pending_review", "needs_clinician_review"):
                return {
                    "state": status,
                    "protocol_id": protocol_id,
                    "submitted_at": latest["created_at"].isoformat()
                    if latest.get("created_at") else None,
                    "reviewed_at": None,
                    "reviewer_initials": None,
                    "notes_excerpt": None,
                }

            # active / rejected: gate on the 72h recency window. If the
            # decision is older than that the patient has had time to see
            # it; we stop nagging the header.
            reviewed_at = latest.get("reviewed_at")
            if reviewed_at is None:
                # Active row with no reviewed_at can't have come from the
                # approve() flow — treat as no recent decision.
                return {
                    "state": "none",
                    "protocol_id": protocol_id,
                    "submitted_at": None,
                    "reviewed_at": None,
                    "reviewer_initials": None,
                    "notes_excerpt": None,
                }

            from datetime import datetime, timezone, timedelta
            now = datetime.now(timezone.utc)
            # reviewed_at is a tz-aware datetime in psycopg3 by default
            # (TIMESTAMPTZ column). Coerce naive to UTC defensively.
            if reviewed_at.tzinfo is None:
                reviewed_at = reviewed_at.replace(tzinfo=timezone.utc)
            window = timedelta(hours=RECENT_REVIEW_WINDOW_HOURS)
            within_window = (now - reviewed_at) <= window

            if status == "active" and within_window:
                initials = _resolve_reviewer_initials(cur, latest.get("reviewed_by")) or "PT"
                return {
                    "state": "recently_approved",
                    "protocol_id": protocol_id,
                    "submitted_at": None,
                    "reviewed_at": reviewed_at.isoformat(),
                    "reviewer_initials": initials,
                    "notes_excerpt": None,
                }

            if status == "rejected" and within_window:
                initials = _resolve_reviewer_initials(cur, latest.get("reviewed_by")) or "PT"
                notes = latest.get("review_notes") or ""
                excerpt = notes[:100] if notes else None
                return {
                    "state": "recently_rejected",
                    "protocol_id": protocol_id,
                    "submitted_at": None,
                    "reviewed_at": reviewed_at.isoformat(),
                    "reviewer_initials": initials,
                    "notes_excerpt": excerpt,
                }

            # active/rejected but outside the recency window — don't pester.
            return {
                "state": "none",
                "protocol_id": protocol_id,
                "submitted_at": None,
                "reviewed_at": None,
                "reviewer_initials": None,
                "notes_excerpt": None,
            }
    except ProtocolRepoError:
        # Re-raise config errors; main.py turns them into "no pill".
        raise
    except Exception as exc:
        logger.exception("get_review_status failed token=%s: %s", token, exc)
        return None


def _column_names(table: str) -> list[str]:
    """Return column names for `table`. Backend-agnostic (sqlite + pg).

    Uses `SELECT * ... LIMIT 0` so no rows are fetched; cursor.description
    contains the column metadata for both sqlite3 and psycopg3 cursors. The
    fallback branch (information_schema) is never reached in normal usage but
    guards against unexpected driver behaviour.
    """
    with _conn() as c:
        cur = c.cursor()
        try:
            cur.execute(f"SELECT * FROM {table} LIMIT 0")
            return [d[0] for d in cur.description]
        except Exception:
            cur.execute(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_name = %s",
                (table,),
            )
            return [
                r[0] if isinstance(r, (list, tuple)) else r["column_name"]
                for r in cur.fetchall()
            ]


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
