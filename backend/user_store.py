"""
user_store.py — per-user data store with pluggable backend.

Public API (unchanged from the flat-file version Nikki shipped in PR #34):
    create_user, create_token, lookup_by_slack_id, link_slack_id,
    token_exists, load_user,
    save_health,
    save_intake, get_intake,
    save_protocol_state,
    save_checkin, get_session_history.

Backend selection via STORAGE_BACKEND env var:
    sqlite (default)  — `users.db` next to the repo root, single file, queryable
    flatfile          — legacy `users/{token}.json`, kept for rollback

A user record returned from load_user() is a dict in the same shape both
backends produce, so callers don't care which one is active.
"""
from __future__ import annotations

import json
import os
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock
from typing import Any

USERS_DIR = Path(__file__).parent.parent / "users"
DB_PATH = Path(__file__).parent.parent / "users.db"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ── Backend selection ─────────────────────────────────────────────────────────


def _backend_name() -> str:
    return os.getenv("STORAGE_BACKEND", "sqlite").strip().lower() or "sqlite"


# ── Flat-file backend (legacy; kept for rollback) ─────────────────────────────

_SLACK_INDEX = USERS_DIR / "_slack_index.json"


def _flat_path(token: str) -> Path:
    return USERS_DIR / f"{token}.json"


def _flat_load_slack_index() -> dict[str, str]:
    if not _SLACK_INDEX.exists():
        return {}
    try:
        return json.loads(_SLACK_INDEX.read_text())
    except Exception:
        return {}


def _flat_save_slack_index(index: dict[str, str]) -> None:
    USERS_DIR.mkdir(exist_ok=True)
    _SLACK_INDEX.write_text(json.dumps(index, indent=2))


def _flat_create_user(slack_user_id: str | None) -> str:
    USERS_DIR.mkdir(exist_ok=True)
    token = str(uuid.uuid4())
    record: dict = {
        "token": token,
        "created_at": _now(),
        "last_active": _now(),
        "last_sync": None,
        "slack_user_id": slack_user_id,
        "patient_name": None,
        "health": None,
        "intake": None,
        "protocol_state": None,
        "session_history": [],
    }
    _flat_path(token).write_text(json.dumps(record, indent=2))
    if slack_user_id:
        index = _flat_load_slack_index()
        index[slack_user_id] = token
        _flat_save_slack_index(index)
    return token


def _flat_lookup_by_slack_id(slack_user_id: str) -> str | None:
    return _flat_load_slack_index().get(slack_user_id)


def _flat_link_slack_id(token: str, slack_user_id: str) -> None:
    user = _flat_load_user(token)
    if user is None:
        return
    user["slack_user_id"] = slack_user_id
    _flat_path(token).write_text(json.dumps(user, indent=2))
    index = _flat_load_slack_index()
    index[slack_user_id] = token
    _flat_save_slack_index(index)


def _flat_token_exists(token: str) -> bool:
    return _flat_path(token).exists()


def _flat_load_user(token: str) -> dict | None:
    p = _flat_path(token)
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text())
    except Exception:
        return None


def _flat_save_user(user: dict) -> None:
    user["last_active"] = _now()
    _flat_path(user["token"]).write_text(json.dumps(user, indent=2))


def _flat_save_health(token: str, record: dict) -> None:
    USERS_DIR.mkdir(exist_ok=True)
    user = _flat_load_user(token) or {
        "token": token,
        "created_at": _now(),
        "session_history": [],
    }
    user["last_sync"] = _now()
    user["health"] = record
    _flat_save_user(user)


def _flat_save_intake(token: str, intake: dict) -> None:
    user = _flat_load_user(token)
    if user is None:
        return
    intake.setdefault("recorded_at", _now())
    user["intake"] = intake
    if intake.get("name"):
        user["patient_name"] = intake["name"]
    _flat_save_user(user)


def _flat_get_intake(token: str) -> dict | None:
    user = _flat_load_user(token)
    return user.get("intake") if user else None


