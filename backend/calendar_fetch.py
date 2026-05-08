from __future__ import annotations
"""
calendar_fetch.py - Fetch today's calendar events via Google Calendar API.

Two paths:

  1. **Per-user** (preferred). When a user_id is provided, look up that
     user's refresh_token in `google_tokens` (populated by the
     /auth/google/start → callback flow in google_oauth.py) and fetch
     their primary calendar.

  2. **Shared pickle** (legacy fallback). The original setup_gcal.py
     workflow: a single GOOGLE_TOKEN_PATH on disk, used by the morning
     cron and any anonymous /context build. Kept so non-user-scoped
     callers don't break, but the user-facing /calendar endpoint always
     goes through the per-user path.

Falls back to mock data if no credentials are usable.
"""

import logging
import os
import re
from datetime import datetime, timezone, timedelta
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)


def get_calendar_events(user_id: str | None = None) -> list[dict]:
    """Return today's calendar events.

    Tries the per-user OAuth credentials first when user_id is given,
    then the shared pickled token, then mock data.
    """
    if user_id:
        events = _try_user_gcal(user_id)
        if events is not None:
            return events
    events = _try_shared_pickle_gcal()
    if events is not None:
        return events
    logger.info("[calendar] Falling back to mock data")
    return get_mock_calendar()


def _try_user_gcal(user_id: str) -> list[dict] | None:
    """Fetch real events for a specific user via google_oauth credentials."""
    try:
        from google_oauth import get_credentials_for_user
    except ImportError:
        return None
    creds = get_credentials_for_user(user_id)
    if not creds:
        return None
    return _fetch_events_with_creds(creds, source=f"user:{user_id[:8]}")


def _try_shared_pickle_gcal() -> list[dict] | None:
    """Legacy: read a pickled token written by setup_gcal.py."""
    token_path = os.getenv("GOOGLE_TOKEN_PATH", "")
    if not token_path or not os.path.exists(token_path):
        return None

    try:
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials  # noqa: F401
        import pickle

        with open(token_path, "rb") as f:
            creds = pickle.load(f)

        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
            with open(token_path, "wb") as f:
                pickle.dump(creds, f)

        if not creds or not creds.valid:
            logger.info("[calendar] Pickled credentials invalid")
            return None
        return _fetch_events_with_creds(creds, source="shared_pickle")
    except ImportError:
        logger.warning("[calendar] google-api-python-client not installed")
        return None
    except Exception as exc:
        logger.warning("[calendar] pickle gcal error: %s", exc)
        return None


def _fetch_events_with_creds(creds, *, source: str) -> list[dict] | None:
    try:
        from googleapiclient.discovery import build
    except ImportError:
        logger.warning("[calendar] googleapiclient not installed")
        return None

    try:
        service = build("calendar", "v3", credentials=creds, cache_discovery=False)

        now = datetime.now(timezone.utc)
        start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        end = start + timedelta(days=1)

        result = service.events().list(
            calendarId="primary",
            timeMin=start.isoformat(),
            timeMax=end.isoformat(),
            singleEvents=True,
            orderBy="startTime",
            maxResults=20,
        ).execute()

        items = result.get("items", [])
        events = []
        for item in items:
            start_dt = item["start"].get("dateTime", item["start"].get("date", ""))
            try:
                dt = datetime.fromisoformat(start_dt.replace("Z", "+00:00"))
                time_str = dt.astimezone().strftime("%-I:%M %p")
            except Exception:
                time_str = start_dt

            summary = item.get("summary", "(No title)")
            high_stakes_keywords = ["presentation", "interview", "demo", "pitch", "review", "deadline"]
            is_high_stakes = any(kw in summary.lower() for kw in high_stakes_keywords)

            events.append({
                "time": time_str,
                "title": summary,
                "duration_min": 60,
                "type": "high_stakes" if is_high_stakes else "meeting",
            })

        logger.info("[calendar] Fetched %d events (source=%s)", len(events), source)
        return events
    except Exception as exc:
        logger.warning("[calendar] GCal API error (source=%s): %s", source, exc)
        return None


def parse_gog_output(raw: str) -> list[dict]:
    """
    Parse gog calendar output into structured events.
    Adjust regex if gog output format differs.
    """
    events = []
    lines = raw.strip().split("\n")
    for line in lines:
        match = re.match(r"(\d{1,2}:\d{2}\s?[AP]M)\s*[-–]\s*(.+?)(?:\s*\((\d+)\s*min\))?$", line, re.IGNORECASE)
        if match:
            time_str, title, duration = match.groups()
            events.append({
                "time": time_str.strip(),
                "title": title.strip(),
                "duration_min": int(duration) if duration else 60,
            })
        elif line.strip() and not line.startswith("#"):
            events.append({
                "time": "TBD",
                "title": line.strip(),
                "duration_min": 60,
            })
    return events if events else get_mock_calendar()


def get_mock_calendar() -> list[dict]:
    return [
        {"time": "9:00 AM",  "title": "Team Standup",          "duration_min": 30,  "type": "meeting"},
        {"time": "11:00 AM", "title": "Deep Work Block",        "duration_min": 180, "type": "focus"},
        {"time": "2:00 PM",  "title": "Client Presentation",   "duration_min": 60,  "type": "high_stakes"},
        {"time": "4:00 PM",  "title": "1:1 with Manager",      "duration_min": 30,  "type": "meeting"},
    ]


def summarize_calendar(events: list[dict]) -> dict:
    """Return a quick summary dict for the context builder."""
    meeting_count = sum(1 for e in events if e.get("type") in ("meeting", "high_stakes", None))
    has_high_stakes = any(e.get("type") == "high_stakes" for e in events)
    focus_blocks = [e for e in events if e.get("type") == "focus"]
    return {
        "total_events": len(events),
        "meeting_count": meeting_count,
        "has_high_stakes_meeting": has_high_stakes,
        "focus_block_available": len(focus_blocks) > 0,
        "events": events,
    }
