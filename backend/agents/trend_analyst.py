"""
trend_analyst.py - Phase C: longitudinal pattern detection over 4-8 weeks.

Aggregates the patient's check-in history and completed sessions over the
last 4-8 weeks, then classifies the trajectory deterministically:

    plateau       - metrics stable, no clear direction, >=3 weeks of data
    breakthrough  - sustained improvement (pain trending down significantly,
                    adherence not collapsing)
    regression    - sustained decline (pain trending up significantly OR
                    adherence collapsing)
    steady        - normal week-to-week variation, no actionable signal

Was a Sonnet 4.6 call until 2026-05-07; now pure Python statistics. The LLM
was repeatedly summarizing pre-aggregated numerical series into one of four
labels - a job that linear regression + Mann-Kendall does more rigorously,
faster, and at zero per-call cost. Output shape unchanged so consumers
(evaluator, safety_reviewer) need no edits.

Output is consumed by EvaluatorAgent (whose prompt incorporates the
returned dict as opaque JSON) and exposed in logs / clinician views.
"""
from __future__ import annotations

import logging
import statistics
from datetime import datetime, timedelta, timezone
from math import erf, sqrt
from typing import Any

logger = logging.getLogger(__name__)


class TrendAnalystError(RuntimeError):
    """Raised when the trend analyst cannot produce a pattern.

    Reserved for genuine infrastructure problems (e.g., the helpers
    receive un-coercible types). Insufficient-history is NOT an error -
    analyze() returns None in that case.
    """


# Thresholds that decide what counts as "meaningful" movement. Pinned here
# as named constants so they're tunable from one place once production
# data accumulates and Andre + Nikki want to A/B against trained models.
_PAIN_SLOPE_THRESHOLD_PER_DAY = 0.05      # 0.5 points/10 days - clinically meaningful
_ADHERENCE_SLOPE_THRESHOLD_PER_WEEK = 0.10  # 10 percentage points/week
_ADHERENCE_STRONG_AVG = 0.70              # >=70% completion = "strong adherence"
_PLATEAU_MIN_WEEKS = 3                    # need >=3 weeks of data to call plateau
_PAIN_TREND_SIGNIFICANCE_P = 0.05         # Mann-Kendall p-value cutoff


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


def _round_avg(values: list[float | int]) -> float | None:
    if not values:
        return None
    return round(sum(values) / len(values), 2)


def _aggregate(
    checkins: list[dict[str, Any]] | None,
    sessions: list[dict[str, Any]] | None,
    weeks: int,
) -> dict[str, Any]:
    """Produce a numerical snapshot from raw history.

    Bucketed by ISO week. Used by the sufficient-history gate and as
    one source of evidence strings on the returned dict.
    """
    now = datetime.now(timezone.utc)
    window_start = now - timedelta(weeks=weeks)

    pain_levels: list[int] = []
    recovery_scores: list[int] = []
    sleep_scores: list[int] = []

    weekly_pain: dict[str, list[int]] = {}
    weekly_recovery: dict[str, list[int]] = {}

    for entry in checkins or []:
        ts = _parse_ts(entry.get("recorded_at"))
        if ts is None or ts < window_start:
            continue
        wk = ts.strftime("%G-W%V")
        if (p := entry.get("pain_level")) is not None:
            pain_levels.append(p)
            weekly_pain.setdefault(wk, []).append(p)
        for k, target, weekly in (
            ("recovery_score", recovery_scores, weekly_recovery),
            ("sleep_score", sleep_scores, None),
        ):
            v = entry.get(k)
            if isinstance(v, (int, float)):
                target.append(int(v))
                if weekly is not None:
                    weekly.setdefault(wk, []).append(int(v))

    completed = 0
    planned = 0
    skipped = 0
    in_progress = 0
    for s in sessions or []:
        ts = _parse_ts(s.get("created_at"))
        if ts is None or ts < window_start:
            continue
        st = s.get("status")
        if st == "completed":
            completed += 1
        elif st == "planned":
            planned += 1
        elif st == "skipped":
            skipped += 1
        elif st == "in_progress":
            in_progress += 1

    total_sessions = completed + planned + skipped + in_progress
    completion_rate = round(completed / total_sessions, 2) if total_sessions else None

    weekly_pain_series = [
        {"week": wk, "avg_pain": _round_avg(vals)}
        for wk, vals in sorted(weekly_pain.items())
    ]
    weekly_recovery_series = [
        {"week": wk, "avg_recovery": _round_avg(vals)}
        for wk, vals in sorted(weekly_recovery.items())
    ]

    return {
        "window_weeks": weeks,
        "pain": {
            "n": len(pain_levels),
            "avg": _round_avg([float(p) for p in pain_levels]),
            "min": min(pain_levels) if pain_levels else None,
            "max": max(pain_levels) if pain_levels else None,
            "weekly": weekly_pain_series,
        },
        "recovery": {
            "n": len(recovery_scores),
            "avg": _round_avg([float(p) for p in recovery_scores]),
            "weekly": weekly_recovery_series,
        },
        "sleep": {
            "n": len(sleep_scores),
            "avg_7d": _round_avg([float(p) for p in sleep_scores[-7:]]),
        },
        "sessions": {
            "completed": completed,
            "planned": planned,
            "in_progress": in_progress,
            "skipped": skipped,
            "completion_rate": completion_rate,
        },
    }


