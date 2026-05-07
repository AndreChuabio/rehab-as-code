"""
features.py - feature engineering for adherence risk.

Pure functions over a snapshot dict (the contract from extract.snapshot).
No DB calls, no clock reads other than the explicit `now` arg - so unit
tests can pin behavior with synthetic snapshots and explicit clocks.

The same feature pipeline feeds:
  * heuristic.py (today)
  * the eventual XGBoost classifier in train_xgb.ipynb / predict._LAYER="xgb"

Keeping it deterministic and side-effect-free is what makes the layer
swap a one-PR change instead of a rebuild.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any


def _parse_ts(value: Any) -> datetime | None:
    if not value:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, str):
        try:
            ts = datetime.fromisoformat(value.replace("Z", "+00:00"))
            return ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)
        except ValueError:
            return None
    return None


def compute(
    snapshot: dict[str, Any],
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Project a snapshot into the feature dict the scorer consumes.

    Returned keys (all optional / defaulted; missing data does NOT raise):

      days_since_last_checkin   : int | None  (None if never checked in)
      days_since_last_completed : int | None
      missed_session_streak     : int         (consecutive days with planned
                                              + nothing-completed)
      sessions_completed_7d     : int
      sessions_completed_14d    : int
      sessions_planned_14d      : int
      completion_rate_7d        : float | None  (None when no planned/completed)
      completion_rate_14d       : float | None
      pain_avg_7d               : float | None
      pain_avg_prev7d           : float | None  (8-14d ago, for slope sign)
      pain_slope_per_day_14d    : float | None  (linear regression slope)
      n_pain_checkins_14d       : int
      injury_category           : str | None
      days_since_intake         : int | None
    """
    if now is None:
        now = datetime.now(timezone.utc)

    checkins = snapshot.get("checkins") or []
    sessions = snapshot.get("sessions") or []
    intake = snapshot.get("intake") or {}
    user = snapshot.get("user") or {}

    last_checkin_ts = _latest_ts(
        c.get("recorded_at") for c in checkins if c.get("recorded_at")
    )
    days_since_last_checkin = _days_between(last_checkin_ts, now)

    completed_ts = sorted(
        (
            _parse_ts(s.get("completed_at") or s.get("created_at"))
            for s in sessions
            if s.get("status") == "completed"
        ),
        key=lambda t: t or datetime.min.replace(tzinfo=timezone.utc),
    )
    last_completed_ts = completed_ts[-1] if completed_ts else None
    days_since_last_completed = _days_between(last_completed_ts, now)

    completed_7d = _count_within(sessions, "completed", days=7, now=now)
    completed_14d = _count_within(sessions, "completed", days=14, now=now)
    planned_14d = _count_within_any(sessions, days=14, now=now,
                                    statuses=("completed", "planned",
                                              "skipped", "in_progress"))
    completed_7d_total = _count_within_any(
        sessions, days=7, now=now,
        statuses=("completed", "planned", "skipped", "in_progress"),
    )
    completion_rate_7d = (
        round(completed_7d / completed_7d_total, 3)
        if completed_7d_total > 0
        else None
    )
    completion_rate_14d = (
        round(completed_14d / planned_14d, 3) if planned_14d > 0 else None
    )

    pain_points_14d = _pain_points(checkins, days=14, now=now)
    pain_points_7d = [(d, p) for d, p in pain_points_14d if d >= -7.0]
    pain_points_prev7d = [(d, p) for d, p in pain_points_14d if -14.0 <= d < -7.0]

    pain_avg_7d = _avg([p for _, p in pain_points_7d])
    pain_avg_prev7d = _avg([p for _, p in pain_points_prev7d])
    pain_slope = _slope(pain_points_14d) if len(pain_points_14d) >= 3 else None

    intake_recorded_at = _parse_ts((intake or {}).get("recorded_at"))
    days_since_intake = _days_between(intake_recorded_at, now)

    return {
        "days_since_last_checkin": days_since_last_checkin,
        "days_since_last_completed": days_since_last_completed,
        "missed_session_streak": _missed_streak(sessions, now=now),
        "sessions_completed_7d": completed_7d,
        "sessions_completed_14d": completed_14d,
        "sessions_planned_14d": planned_14d,
        "completion_rate_7d": completion_rate_7d,
        "completion_rate_14d": completion_rate_14d,
        "pain_avg_7d": pain_avg_7d,
        "pain_avg_prev7d": pain_avg_prev7d,
        "pain_slope_per_day_14d": pain_slope,
        "n_pain_checkins_14d": len(pain_points_14d),
        "injury_category": user.get("injury_category") or intake.get("injury_type"),
        "days_since_intake": days_since_intake,
    }


