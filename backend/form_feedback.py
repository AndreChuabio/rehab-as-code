"""form_feedback.py — clinician form-feedback rollup (completed-pose-sessions-only).

Aggregates the 2D-webcam form-check telemetry that already persists per
completed pose set into a per-exercise rollup a clinician can scan. Mirrors the
posture of superbill.py: completed-sessions-only, never raises on a missing
session store, degrades to an empty-but-well-formed dict, and is honest in this
docstring about what the signal is and is NOT.

HONEST FRAMING (do not weaken):
  * This is PRESENCE / AGGREGATE form feedback from a 2D home webcam (MediaPipe
    pose landmarks), surfaced as a TREND SIGNAL — "this patient keeps tripping
    the same cue on this exercise" — not a clinical goniometric measurement.
  * `best_depth`, `worst_status`, and the warning counts are heuristic outputs
    of the in-browser pose checker (frontend/pose.js), captured per set. A
    single-camera, uncalibrated view cannot assert true joint angles or ROM.
  * Worst-case status across sessions is reported so a clinician decides whether
    to look closer — it does not, on its own, justify a protocol change.

DATA CONTRACT (keyed off the /pose/session handler in backend/main.py):
  session_repo.list_recent(token, days) returns session rows. Completed pose
  sets have status == "completed" and a `pose_metrics` JSONB dict shaped:
      {
        "rep_count":    int | None,
        "best_depth":   float | None,
        "worst_status": "good" | "warn" | "fail",
        "warnings": [ { "id": str, "msg": str, "status": str }, ... ],
      }
  Plus row-level `exercise_id`, `completed_at`, `created_at`. Non-completed
  rows (planned / in_progress / skipped) are ignored.

PHI hygiene: never log warning text, exercise names, or any field VALUE — only
counts and tokens at info level, the same posture as superbill.py / session_repo.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger(__name__)

# Ranks for collapsing per-session worst_status into a per-exercise worst.
# Mirrors the rank map in main._summarize_pose_set so "fail" always wins.
_STATUS_RANK: dict[str, int] = {"good": 0, "warn": 1, "fail": 2}

_NOTE_SIGNAL = (
    "Presence/aggregate form feedback from a 2D home webcam — a trend signal "
    "(repeated cue trips per exercise), NOT a goniometric or clinical "
    "measurement. A clinician decides whether to look closer."
)
_NOTE_EMPTY = "No completed form-check sessions in the window."
_NOTE_UNAVAILABLE = "Session store unavailable — no form-check sessions counted."


def _completed(sessions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Keep only completed session rows. Non-completed rows are not form data."""
    return [s for s in (sessions or []) if (s or {}).get("status") == "completed"]


def _worse(a: str | None, b: str | None) -> str:
    """Return whichever status is worse under _STATUS_RANK (default 'good')."""
    a = a or "good"
    b = b or "good"
    return a if _STATUS_RANK.get(a, 0) >= _STATUS_RANK.get(b, 0) else b


def _row_date(row: dict[str, Any]) -> str:
    """Date prefix (YYYY-MM-DD) preferring completed_at, then created_at."""
    return str(row.get("completed_at") or row.get("created_at") or "")[:10]


def summarize_form(token: str, *, window_days: int = 28) -> dict[str, Any]:
    """Roll up completed pose-form-check sessions into a per-exercise summary.

    Reads completed sessions via session_repo.list_recent, filters to
    status == "completed", and groups by exercise_id. For each exercise it
    reports session count, total reps (sum of pose_metrics.rep_count), the
    worst status seen across sessions, the aggregated form warnings (each:
    warning id/key, human message, total count across sessions), and the
    last-seen date.

    Returns an empty-but-well-formed dict (empty lists + a note) when the
    session store is unavailable or there are no completed sessions. Never
    raises — same degrade posture as superbill.generate_draft and the rest of
    the patient-state read path.

    Output shape::

        {
          "exercises": [
            {
              "exercise_id": str,
              "n_sessions": int,
              "total_reps": int,
              "worst_status": "good" | "warn" | "fail",
              "warnings": [ {"id": str, "msg": str, "count": int}, ... ],
              "last_seen": "YYYY-MM-DD" | None,
            },
            ...
          ],
          "exercises_with_issues": [ ...subset where worst_status != "good"
                                       or warnings is non-empty... ],
          "totals": {"n_exercises": int, "n_sessions": int, "n_warnings": int},
          "window_days": int,
          "generated_at": <ISO 8601 UTC>,
          "source": "completed_pose_sessions_only",
          "notes": [ ...framing + any degrade note... ],
        }
    """
    notes: list[str] = [_NOTE_SIGNAL]

    completed: list[dict[str, Any]] = []
    try:
        import session_repo

        completed = _completed(session_repo.list_recent(token, days=window_days))
    except Exception as exc:  # noqa: BLE001 — session store optional in some envs
        logger.info("form_feedback: session store unavailable token=%s: %s", token, exc)
        notes.append(_NOTE_UNAVAILABLE)
        return _empty(window_days, notes)

    if not completed:
        notes.append(_NOTE_EMPTY)
        return _empty(window_days, notes)

    # Accumulate per exercise_id. Warnings keyed by warning id so the same cue
    # tripped across sessions sums into one row; keep the first human message.
    rollup: dict[str, dict[str, Any]] = {}
    for s in completed:
        eid = str(s.get("exercise_id") or "")
        metrics = s.get("pose_metrics") or {}

        bucket = rollup.setdefault(
            eid,
            {
                "exercise_id": eid,
                "n_sessions": 0,
                "total_reps": 0,
                "worst_status": "good",
                "_warnings": {},  # id -> {"id","msg","count"}
                "last_seen": None,
            },
        )
        bucket["n_sessions"] += 1

        reps = metrics.get("rep_count")
        if isinstance(reps, (int, float)):
            bucket["total_reps"] += int(reps)

        bucket["worst_status"] = _worse(bucket["worst_status"], metrics.get("worst_status"))

        for w in metrics.get("warnings") or []:
            if not isinstance(w, dict):
                continue
            wid = str(w.get("id") or "")
            if not wid:
                continue
            wb = bucket["_warnings"].setdefault(
                wid, {"id": wid, "msg": str(w.get("msg") or ""), "count": 0}
            )
            wb["count"] += 1

        d = _row_date(s)
        if d and (bucket["last_seen"] is None or d > bucket["last_seen"]):
            bucket["last_seen"] = d

    exercises: list[dict[str, Any]] = []
    total_warnings = 0
    for eid in sorted(rollup):
        b = rollup[eid]
        warnings = sorted(
            b.pop("_warnings").values(), key=lambda w: (-w["count"], w["id"])
        )
        total_warnings += sum(w["count"] for w in warnings)
        b["warnings"] = warnings
        exercises.append(b)

    exercises_with_issues = [
        e for e in exercises if e["worst_status"] != "good" or e["warnings"]
    ]

    return {
        "exercises": exercises,
        "exercises_with_issues": exercises_with_issues,
        "totals": {
            "n_exercises": len(exercises),
            "n_sessions": len(completed),
            "n_warnings": total_warnings,
        },
        "window_days": window_days,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "source": "completed_pose_sessions_only",
        "notes": notes,
    }


def _empty(window_days: int, notes: list[str]) -> dict[str, Any]:
    """A well-formed empty rollup so the clinician panel renders uniformly."""
    return {
        "exercises": [],
        "exercises_with_issues": [],
        "totals": {"n_exercises": 0, "n_sessions": 0, "n_warnings": 0},
        "window_days": window_days,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "source": "completed_pose_sessions_only",
        "notes": notes,
    }