def _has_enough_history(agg: dict[str, Any]) -> bool:
    return (
        (agg.get("pain") or {}).get("n", 0) >= 4
        or (agg.get("sessions") or {}).get("completed", 0) >= 4
    )


def _mann_kendall(values: list[float]) -> tuple[int, float]:
    """Mann-Kendall trend test on an ordered series.

    Returns (S, p_value) where S is the rank statistic and p_value is the
    two-tailed normal-approximation p. Stdlib only; scipy not required.

    For series shorter than 3 points the test is not meaningful and we
    return (0, 1.0) - the caller treats that as "not significant".
    """
    n = len(values)
    if n < 3:
        return 0, 1.0
    s = 0
    for i in range(n - 1):
        for j in range(i + 1, n):
            d = values[j] - values[i]
            if d > 0:
                s += 1
            elif d < 0:
                s -= 1
    var_s = n * (n - 1) * (2 * n + 5) / 18.0
    if var_s <= 0:
        return s, 1.0
    if s > 0:
        z = (s - 1) / sqrt(var_s)
    elif s < 0:
        z = (s + 1) / sqrt(var_s)
    else:
        z = 0.0
    # Two-tailed p-value via the normal CDF, computed from erf.
    p = 2.0 * (1.0 - 0.5 * (1.0 + erf(abs(z) / sqrt(2.0))))
    return s, max(0.0, min(1.0, p))


