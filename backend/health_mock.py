"""
health_mock.py - Wearable health data with 7-day trend history

Data priority:
  1. health_cache.json written by the iOS Shortcut via POST /health-sync
  2. Mock data (realistic declining-trend week) as fallback

Cache is considered fresh if the most recent entry is from today.
The Shortcut posts raw Apple Watch metrics; sleep_score and recovery_score
are derived here so the Shortcut payload stays simple.
"""

import json
import logging
import os
from datetime import date, timedelta
from pathlib import Path

logger = logging.getLogger(__name__)

CACHE_FILE = Path(__file__).parent.parent / "health_cache.json"
MAX_HISTORY_DAYS = 7


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def get_health_data() -> dict:
    """
    Returns today's health metrics + 7-day history.
    Prefers real Apple Watch data from cache; falls back to mock.
    """
    cache = _load_cache()
    if cache and _is_fresh(cache):
        return cache
    return get_mock_health_data()


def ingest_shortcut_payload(payload: dict) -> dict:
    """
    Accept raw metrics from the iOS Shortcut, derive scores, append to
    rolling 7-day cache, and return the completed day record.

    Expected payload fields (all optional except at least one metric):
      hrv_ms, resting_hr, sleep_hours, steps_yesterday, calories_burned
    """
    today_str = str(date.today())
    raw = {
        "date":             today_str,
        "hrv_ms":           int(payload.get("hrv_ms") or 0),
        "resting_hr":       int(payload.get("resting_hr") or 0),
        "sleep_hours":      float(payload.get("sleep_hours") or 0.0),
        "steps_yesterday":  int(payload.get("steps_yesterday") or 0),
        "calories_burned":  int(payload.get("calories_burned") or 0),
        "source":           "apple_watch",
    }

    history = _load_history()
    # Replace today's entry if it already exists (re-sync case)
    history = [d for d in history if d["date"] != today_str]
    history.append(raw)
    # Keep only last MAX_HISTORY_DAYS days
    history = sorted(history, key=lambda d: d["date"])[-MAX_HISTORY_DAYS:]

    record = _build_full_record(raw, history)
    _save_cache(record, history)
    return record


# ---------------------------------------------------------------------------
# Cache helpers
# ---------------------------------------------------------------------------

def _load_cache() -> dict | None:
    if not CACHE_FILE.exists():
        return None
    try:
        with open(CACHE_FILE) as f:
            return json.load(f)
    except Exception as e:
        logger.warning(f"Could not read health cache: {e}")
        return None


def _save_cache(record: dict, history: list) -> None:
    data = {**record, "history": history, "trend": _analyze_trend(history, record)}
    try:
        with open(CACHE_FILE, "w") as f:
            json.dump(data, f, indent=2)
    except Exception as e:
        logger.error(f"Could not write health cache: {e}")


def _load_history() -> list:
    cache = _load_cache()
    if not cache:
        return []
    return cache.get("history", [])


def _is_fresh(cache: dict) -> bool:
    """Cache is fresh if the record date is today."""
    return cache.get("date") == str(date.today())


# ---------------------------------------------------------------------------
# Score derivation
# ---------------------------------------------------------------------------

def _derive_sleep_score(sleep_hours: float) -> int:
    """
    Simple score: 8h = 100, scales linearly, floor at 0.
    Penalises both under- and over-sleeping slightly.
    """
    if sleep_hours <= 0:
        return 0
    if sleep_hours >= 9:
        return max(0, 100 - int((sleep_hours - 9) * 10))
    return min(100, int((sleep_hours / 8.0) * 100))


def _derive_recovery_score(hrv_ms: int, hrv_7day_avg: float, resting_hr: int) -> int:
    """
    Recovery score based on:
      - HRV relative to personal 7-day baseline (primary signal)
      - Resting HR as secondary signal (lower = better recovered)

    Score range: 0-100.
    """
    if hrv_ms <= 0 or hrv_7day_avg <= 0:
        return 65  # neutral default when data missing

    hrv_ratio = hrv_ms / hrv_7day_avg   # > 1 means better than baseline
    base = min(100, max(0, int(hrv_ratio * 70)))

    # Resting HR adjustment: < 60 adds points, > 70 subtracts
    hr_adj = 0
    if resting_hr > 0:
        hr_adj = int((65 - resting_hr) * 0.3)  # ~3pts per 10bpm deviation

    return min(100, max(0, base + hr_adj))


def _build_full_record(raw: dict, history: list) -> dict:
    """
    Given raw Watch metrics and history, return a complete day record
    matching the shape the rest of the pipeline expects.
    """
    hrv_values = [d["hrv_ms"] for d in history if d["hrv_ms"] > 0]
    hrv_7day_avg = round(sum(hrv_values) / len(hrv_values), 1) if hrv_values else raw["hrv_ms"]

    sleep_score    = _derive_sleep_score(raw["sleep_hours"])
    recovery_score = _derive_recovery_score(raw["hrv_ms"], hrv_7day_avg, raw["resting_hr"])

    # Derive a simple stress level from recovery score
    if recovery_score >= 75:
        stress = "low"
    elif recovery_score >= 55:
        stress = "moderate"
    else:
        stress = "high"

    return {
        **raw,
        "sleep_score":      sleep_score,
        "recovery_score":   recovery_score,
        "hrv_7day_avg":     hrv_7day_avg,
        "stress_level":     stress,
    }


