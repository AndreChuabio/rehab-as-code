"""
mock_open_wearables_server.py

Minimal Open Wearables API mock — runs on port 8001.
Returns 7 days of realistic wearable data so the health_mock.py
open_wearables path can be exercised without a real account.

Start with:
    python backend/scripts/mock_open_wearables_server.py
"""

from __future__ import annotations

import math
from datetime import date, timedelta

from fastapi import FastAPI, Header, HTTPException, Query
from fastapi.responses import JSONResponse

app = FastAPI(title="Open Wearables Mock")

API_KEY = "sk-mock-open-wearables"
USER_ID = "mock-user-001"


def _check_auth(x_open_wearables_api_key: str = Header(default="")):
    if x_open_wearables_api_key != API_KEY:
        raise HTTPException(status_code=401, detail="invalid api key")


def _day_seed(d: date) -> float:
    """Deterministic per-day value in [0,1] so data looks consistent."""
    return (math.sin(d.toordinal() * 0.7) + 1) / 2


def _activity(d: date) -> dict:
    s = _day_seed(d)
    return {
        "date": str(d),
        "steps": int(5000 + s * 5000),
        "total_calories_kcal": int(1600 + s * 600),
        "active_minutes": int(20 + s * 60),
    }


def _sleep(d: date) -> dict:
    s = _day_seed(d)
    return {
        "date": str(d),
        "duration_minutes": int(330 + s * 120),   # 5.5h – 7.5h
        "efficiency_pct": int(75 + s * 20),
        "rem_minutes": int(60 + s * 40),
        "deep_minutes": int(40 + s * 30),
    }


def _recovery(d: date) -> dict:
    s = _day_seed(d)
    return {
        "date": str(d),
        "avg_hrv_sdnn_ms": round(38 + s * 30, 1),   # 38–68 ms
        "resting_heart_rate_bpm": round(55 + (1 - s) * 15, 1),  # 55–70 bpm
        "spo2_pct": round(96 + s * 3, 1),
    }


SUMMARY_BUILDERS = {
    "activity": _activity,
    "sleep": _sleep,
    "recovery": _recovery,
}


@app.get("/api/v1/users/{user_id}/summaries/{summary_type}")
def get_summaries(
    user_id: str,
    summary_type: str,
    start_date: str = Query(...),
    end_date: str = Query(...),
    x_open_wearables_api_key: str = Header(default=""),
):
    _check_auth(x_open_wearables_api_key)

    if user_id != USER_ID:
        raise HTTPException(status_code=404, detail="user not found")
    if summary_type not in SUMMARY_BUILDERS:
        raise HTTPException(status_code=404, detail=f"unknown summary type: {summary_type}")

    build = SUMMARY_BUILDERS[summary_type]
    start = date.fromisoformat(start_date)
    end = date.fromisoformat(end_date)

    data = []
    current = start
    while current <= end:
        data.append(build(current))
        current += timedelta(days=1)

    return JSONResponse({"data": data, "pagination": {"has_more": False, "next_cursor": None}})


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8001)
