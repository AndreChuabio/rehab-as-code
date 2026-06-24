"""Junction (the rebrand of Vital) integration tests.

Covers:
  1. Field -> health-schema mapping
       - sleep.total SECONDS / 3600 -> sleep_hours (the units gotcha)
       - average_hrv -> hrv_ms; hr_lowest -> resting_hr (activity fallback)
       - activity.steps -> steps_yesterday; calories_total -> calories_burned
       - NULL sleep.score -> derived sleep_score (Apple/Fitbit never blank)
       - recovery_score derived via _derive_recovery_score
       - hrv_7day_avg is the running mean; trend{} carries the required keys
  2. get_health_data fallback branches (the product-owner's fail-open contract)
       - CONNECTED + FRESH cache  -> real metrics, isLive=True, source=junction
       - NOT CONNECTED            -> mock defaults, isLive=False, not_connected
       - FETCH-FAIL               -> mock, never raises
  3. Clinical-gate integrity: the mapped dict exposes the same keys the
     planner/safety gates read (HRV +5/-8, sleep<70, recovery<60).
  4. Router endpoints (/api/junction/link|status|refresh) — authed, key never
     leaked, refresh non-fatal on client error.

All Junction HTTP is mocked; no live API calls. junction_repo._conn is never
hit (the resolver catches JunctionRepoError, or we stub junction_repo helpers).
"""
from __future__ import annotations

from datetime import date, timedelta

import pytest

import health_mock
from junction_client import JunctionClient, JunctionConfig, JunctionError


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------

def _sleep_day(day: str, *, total_seconds: float, hrv: float | None, hr_lowest, score):
    return {
        "calendar_date": day,
        "total": total_seconds,
        "average_hrv": hrv,
        "hr_lowest": hr_lowest,
        "hr_average": 58,
        "score": score,
    }


def _activity_day(day: str, *, steps, calories, resting_bpm):
    return {
        "calendar_date": day,
        "steps": steps,
        "calories_total": calories,
        "heart_rate": {"resting_bpm": resting_bpm},
    }


def _seven_days():
    """Return (sleep_rows, activity_rows) for the trailing 7-day window."""
    today = date.today()
    sleep_rows = []
    activity_rows = []
    for offset in range(6, -1, -1):
        day = str(today - timedelta(days=offset))
        # 7.0h sleep -> 25200s; null score (Apple/Fitbit case) on every day.
        sleep_rows.append(
            _sleep_day(day, total_seconds=25200, hrv=55.4, hr_lowest=60, score=None)
        )
        activity_rows.append(
            _activity_day(day, steps=8000, calories=2100, resting_bpm=61)
        )
    return sleep_rows, activity_rows


class _FakeResponse:
    def __init__(self, status_code=200, json_body=None):
        self.status_code = status_code
        self._json = json_body or {}

    def json(self):
        return self._json

    def raise_for_status(self):
        if self.status_code >= 400:
            import httpx

            raise httpx.HTTPStatusError("err", request=None, response=None)


# ---------------------------------------------------------------------------
# 1. Mapping correctness (client + normalize)
# ---------------------------------------------------------------------------

def test_fetch_daily_maps_sleep_and_activity(monkeypatch):
    sleep_rows, activity_rows = _seven_days()

    def _fake_get(self, client, path, params, envelope_key):
        return sleep_rows if envelope_key == "sleep" else activity_rows

    monkeypatch.setattr(JunctionClient, "_get_list", _fake_get)

    client = JunctionClient(JunctionConfig(api_key="k", base_url="https://x"))
    today = date.today()
    raw = client.fetch_daily("uid", today - timedelta(days=6), today)

    assert len(raw) == 7
    row = raw[-1]
    # SECONDS / 3600 -> hours (NOT minutes/60).
    assert row["sleep_hours"] == 7.0
    assert row["hrv_ms"] == 55  # round(55.4)
    assert row["resting_hr"] == 60  # hr_lowest wins over activity.resting_bpm
    assert row["steps_yesterday"] == 8000
    assert row["calories_burned"] == 2100
    assert row["source"] == "junction"


