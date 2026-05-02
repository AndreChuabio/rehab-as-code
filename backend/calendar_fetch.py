"""
calendar_fetch.py - Fetch today's calendar events via Google Calendar API

Uses google-api-python-client with OAuth2 credentials.
Falls back to mock data if credentials are unavailable.
"""

import os
import subprocess
import json
import re
from datetime import datetime, timezone, timedelta
from dotenv import load_dotenv

load_dotenv()


def get_calendar_events() -> list[dict]:
    """
    Returns today's calendar events.
    Tries Google Calendar API first, falls back to mock.
    """
    events = _try_gcal_api()
    if events is not None:
        return events
    print("[calendar] Falling back to mock data")
    return get_mock_calendar()


def _try_gcal_api() -> list[dict] | None:
    """Fetch real events from Google Calendar API using service account or OAuth credentials."""
    creds_path = os.getenv("GOOGLE_CREDENTIALS_PATH", "")
    token_path = os.getenv("GOOGLE_TOKEN_PATH", "")

    if not creds_path and not token_path:
        print("[calendar] No GOOGLE_CREDENTIALS_PATH or GOOGLE_TOKEN_PATH set")
        return None

    try:
        from google.oauth2.credentials import Credentials
        from google.auth.transport.requests import Request
        from googleapiclient.discovery import build
        import pickle

        creds = None

        # Load saved token
        if token_path and os.path.exists(token_path):
            with open(token_path, "rb") as f:
                creds = pickle.load(f)

        # Refresh if expired
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
            with open(token_path, "wb") as f:
                pickle.dump(creds, f)

        if not creds or not creds.valid:
            print("[calendar] Credentials invalid or missing")
            return None

        service = build("calendar", "v3", credentials=creds)

        # Get today's events
        now = datetime.now(timezone.utc)
        start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        end = start + timedelta(days=1)

        result = service.events().list(
            calendarId="primary",
            timeMin=start.isoformat(),
            timeMax=end.isoformat(),
            singleEvents=True,
            orderBy="startTime",
            maxResults=20
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
            # Detect high-stakes meetings by keywords
            high_stakes_keywords = ["presentation", "interview", "demo", "pitch", "review", "deadline"]
            is_high_stakes = any(kw in summary.lower() for kw in high_stakes_keywords)

            events.append({
                "time": time_str,
                "title": summary,
                "duration_min": 60,
                "type": "high_stakes" if is_high_stakes else "meeting"
            })

        print(f"[calendar] Fetched {len(events)} real events from Google Calendar")
        return events if events else []

    except ImportError:
        print("[calendar] google-api-python-client not installed, run: pip install google-api-python-client google-auth")
        return None
    except Exception as e:
        print(f"[calendar] GCal API error: {e}")
        return None


def parse_gog_output(raw: str) -> list[dict]:
    """
    Parse gog calendar output into structured events.
    Adjust regex if gog output format differs.
    """
    events = []
    lines = raw.strip().split("\n")
    for line in lines:
        # Try to match lines like: "9:00 AM - Team Standup (30 min)"
        match = re.match(r"(\d{1,2}:\d{2}\s?[AP]M)\s*[-–]\s*(.+?)(?:\s*\((\d+)\s*min\))?$", line, re.IGNORECASE)
        if match:
            time_str, title, duration = match.groups()
            events.append({
                "time": time_str.strip(),
                "title": title.strip(),
                "duration_min": int(duration) if duration else 60
            })
        elif line.strip() and not line.startswith("#"):
            # Fallback: include raw line as event title
            events.append({
                "time": "TBD",
                "title": line.strip(),
                "duration_min": 60
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
