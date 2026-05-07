"""
heuristic.py - rules-based 14-day dropout risk score (Layer 1).

Pure function over the feature dict from `features.compute()`. No
training, no labels. Encodes Andre + Nikki's clinical priors as
weighted rules a clinician can read and audit.

Update workflow:
  * Tweak the weights or thresholds below.
  * Run `pytest backend/tests/test_risk_cohort.py -v` (synthetic feature
    dicts -> expected band).
  * Commit. Every constant has a comment justifying its weight - don't
    drop those when tuning, leave a paper trail.

When production data accumulates and the team wants to A/B against an
XGBoost: the contract of `score(features) -> {risk, band, factors}` does
NOT change. predict.py swaps the implementation; this file stays as the
fallback / interpretable comparison.
"""
from __future__ import annotations

from typing import Any

# ---------------------------------------------------------------------------
# Tunable constants (review weekly with Nikki)
# ---------------------------------------------------------------------------

# Missed sessions: 3+ consecutive days no completion -> patient is sliding.
# This is the strongest signal in the heuristic; weight reflects that.
MISSED_STREAK_DAYS = 3
MISSED_STREAK_WEIGHT = 0.30

# Completion rate over last 7 days. <30% over a week is a red flag - the
# patient is opening sessions but not finishing them.
COMPLETION_RATE_7D_FLOOR = 0.30
COMPLETION_RATE_WEIGHT = 0.30

# Pain trend per day. Positive = worsening. The threshold is small because
# pain is on a 0-10 scale; +0.05/day is +0.7 over 14 days.
PAIN_SLOPE_PER_DAY = 0.05
PAIN_SLOPE_WEIGHT = 0.20

# Patient hasn't checked in for 5+ days. Even if sessions are completed,
# silence on the pain side is a relationship signal worth catching.
NO_CHECKIN_DAYS = 5
NO_CHECKIN_WEIGHT = 0.20

# Risk -> band cutoffs. high -> top of dashboard, action required.
BAND_HIGH_THRESHOLD = 0.70
BAND_MED_THRESHOLD = 0.40


def score(features: dict[str, Any]) -> dict[str, Any]:
    """Map feature dict to {risk, band, factors, layer}.

    Returns
    -------
    dict
      risk    : float 0-1
      band    : "high" | "med" | "low"
      factors : list[dict] - which rules fired, with the weight and the
                feature value that triggered them. Clinician-facing.
      layer   : "heuristic" - so the dashboard can label predictions and
                we can A/B against a future "xgb" layer cleanly.
    """
    risk = 0.0
    factors: list[dict[str, Any]] = []

    streak = features.get("missed_session_streak") or 0
    if streak >= MISSED_STREAK_DAYS:
        risk += MISSED_STREAK_WEIGHT
        factors.append({
            "rule": "missed_session_streak",
            "weight": MISSED_STREAK_WEIGHT,
            "value": streak,
            "summary": f"{streak} day streak with no completed sessions",
        })

    cr7 = features.get("completion_rate_7d")
    if cr7 is not None and cr7 < COMPLETION_RATE_7D_FLOOR:
        risk += COMPLETION_RATE_WEIGHT
        factors.append({
            "rule": "completion_rate_7d",
            "weight": COMPLETION_RATE_WEIGHT,
            "value": cr7,
            "summary": f"Completion rate {cr7:.0%} over last 7 days",
        })

    pain_slope = features.get("pain_slope_per_day_14d")
    if pain_slope is not None and pain_slope > PAIN_SLOPE_PER_DAY:
        risk += PAIN_SLOPE_WEIGHT
        factors.append({
            "rule": "pain_slope_per_day_14d",
            "weight": PAIN_SLOPE_WEIGHT,
            "value": pain_slope,
            "summary": f"Pain trending up at {pain_slope:+.3f}/day",
        })

    days_since_checkin = features.get("days_since_last_checkin")
    if days_since_checkin is not None and days_since_checkin >= NO_CHECKIN_DAYS:
        risk += NO_CHECKIN_WEIGHT
        factors.append({
            "rule": "days_since_last_checkin",
            "weight": NO_CHECKIN_WEIGHT,
            "value": days_since_checkin,
            "summary": f"No checkin in {days_since_checkin} days",
        })

    risk = round(min(risk, 1.0), 3)
    band = (
        "high" if risk >= BAND_HIGH_THRESHOLD
        else "med" if risk >= BAND_MED_THRESHOLD
        else "low"
    )
    return {
        "risk": risk,
        "band": band,
        "factors": factors,
        "layer": "heuristic",
    }
