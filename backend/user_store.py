"""
user_store.py — per-user flat-file data store.

Each user is identified by a UUID token. Data lives in users/{token}.json
relative to the repo root. No database required.
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from pathlib import Path

USERS_DIR = Path(__file__).parent.parent / "users"
_SLACK_INDEX = USERS_DIR / "_slack_index.json"


def _path(token: str) -> Path:
    return USERS_DIR / f"{token}.json"


def _load_slack_index() -> dict[str, str]:
    if not _SLACK_INDEX.exists():
        return {}
    try:
        return json.loads(_SLACK_INDEX.read_text())
    except Exception:
        return {}


def _save_slack_index(index: dict[str, str]) -> None:
    USERS_DIR.mkdir(exist_ok=True)
    _SLACK_INDEX.write_text(json.dumps(index, indent=2))


# ── create / lookup ───────────────────────────────────────────────────────────

def create_token() -> str:
    """Backwards-compatible alias for create_user()."""
    return create_user()


def create_user(slack_user_id: str | None = None) -> str:
    """Create a new user record. Returns the stable UUID token."""
    USERS_DIR.mkdir(exist_ok=True)
    token = str(uuid.uuid4())
    record: dict = {
        "token": token,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "last_active": datetime.now(timezone.utc).isoformat(),
        "last_sync": None,
        "slack_user_id": slack_user_id,
        "patient_name": None,
        "health": None,
        "intake": None,
        "protocol_state": None,
        "session_history": [],
    }
    _path(token).write_text(json.dumps(record, indent=2))
    if slack_user_id:
        index = _load_slack_index()
        index[slack_user_id] = token
        _save_slack_index(index)
    return token


def lookup_by_slack_id(slack_user_id: str) -> str | None:
    """Return the token for a Slack user_id, or None if not found."""
    index = _load_slack_index()
    return index.get(slack_user_id)


def link_slack_id(token: str, slack_user_id: str) -> None:
    """Associate a Slack user_id with an existing token."""
    user = load_user(token)
    if user is None:
        return
    user["slack_user_id"] = slack_user_id
    _path(token).write_text(json.dumps(user, indent=2))
    index = _load_slack_index()
    index[slack_user_id] = token
    _save_slack_index(index)


def token_exists(token: str) -> bool:
    return _path(token).exists()


def load_user(token: str) -> dict | None:
    p = _path(token)
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text())
    except Exception:
        return None


def _save_user(user: dict) -> None:
    user["last_active"] = datetime.now(timezone.utc).isoformat()
    _path(user["token"]).write_text(json.dumps(user, indent=2))


# ── health (existing) ─────────────────────────────────────────────────────────

def save_health(token: str, record: dict) -> None:
    USERS_DIR.mkdir(exist_ok=True)
    user = load_user(token) or {
        "token": token,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "session_history": [],
    }
    user["last_sync"] = datetime.now(timezone.utc).isoformat()
    user["health"] = record
    _save_user(user)


# ── intake ────────────────────────────────────────────────────────────────────

def save_intake(token: str, intake: dict) -> None:
    """Persist a completed intake record. Adds recorded_at if missing."""
    user = load_user(token)
    if user is None:
        return
    intake.setdefault("recorded_at", datetime.now(timezone.utc).isoformat())
    user["intake"] = intake
    if intake.get("name"):
        user["patient_name"] = intake["name"]
    _save_user(user)


def get_intake(token: str) -> dict | None:
    user = load_user(token)
    return user.get("intake") if user else None


# ── protocol state ────────────────────────────────────────────────────────────

def save_protocol_state(token: str, state: dict) -> None:
    """Update protocol_state after a CodingAgent run."""
    user = load_user(token)
    if user is None:
        return
    state.setdefault("last_updated", datetime.now(timezone.utc).isoformat())
    user["protocol_state"] = state
    _save_user(user)


# ── session history / check-ins ───────────────────────────────────────────────

def save_checkin(token: str, checkin: dict) -> None:
    """Append a check-in record to the user's session_history."""
    user = load_user(token)
    if user is None:
        return
    checkin.setdefault("recorded_at", datetime.now(timezone.utc).isoformat())
    checkin.setdefault("session_id", str(uuid.uuid4()))
    history: list = user.setdefault("session_history", [])
    history.append(checkin)
    _save_user(user)


def get_session_history(token: str, limit: int = 10) -> list[dict]:
    """Return the most recent `limit` sessions from session_history."""
    user = load_user(token)
    if not user:
        return []
    history = user.get("session_history", [])
    return history[-limit:]
