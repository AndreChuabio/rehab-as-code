"""
test_risk_cohort - Slot 2 coverage for ml.adherence + /clinician/risk-cohort.

Three layers:
  1. heuristic.score(features)         - rule firing on synthetic features
  2. features.compute(snapshot)        - snapshot -> feature dict
  3. /clinician/risk-cohort endpoint   - auth gate + ordering + return shape

The pipeline is deterministic by design (no Anthropic, no clocks beyond
the explicit `now` arg in features.compute), so every test is hermetic.
"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

_BACKEND_DIR = Path(__file__).resolve().parent.parent
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))


# ---------------------------------------------------------------------------
# Layer 1: heuristic
# ---------------------------------------------------------------------------

def _features(**overrides) -> dict:
    """Build a feature dict with defaults that score 0 risk. Overrides
    flip individual signals so each test asserts a single rule firing."""
    base = {
        "days_since_last_checkin": 0,
        "days_since_last_completed": 0,
        "missed_session_streak": 0,
        "sessions_completed_7d": 5,
        "sessions_completed_14d": 10,
        "sessions_planned_14d": 10,
        "completion_rate_7d": 1.0,
        "completion_rate_14d": 1.0,
        "pain_avg_7d": 3.0,
        "pain_avg_prev7d": 3.0,
        "pain_slope_per_day_14d": 0.0,
        "n_pain_checkins_14d": 5,
        "injury_category": "knee",
        "days_since_intake": 14,
    }
    base.update(overrides)
    return base


def test_heuristic_scores_zero_for_engaged_patient():
    from ml.adherence.heuristic import score
    out = score(_features())
    assert out["risk"] == 0.0
    assert out["band"] == "low"
    assert out["factors"] == []
    assert out["layer"] == "heuristic"


def test_heuristic_fires_on_missed_streak():
    from ml.adherence.heuristic import score, MISSED_STREAK_WEIGHT
    out = score(_features(missed_session_streak=4))
    assert out["risk"] == pytest.approx(MISSED_STREAK_WEIGHT)
    assert out["band"] == "low"  # single rule isn't enough for med
    assert any(f["rule"] == "missed_session_streak" for f in out["factors"])


def test_heuristic_fires_on_low_completion_rate():
    from ml.adherence.heuristic import score, COMPLETION_RATE_WEIGHT
    out = score(_features(completion_rate_7d=0.20))
    assert out["risk"] == pytest.approx(COMPLETION_RATE_WEIGHT)
    assert any(f["rule"] == "completion_rate_7d" for f in out["factors"])


def test_heuristic_fires_on_pain_uptrend():
    from ml.adherence.heuristic import score, PAIN_SLOPE_WEIGHT
    out = score(_features(pain_slope_per_day_14d=0.10))
    assert out["risk"] == pytest.approx(PAIN_SLOPE_WEIGHT)
    assert any(f["rule"] == "pain_slope_per_day_14d" for f in out["factors"])


def test_heuristic_fires_on_no_checkin_silence():
    from ml.adherence.heuristic import score, NO_CHECKIN_WEIGHT
    out = score(_features(days_since_last_checkin=7))
    assert out["risk"] == pytest.approx(NO_CHECKIN_WEIGHT)
    assert any(f["rule"] == "days_since_last_checkin" for f in out["factors"])


def test_heuristic_combines_into_band_high():
    """All four signals firing -> risk caps at 1.0, band is high."""
    from ml.adherence.heuristic import score
    out = score(_features(
        missed_session_streak=5,
        completion_rate_7d=0.10,
        pain_slope_per_day_14d=0.20,
        days_since_last_checkin=7,
    ))
    assert out["risk"] == 1.0
    assert out["band"] == "high"
    assert len(out["factors"]) == 4


def test_heuristic_med_band_on_two_signals():
    from ml.adherence.heuristic import score
    out = score(_features(
        missed_session_streak=4,
        pain_slope_per_day_14d=0.10,
    ))
    # 0.30 + 0.20 = 0.50 -> med band
    assert out["risk"] == pytest.approx(0.50)
    assert out["band"] == "med"


def test_heuristic_handles_none_safely():
    """Missing values must not crash; rules silently abstain."""
    from ml.adherence.heuristic import score
    out = score({})
    assert out["risk"] == 0.0
    assert out["band"] == "low"
    assert out["factors"] == []


# ---------------------------------------------------------------------------
# Layer 2: features.compute
# ---------------------------------------------------------------------------

def _snapshot(**overrides) -> dict:
    base = {
        "token": "patient-1",
        "snapshot_at": "2026-05-07T12:00:00Z",
        "intake": None,
        "checkins": [],
        "sessions": [],
        "user": {"injury_category": "knee", "patient_name": "Test"},
    }
    base.update(overrides)
    return base


def test_features_empty_snapshot_returns_defaults():
    from ml.adherence.features import compute
    feat = compute(_snapshot())
    assert feat["days_since_last_checkin"] is None
    assert feat["days_since_last_completed"] is None
    assert feat["missed_session_streak"] >= 1
    assert feat["sessions_completed_14d"] == 0
    assert feat["completion_rate_14d"] is None


def test_features_completion_rate_computed():
    from ml.adherence.features import compute
    now = datetime(2026, 5, 7, 12, 0, tzinfo=timezone.utc)
    sessions = []
    # 4 completed, 1 skipped, 5 planned -> 4/10 = 0.4 over 14d
    for i in range(4):
        sessions.append({
            "status": "completed",
            "created_at": (now - timedelta(days=i + 1)).isoformat(),
            "completed_at": (now - timedelta(days=i + 1)).isoformat(),
        })
    sessions.append({
        "status": "skipped",
        "created_at": (now - timedelta(days=2)).isoformat(),
    })
    for i in range(5):
        sessions.append({
            "status": "planned",
            "created_at": (now - timedelta(days=i + 1)).isoformat(),
        })
    feat = compute(_snapshot(sessions=sessions), now=now)
    assert feat["sessions_completed_14d"] == 4
    assert feat["sessions_planned_14d"] == 10
    assert feat["completion_rate_14d"] == pytest.approx(0.4)


def test_features_pain_slope_detects_uptrend():
    from ml.adherence.features import compute
    now = datetime(2026, 5, 7, 12, 0, tzinfo=timezone.utc)
    # Pain rising from 2 -> 8 over 14 days
    pain_series = [2, 2, 3, 3, 4, 4, 5, 5, 6, 6, 7, 7, 8, 8]
    checkins = [
        {
            "kind": "checkin",
            "pain_level": p,
            "recorded_at": (now - timedelta(days=14 - i)).isoformat(),
        }
        for i, p in enumerate(pain_series)
    ]
    feat = compute(_snapshot(checkins=checkins), now=now)
    assert feat["pain_slope_per_day_14d"] is not None
    assert feat["pain_slope_per_day_14d"] > 0.05
    assert feat["n_pain_checkins_14d"] == 14


def test_features_missed_streak_counts_back_to_last_completion():
    from ml.adherence.features import compute
    now = datetime(2026, 5, 7, 12, 0, tzinfo=timezone.utc)
    # Last completion was 4 days ago.
    sessions = [{
        "status": "completed",
        "created_at": (now - timedelta(days=4)).isoformat(),
        "completed_at": (now - timedelta(days=4)).isoformat(),
    }]
    feat = compute(_snapshot(sessions=sessions), now=now)
    assert feat["missed_session_streak"] == 4


# ---------------------------------------------------------------------------
# Endpoint: /clinician/risk-cohort
# ---------------------------------------------------------------------------

@pytest.fixture
def stub_predict(monkeypatch):
    """Replace ml.adherence.predict.score with a deterministic stub keyed
    by token -> band, so we can assert ordering without exercising the
    full snapshot pipeline (covered by the layer-1/layer-2 tests above)."""
    import ml.adherence.predict as predict

    state: dict[str, dict] = {
        "patient-high": {
            "token": "patient-high", "risk": 0.85, "band": "high",
            "factors": [{"rule": "missed_session_streak"}],
            "layer": "heuristic", "patient_name": "High",
            "injury_category": "knee", "snapshot_at": "2026-05-07T12:00:00Z",
        },
        "patient-med": {
            "token": "patient-med", "risk": 0.50, "band": "med",
            "factors": [], "layer": "heuristic", "patient_name": "Med",
            "injury_category": "ankle", "snapshot_at": "2026-05-07T12:00:00Z",
        },
        "patient-low": {
            "token": "patient-low", "risk": 0.10, "band": "low",
            "factors": [], "layer": "heuristic", "patient_name": "Low",
            "injury_category": "knee", "snapshot_at": "2026-05-07T12:00:00Z",
        },
    }
    monkeypatch.setattr(predict, "score", lambda token: state[token])
    return state


def test_risk_cohort_unauthed_is_rejected(unauthed_client):
    resp = unauthed_client.get("/clinician/risk-cohort")
    assert resp.status_code == 401


def test_risk_cohort_returns_sorted_by_risk_desc(
    authed_clinician_client, monkeypatch, stub_predict,
):
    import user_store
    monkeypatch.setattr(
        user_store, "list_active_tokens",
        lambda days=21, limit=500: ["patient-low", "patient-high", "patient-med"],
    )

    resp = authed_clinician_client.get("/clinician/risk-cohort")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    bands = [p["band"] for p in body["patients"]]
    assert bands == ["high", "med", "low"]
    assert body["window_days"] == 21
    assert "scored_at" in body


def test_risk_cohort_skips_failing_score(
    authed_clinician_client, monkeypatch, stub_predict,
):
    """A scoring failure on one patient must not 500 the whole cohort;
    surfaces an exception log, drops the patient, returns the rest."""
    import user_store
    import ml.adherence.predict as predict

    def _score(token: str):
        if token == "patient-broken":
            raise RuntimeError("synthetic failure")
        return stub_predict[token]

    monkeypatch.setattr(predict, "score", _score)
    monkeypatch.setattr(
        user_store, "list_active_tokens",
        lambda days=21, limit=500: ["patient-broken", "patient-high"],
    )

    resp = authed_clinician_client.get("/clinician/risk-cohort")
    assert resp.status_code == 200
    body = resp.json()
    tokens = [p["token"] for p in body["patients"]]
    assert tokens == ["patient-high"]