def test_fetch_daily_resting_hr_falls_back_to_activity(monkeypatch):
    today = str(date.today())
    sleep_rows = [_sleep_day(today, total_seconds=25200, hrv=50, hr_lowest=None, score=None)]
    activity_rows = [_activity_day(today, steps=1000, calories=900, resting_bpm=58)]

    def _fake_get(self, client, path, params, envelope_key):
        return sleep_rows if envelope_key == "sleep" else activity_rows

    monkeypatch.setattr(JunctionClient, "_get_list", _fake_get)
    client = JunctionClient(JunctionConfig(api_key="k", base_url="https://x"))
    raw = client.fetch_daily("uid", date.today(), date.today())
    assert raw[0]["resting_hr"] == 58  # activity.heart_rate.resting_bpm fallback


def test_fetch_daily_null_hrv_does_not_crash(monkeypatch):
    today = str(date.today())
    sleep_rows = [_sleep_day(today, total_seconds=0, hrv=None, hr_lowest=None, score=None)]
    activity_rows = [_activity_day(today, steps=None, calories=None, resting_bpm=None)]

    def _fake_get(self, client, path, params, envelope_key):
        return sleep_rows if envelope_key == "sleep" else activity_rows

    monkeypatch.setattr(JunctionClient, "_get_list", _fake_get)
    client = JunctionClient(JunctionConfig(api_key="k", base_url="https://x"))
    raw = client.fetch_daily("uid", date.today(), date.today())
    assert raw[0]["hrv_ms"] == 0
    assert raw[0]["sleep_hours"] == 0.0


def test_normalize_derives_scores_with_null_provider_score():
    """Null sleep.score (Apple/Fitbit) -> derived sleep_score; panel never blank."""
    sleep_rows, activity_rows = _seven_days()
    client = JunctionClient(JunctionConfig(api_key="k", base_url="https://x"))
    raw = client._merge_daily(sleep_rows, activity_rows)
    mapped = health_mock._normalize_junction_data(raw, date.today())

    # sleep_score derived from sleep_hours, NOT the (null) provider score.
    assert mapped["sleep_score"] == health_mock._derive_sleep_score(7.0)
    assert mapped["sleep_score"] > 0
    # recovery derived locally (no free Junction recovery field).
    assert mapped["recovery_score"] == health_mock._derive_recovery_score(
        mapped["hrv_ms"], mapped["hrv_7day_avg"], mapped["resting_hr"]
    )
    # hrv_7day_avg is the running mean of the 7 rows.
    assert mapped["hrv_7day_avg"] == 55.0
    # trend{} carries every key downstream consumers read.
    trend = mapped["trend"]
    for key in (
        "hrv_trend",
        "hrv_delta_7d",
        "hrv_trend_summary",
        "sleep_score_7day_avg",
        "recovery_7day_avg",
        "consecutive_days_low_hrv",
        "weekly_insight",
    ):
        assert key in trend


def test_normalize_zero_fills_missing_days():
    """A short window still produces a 7-row history (zero-filled gaps)."""
    today = str(date.today())
    sleep_rows = [_sleep_day(today, total_seconds=25200, hrv=60, hr_lowest=55, score=None)]
    activity_rows = [_activity_day(today, steps=5000, calories=1800, resting_bpm=55)]
    client = JunctionClient(JunctionConfig(api_key="k", base_url="https://x"))
    raw = client._merge_daily(sleep_rows, activity_rows)
    mapped = health_mock._normalize_junction_data(raw, date.today())
    assert len(mapped["history"]) == 6  # 7-day window minus today
    # Backfilled gap days carry zeroed raw metrics, not non-zero defaults.
    earliest = mapped["history"][0]
    assert earliest["hrv_ms"] == 0
    assert earliest["sleep_hours"] == 0.0


# ---------------------------------------------------------------------------
# 2. Clinical-gate integrity
# ---------------------------------------------------------------------------

def test_mapped_dict_exposes_clinical_gate_keys():
    sleep_rows, activity_rows = _seven_days()
    client = JunctionClient(JunctionConfig(api_key="k", base_url="https://x"))
    raw = client._merge_daily(sleep_rows, activity_rows)
    mapped = health_mock._normalize_junction_data(raw, date.today())

    # The exact keys the HRV +5/-8, sleep<70, recovery<60 gates read.
    assert isinstance(mapped["hrv_ms"], int)
    assert isinstance(mapped["hrv_7day_avg"], (int, float))
    assert isinstance(mapped["sleep_score"], int)
    assert isinstance(mapped["recovery_score"], int)
    assert isinstance(mapped["trend"]["sleep_score_7day_avg"], (int, float))
    assert isinstance(mapped["trend"]["recovery_7day_avg"], (int, float))