def _flat_save_protocol_state(token: str, state: dict) -> None:
    user = _flat_load_user(token)
    if user is None:
        return
    state.setdefault("last_updated", _now())
    user["protocol_state"] = state
    _flat_save_user(user)


def _flat_save_checkin(token: str, checkin: dict) -> None:
    user = _flat_load_user(token)
    if user is None:
        return
    checkin.setdefault("recorded_at", _now())
    checkin.setdefault("session_id", str(uuid.uuid4()))
    history: list = user.setdefault("session_history", [])
    history.append(checkin)
    _flat_save_user(user)


def _flat_get_session_history(token: str, limit: int = 10) -> list[dict]:
    user = _flat_load_user(token)
    if not user:
        return []
    history = user.get("session_history", [])
    return history[-limit:]


# ── SQLite backend (new default) ──────────────────────────────────────────────

_SQL_INIT_LOCK = Lock()
_SQL_INITIALIZED = False

_SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    token TEXT PRIMARY KEY,
    slack_user_id TEXT UNIQUE,
    patient_name TEXT,
    created_at TEXT NOT NULL,
    last_active TEXT NOT NULL,
    last_sync TEXT,
    injury_category TEXT
);

CREATE TABLE IF NOT EXISTS health_records (
    token TEXT NOT NULL,
    recorded_at TEXT NOT NULL,
    payload TEXT NOT NULL,
    PRIMARY KEY (token, recorded_at),
    FOREIGN KEY (token) REFERENCES users(token) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS intake_records (
    token TEXT PRIMARY KEY,
    recorded_at TEXT NOT NULL,
    payload TEXT NOT NULL,
    FOREIGN KEY (token) REFERENCES users(token) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS protocol_state (
    token TEXT PRIMARY KEY,
    last_updated TEXT NOT NULL,
    current_phase TEXT,
    current_week INTEGER,
    last_pr_url TEXT,
    payload TEXT NOT NULL,
    FOREIGN KEY (token) REFERENCES users(token) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS checkins (
    session_id TEXT PRIMARY KEY,
    token TEXT NOT NULL,
    recorded_at TEXT NOT NULL,
    pain_level INTEGER,
    payload TEXT NOT NULL,
    FOREIGN KEY (token) REFERENCES users(token) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_checkins_token_time ON checkins(token, recorded_at);
CREATE INDEX IF NOT EXISTS idx_users_slack ON users(slack_user_id);
"""


def _sql_conn() -> sqlite3.Connection:
    _sql_init()
    conn = sqlite3.connect(DB_PATH, isolation_level=None)  # autocommit
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


def _sql_init() -> None:
    global _SQL_INITIALIZED
    if _SQL_INITIALIZED:
        return
    with _SQL_INIT_LOCK:
        if _SQL_INITIALIZED:
            return
        DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(DB_PATH) as conn:
            conn.executescript(_SCHEMA)
        _SQL_INITIALIZED = True


def _sql_create_user(slack_user_id: str | None) -> str:
    token = str(uuid.uuid4())
    with _sql_conn() as c:
        c.execute(
            "INSERT INTO users (token, slack_user_id, created_at, last_active) "
            "VALUES (?, ?, ?, ?)",
            (token, slack_user_id, _now(), _now()),
        )
    return token


def _sql_lookup_by_slack_id(slack_user_id: str) -> str | None:
    with _sql_conn() as c:
        row = c.execute(
            "SELECT token FROM users WHERE slack_user_id = ?", (slack_user_id,)
        ).fetchone()
    return row["token"] if row else None


def _sql_link_slack_id(token: str, slack_user_id: str) -> None:
    with _sql_conn() as c:
        c.execute(
            "UPDATE users SET slack_user_id = ?, last_active = ? WHERE token = ?",
            (slack_user_id, _now(), token),
        )


def _sql_token_exists(token: str) -> bool:
    with _sql_conn() as c:
        row = c.execute("SELECT 1 FROM users WHERE token = ?", (token,)).fetchone()
    return row is not None


def _sql_load_user(token: str) -> dict | None:
    with _sql_conn() as c:
        urow = c.execute("SELECT * FROM users WHERE token = ?", (token,)).fetchone()
        if not urow:
            return None
        user: dict[str, Any] = {
            "token": urow["token"],
            "slack_user_id": urow["slack_user_id"],
            "patient_name": urow["patient_name"],
            "created_at": urow["created_at"],
            "last_active": urow["last_active"],
            "last_sync": urow["last_sync"],
            "injury_category": urow["injury_category"],
        }
        h = c.execute(
            "SELECT payload FROM health_records WHERE token = ? "
            "ORDER BY recorded_at DESC LIMIT 1",
            (token,),
        ).fetchone()
        user["health"] = json.loads(h["payload"]) if h else None

        i = c.execute(
            "SELECT payload FROM intake_records WHERE token = ?", (token,)
        ).fetchone()
        user["intake"] = json.loads(i["payload"]) if i else None

        p = c.execute(
            "SELECT payload FROM protocol_state WHERE token = ?", (token,)
        ).fetchone()
        user["protocol_state"] = json.loads(p["payload"]) if p else None

        sessions = c.execute(
            "SELECT payload FROM checkins WHERE token = ? "
            "ORDER BY recorded_at ASC",
            (token,),
        ).fetchall()
        user["session_history"] = [json.loads(s["payload"]) for s in sessions]
    return user


def _sql_touch(c: sqlite3.Connection, token: str) -> None:
    c.execute("UPDATE users SET last_active = ? WHERE token = ?", (_now(), token))


def _sql_save_health(token: str, record: dict) -> None:
    with _sql_conn() as c:
        # ensure user row exists (existing flat-file behavior auto-created users on health-sync)
        c.execute(
            "INSERT OR IGNORE INTO users (token, created_at, last_active) "
            "VALUES (?, ?, ?)",
            (token, _now(), _now()),
        )
        recorded_at = _now()
        c.execute(
            "INSERT OR REPLACE INTO health_records (token, recorded_at, payload) "
            "VALUES (?, ?, ?)",
            (token, recorded_at, json.dumps(record)),
        )
        c.execute(
            "UPDATE users SET last_sync = ?, last_active = ? WHERE token = ?",
            (recorded_at, _now(), token),
        )


def _sql_save_intake(token: str, intake: dict) -> None:
    intake.setdefault("recorded_at", _now())
    with _sql_conn() as c:
        if not c.execute("SELECT 1 FROM users WHERE token = ?", (token,)).fetchone():
            return
        c.execute(
            "INSERT OR REPLACE INTO intake_records (token, recorded_at, payload) "
            "VALUES (?, ?, ?)",
            (token, intake["recorded_at"], json.dumps(intake)),
        )
        if intake.get("name"):
            c.execute(
                "UPDATE users SET patient_name = ? WHERE token = ?",
                (intake["name"], token),
            )
        if intake.get("injury_type") and not c.execute(
            "SELECT injury_category FROM users WHERE token = ?", (token,)
        ).fetchone()["injury_category"]:
            inferred = _infer_injury_category(intake["injury_type"])
            if inferred:
                c.execute(
                    "UPDATE users SET injury_category = ? WHERE token = ?",
                    (inferred, token),
                )
        _sql_touch(c, token)


def _infer_injury_category(injury_type: str) -> str | None:
    """Map a free-text injury_type string to one of the 6 enum values, best-effort."""
    s = (injury_type or "").lower()
    table = {
        "knee": ["knee", "acl", "mcl", "meniscus", "patell"],
        "ankle": ["ankle", "achilles", "calf"],
        "shoulder": ["shoulder", "rotator cuff", "labrum"],
        "low_back": ["low back", "lumbar", "back pain", "lbp"],
        "hamstring": ["hamstring", "ham strain"],
        "elbow": ["elbow", "tennis elbow", "golfer", "epicond"],
    }
    for cat, keys in table.items():
        if any(k in s for k in keys):
            return cat
    return None


def _sql_get_intake(token: str) -> dict | None:
    with _sql_conn() as c:
        row = c.execute(
            "SELECT payload FROM intake_records WHERE token = ?", (token,)
        ).fetchone()
    return json.loads(row["payload"]) if row else None


def _sql_save_protocol_state(token: str, state: dict) -> None:
    state.setdefault("last_updated", _now())
    with _sql_conn() as c:
        if not c.execute("SELECT 1 FROM users WHERE token = ?", (token,)).fetchone():
            return
        c.execute(
            "INSERT OR REPLACE INTO protocol_state "
            "(token, last_updated, current_phase, current_week, last_pr_url, payload) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                token,
                state["last_updated"],
                state.get("current_phase"),
                state.get("current_week"),
                state.get("last_pr_url"),
                json.dumps(state),
            ),
        )
        _sql_touch(c, token)


def _sql_save_checkin(token: str, checkin: dict) -> None:
    checkin.setdefault("recorded_at", _now())
    checkin.setdefault("session_id", str(uuid.uuid4()))
    with _sql_conn() as c:
        if not c.execute("SELECT 1 FROM users WHERE token = ?", (token,)).fetchone():
            return
        c.execute(
            "INSERT OR REPLACE INTO checkins "
            "(session_id, token, recorded_at, pain_level, payload) "
            "VALUES (?, ?, ?, ?, ?)",
            (
                checkin["session_id"],
                token,
                checkin["recorded_at"],
                checkin.get("pain_level"),
                json.dumps(checkin),
            ),
        )
        _sql_touch(c, token)


def _sql_get_session_history(token: str, limit: int = 10) -> list[dict]:
    with _sql_conn() as c:
        rows = c.execute(
            "SELECT payload FROM checkins WHERE token = ? "
            "ORDER BY recorded_at DESC LIMIT ?",
            (token, limit),
        ).fetchall()
    # callers expect oldest-first up to the latest `limit` items
    return [json.loads(r["payload"]) for r in reversed(rows)]


# ── Public API (dispatch on backend) ──────────────────────────────────────────


def create_token() -> str:
    """Backwards-compatible alias for create_user()."""
    return create_user()


def create_user(slack_user_id: str | None = None) -> str:
    if _backend_name() == "flatfile":
        return _flat_create_user(slack_user_id)
    return _sql_create_user(slack_user_id)


def lookup_by_slack_id(slack_user_id: str) -> str | None:
    if _backend_name() == "flatfile":
        return _flat_lookup_by_slack_id(slack_user_id)
    return _sql_lookup_by_slack_id(slack_user_id)


def link_slack_id(token: str, slack_user_id: str) -> None:
    if _backend_name() == "flatfile":
        return _flat_link_slack_id(token, slack_user_id)
    return _sql_link_slack_id(token, slack_user_id)


def token_exists(token: str) -> bool:
    if _backend_name() == "flatfile":
        return _flat_token_exists(token)
    return _sql_token_exists(token)


def load_user(token: str) -> dict | None:
    if _backend_name() == "flatfile":
        return _flat_load_user(token)
    return _sql_load_user(token)


def save_health(token: str, record: dict) -> None:
    if _backend_name() == "flatfile":
        return _flat_save_health(token, record)
    return _sql_save_health(token, record)


def save_intake(token: str, intake: dict) -> None:
    if _backend_name() == "flatfile":
        return _flat_save_intake(token, intake)
    return _sql_save_intake(token, intake)


def get_intake(token: str) -> dict | None:
    if _backend_name() == "flatfile":
        return _flat_get_intake(token)
    return _sql_get_intake(token)


def save_protocol_state(token: str, state: dict) -> None:
    if _backend_name() == "flatfile":
        return _flat_save_protocol_state(token, state)
    return _sql_save_protocol_state(token, state)


def save_checkin(token: str, checkin: dict) -> None:
    if _backend_name() == "flatfile":
        return _flat_save_checkin(token, checkin)
    return _sql_save_checkin(token, checkin)


def get_session_history(token: str, limit: int = 10) -> list[dict]:
    if _backend_name() == "flatfile":
        return _flat_get_session_history(token, limit)
    return _sql_get_session_history(token, limit)
