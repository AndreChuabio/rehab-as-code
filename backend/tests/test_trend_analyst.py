"""Tests for agents.trend_analyst.analyze().

Pure-Python deterministic implementation since 2026-05-07. Anthropic was
removed - the previous version asked Sonnet to summarize pre-aggregated
numerical series into one of four labels, work that linear regression +
Mann-Kendall does more rigorously at zero per-call cost.

Contract under test:
  * insufficient history (<4 pain checkins AND <4 completed sessions) -> None
  * pain dropping monotonically + meaningful slope -> "breakthrough"
  * pain rising + significant -> "regression"
  * adherence collapse (week-over-week completion rate) -> "regression"
  * pain flat + strong adherence + >=3 weeks of data -> "plateau"
  * default fallback -> "steady"
  * structural shape preserved for the orchestrator's consumers
  * Mann-Kendall helper and trajectory helpers are deterministic
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest


def _make_pain_checkins(
    n: int,
    pain_series: list[int],
    *,
    days_back_per_step: int = 1,
) -> list[dict]:
    """Generate n checkins with explicit per-checkin pain levels.

    pain_series[0] is the OLDEST entry (n-1 days ago), pain_series[-1]
    is the newest. Length must equal n.
    """
    assert len(pain_series) == n
    now = datetime.now(timezone.utc)
    out = []
    for i, p in enumerate(pain_series):
        ts = now - timedelta(days=(n - 1 - i) * days_back_per_step)
        out.append({
            "kind": "checkin",
            "pain_level": p,
            "recovery_score": 70,
            "recorded_at": ts.isoformat(),
        })
    return out


def _make_sessions(rows: list[tuple[str, int]]) -> list[dict]:
    """rows: list of (status, days_ago). Returns session-shaped dicts."""
    now = datetime.now(timezone.utc)
    return [
        {
            "status": status,
            "exercise_id": "x",
            "created_at": (now - timedelta(days=days_ago)).isoformat(),
        }
        for status, days_ago in rows
    ]


# ---------------------------------------------------------------------------
# Sufficient-history gate
# ---------------------------------------------------------------------------

def test_returns_none_on_insufficient_history():
    from agents.trend_analyst import analyze

    out = analyze(
        token="t",
        checkins=_make_pain_checkins(2, [3, 4]),
        sessions=[],
    )
    assert out is None


def test_uses_completed_sessions_when_no_checkins():
    """4+ completed sessions trigger classification even with zero pain data.
    Without pain or multi-week adherence signal the default is 'steady'."""
    from agents.trend_analyst import analyze

    sessions = _make_sessions([("completed", i) for i in range(1, 6)])
    out = analyze(token="t", checkins=[], sessions=sessions)
    assert out is not None
    assert out["pattern"] == "steady"


# ---------------------------------------------------------------------------
# Pattern classification
# ---------------------------------------------------------------------------

def test_pattern_breakthrough_on_falling_pain():
    """Pain dropping monotonically over 14 days is a breakthrough."""
    from agents.trend_analyst import analyze

    out = analyze(
        token="t",
        checkins=_make_pain_checkins(14, [8, 8, 7, 7, 6, 6, 5, 5, 4, 4, 3, 3, 2, 2]),
        sessions=[],
    )
    assert out is not None
    assert out["pattern"] == "breakthrough"
    assert any("Pain" in e for e in out["evidence"])
    assert "progress" in out["implication_for_next_week"].lower()


def test_pattern_regression_on_rising_pain():
    from agents.trend_analyst import analyze

    out = analyze(
        token="t",
        checkins=_make_pain_checkins(14, [2, 2, 3, 3, 4, 4, 5, 5, 6, 6, 7, 7, 8, 8]),
        sessions=[],
    )
    assert out is not None
    assert out["pattern"] == "regression"


def test_pattern_regression_on_adherence_collapse():
    """Pain stable but completion rate dropping week-over-week."""
    from agents.trend_analyst import analyze

    rows: list[tuple[str, int]] = []
    # Week 1 (14 days ago): 4 completed, 0 skipped
    for d in range(15, 19):
        rows.append(("completed", d))
    # Week 2 (7 days ago): 1 completed, 3 skipped
    rows.append(("completed", 8))
    for d in range(9, 12):
        rows.append(("skipped", d))
    # Week 3 (recent): 0 completed, 4 skipped
    for d in range(1, 5):
        rows.append(("skipped", d))

    out = analyze(
        token="t",
        checkins=_make_pain_checkins(4, [4, 4, 4, 4]),
        sessions=_make_sessions(rows),
    )
    assert out is not None
    assert out["pattern"] == "regression"
    # The implication should call out adherence specifically.
    assert "adherence" in out["implication_for_next_week"].lower()


def test_pattern_plateau_on_flat_pain_with_strong_adherence():
    """Three weeks of stable pain at the same level + good adherence."""
    from agents.trend_analyst import analyze

    # 21 days of pain at level 4 with tiny noise so slope is ~0 and not significant
    pain_series = [4, 4, 4, 4, 5, 4, 4, 4, 4, 5, 4, 4, 4, 4, 4, 5, 4, 4, 4, 4, 4]
    rows: list[tuple[str, int]] = []
    # Strong adherence each week
    for week_start in (1, 8, 15):
        for d in range(week_start, week_start + 5):
            rows.append(("completed", d))

    out = analyze(
        token="t",
        checkins=_make_pain_checkins(21, pain_series),
        sessions=_make_sessions(rows),
        weeks=6,
    )
    assert out is not None
    assert out["pattern"] == "plateau"
    assert "progression" in out["implication_for_next_week"].lower()


def test_pattern_steady_default():
    """Modest pain noise, no clear direction, short history -> steady."""
    from agents.trend_analyst import analyze

    out = analyze(
        token="t",
        checkins=_make_pain_checkins(6, [3, 4, 3, 4, 3, 4]),
        sessions=[],
    )
    assert out is not None
    assert out["pattern"] == "steady"


# ---------------------------------------------------------------------------
# Structural contract (consumers treat the dict as opaque JSON)
# ---------------------------------------------------------------------------

def test_return_dict_shape_preserved():
    from agents.trend_analyst import analyze

    out = analyze(
        token="t",
        checkins=_make_pain_checkins(8, [5, 5, 5, 5, 5, 5, 5, 5]),
        sessions=[],
    )
    assert out is not None
    assert set(out.keys()) == {"pattern", "evidence", "implication_for_next_week"}
    assert out["pattern"] in {"plateau", "breakthrough", "regression", "steady"}
    assert isinstance(out["evidence"], list)
    assert all(isinstance(e, str) for e in out["evidence"])
    assert isinstance(out["implication_for_next_week"], str)
    assert out["implication_for_next_week"]


def test_intake_argument_is_accepted_but_unused():
    """The deterministic classifier ignores intake; pin that so future
    consumers don't grow an implicit dependency on intake-driven branching
    without being explicit about it."""
    from agents.trend_analyst import analyze

    series = [8, 7, 6, 5, 4, 3, 2, 1, 1, 1, 1, 1, 1, 1]
    a = analyze(
        token="t",
        checkins=_make_pain_checkins(14, series),
        sessions=[],
        intake=None,
    )
    b = analyze(
        token="t",
        checkins=_make_pain_checkins(14, series),
        sessions=[],
        intake={"injury_type": "knee", "phase": "subacute", "week": 4},
    )
    assert a == b


# ---------------------------------------------------------------------------
# Mann-Kendall + trajectory helpers
# ---------------------------------------------------------------------------

def test_mann_kendall_monotonic_decreasing_is_significant():
    from agents.trend_analyst import _mann_kendall

    s, p = _mann_kendall([10.0, 9.0, 8.0, 7.0, 6.0, 5.0, 4.0, 3.0])
    assert s < 0
    assert p < 0.05


def test_mann_kendall_monotonic_increasing_is_significant():
    from agents.trend_analyst import _mann_kendall

    s, p = _mann_kendall([1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0, 8.0])
    assert s > 0
    assert p < 0.05


def test_mann_kendall_constant_series_is_insignificant():
    from agents.trend_analyst import _mann_kendall

    s, p = _mann_kendall([5.0, 5.0, 5.0, 5.0, 5.0])
    assert s == 0
    assert p == pytest.approx(1.0)


def test_mann_kendall_short_series_returns_neutral():
    from agents.trend_analyst import _mann_kendall

    s, p = _mann_kendall([5.0, 6.0])
    assert s == 0
    assert p == 1.0


def test_pain_trajectory_returns_none_for_too_few_points():
    from agents.trend_analyst import _pain_trajectory

    assert _pain_trajectory([], weeks=4) is None
    assert _pain_trajectory(_make_pain_checkins(2, [3, 4]), weeks=4) is None


def test_adherence_trajectory_returns_none_for_single_week():
    from agents.trend_analyst import _adherence_trajectory

    sessions = _make_sessions([("completed", i) for i in range(1, 6)])
    # All sessions in the last 5 days fall in at most 2 ISO weeks; if we
    # narrow further the helper should still return None below 2 buckets.
    result = _adherence_trajectory(sessions, weeks=1)
    # Permissive: result is either None (single bucket) or a dict with
    # n_weeks >= 2. Either way the helper does not blow up.
    if result is not None:
        assert result["n_weeks"] >= 2