# ---------------------------------------------------------------------------
# 3. get_health_data fallback branches
# ---------------------------------------------------------------------------

def test_get_health_data_connected_fresh_returns_real(monkeypatch):
    """CONNECTED + FRESH cache -> real metrics flagged junction/Live."""
    today = str(date.today())
    cached = {**health_mock.get_mock_health_data(), "date": today, "sleep_score": 91}

    monkeypatch.setattr(
        "junction_repo.get_by_token",
        lambda token: {
            "status": "connected",
            "providers": ["oura"],
            "vital_user_id": "vid",
            "cached_metrics": cached,
        },
    )
    # Per-user store must NOT be consulted before Junction; guard it anyway.
    monkeypatch.setattr("user_store.load_user", lambda token: None)

    out = health_mock.get_health_data(user_token="patient-1")
    assert out["data_source"] == "junction"
    assert out["status"] == "connected"
    assert out["isLive"] is True
    assert out["source"] == "oura"  # provider label
    assert out["sleep_score"] == 91


def test_get_health_data_connected_stale_serves_cache_no_network(monkeypatch):
    """CONNECTED + STALE cache -> serve the cache flagged stale, NO inline fetch.

    The read path must never make a Junction round trip (a slow upstream would
    add seconds of synchronous latency to /chat, /context, /health-data). The
    frontend repopulates the cache out-of-band via POST /api/junction/refresh.
    """
    stale = {**health_mock.get_mock_health_data(), "date": "2000-01-01", "sleep_score": 77}
    monkeypatch.setattr(
        "junction_repo.get_by_token",
        lambda token: {
            "status": "connected",
            "providers": ["garmin"],
            "vital_user_id": "vid",
            "last_synced_at": "2000-01-01T08:00:00+00:00",
            "cached_metrics": stale,
        },
    )

    # If the read path ever tried to refresh inline, this would blow up — proving
    # the resolver does NOT touch the network on a stale read.
    def _must_not_refresh(vid):
        raise AssertionError("read path must not refresh inline")

    monkeypatch.setattr("health_mock._refresh_junction_metrics", _must_not_refresh)

    out = health_mock.get_health_data(user_token="patient-1")
    assert out["status"] == "connected"
    assert out["isLive"] is True
    assert out["stale"] is True  # day-old cache is flagged, not presented as fresh
    assert out["last_synced_at"] == "2000-01-01T08:00:00+00:00"
    assert out["sleep_score"] == 77  # the cached (real) value is served as-is


def test_get_health_data_connected_no_cache_returns_mock(monkeypatch):
    """CONNECTED but cache not yet populated -> mock; read path does not fetch."""
    monkeypatch.setattr(
        "junction_repo.get_by_token",
        lambda token: {
            "status": "connected",
            "providers": ["oura"],
            "vital_user_id": "vid",
            "cached_metrics": None,
        },
    )

    def _must_not_refresh(vid):
        raise AssertionError("read path must not refresh inline")

    monkeypatch.setattr("health_mock._refresh_junction_metrics", _must_not_refresh)
    monkeypatch.setattr("user_store.load_user", lambda token: None)

    out = health_mock.get_health_data(user_token="patient-1")
    assert out["status"] == "not_connected"
    assert out["isLive"] is False
    assert out["sleep_score"] > 0  # mock defaults present


def test_get_health_data_not_connected_returns_mock(monkeypatch):
    """NOT CONNECTED -> mock defaults, isLive False, full numeric defaults."""
    monkeypatch.setattr("junction_repo.get_by_token", lambda token: None)
    monkeypatch.setattr("user_store.load_user", lambda token: None)

    out = health_mock.get_health_data(user_token="patient-1")
    assert out["status"] == "not_connected"
    assert out["isLive"] is False
    # full numeric defaults present — panel never shows '--'.
    assert out["sleep_score"] is not None and out["sleep_score"] > 0
    assert out["hrv_ms"] is not None and out["hrv_ms"] > 0
    assert out["recovery_score"] is not None and out["recovery_score"] > 0


