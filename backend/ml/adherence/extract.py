"""
extract.py - per-patient snapshot for adherence scoring.

Reads from existing repos (no new SQL paths, no schema change). The
snapshot dict is the boundary between data-fetching and feature-building:
all randomness / timezone handling / missing-data coercion happens here so
features.py can be a pure function over the dict.

Schema of a snapshot:
  {
    "token": str,
    "snapshot_at": ISO8601 str (UTC),
    "intake": dict | None,
    "checkins": list[dict],   # most-recent first; raw payloads
    "sessions": list[dict],   # newest-first; raw rows from session_repo
    "user": dict | None,      # users-table row (last_active, patient_name, etc.)
  }

Failure mode: any DB error bubbles up. Caller (predict.score) decides
whether to drop the patient from the cohort or surface the error.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger(__name__)


def snapshot(token: str, *, days: int = 21) -> dict[str, Any]:
    """Build a feature-engineering snapshot for one patient.

    Parameters
    ----------
    token : str
        Patient auth.uid().
    days : int
        Lookback window for sessions / checkins. The risk heuristic
        operates on the last 14 days but we pull a slightly wider window
        so trajectory features have headroom.
    """
    import session_repo
    import user_store

    user = user_store.load_user(token)
    intake = (user or {}).get("intake")
    checkins = list((user or {}).get("session_history") or [])
    try:
        sessions = session_repo.list_recent(token, days=days)
    except Exception as exc:
        logger.warning("snapshot: session_repo.list_recent failed token=%s: %s",
                       token, exc)
        sessions = []

    return {
        "token": token,
        "snapshot_at": datetime.now(timezone.utc).isoformat(),
        "intake": intake,
        "checkins": checkins,
        "sessions": sessions,
        "user": user,
    }
