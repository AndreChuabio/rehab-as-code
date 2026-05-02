"""
schedule_store.py — persist the patient's training-day schedule to disk.

Simple JSON file at backend/data/schedule.json.
Loaded on startup so APScheduler can restore the job.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

_STORE_PATH = Path(__file__).parent / "data" / "schedule.json"


def save_schedule(patient: str, days: list[str], exercises: list[str], hour: int = 9) -> None:
    _STORE_PATH.parent.mkdir(exist_ok=True)
    data = {"patient": patient, "days": days, "exercises": exercises, "hour": hour}
    _STORE_PATH.write_text(json.dumps(data, indent=2))
    logger.info("Schedule saved: %s on %s at %02d:00", patient, days, hour)


def load_schedule() -> dict | None:
    if not _STORE_PATH.exists():
        return None
    try:
        return json.loads(_STORE_PATH.read_text())
    except Exception:
        return None