def test_get_health_data_pending_status_returns_mock(monkeypatch):
    """A row exists but status != connected -> still mock defaults."""
    monkeypatch.setattr(
        "junction_repo.get_by_token", lambda token: {"status": "pending"}
    )
    monkeypatch.setattr("user_store.load_user", lambda token: None)
    out = health_mock.get_health_data(user_token="patient-1")
    assert out["status"] == "not_connected"
    assert out["isLive"] is False


def test_get_health_data_repo_raises_returns_mock(monkeypatch):
    """No DATABASE_URL (JunctionRepoError) -> skip Junction, serve mock; no raise."""
    from junction_repo import JunctionRepoError

    def _boom(token):
        raise JunctionRepoError("DATABASE_URL is required")

    monkeypatch.setattr("junction_repo.get_by_token", _boom)
    monkeypatch.setattr("user_store.load_user", lambda token: None)

    out = health_mock.get_health_data(user_token="patient-1")
    assert out["status"] == "not_connected"
    assert out["isLive"] is False
    assert out["sleep_score"] > 0


def test_refresh_metrics_swallows_junction_error_returns_none(monkeypatch):
    """FETCH-FAIL: _refresh_junction_metrics catches JunctionError -> None.

    This is the real fetch-fail seam now that the read path serves the cache
    instead of fetching inline. /api/junction/refresh calls this helper; a None
    return there is turned into status='error' (HTTP 200), never a 5xx.
    """
    monkeypatch.setenv("VITAL_API_KEY", "k")

    def _raise(self, user_id, start_date, end_date):
        raise JunctionError("upstream 500")

    monkeypatch.setattr("junction_client.JunctionClient.fetch_daily", _raise)
    assert health_mock._refresh_junction_metrics("vid") is None


def test_refresh_metrics_returns_none_when_unconfigured(monkeypatch):
    """No API key -> _refresh_junction_metrics returns None (degrade to mock)."""
    monkeypatch.delenv("VITAL_API_KEY", raising=False)
    monkeypatch.delenv("JUNCTION_API_KEY", raising=False)
    assert health_mock._refresh_junction_metrics("vid") is None


def test_get_health_data_generic_exception_never_throws(monkeypatch):
    """Any generic Exception in the Junction path -> mock, consumer gets a dict."""

    def _boom(token):
        raise RuntimeError("unexpected")

    monkeypatch.setattr("junction_repo.get_by_token", _boom)
    monkeypatch.setattr("user_store.load_user", lambda token: None)

    out = health_mock.get_health_data(user_token="patient-1")
    assert isinstance(out, dict)
    assert out["status"] == "not_connected"


def test_get_health_data_no_token_unaffected():
    """No token -> Junction skipped entirely; existing mock/cache path."""
    out = health_mock.get_health_data()
    assert isinstance(out, dict)
    assert out.get("sleep_score") is not None


# ---------------------------------------------------------------------------
# 4. create_or_get_user idempotency
# ---------------------------------------------------------------------------

def test_create_or_get_user_reuses_existing_on_conflict(monkeypatch):
    import httpx

    class _Client:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def post(self, url, headers=None, json=None):
            return _FakeResponse(status_code=409, json_body={"user_id": "existing-uid"})

    monkeypatch.setattr(httpx, "Client", _Client)
    client = JunctionClient(JunctionConfig(api_key="k", base_url="https://x"))
    assert client.create_or_get_user("token-1") == "existing-uid"


def test_create_or_get_user_returns_new_id(monkeypatch):
    import httpx

    class _Client:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def post(self, url, headers=None, json=None):
            return _FakeResponse(status_code=200, json_body={"user_id": "new-uid"})

    monkeypatch.setattr(httpx, "Client", _Client)
    client = JunctionClient(JunctionConfig(api_key="k", base_url="https://x"))
    assert client.create_or_get_user("token-1") == "new-uid"


# ---------------------------------------------------------------------------
# 5. Router endpoints (FastAPI TestClient, authed)
# ---------------------------------------------------------------------------

