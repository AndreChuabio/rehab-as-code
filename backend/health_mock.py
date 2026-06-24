from __future__ import annotations
"""
health_mock.py - Wearable health data with 7-day trend history

Data priority:
  1. Open Wearables (if HEALTH_DATA_SOURCE permits and env is configured)
  2. health_cache.json written by the iOS Shortcut via POST /health-sync
  3. Mock data (realistic declining-trend week) as fallback

Cache is considered fresh if the most recent entry is from today.
The Shortcut posts raw Apple Watch metrics; sleep_score and recovery_score
are derived here so the Shortcut payload stays simple.
"""

import json
import logging
import os
from datetime import date, timedelta
from pathlib import Path

from open_wearables_client import OpenWearablesClient, OpenWearablesConfig, OpenWearablesError

logger = logging.getLogger(__name__)

CACHE_FILE = Path(__file__).parent.parent / "health_cache.json"
MAX_HISTORY_DAYS = 7


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def get_health_data(user_token: str | None = None) -> dict:
    """
    Returns today's health metrics + 7-day history.

    Resolution precedence when a user_token is supplied:
      1. Junction (the rebrand of Vital) — when the patient has a connected
         junction_connections row. Real wearable data is purely ADDITIVE: any
         Junction failure (not connected, stale, HTTP error, missing env,
         missing DATABASE_URL, mapping error) falls through to the existing
         chain and NEVER raises to the caller.
      2. Per-token user store (data posted by the patient's iOS Shortcut).
      3. Open Wearables / Apple cache / mock (the existing HEALTH_DATA_SOURCE
         chain below).

    Without a token, Junction and the per-user store are skipped; the
    HEALTH_DATA_SOURCE mode controls the rest:
      - open_wearables: force Open Wearables read path
      - apple_cache: only use Apple cache + mock fallback
      - auto (default): try Open Wearables when configured, then cache, then mock

    Every not-connected / fallback return is stamped via _stamp_not_connected so
    the dashboard always shows full numeric defaults with a "not_connected" flag.
    """
    if user_token:
        junction = _get_junction_health_data(user_token)
        if junction:
            return junction

        from user_store import load_user
        user = load_user(user_token)
        if user and user.get("health"):
            return _stamp_not_connected(user["health"])

    source_mode = (os.getenv("HEALTH_DATA_SOURCE") or "auto").strip().lower()

    if source_mode == "open_wearables":
        ow_data = _get_open_wearables_health_data()
        if ow_data:
            return ow_data
        return _get_cache_or_mock()

    if source_mode == "apple_cache":
        return _get_cache_or_mock()

    # auto mode
    ow_data = _get_open_wearables_health_data()
    if ow_data:
        return ow_data
    return _get_cache_or_mock()


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


def _get_cache_or_mock() -> dict:
    cache = _load_cache()
    if cache and _is_fresh(cache):
        return _stamp_not_connected(cache)
    return _stamp_not_connected(get_mock_health_data())


# ---------------------------------------------------------------------------
# Junction (the rebrand of Vital) — real wearable source, additive over mock
# ---------------------------------------------------------------------------

def _stamp_not_connected(health: dict) -> dict:
    """Overlay not-connected flags WITHOUT clobbering the existing source.

    The dashboard reads `status` + `isLive` to pick the badge state. The mock /
    cache / per-user dict already carries full numeric defaults (sleep_score,
    hrv_ms, recovery_score), so the panel never renders '--' or NaN — the
    defaults are just there. `source` is preserved so the legacy apple_watch
    "Live" affordance keeps working unchanged.
    """
    return {**health, "status": "not_connected", "isLive": False}


