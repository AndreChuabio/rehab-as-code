"""Tests for form_feedback.summarize_form().

session_repo monkeypatched so no DB is touched. Behavior contract:
  * groups completed pose sessions by exercise_id
  * sums rep_count into total_reps per exercise
  * aggregates warnings by warning id, summing counts across sessions
  * collapses per-session worst_status into the worst seen (fail > warn > good)
  * ignores non-completed rows
  * exercises_with_issues is the subset with a non-good status or any warning
  * degrades to an empty, well-formed dict when session_repo is unavailable,
    never raising
"""
from __future__ import annotations

from typing import Any


def _fake_sessions() -> list[dict[str, Any]]:
    """Two completed `knee_squat` sets with overlapping warnings, one completed
    `single_leg_balance` set, and one NON-completed row that must be ignored."""
    return [
        {
            "exercise_id": "knee_squat",
            "status": "completed",
            "created_at": "2026-06-01T10:00:00+00:00",
            "completed_at": "2026-06-01T10:05:00+00:00",
            "pose_metrics": {
                "rep_count": 8,
                "best_depth": 92.0,
                "worst_status": "warn",
                "warnings": [
                    {"id": "knee_valgus", "msg": "knees caving in", "status": "warn"},
                    {"id": "shallow_depth", "msg": "go a little deeper", "status": "warn"},
                ],
            },
        },
        {
            "exercise_id": "knee_squat",
            "status": "completed",
            "created_at": "2026-06-03T09:00:00+00:00",
            "completed_at": "2026-06-03T09:06:00+00:00",
            "pose_metrics": {
                "rep_count": 10,
                "best_depth": 88.0,
                "worst_status": "fail",
                "warnings": [
                    # overlaps the first session -> count should sum to 2
                    {"id": "knee_valgus", "msg": "knees caving in", "status": "fail"},
                ],
            },
        },
        {
            "exercise_id": "single_leg_balance",
            "status": "completed",
            "created_at": "2026-06-02T08:00:00+00:00",
            "completed_at": "2026-06-02T08:03:00+00:00",
            "pose_metrics": {
                "rep_count": 5,
                "best_depth": None,
                "worst_status": "good",
                "warnings": [],
            },
        },
        {
            # NON-completed row — must be ignored entirely.
            "exercise_id": "knee_squat",
            "status": "planned",
            "created_at": "2026-06-04T07:00:00+00:00",
            "completed_at": None,
            "pose_metrics": {
                "rep_count": 99,
                "worst_status": "fail",
                "warnings": [{"id": "knee_valgus", "msg": "x", "status": "fail"}],
            },
        },
    ]


def _by_id(exercises: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {e["exercise_id"]: e for e in exercises}


def test_rollup_groups_sums_and_aggregates(monkeypatch):
    import form_feedback

    monkeypatch.setattr(
        "session_repo.list_recent",
        lambda token, days=7: _fake_sessions(),
    )

    out = form_feedback.summarize_form("tok", window_days=28)

    # Two distinct exercises; the planned row is ignored (not 3 sessions worth).
    assert out["totals"]["n_exercises"] == 2
    assert out["totals"]["n_sessions"] == 3
    assert out["window_days"] == 28
    assert out["source"] == "completed_pose_sessions_only"
    assert out["generated_at"]

    ex = _by_id(out["exercises"])
    assert set(ex) == {"knee_squat", "single_leg_balance"}

    squat = ex["knee_squat"]
    # Grouped: two completed knee_squat sessions, reps summed 8 + 10 = 18.
    assert squat["n_sessions"] == 2
    assert squat["total_reps"] == 18
    # worst_status collapses to the worst seen across sessions.
    assert squat["worst_status"] == "fail"
    # Warnings aggregated by id: knee_valgus seen in both -> count 2;
    # shallow_depth only once -> count 1.
    wcounts = {w["id"]: w["count"] for w in squat["warnings"]}
    assert wcounts == {"knee_valgus": 2, "shallow_depth": 1}
    # Human message preserved.
    knee_valgus = next(w for w in squat["warnings"] if w["id"] == "knee_valgus")
    assert knee_valgus["msg"] == "knees caving in"
    # last_seen is the later of the two completed dates.
    assert squat["last_seen"] == "2026-06-03"

    balance = ex["single_leg_balance"]
    assert balance["n_sessions"] == 1
    assert balance["total_reps"] == 5
    assert balance["worst_status"] == "good"
    assert balance["warnings"] == []

    # Total warning count across everything: 2 (knee_valgus) + 1 (shallow) = 3.
    assert out["totals"]["n_warnings"] == 3

    # exercises_with_issues excludes the clean balance exercise.
    issues = _by_id(out["exercises_with_issues"])
    assert set(issues) == {"knee_squat"}


def test_degrades_when_session_store_unavailable(monkeypatch):
    import form_feedback

    def _boom(token, days=7):
        raise RuntimeError("DATABASE_URL missing")

    monkeypatch.setattr("session_repo.list_recent", _boom)

    out = form_feedback.summarize_form("tok")

    # Well-formed empty result, no exception.
    assert out["exercises"] == []
    assert out["exercises_with_issues"] == []
    assert out["totals"] == {"n_exercises": 0, "n_sessions": 0, "n_warnings": 0}
    assert out["window_days"] == 28
    assert out["generated_at"]
    # A degrade note is surfaced for the clinician panel.
    assert any("unavailable" in n.lower() for n in out["notes"])


def test_empty_when_no_completed_sessions(monkeypatch):
    import form_feedback

    # Only a planned row -> nothing completed.
    monkeypatch.setattr(
        "session_repo.list_recent",
        lambda token, days=7: [
            {"exercise_id": "knee_squat", "status": "planned", "pose_metrics": {}}
        ],
    )

    out = form_feedback.summarize_form("tok")
    assert out["exercises"] == []
    assert out["totals"]["n_sessions"] == 0
    assert any("no completed" in n.lower() for n in out["notes"])