def test_link_returns_url_and_never_leaks_key(authed_client, monkeypatch):
    monkeypatch.setenv("VITAL_API_KEY", "secret-team-key")
    monkeypatch.setattr(
        "junction_client.JunctionClient.create_or_get_user", lambda self, client_user_id: "vid"
    )
    monkeypatch.setattr("junction_repo.upsert_pending", lambda token, vid: {"status": "pending"})
    monkeypatch.setattr(
        "junction_client.JunctionClient.create_link_token",
        lambda self, user_id, redirect_url=None: {
            "link_token": "lt",
            "link_web_url": "https://link.junction.com/abc",
        },
    )

    res = authed_client.post("/api/junction/link")
    assert res.status_code == 200
    body = res.json()
    assert body["link_web_url"] == "https://link.junction.com/abc"
    assert body["status"] == "pending"
    # Key + raw link_token must never reach the browser.
    assert "secret-team-key" not in res.text
    assert "link_token" not in body


def test_link_503_when_not_configured(authed_client, monkeypatch):
    monkeypatch.delenv("VITAL_API_KEY", raising=False)
    monkeypatch.delenv("JUNCTION_API_KEY", raising=False)
    res = authed_client.post("/api/junction/link")
    assert res.status_code == 503


def test_link_requires_auth(unauthed_client):
    res = unauthed_client.post("/api/junction/link")
    assert res.status_code in (401, 403)


def test_status_no_row_is_benign(authed_client, monkeypatch):
    monkeypatch.setattr("junction_repo.get_by_token", lambda token: None)
    res = authed_client.get("/api/junction/status")
    assert res.status_code == 200
    body = res.json()
    assert body["status"] == "not_connected"
    assert body["isLive"] is False
    assert body["providers"] == []


def test_status_connected_no_raw_metrics(authed_client, monkeypatch):
    monkeypatch.setattr(
        "junction_repo.get_by_token",
        lambda token: {
            "status": "connected",
            "providers": ["oura"],
            "last_synced_at": "2026-06-24T10:00:00+00:00",
            "cached_metrics": {"sleep_score": 88, "hrv_ms": 60},
        },
    )
    res = authed_client.get("/api/junction/status")
    body = res.json()
    assert body["status"] == "connected"
    assert body["isLive"] is True
    # No raw metric values in the status response.
    assert "sleep_score" not in res.text
    assert "hrv_ms" not in res.text


def test_refresh_non_fatal_on_client_error(authed_client, monkeypatch):
    monkeypatch.setenv("VITAL_API_KEY", "k")
    monkeypatch.setattr(
        "junction_repo.get_by_token", lambda token: {"vital_user_id": "vid"}
    )

    def _raise(self, user_id, start_date, end_date):
        raise JunctionError("upstream 500")

    monkeypatch.setattr("junction_client.JunctionClient.fetch_daily", _raise)
    monkeypatch.setattr("junction_repo.set_error", lambda token: None)

    res = authed_client.post("/api/junction/refresh")
    assert res.status_code == 200  # non-fatal
    assert res.json()["status"] == "error"


def test_refresh_404_when_no_connection(authed_client, monkeypatch):
    monkeypatch.setattr("junction_repo.get_by_token", lambda token: None)
    res = authed_client.post("/api/junction/refresh")
    assert res.status_code == 404


def test_refresh_happy_path(authed_client, monkeypatch):
    monkeypatch.setenv("VITAL_API_KEY", "k")
    monkeypatch.setattr(
        "junction_repo.get_by_token", lambda token: {"vital_user_id": "vid"}
    )
    sleep_rows, activity_rows = _seven_days()
    raw = JunctionClient(JunctionConfig(api_key="k", base_url="https://x"))._merge_daily(
        sleep_rows, activity_rows
    )
    monkeypatch.setattr(
        "junction_client.JunctionClient.fetch_daily",
        lambda self, user_id, start_date, end_date: raw,
    )
    monkeypatch.setattr(
        "junction_repo.set_connected",
        lambda token, providers, metrics: {"last_synced_at": "2026-06-24T11:00:00+00:00"},
    )
    res = authed_client.post("/api/junction/refresh")
    assert res.status_code == 200
    assert res.json()["status"] == "connected"
