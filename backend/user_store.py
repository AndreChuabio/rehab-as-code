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


def _path(token: str) -> Path:
    return USERS_DIR / f"{token}.json"


def create_token() -> str:
    USERS_DIR.mkdir(exist_ok=True)
    token = str(uuid.uuid4())
    _path(token).write_text(json.dumps({
        "token": token,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "last_sync": None,
        "health": None,
    }, indent=2))
    return token


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


def save_health(token: str, record: dict) -> None:
    USERS_DIR.mkdir(exist_ok=True)
    user = load_user(token) or {
        "token": token,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    user["last_sync"] = datetime.now(timezone.utc).isoformat()
    user["health"] = record
    _path(token).write_text(json.dumps(user, indent=2))