# ---------------------------------------------------------------------------
# helpers (private)
# ---------------------------------------------------------------------------

def _latest_ts(iterable) -> datetime | None:
    parsed = [_parse_ts(v) for v in iterable]
    parsed = [t for t in parsed if t is not None]
    return max(parsed) if parsed else None


def _days_between(then: datetime | None, now: datetime) -> int | None:
    if then is None:
        return None
    delta = now - then
    return max(0, int(delta.total_seconds() // 86400))


def _count_within(
    sessions: list[dict[str, Any]],
    status: str,
    *,
    days: int,
    now: datetime,
) -> int:
    cutoff = now - timedelta(days=days)
    n = 0
    for s in sessions:
        if s.get("status") != status:
            continue
        ts = _parse_ts(s.get("created_at"))
        if ts and ts >= cutoff:
            n += 1
    return n


def _count_within_any(
    sessions: list[dict[str, Any]],
    *,
    days: int,
    now: datetime,
    statuses: tuple[str, ...],
) -> int:
    cutoff = now - timedelta(days=days)
    n = 0
    for s in sessions:
        if s.get("status") not in statuses:
            continue
        ts = _parse_ts(s.get("created_at"))
        if ts and ts >= cutoff:
            n += 1
    return n


def _missed_streak(
    sessions: list[dict[str, Any]],
    *,
    now: datetime,
) -> int:
    """Consecutive days ending today with no completed session.

    Counts days backward from today; stops at the first day that has at
    least one completed session. A patient who completed a session today
    has streak 0; one who last completed two days ago has streak 2.
    """
    completed_dates: set = set()
    for s in sessions:
        if s.get("status") != "completed":
            continue
        ts = _parse_ts(s.get("completed_at") or s.get("created_at"))
        if ts is None:
            continue
        completed_dates.add(ts.date())

    today = now.date()
    streak = 0
    cur = today
    # Cap at 60 days so a patient who never completed anything doesn't
    # produce a runaway value; 60 is enough for the heuristic + clearly
    # signals "long absence" without misleading clinicians.
    for _ in range(60):
        if cur in completed_dates:
            return streak
        streak += 1
        cur -= timedelta(days=1)
    return streak


def _pain_points(
    checkins: list[dict[str, Any]],
    *,
    days: int,
    now: datetime,
) -> list[tuple[float, float]]:
    """Return [(days_offset_from_now_negative, pain_level), ...] for the
    last `days` days. days_offset is negative (older = more negative).
    Only checkins with a numeric pain_level are included."""
    cutoff = now - timedelta(days=days)
    points: list[tuple[float, float]] = []
    for c in checkins:
        ts = _parse_ts(c.get("recorded_at"))
        if ts is None or ts < cutoff:
            continue
        p = c.get("pain_level")
        if p is None:
            continue
        offset = (ts - now).total_seconds() / 86400.0
        points.append((offset, float(p)))
    points.sort(key=lambda x: x[0])
    return points


def _avg(values: list[float]) -> float | None:
    if not values:
        return None
    return round(sum(values) / len(values), 3)


def _slope(points: list[tuple[float, float]]) -> float | None:
    """Linear regression slope (pain per day). Stdlib only."""
    import statistics
    if len(points) < 2:
        return None
    xs = [p[0] for p in points]
    ys = [p[1] for p in points]
    try:
        slope, _intercept = statistics.linear_regression(xs, ys)
    except statistics.StatisticsError:
        return None
    return round(slope, 4)