# ---------------------------------------------------------------------------
# Trend analysis (unchanged logic, works on real or mock history)
# ---------------------------------------------------------------------------

def _analyze_trend(history: list, today: dict) -> dict:
    """Compute week-over-week trend signals for the coach to reference."""
    hrv_values      = [d["hrv_ms"] for d in history] + [today["hrv_ms"]]
    sleep_scores    = [d.get("sleep_score", 0) for d in history] + [today.get("sleep_score", 0)]
    recovery_scores = [d.get("recovery_score", 0) for d in history] + [today.get("recovery_score", 0)]

    hrv_start     = hrv_values[0]
    hrv_end       = hrv_values[-1]
    hrv_direction = "declining" if hrv_end < hrv_start else "improving"
    hrv_delta     = abs(hrv_end - hrv_start)

    sleep_avg    = round(sum(sleep_scores) / len(sleep_scores), 1)
    recovery_avg = round(sum(recovery_scores) / len(recovery_scores), 1)

    consecutive_low_hrv = 0
    for d in reversed(history + [today]):
        if d["hrv_ms"] < 50:
            consecutive_low_hrv += 1
        else:
            break

    return {
        "hrv_trend":             hrv_direction,
        "hrv_delta_7d":          hrv_delta,
        "hrv_trend_summary":     f"HRV has {hrv_direction} by {hrv_delta}ms over the past 7 days (from {hrv_start}ms to {hrv_end}ms)",
        "sleep_score_7day_avg":  sleep_avg,
        "recovery_7day_avg":     recovery_avg,
        "consecutive_days_low_hrv": consecutive_low_hrv,
        "weekly_insight":        _generate_insight(hrv_direction, hrv_delta, sleep_avg, consecutive_low_hrv),
    }


def _generate_insight(hrv_trend: str, hrv_delta: int, sleep_avg: float, consecutive_low: int) -> str:
    if hrv_trend == "declining" and hrv_delta >= 15:
        return (
            f"This has been a tough week. HRV has dropped significantly over 7 days "
            f"and has been below 50ms for {consecutive_low} consecutive days. "
            f"The body is showing signs of accumulated stress. Prioritise sleep and light activity today."
        )
    elif hrv_trend == "declining" and hrv_delta >= 8:
        return (
            f"A gradual decline in recovery this week. HRV trending down, "
            f"sleep scores averaging {sleep_avg}/100. Worth being intentional about wind-down tonight."
        )
    elif hrv_trend == "improving":
        return (
            f"Good trajectory this week — HRV has been climbing. "
            f"Sleep averaging {sleep_avg}/100. Keep building on this momentum."
        )
    else:
        return f"Mixed week overall. Sleep averaging {sleep_avg}/100. Consistency is key."


# ---------------------------------------------------------------------------
# Mock data (fallback)
# ---------------------------------------------------------------------------

def get_mock_health_data() -> dict:
    today = date.today()

    history = [
        {"date": str(today - timedelta(days=6)), "sleep_hours": 7.8, "sleep_score": 88, "hrv_ms": 61, "resting_hr": 62, "recovery_score": 85, "steps_yesterday": 8200, "calories_burned": 2100},
        {"date": str(today - timedelta(days=5)), "sleep_hours": 7.2, "sleep_score": 82, "hrv_ms": 58, "resting_hr": 63, "recovery_score": 80, "steps_yesterday": 7400, "calories_burned": 1980},
        {"date": str(today - timedelta(days=4)), "sleep_hours": 6.9, "sleep_score": 76, "hrv_ms": 54, "resting_hr": 65, "recovery_score": 74, "steps_yesterday": 6100, "calories_burned": 1870},
        {"date": str(today - timedelta(days=3)), "sleep_hours": 6.5, "sleep_score": 72, "hrv_ms": 50, "resting_hr": 66, "recovery_score": 70, "steps_yesterday": 5800, "calories_burned": 1760},
        {"date": str(today - timedelta(days=2)), "sleep_hours": 6.1, "sleep_score": 68, "hrv_ms": 46, "resting_hr": 67, "recovery_score": 66, "steps_yesterday": 4900, "calories_burned": 1650},
        {"date": str(today - timedelta(days=1)), "sleep_hours": 6.3, "sleep_score": 70, "hrv_ms": 44, "resting_hr": 68, "recovery_score": 64, "steps_yesterday": 5100, "calories_burned": 1700},
    ]

    today_data = {
        "date":             str(today),
        "sleep_hours":      6.2,
        "sleep_score":      71,
        "hrv_ms":           42,
        "hrv_7day_avg":     56,
        "resting_hr":       68,
        "recovery_score":   65,
        "steps_yesterday":  5100,
        "calories_burned":  1850,
        "stress_level":     "moderate",
        "source":           "mock",
    }

    trend = _analyze_trend(history, today_data)
    return {**today_data, "history": history, "trend": trend}