def _get_junction_health_data(user_token: str) -> dict | None:
    """Return mapped REAL Junction metrics, or None to fall through to mock.

    Fail-open contract (product owner's hard requirement): the ENTIRE body is
    wrapped so any failure — not connected, stale cache that can't refresh,
    JunctionError, JunctionRepoError, missing env, missing DATABASE_URL, or a
    mapping bug — returns None instead of raising. The resolver then serves the
    existing mock defaults. A Junction outage can never 500 the dashboard or
    break the planner pipeline.

    PHI hygiene: logs at WARNING carry no raw metric values or vital_user_id.
    """
    try:
        import junction_repo
        from junction_repo import JunctionRepoError

        try:
            row = junction_repo.get_by_token(user_token)
        except JunctionRepoError:
            # No DATABASE_URL (local sqlite / CI) or repo unavailable — skip
            # Junction silently and let the existing chain answer.
            return None

        if not row or row.get("status") != "connected":
            return None

        cached = row.get("cached_metrics") or {}
        if not cached:
            # Connected but nothing cached yet (link returned before the first
            # webhook/refresh landed). Don't block the read path on a network
            # round trip — serve mock and let the frontend trigger
            # POST /api/junction/refresh out-of-band to populate the cache.
            return None

        # Serve the cache on the read path REGARDLESS of freshness. The read
        # path (/health-data, /chat, /context) must never make a 2-leg Junction
        # round trip — a slow/hung upstream would add seconds of synchronous
        # latency to a patient-facing request. The frontend repopulates the
        # cache asynchronously via POST /api/junction/refresh (the refresh seam).
        # Same-day cache reads as fresh ("Live"); older reads are flagged stale
        # so the UI can show "Synced {date}" instead of overstating recency.
        return _stamp_junction(
            cached, row.get("providers"), row.get("last_synced_at"), stale=not _is_fresh(cached)
        )
    except Exception:  # noqa: BLE001 — fail-open: never raise to consumers
        logger.warning("junction: read path failed, falling back to mock")
        return None


def _refresh_junction_metrics(vital_user_id: str) -> dict | None:
    """Pull the 7-day window from Junction and map into the health schema.

    Returns the full health dict (today + history + trend), or None on any
    Junction failure so the caller degrades to mock.
    """
    from junction_client import JunctionClient, JunctionError, build_config

    config = build_config()
    if not config:
        return None
    try:
        client = JunctionClient(config)
        today = date.today()
        start_date = today - timedelta(days=6)
        raw_days = client.fetch_daily(vital_user_id, start_date, today)
        return _normalize_junction_data(raw_days, today)
    except JunctionError:
        logger.warning("junction: fetch_daily failed")
        return None
    except Exception:  # noqa: BLE001 — mapping/parse failure also degrades to mock
        logger.warning("junction: unexpected error mapping data")
        return None


def _normalize_junction_data(raw_days: list[dict], today: date) -> dict:
    """Build the full 7-day health dict from raw Junction rows.

    Identical pipeline to _normalize_open_wearables_data: each day's raw metrics
    feed _build_full_record (deriving sleep_score / recovery_score / hrv_7day_avg
    locally — Junction's per-provider sleep.score is NULL for Apple/Fitbit, and
    recovery is not a free summary field), then _analyze_trend yields trend{}.
    Every clinical gate (HRV +5/-8, sleep<70, recovery<60) reads the same keys.

    Missing days are zero-filled (same as the Open Wearables path) so a partial
    window never shifts the trend window.
    """
    by_day = {d["date"]: d for d in raw_days if d.get("date")}

    rows: list[dict] = []
    raw_history: list[dict] = []
    for offset in range(6, -1, -1):
        day = today - timedelta(days=offset)
        day_key = str(day)
        raw = by_day.get(day_key) or _empty_junction_raw(day_key)
        record = _build_full_record(raw, raw_history + [raw])
        rows.append(record)
        raw_history.append(raw)

    history = rows[:-1]
    today_data = rows[-1]
    trend = _analyze_trend(history, today_data)
    return {**today_data, "history": history, "trend": trend}


def _empty_junction_raw(day_key: str) -> dict:
    return {
        "date": day_key,
        "sleep_hours": 0.0,
        "hrv_ms": 0,
        "resting_hr": 0,
        "steps_yesterday": 0,
        "calories_burned": 0,
        "source": "junction",
    }