def _pain_trajectory(
    checkins: list[dict[str, Any]] | None,
    weeks: int,
) -> dict[str, Any] | None:
    """Linear-regression slope + Mann-Kendall significance on pain over the window.

    Returns None if fewer than 3 pain points exist - linear regression is
    not meaningful below that.
    """
    now = datetime.now(timezone.utc)
    window_start = now - timedelta(weeks=weeks)
    points: list[tuple[float, float]] = []
    for entry in checkins or []:
        ts = _parse_ts(entry.get("recorded_at"))
        if ts is None or ts < window_start:
            continue
        p = entry.get("pain_level")
        if p is None:
            continue
        days = (ts - window_start).total_seconds() / 86400.0
        points.append((days, float(p)))

    if len(points) < 3:
        return None

    points.sort(key=lambda pt: pt[0])
    xs = [pt[0] for pt in points]
    ys = [pt[1] for pt in points]
    try:
        slope, _intercept = statistics.linear_regression(xs, ys)
    except statistics.StatisticsError:
        return None

    _s, p_value = _mann_kendall(ys)
    third = max(1, len(ys) // 3)
    direction = "down" if slope < 0 else "up" if slope > 0 else "flat"

    return {
        "n": len(points),
        "first": ys[0],
        "last": ys[-1],
        "avg_first_third": _round_avg(ys[:third]),
        "avg_last_third": _round_avg(ys[-third:]),
        "slope_per_day": round(slope, 4),
        "p_value": round(p_value, 4),
        "significant": p_value < _PAIN_TREND_SIGNIFICANCE_P,
        "direction": direction,
    }


def _adherence_trajectory(
    sessions: list[dict[str, Any]] | None,
    weeks: int,
) -> dict[str, Any] | None:
    """Weekly completion-rate slope.

    Buckets sessions by ISO week, computes completion_rate per bucket,
    runs linear regression on the weekly series. Returns None when
    fewer than 2 weekly buckets exist (slope undefined).
    """
    now = datetime.now(timezone.utc)
    window_start = now - timedelta(weeks=weeks)

    weekly: dict[str, dict[str, int]] = {}
    for s in sessions or []:
        ts = _parse_ts(s.get("created_at"))
        if ts is None or ts < window_start:
            continue
        wk = ts.strftime("%G-W%V")
        bucket = weekly.setdefault(wk, {"completed": 0, "planned": 0,
                                        "skipped": 0, "in_progress": 0})
        st = s.get("status")
        if st in bucket:
            bucket[st] += 1

    items = sorted(weekly.items())
    xs: list[float] = []
    ys: list[float] = []
    for i, (_wk, bucket) in enumerate(items):
        total = sum(bucket.values())
        if total == 0:
            continue
        xs.append(float(i))
        ys.append(bucket["completed"] / total)

    if len(xs) < 2:
        return None

    try:
        slope, _intercept = statistics.linear_regression(xs, ys)
    except statistics.StatisticsError:
        return None

    avg_rate = sum(ys) / len(ys)
    direction = "down" if slope < 0 else "up" if slope > 0 else "flat"

    return {
        "n_weeks": len(xs),
        "first_week_rate": round(ys[0], 2),
        "last_week_rate": round(ys[-1], 2),
        "avg_rate": round(avg_rate, 2),
        "slope_per_week": round(slope, 4),
        "direction": direction,
    }


def _classify_pattern(
    pain: dict[str, Any] | None,
    adherence: dict[str, Any] | None,
    agg: dict[str, Any],
) -> str:
    """Map (pain trajectory, adherence trajectory, agg snapshot) to one of
    four pattern labels. First-match-wins:

      regression  - pain trending up significantly OR adherence collapsing
      breakthrough - pain trending down significantly AND adherence holding
      plateau     - both trends flat, >=3 weeks of data, strong adherence
      steady      - default
    """
    pain_up_sig = (
        pain is not None
        and pain["significant"]
        and pain["slope_per_day"] > _PAIN_SLOPE_THRESHOLD_PER_DAY
    )
    pain_down_sig = (
        pain is not None
        and pain["significant"]
        and pain["slope_per_day"] < -_PAIN_SLOPE_THRESHOLD_PER_DAY
    )
    adherence_collapse = (
        adherence is not None
        and adherence["slope_per_week"] < -_ADHERENCE_SLOPE_THRESHOLD_PER_WEEK
    )

    if pain_up_sig or adherence_collapse:
        return "regression"

    adherence_not_falling = (
        adherence is None
        or adherence["slope_per_week"] >= -0.05
    )
    if pain_down_sig and adherence_not_falling:
        return "breakthrough"

    weekly_pain = (agg.get("pain") or {}).get("weekly") or []
    has_three_weeks = len(weekly_pain) >= _PLATEAU_MIN_WEEKS
    pain_flat = (
        pain is not None
        and not pain["significant"]
        and abs(pain["slope_per_day"]) < _PAIN_SLOPE_THRESHOLD_PER_DAY
    )
    adherence_strong = (
        adherence is not None
        and adherence["avg_rate"] >= _ADHERENCE_STRONG_AVG
        and adherence["slope_per_week"] >= -0.05
    )
    if has_three_weeks and pain_flat and adherence_strong:
        return "plateau"

    return "steady"


def _build_evidence(
    pain: dict[str, Any] | None,
    adherence: dict[str, Any] | None,
    agg: dict[str, Any],
) -> list[str]:
    """Construct the human-readable evidence list. Each entry is one
    self-contained sentence with the actual numbers - clinicians and the
    evaluator agent can audit the classification by reading these alone."""
    out: list[str] = []
    if pain is not None:
        sig_label = "significant" if pain["significant"] else "not significant"
        out.append(
            f"Pain: {pain['first']:.0f} -> {pain['last']:.0f} over {pain['n']} "
            f"checkins (slope {pain['slope_per_day']:+.3f}/day, "
            f"p={pain['p_value']:.2f}, {sig_label})."
        )
    sessions = agg.get("sessions") or {}
    completion_rate = sessions.get("completion_rate")
    if completion_rate is not None:
        total = sum(sessions.get(k, 0) for k in
                    ("completed", "planned", "skipped", "in_progress"))
        out.append(
            f"Completion: {completion_rate:.0%} "
            f"({sessions.get('completed', 0)} of {total} sessions)."
        )
    if adherence is not None:
        out.append(
            f"Adherence trend: {adherence['first_week_rate']:.0%} -> "
            f"{adherence['last_week_rate']:.0%} over {adherence['n_weeks']} "
            f"weeks (slope {adherence['slope_per_week']:+.3f}/week)."
        )
    return out


def _build_implication(
    pattern: str,
    pain: dict[str, Any] | None,
    adherence: dict[str, Any] | None,
) -> str:
    if pattern == "regression":
        if adherence is not None and adherence["slope_per_week"] < -_ADHERENCE_SLOPE_THRESHOLD_PER_WEEK:
            return (
                "Adherence dropping; review what's blocking the patient before "
                "changing dose."
            )
        return (
            "Pain trending against recovery; consider hold or regress and "
            "review the patient's symptom log."
        )
    if pattern == "breakthrough":
        return "Sustained improvement; consider progressing dose this week."
    if pattern == "plateau":
        return (
            "Patient holding steady at current level for >=3 weeks; consider "
            "introducing a progression to break through."
        )
    return "No clear trend; continue current prescription."


def analyze(
    *,
    token: str,
    checkins: list[dict[str, Any]] | None = None,
    sessions: list[dict[str, Any]] | None = None,
    intake: dict[str, Any] | None = None,
    weeks: int = 4,
) -> dict[str, Any] | None:
    """Classify the patient's longitudinal pattern.

    Parameters
    ----------
    token : str
        Patient auth.uid(). For logging only.
    checkins : list[dict] | None
        Recent symptom / pain check-ins. Same shape as
        user_store.get_session_history (mixed kinds; this analyzer filters
        on pain_level presence).
    sessions : list[dict] | None
        Recent rows from session_repo.list_recent.
    intake : dict | None
        Optional baseline. Currently unused by the deterministic classifier
        - kept in the signature so the existing orchestrator call (and any
        future LLM-augmented version) doesn't need to change.
    weeks : int
        Lookback window in ISO weeks. 4-8 is the sweet spot.

    Returns
    -------
    dict | None
        {"pattern": ..., "evidence": [...], "implication_for_next_week": ...}
        when there is enough data. None when there is insufficient history
        (orchestrator passes None to the evaluator, which handles it).

    Raises
    ------
    TrendAnalystError
        On unexpected failure (e.g., un-coercible input types). Insufficient
        history is NOT an error - it returns None.
    """
    # `intake` is intentionally accepted but unused; see docstring.
    _ = intake

    agg = _aggregate(checkins, sessions, weeks)
    if not _has_enough_history(agg):
        logger.info(
            "trend_analyst: insufficient history token=%s pain_n=%s sessions_completed=%s",
            token,
            (agg.get("pain") or {}).get("n"),
            (agg.get("sessions") or {}).get("completed"),
        )
        return None

    try:
        pain = _pain_trajectory(checkins, weeks)
        adherence = _adherence_trajectory(sessions, weeks)
    except (TypeError, ValueError) as exc:
        raise TrendAnalystError(f"trend analyst input error: {exc}") from exc

    pattern = _classify_pattern(pain, adherence, agg)
    evidence = _build_evidence(pain, adherence, agg)
    implication = _build_implication(pattern, pain, adherence)

    pain_slope = pain["slope_per_day"] if pain else None
    adherence_slope = adherence["slope_per_week"] if adherence else None
    logger.info(
        "trend_analyst pattern=%s token=%s pain_slope=%s adherence_slope=%s "
        "n_evidence=%d",
        pattern, token, pain_slope, adherence_slope, len(evidence),
    )

    return {
        "pattern": pattern,
        "evidence": evidence,
        "implication_for_next_week": implication,
    }