def _provider_list(mapped: dict) -> list[str]:
    """Best-effort provider label for the cache; defaults to ['junction']."""
    src = mapped.get("source")
    if src and src != "junction":
        return [src]
    return ["junction"]


def _stamp_junction(health: dict, providers, last_synced_at=None, stale: bool = False) -> dict:
    """Overlay the connected/Live flags + a provider-derived source label.

    `last_synced_at` (junction_connections.last_synced_at, ISO string) and
    `stale` are surfaced so the frontend can show a visible "Updated {when}"
    line and reserve the "Live" wording for genuinely same-day data. A stale
    cache stays connected (real metrics, real provider) — it is just labelled
    honestly rather than presented as fresh.
    """
    provider = None
    if isinstance(providers, (list, tuple)) and providers:
        provider = providers[0]
    return {
        **health,
        "source": provider or health.get("source") or "junction",
        "data_source": "junction",
        "status": "connected",
        "isLive": True,
        "last_synced_at": last_synced_at,
        "stale": bool(stale),
    }


def _get_open_wearables_health_data() -> dict | None:
    config = _build_open_wearables_config()
    if not config:
        return None

    today = date.today()
    start_date = today - timedelta(days=6)
    try:
        client = OpenWearablesClient(config)
        daily = client.get_daily_summaries(start_date=start_date, end_date=today)
        return _normalize_open_wearables_data(daily, today)
    except OpenWearablesError as e:
        logger.warning("Open Wearables read failed, falling back: %s", e)
        return None
    except Exception as e:
        logger.warning("Unexpected Open Wearables error, falling back: %s", e)
        return None


def _build_open_wearables_config() -> OpenWearablesConfig | None:
    base_url = (os.getenv("OPEN_WEARABLES_API_URL") or "").strip()
    api_key = (os.getenv("OPEN_WEARABLES_API_KEY") or "").strip()
    user_id = (os.getenv("OPEN_WEARABLES_USER_ID") or "").strip()

    if not base_url or not api_key or not user_id:
        return None

    return OpenWearablesConfig(base_url=base_url, api_key=api_key, user_id=user_id)


def _normalize_open_wearables_data(daily: dict, today: date) -> dict:
    rows: list[dict] = []
    raw_history: list[dict] = []
    for offset in range(6, -1, -1):
        day = today - timedelta(days=offset)
        day_key = str(day)
        row = daily.get(day_key, {})
        raw = _build_open_wearables_raw(day_key, row, source="open_wearables")
        record = _build_full_record(raw, raw_history + [raw])
        rows.append(record)
        raw_history.append(raw)

    history = rows[:-1]
    today_data = rows[-1]
    trend = _analyze_trend(history, today_data)
    return {**today_data, "history": history, "trend": trend}


def _build_open_wearables_raw(day_key: str, row: dict, source: str) -> dict:
    activity = row.get("activity") or {}
    sleep = row.get("sleep") or {}
    recovery = row.get("recovery") or {}

    sleep_minutes = _to_number(sleep.get("duration_minutes"))
    sleep_hours = round(sleep_minutes / 60.0, 2) if sleep_minutes > 0 else 0.0

    hrv_ms = int(round(_to_number(recovery.get("avg_hrv_sdnn_ms"))))
    resting_hr = int(round(_to_number(recovery.get("resting_heart_rate_bpm"))))
    steps = int(round(_to_number(activity.get("steps"))))
    total_calories = int(round(_to_number(activity.get("total_calories_kcal"))))

    return {
        "date": day_key,
        "sleep_hours": sleep_hours,
        "hrv_ms": hrv_ms,
        "resting_hr": resting_hr,
        "steps_yesterday": steps,
        "calories_burned": total_calories,
        "source": source,
    }


def _to_number(value) -> float:
    try:
        if value is None:
            return 0.0
        return float(value)
    except (TypeError, ValueError):
        return 0.0


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
