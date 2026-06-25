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
import logging
import os
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock
from typing import Any

logger = logging.getLogger(__name__)

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


def _flat_ensure_user(token: str, slack_user_id: str | None) -> str:
    """Create or refresh a flat-file user record under an explicit token."""
    if not token:
        raise ValueError("ensure_user requires a non-empty token")
    USERS_DIR.mkdir(exist_ok=True)
    existing = _flat_load_user(token)
    if existing:
        existing["last_active"] = _now()
        if slack_user_id and not existing.get("slack_user_id"):
            existing["slack_user_id"] = slack_user_id
            index = _flat_load_slack_index()
            index[slack_user_id] = token
            _flat_save_slack_index(index)
        _flat_save_user(existing)
        return token
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


def _flat_delete_intake(token: str) -> None:
    user = _flat_load_user(token)
    if user is None or "intake" not in user:
        return
    user.pop("intake", None)
    _flat_save_user(user)


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


def _flat_get_last_set_completion(
    token: str, exercise_id: str | None = None
) -> dict | None:
    user = _flat_load_user(token)
    if not user:
        return None
    for entry in reversed(user.get("session_history", [])):
        if entry.get("kind") != "set_completion":
            continue
        if exercise_id and entry.get("exercise_id") != exercise_id:
            continue
        return entry
    return None


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


def _sql_ensure_user(token: str, slack_user_id: str | None) -> str:
    """Register a known token (e.g., a Supabase auth.uid()) if missing."""
    if not token:
        raise ValueError("ensure_user requires a non-empty token")
    with _sql_conn() as c:
        c.execute(
            "INSERT OR IGNORE INTO users (token, slack_user_id, created_at, last_active) "
            "VALUES (?, ?, ?, ?)",
            (token, slack_user_id, _now(), _now()),
        )
        c.execute(
            "UPDATE users SET last_active = ? WHERE token = ?",
            (_now(), token),
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


def _sql_delete_intake(token: str) -> None:
    with _sql_conn() as c:
        c.execute("DELETE FROM intake_records WHERE token = ?", (token,))
        _sql_touch(c, token)


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


def _sql_get_last_set_completion(
    token: str, exercise_id: str | None = None
) -> dict | None:
    with _sql_conn() as c:
        if exercise_id:
            row = c.execute(
                "SELECT payload FROM checkins WHERE token = ? "
                "AND json_extract(payload, '$.kind') = 'set_completion' "
                "AND json_extract(payload, '$.exercise_id') = ? "
                "ORDER BY recorded_at DESC LIMIT 1",
                (token, exercise_id),
            ).fetchone()
        else:
            row = c.execute(
                "SELECT payload FROM checkins WHERE token = ? "
                "AND json_extract(payload, '$.kind') = 'set_completion' "
                "ORDER BY recorded_at DESC LIMIT 1",
                (token,),
            ).fetchone()
    return json.loads(row["payload"]) if row else None


# ── Postgres backend (Supabase / Vercel Postgres / Neon / Railway) ────────────
#
# Connection via DATABASE_URL env var. On Supabase, prefer the *pooler* URL
# (port 6543, transaction mode) for serverless deploys — it multiplexes
# short-lived Vercel function connections cleanly. The direct URL (5432) is
# fine for local dev and for the schema-init script.
#
# Schema differences from SQLite:
#   * JSONB payload columns (queryable, indexable, native to PG)
#   * ON CONFLICT clauses replace SQLite's INSERT OR REPLACE / OR IGNORE
#   * %s parameter placeholders instead of ?
# Public-API dict shape (load_user output) is identical.

_PG_INITIALIZED = False

# Postgres schema lives in supabase/migrations/. Adding a runtime
# CREATE TABLE IF NOT EXISTS block here would shadow drift between code and
# DB and race the deploy-time apply. See _pg_init() below.


def _pg_dsn() -> str:
    dsn = os.getenv("DATABASE_URL", "").strip()
    if not dsn:
        raise RuntimeError(
            "STORAGE_BACKEND=postgres requires DATABASE_URL. "
            "Get the connection string from Supabase: Project Settings → Database "
            "→ Connection string → Transaction pooler (port 6543) for serverless."
        )
    return dsn


def _pg_conn():
    """Yield a pooled connection (autocommit=True for legacy single-statement writes).

    Routes through backend.db.get_conn so every PG read shares one pool
    across the FastAPI process. Tests monkeypatch this name directly
    (see test_display_name.py), so the get_conn import stays inside the
    function body to keep the patch surface narrow.
    """
    from db import get_conn

    _pg_init()
    return get_conn(autocommit=True)


def _pg_init() -> None:
    """No-op for the Postgres backend.

    Schema is owned by Supabase migrations (supabase/migrations/*.sql) — see
    20260504185400_init_user_store.sql for the canonical shape and any
    later dated migration for additive changes. Running CREATE TABLE IF
    NOT EXISTS at runtime against Postgres masks migration drift (the
    server quietly comes up against a half-migrated DB) and races the
    deployment-time apply step. Sqlite local-dev still gets its lazy
    init below.
    """
    global _PG_INITIALIZED
    _PG_INITIALIZED = True


def _pg_create_user(slack_user_id: str | None) -> str:
    token = str(uuid.uuid4())
    with _pg_conn() as c, c.cursor() as cur:
        cur.execute(
            "INSERT INTO users (token, slack_user_id, created_at, last_active) "
            "VALUES (%s, %s, %s, %s)",
            (token, slack_user_id, _now(), _now()),
        )
    return token


def _pg_ensure_user(token: str, slack_user_id: str | None) -> str:
    """Register a known token (e.g., a Supabase auth.uid()) if missing.

    Differs from _pg_create_user: caller supplies the token rather than the
    DB generating one. Idempotent — re-calling returns the same token.
    """
    if not token:
        raise ValueError("ensure_user requires a non-empty token")
    with _pg_conn() as c, c.cursor() as cur:
        cur.execute(
            "INSERT INTO users (token, slack_user_id, created_at, last_active) "
            "VALUES (%s, %s, %s, %s) "
            "ON CONFLICT (token) DO UPDATE SET last_active = EXCLUDED.last_active",
            (token, slack_user_id, _now(), _now()),
        )
    return token


def _pg_lookup_by_slack_id(slack_user_id: str) -> str | None:
    with _pg_conn() as c, c.cursor() as cur:
        cur.execute(
            "SELECT token FROM users WHERE slack_user_id = %s", (slack_user_id,)
        )
        row = cur.fetchone()
    return row["token"] if row else None


def _pg_link_slack_id(token: str, slack_user_id: str) -> None:
    with _pg_conn() as c, c.cursor() as cur:
        cur.execute(
            "UPDATE users SET slack_user_id = %s, last_active = %s WHERE token = %s",
            (slack_user_id, _now(), token),
        )


def _pg_token_exists(token: str) -> bool:
    with _pg_conn() as c, c.cursor() as cur:
        cur.execute("SELECT 1 FROM users WHERE token = %s", (token,))
        return cur.fetchone() is not None


def _pg_load_user(token: str) -> dict | None:
    with _pg_conn() as c, c.cursor() as cur:
        cur.execute("SELECT * FROM users WHERE token = %s", (token,))
        urow = cur.fetchone()
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

        cur.execute(
            "SELECT payload FROM health_records WHERE token = %s "
            "ORDER BY recorded_at DESC LIMIT 1",
            (token,),
        )
        h = cur.fetchone()
        user["health"] = h["payload"] if h else None  # JSONB → dict natively

        cur.execute("SELECT payload FROM intake_records WHERE token = %s", (token,))
        i = cur.fetchone()
        user["intake"] = i["payload"] if i else None

        cur.execute("SELECT payload FROM protocol_state WHERE token = %s", (token,))
        p = cur.fetchone()
        user["protocol_state"] = p["payload"] if p else None

        cur.execute(
            "SELECT payload FROM checkins WHERE token = %s ORDER BY recorded_at ASC",
            (token,),
        )
        user["session_history"] = [r["payload"] for r in cur.fetchall()]
    return user


def _pg_touch(cur, token: str) -> None:
    cur.execute(
        "UPDATE users SET last_active = %s WHERE token = %s", (_now(), token)
    )


def _pg_save_health(token: str, record: dict) -> None:
    from psycopg.types.json import Json

    with _pg_conn() as c, c.cursor() as cur:
        cur.execute(
            "INSERT INTO users (token, created_at, last_active) "
            "VALUES (%s, %s, %s) ON CONFLICT (token) DO NOTHING",
            (token, _now(), _now()),
        )
        recorded_at = _now()
        cur.execute(
            "INSERT INTO health_records (token, recorded_at, payload) "
            "VALUES (%s, %s, %s) "
            "ON CONFLICT (token, recorded_at) DO UPDATE SET payload = EXCLUDED.payload",
            (token, recorded_at, Json(record)),
        )
        cur.execute(
            "UPDATE users SET last_sync = %s, last_active = %s WHERE token = %s",
            (recorded_at, _now(), token),
        )


def _pg_save_intake(token: str, intake: dict) -> None:
    from psycopg.types.json import Json

    intake.setdefault("recorded_at", _now())
    with _pg_conn() as c, c.cursor() as cur:
        cur.execute("SELECT injury_category FROM users WHERE token = %s", (token,))
        existing = cur.fetchone()
        if not existing:
            return
        cur.execute(
            "INSERT INTO intake_records (token, recorded_at, payload) "
            "VALUES (%s, %s, %s) "
            "ON CONFLICT (token) DO UPDATE SET "
            "  recorded_at = EXCLUDED.recorded_at, payload = EXCLUDED.payload",
            (token, intake["recorded_at"], Json(intake)),
        )
        if intake.get("name"):
            cur.execute(
                "UPDATE users SET patient_name = %s WHERE token = %s",
                (intake["name"], token),
            )
        if intake.get("injury_type") and not existing["injury_category"]:
            inferred = _infer_injury_category(intake["injury_type"])
            if inferred:
                cur.execute(
                    "UPDATE users SET injury_category = %s WHERE token = %s",
                    (inferred, token),
                )
        _pg_touch(cur, token)


def _pg_get_intake(token: str) -> dict | None:
    with _pg_conn() as c, c.cursor() as cur:
        cur.execute("SELECT payload FROM intake_records WHERE token = %s", (token,))
        row = cur.fetchone()
    return row["payload"] if row else None


def _pg_delete_intake(token: str) -> None:
    with _pg_conn() as c, c.cursor() as cur:
        cur.execute("DELETE FROM intake_records WHERE token = %s", (token,))
        _pg_touch(cur, token)


def _pg_save_protocol_state(token: str, state: dict) -> None:
    from psycopg.types.json import Json

    state.setdefault("last_updated", _now())
    with _pg_conn() as c, c.cursor() as cur:
        cur.execute("SELECT 1 FROM users WHERE token = %s", (token,))
        if not cur.fetchone():
            return
        cur.execute(
            "INSERT INTO protocol_state "
            "(token, last_updated, current_phase, current_week, last_pr_url, payload) "
            "VALUES (%s, %s, %s, %s, %s, %s) "
            "ON CONFLICT (token) DO UPDATE SET "
            "  last_updated = EXCLUDED.last_updated, "
            "  current_phase = EXCLUDED.current_phase, "
            "  current_week = EXCLUDED.current_week, "
            "  last_pr_url = EXCLUDED.last_pr_url, "
            "  payload = EXCLUDED.payload",
            (
                token,
                state["last_updated"],
                state.get("current_phase"),
                state.get("current_week"),
                state.get("last_pr_url"),
                Json(state),
            ),
        )
        _pg_touch(cur, token)


def _pg_save_checkin(token: str, checkin: dict) -> None:
    from psycopg.types.json import Json

    checkin.setdefault("recorded_at", _now())
    checkin.setdefault("session_id", str(uuid.uuid4()))
    with _pg_conn() as c, c.cursor() as cur:
        cur.execute("SELECT 1 FROM users WHERE token = %s", (token,))
        if not cur.fetchone():
            return
        cur.execute(
            "INSERT INTO checkins (session_id, token, recorded_at, pain_level, payload) "
            "VALUES (%s, %s, %s, %s, %s) "
            "ON CONFLICT (session_id) DO UPDATE SET "
            "  token = EXCLUDED.token, "
            "  recorded_at = EXCLUDED.recorded_at, "
            "  pain_level = EXCLUDED.pain_level, "
            "  payload = EXCLUDED.payload",
            (
                checkin["session_id"],
                token,
                checkin["recorded_at"],
                checkin.get("pain_level"),
                Json(checkin),
            ),
        )
        _pg_touch(cur, token)


def _pg_get_session_history(token: str, limit: int = 10) -> list[dict]:
    with _pg_conn() as c, c.cursor() as cur:
        cur.execute(
            "SELECT payload FROM checkins WHERE token = %s "
            "ORDER BY recorded_at DESC LIMIT %s",
            (token, limit),
        )
        rows = cur.fetchall()
    return [r["payload"] for r in reversed(rows)]


def _pg_get_last_set_completion(
    token: str, exercise_id: str | None = None
) -> dict | None:
    with _pg_conn() as c, c.cursor() as cur:
        if exercise_id:
            cur.execute(
                "SELECT payload FROM checkins WHERE token = %s "
                "AND payload->>'kind' = 'set_completion' "
                "AND payload->>'exercise_id' = %s "
                "ORDER BY recorded_at DESC LIMIT 1",
                (token, exercise_id),
            )
        else:
            cur.execute(
                "SELECT payload FROM checkins WHERE token = %s "
                "AND payload->>'kind' = 'set_completion' "
                "ORDER BY recorded_at DESC LIMIT 1",
                (token,),
            )
        row = cur.fetchone()
    return row["payload"] if row else None


# ── Public API (dispatch on backend) ──────────────────────────────────────────


def create_token() -> str:
    """Backwards-compatible alias for create_user()."""
    return create_user()


def _pick(flat, sql, pg, *args):
    name = _backend_name()
    if name == "flatfile":
        return flat(*args)
    if name == "postgres":
        return pg(*args)
    return sql(*args)


def create_user(slack_user_id: str | None = None) -> str:
    return _pick(_flat_create_user, _sql_create_user, _pg_create_user, slack_user_id)


def ensure_user(token: str, slack_user_id: str | None = None) -> str:
    """Idempotently register a known token (e.g., Supabase auth.uid()).

    Use this when the caller already has a stable identifier (a JWT subject).
    Use create_user() instead when generating a fresh anonymous token.
    """
    return _pick(
        _flat_ensure_user, _sql_ensure_user, _pg_ensure_user, token, slack_user_id,
    )


def lookup_by_slack_id(slack_user_id: str) -> str | None:
    return _pick(
        _flat_lookup_by_slack_id, _sql_lookup_by_slack_id, _pg_lookup_by_slack_id,
        slack_user_id,
    )


def link_slack_id(token: str, slack_user_id: str) -> None:
    return _pick(
        _flat_link_slack_id, _sql_link_slack_id, _pg_link_slack_id,
        token, slack_user_id,
    )


def token_exists(token: str) -> bool:
    return _pick(_flat_token_exists, _sql_token_exists, _pg_token_exists, token)


def load_user(token: str) -> dict | None:
    return _pick(_flat_load_user, _sql_load_user, _pg_load_user, token)


def save_health(token: str, record: dict) -> None:
    return _pick(_flat_save_health, _sql_save_health, _pg_save_health, token, record)


def save_intake(token: str, intake: dict) -> None:
    return _pick(_flat_save_intake, _sql_save_intake, _pg_save_intake, token, intake)


def get_intake(token: str) -> dict | None:
    return _pick(_flat_get_intake, _sql_get_intake, _pg_get_intake, token)


def delete_intake(token: str) -> None:
    """Erase the intake record so the patient is treated as fresh on next /patient/interact.

    Admin escape-hatch invoked by coach_chat.fire_intake_trigger when the
    patient explicitly requests a re-intake.
    """
    return _pick(
        _flat_delete_intake, _sql_delete_intake, _pg_delete_intake, token,
    )


# -- Payer model (clinician-owned billing / goal-language mode) -------------
#
# payer_model drives BOTH payer-aware goal language (planner) and whether the
# super-bill surfaces. It is clinician-owned — the patient never sets it. We
# store it on the canonical intake_records.payload (the single source already
# loaded everywhere via get_intake) rather than denormalizing onto the protocol
# payload; the protocol-payload drift trap is exactly what leaked a stale
# patient name ("Christian"). Default is "cash": the confirmed
# insurance-lapse-bridge go-to-market is cash-pay first.

PAYER_MODELS = ("insurance", "medicare", "cash")
DEFAULT_PAYER_MODEL = "cash"


def resolve_payer_model(token: str) -> str:
    """Return the patient's payer model, defaulting to cash.

    Reads intake_records.payload.payer_model. Any unset / unrecognized value
    falls back to DEFAULT_PAYER_MODEL so callers always get a valid enum
    member. Never raises — a missing intake just means the default.
    """
    if not token:
        return DEFAULT_PAYER_MODEL
    try:
        intake = get_intake(token) or {}
    except Exception:  # pragma: no cover - defensive; default beats a 500
        return DEFAULT_PAYER_MODEL
    model = str(intake.get("payer_model") or "").strip().lower()
    return model if model in PAYER_MODELS else DEFAULT_PAYER_MODEL


def set_payer_model(token: str, model: str) -> str:
    """Set the patient's payer model (clinician-owned). Returns the stored value.

    Merges payer_model into the existing intake payload. Raises ValueError on
    an unrecognized model so the API surfaces a 400 rather than silently
    storing a value resolve_payer_model would then ignore.
    """
    if not token:
        raise ValueError("token required")
    normalized = str(model or "").strip().lower()
    if normalized not in PAYER_MODELS:
        raise ValueError(
            f"payer_model must be one of {PAYER_MODELS}, got {model!r}"
        )
    intake = get_intake(token) or {}
    save_intake(token, {**intake, "payer_model": normalized})
    return normalized


# -- Display name (patient-owned account setting) ---------------------------
#
# The patient sets their own display name in Profile / Settings. We write the
# canonical first link of the resolution chain (intake_records.payload.name)
# via the same merge pattern as set_payer_model, so save_intake mirrors it onto
# users.patient_name on both pg + sqlite. NEVER touch protocol.payload.patient
# (that field drifts and once leaked "Christian" into the chat greeting).


def set_display_name(token: str, name: str) -> str:
    """Set the patient's display name on the canonical intake payload.

    Merges `name` into the existing intake record so the rest of the payload
    (injury_type, payer_model, etc.) is preserved. Raises ValueError on an
    empty / whitespace-only name so the API surfaces a 400 rather than storing
    a blank the resolver would skip. Returns the stored (stripped) name.
    """
    if not token:
        raise ValueError("token required")
    cleaned = str(name or "").strip()
    if not cleaned:
        raise ValueError("name must not be empty")
    # save_intake silently no-ops when the parent users row is absent. A patient
    # may reach Settings before any patient-interaction endpoint ran ensure_user,
    # so register the (idempotent) users row first or the write is lost while the
    # endpoint still returns 200. ensure_user uses ON CONFLICT DO UPDATE (pg) /
    # INSERT OR IGNORE (sqlite), so an existing row is untouched.
    ensure_user(token)
    intake = get_intake(token) or {}
    save_intake(token, {**intake, "name": cleaned})
    return cleaned


# -- Consent status (intake-payload backed; no migration) -------------------
#
# v1 records consent on the canonical intake payload (same merge mechanism as
# payer_model). No dedicated table, so no migration. get_consent returns a
# benign "not_recorded" shape when nothing is on file rather than fabricating a
# consented state.


def get_consent(token: str) -> dict[str, Any]:
    """Return the patient's consent status, or a not_recorded default.

    Shape: {status: 'recorded' | 'not_recorded', recorded_at: str | None}.
    Never raises — a missing intake just means consent is not recorded.
    """
    if not token:
        return {"status": "not_recorded", "recorded_at": None}
    try:
        intake = get_intake(token) or {}
    except Exception:  # pragma: no cover - defensive; default beats a 500
        return {"status": "not_recorded", "recorded_at": None}
    consent = intake.get("consent")
    if isinstance(consent, dict) and consent.get("status") == "recorded":
        return {
            "status": "recorded",
            "recorded_at": consent.get("recorded_at"),
        }
    return {"status": "not_recorded", "recorded_at": None}


def set_consent(token: str) -> dict[str, Any]:
    """Record the patient's consent on the canonical intake payload.

    Stores {status: 'recorded', recorded_at: <iso>} under intake.consent,
    merging into the existing payload. Returns the stored consent dict.
    """
    if not token:
        raise ValueError("token required")
    consent = {"status": "recorded", "recorded_at": _now()}
    # As with set_display_name: ensure the parent users row exists so save_intake
    # actually persists rather than silently no-opping into a false 200.
    ensure_user(token)
    intake = get_intake(token) or {}
    save_intake(token, {**intake, "consent": consent})
    return consent


# -- Patient settings prefs (intake-payload backed; no migration) -----------
#
# Settings v2 patient prefs (notifications, display, coach-maya) ride the SAME
# canonical intake_records.payload merge seam as set_consent / set_payer_model.
# No dedicated table, so no migration. Each getter returns a benign default
# shape when nothing is on file (never raises); each setter coerces unknown
# keys away so the stored blob stays a known shape. We ALWAYS ensure_user before
# save_intake — save_intake silently no-ops if the parent users row is absent
# (a patient may reach Settings before any interaction endpoint ran ensure_user;
# documented at set_display_name). PHI: callers must never log the values.

# Notification / reminder preferences. Delivery (email/push) is NOT built in
# v1 — these are stored only and surfaced honestly in the UI as "coming soon".
NOTIFICATION_PREF_KEYS = (
    "session_reminders",
    "checkin_reminders",
    "plan_updated",
    "symptom_flag_receipts",
    "email_opt_in",
)
_DEFAULT_NOTIFICATION_PREFS = {
    "session_reminders": True,
    "checkin_reminders": True,
    "plan_updated": True,
    "symptom_flag_receipts": True,
    "email_opt_in": False,
}


def get_notification_prefs(token: str) -> dict[str, bool]:
    """Return the patient's notification prefs, merged over benign defaults.

    Shape: a bool per NOTIFICATION_PREF_KEYS. Never raises — a missing intake
    just means the defaults. Unknown stored keys are dropped on read.
    """
    prefs = dict(_DEFAULT_NOTIFICATION_PREFS)
    if not token:
        return prefs
    try:
        intake = get_intake(token) or {}
    except Exception:  # pragma: no cover - defensive; default beats a 500
        return prefs
    stored = intake.get("notification_prefs")
    if isinstance(stored, dict):
        for key in NOTIFICATION_PREF_KEYS:
            if key in stored:
                prefs[key] = bool(stored[key])
    return prefs


def set_notification_prefs(token: str, prefs: dict[str, Any]) -> dict[str, bool]:
    """Persist the patient's notification prefs on the intake payload.

    Only the known NOTIFICATION_PREF_KEYS are stored (coerced to bool); any
    other keys in `prefs` are ignored so the blob stays a known shape. Merges
    over the existing payload so other intake keys survive. Returns the stored
    (merged-over-default) shape.
    """
    if not token:
        raise ValueError("token required")
    clean = {
        key: bool(prefs.get(key, _DEFAULT_NOTIFICATION_PREFS[key]))
        for key in NOTIFICATION_PREF_KEYS
    }
    ensure_user(token)
    intake = get_intake(token) or {}
    save_intake(token, {**intake, "notification_prefs": clean})
    return clean


# Display preferences. Theme / text-size / reduced-motion are applied CLIENT
# side from localStorage (instant, source of truth); this is the durable
# cross-device mirror. Values are constrained to known enum members.
_THEME_VALUES = ("light", "dark")
_TEXT_SIZE_VALUES = ("normal", "large")
_DEFAULT_DISPLAY_PREFS = {
    "theme": "light",
    "text_size": "normal",
    "reduced_motion": False,
}


def get_display_prefs(token: str) -> dict[str, Any]:
    """Return the patient's display prefs (theme/text_size/reduced_motion)."""
    prefs = dict(_DEFAULT_DISPLAY_PREFS)
    if not token:
        return prefs
    try:
        intake = get_intake(token) or {}
    except Exception:  # pragma: no cover - defensive
        return prefs
    stored = intake.get("display_prefs")
    if isinstance(stored, dict):
        theme = str(stored.get("theme") or "").strip().lower()
        if theme in _THEME_VALUES:
            prefs["theme"] = theme
        text_size = str(stored.get("text_size") or "").strip().lower()
        if text_size in _TEXT_SIZE_VALUES:
            prefs["text_size"] = text_size
        if "reduced_motion" in stored:
            prefs["reduced_motion"] = bool(stored["reduced_motion"])
    return prefs


def set_display_prefs(token: str, prefs: dict[str, Any]) -> dict[str, Any]:
    """Persist the patient's display prefs on the intake payload (mirror)."""
    if not token:
        raise ValueError("token required")
    theme = str(prefs.get("theme") or "").strip().lower()
    text_size = str(prefs.get("text_size") or "").strip().lower()
    clean = {
        "theme": theme if theme in _THEME_VALUES else _DEFAULT_DISPLAY_PREFS["theme"],
        "text_size": (
            text_size if text_size in _TEXT_SIZE_VALUES
            else _DEFAULT_DISPLAY_PREFS["text_size"]
        ),
        "reduced_motion": bool(
            prefs.get("reduced_motion", _DEFAULT_DISPLAY_PREFS["reduced_motion"])
        ),
    }
    ensure_user(token)
    intake = get_intake(token) or {}
    save_intake(token, {**intake, "display_prefs": clean})
    return clean


# Coach Maya preferences. `voice` is the load-bearing one: the frontend mirrors
# it into localStorage 'rac-maya-voice' and reads it synchronously to gate the
# in-call rep-count echo. greeting_cadence gates the state-aware greeting.
# language is stored only (the greeting copy is English-only in v1).
_GREETING_CADENCE_VALUES = ("every_visit", "first_of_day", "off")
_LANGUAGE_VALUES = ("en",)
_DEFAULT_COACH_PREFS = {
    "voice": True,
    "greeting_cadence": "every_visit",
    "language": "en",
}


def get_coach_prefs(token: str) -> dict[str, Any]:
    """Return the patient's Coach Maya prefs (voice/greeting_cadence/language)."""
    prefs = dict(_DEFAULT_COACH_PREFS)
    if not token:
        return prefs
    try:
        intake = get_intake(token) or {}
    except Exception:  # pragma: no cover - defensive
        return prefs
    stored = intake.get("coach_prefs")
    if isinstance(stored, dict):
        if "voice" in stored:
            prefs["voice"] = bool(stored["voice"])
        cadence = str(stored.get("greeting_cadence") or "").strip().lower()
        if cadence in _GREETING_CADENCE_VALUES:
            prefs["greeting_cadence"] = cadence
        language = str(stored.get("language") or "").strip().lower()
        if language in _LANGUAGE_VALUES:
            prefs["language"] = language
    return prefs


def set_coach_prefs(token: str, prefs: dict[str, Any]) -> dict[str, Any]:
    """Persist the patient's Coach Maya prefs on the intake payload."""
    if not token:
        raise ValueError("token required")
    cadence = str(prefs.get("greeting_cadence") or "").strip().lower()
    language = str(prefs.get("language") or "").strip().lower()
    clean = {
        "voice": bool(prefs.get("voice", _DEFAULT_COACH_PREFS["voice"])),
        "greeting_cadence": (
            cadence if cadence in _GREETING_CADENCE_VALUES
            else _DEFAULT_COACH_PREFS["greeting_cadence"]
        ),
        "language": (
            language if language in _LANGUAGE_VALUES
            else _DEFAULT_COACH_PREFS["language"]
        ),
    }
    ensure_user(token)
    intake = get_intake(token) or {}
    save_intake(token, {**intake, "coach_prefs": clean})
    return clean


# -- Destructive account deletion (self-scoped; cascade-backed) -------------
#
# delete_account removes the patient's `users` row, which CASCADEs to every
# child table (health_records, intake_records, protocol_state, checkins on both
# backends; protocols, sessions, tavus_sessions, junction_connections on pg via
# REFERENCES users(token) ON DELETE CASCADE). The sqlite path MUST route through
# _sql_conn so PRAGMA foreign_keys=ON fires the cascade. The token is supplied
# only by the caller's authenticated identity (current_user_id) — never a body /
# path value — so a patient can never widen the blast radius to another row.
#
# NOT erased (documented residual): the Supabase auth.users login row (separate
# schema; the pooled service-role DSN lacks DELETE perms) and pipeline_runs rows
# (NOT token-scoped; protocol_id is ON DELETE SET NULL so they survive). v1
# erases all public.* PHI and flags both for v2.


def _flat_delete_account(token: str) -> None:
    p = _flat_path(token)
    user = _flat_load_user(token)
    if user and user.get("slack_user_id"):
        index = _flat_load_slack_index()
        index.pop(user["slack_user_id"], None)
        _flat_save_slack_index(index)
    if p.exists():
        p.unlink()


def _sql_delete_account(token: str) -> None:
    # _sql_conn enables PRAGMA foreign_keys, so the single users delete
    # cascades to health_records / intake_records / protocol_state / checkins.
    with _sql_conn() as c:
        c.execute("DELETE FROM users WHERE token = ?", (token,))


def _pg_delete_account(token: str) -> None:
    # The FK cascade (REFERENCES users(token) ON DELETE CASCADE) removes every
    # child row atomically; no per-table delete needed.
    with _pg_conn() as c, c.cursor() as cur:
        cur.execute("DELETE FROM users WHERE token = %s", (token,))


def delete_account(token: str) -> None:
    """Permanently delete the patient's `users` row + all cascaded child data.

    Self-scoped: the caller passes their own current_user_id token only. The
    single-row delete relies on the verified ON DELETE CASCADE FKs — no
    hand-rolled per-table delete (sessions.protocol_id / protocols.parent_id are
    NO ACTION self/cross FKs that the token-cascade fires intra-statement).
    """
    if not token:
        raise ValueError("token required")
    return _pick(
        _flat_delete_account, _sql_delete_account, _pg_delete_account, token,
    )


# -- Clinician display name (staff_users-backed; postgres-only) -------------
#
# Distinct from get_display_name (patient-only resolution chain). The clinician
# name lives on the staff_users base table (display_name column added in
# migration 20260507180000_staff_roles.sql) and is self-scoped to the
# authenticated clinician's user_id. We target staff_users directly, NOT the
# `clinicians` VIEW.


def get_clinician_display_name(user_id: str) -> str | None:
    """Return the clinician's display_name from staff_users, or None.

    Postgres-only (staff_users is a Supabase table). Returns None on a missing
    DATABASE_URL / DB error so the settings endpoint degrades cleanly.
    """
    if not user_id:
        return None
    try:
        from db import DbConfigError, get_conn
    except ImportError:
        return None
    try:
        with get_conn(autocommit=True) as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT display_name FROM staff_users WHERE user_id = %s LIMIT 1",
                (user_id,),
            )
            row = cur.fetchone()
    except DbConfigError:
        return None
    except Exception as exc:
        logger.warning(
            "clinician display_name lookup failed: %s: %s",
            type(exc).__name__, exc,
        )
        return None
    if not row:
        return None
    name = row.get("display_name")
    return str(name).strip() or None if name else None


def set_clinician_display_name(user_id: str, name: str) -> str:
    """Set the clinician's display_name on staff_users (self-scoped).

    Raises ValueError on an empty name (-> API 400) or when the DB is
    unavailable / the row is missing. Targets the staff_users base table, not
    the clinicians VIEW, scoped to the authenticated user_id only.
    """
    if not user_id:
        raise ValueError("user_id required")
    cleaned = str(name or "").strip()
    if not cleaned:
        raise ValueError("name must not be empty")
    try:
        from db import DbConfigError, get_conn
    except ImportError as exc:
        raise ValueError("staff store unavailable") from exc
    try:
        with get_conn(autocommit=True) as conn, conn.cursor() as cur:
            cur.execute(
                "UPDATE staff_users SET display_name = %s WHERE user_id = %s",
                (cleaned, user_id),
            )
            updated = cur.rowcount
    except DbConfigError as exc:
        raise ValueError("staff store unavailable") from exc
    if not updated:
        raise ValueError("clinician record not found")
    return cleaned


# -- Clinic profile + clinician prefs (staff_users-backed; postgres-only) ----
#
# Settings v2 clinic-profile fields (clinic_name / clinic_phone /
# license_number / signature) and the JSONB clinician prefs (notif_prefs,
# goal_templates) live on the staff_users base table (columns added in
# 20260624180000_staff_clinic_profile.sql). Postgres-only and self-scoped to
# the authenticated clinician's user_id, mirroring get/set_clinician_display_name:
# every helper degrades to None/empty on a missing DATABASE_URL / DB error so
# the settings endpoints never 5xx in the sqlite test env (where staff_users
# does not exist). PHI: never log signature / license / phone / template text.

_CLINIC_PROFILE_FIELDS = ("clinic_name", "clinic_phone", "license_number", "signature")


def get_clinic_profile(user_id: str) -> dict[str, str | None]:
    """Return the clinician's clinic-profile fields from staff_users.

    Postgres-only. Returns every field as None when the DB is unavailable / the
    row is missing so the settings endpoint degrades cleanly (no 5xx).
    """
    empty = {field: None for field in _CLINIC_PROFILE_FIELDS}
    if not user_id:
        return empty
    try:
        from db import DbConfigError, get_conn
    except ImportError:
        return empty
    try:
        with get_conn(autocommit=True) as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT clinic_name, clinic_phone, license_number, signature "
                "FROM staff_users WHERE user_id = %s LIMIT 1",
                (user_id,),
            )
            row = cur.fetchone()
    except DbConfigError:
        return empty
    except Exception as exc:
        logger.warning(
            "clinic_profile lookup failed: %s: %s", type(exc).__name__, exc,
        )
        return empty
    if not row:
        return empty
    out: dict[str, str | None] = {}
    for field in _CLINIC_PROFILE_FIELDS:
        val = row.get(field)
        out[field] = str(val).strip() or None if val else None
    return out


def set_clinic_profile(user_id: str, fields: dict[str, Any]) -> dict[str, str | None]:
    """Set the clinician's clinic-profile fields on staff_users (self-scoped).

    Only the known _CLINIC_PROFILE_FIELDS are written; each is trimmed and an
    empty string is stored as NULL so a cleared field reverts to the env / unset
    behavior. Raises ValueError on a missing DB / row (-> API 400). Targets the
    staff_users base table, never the clinicians VIEW, scoped to user_id only.
    """
    if not user_id:
        raise ValueError("user_id required")
    cleaned: dict[str, str | None] = {}
    for field in _CLINIC_PROFILE_FIELDS:
        if field in fields:
            val = str(fields.get(field) or "").strip()
            cleaned[field] = val or None
    if not cleaned:
        # Nothing to write; return the current stored shape.
        return get_clinic_profile(user_id)
    try:
        from db import DbConfigError, get_conn
    except ImportError as exc:
        raise ValueError("staff store unavailable") from exc
    set_clause = ", ".join(f"{field} = %s" for field in cleaned)
    params = list(cleaned.values()) + [user_id]
    try:
        with get_conn(autocommit=True) as conn, conn.cursor() as cur:
            cur.execute(
                f"UPDATE staff_users SET {set_clause} WHERE user_id = %s",
                params,
            )
            updated = cur.rowcount
    except DbConfigError as exc:
        raise ValueError("staff store unavailable") from exc
    if not updated:
        raise ValueError("clinician record not found")
    return get_clinic_profile(user_id)


def _get_clinician_jsonb(user_id: str, column: str) -> dict[str, Any]:
    """Read a JSONB column off staff_users for one clinician, or {} on degrade."""
    if not user_id:
        return {}
    try:
        from db import DbConfigError, get_conn
    except ImportError:
        return {}
    try:
        with get_conn(autocommit=True) as conn, conn.cursor() as cur:
            cur.execute(
                f"SELECT {column} FROM staff_users WHERE user_id = %s LIMIT 1",
                (user_id,),
            )
            row = cur.fetchone()
    except DbConfigError:
        return {}
    except Exception as exc:
        logger.warning(
            "clinician %s lookup failed: %s: %s", column, type(exc).__name__, exc,
        )
        return {}
    if not row:
        return {}
    val = row.get(column)
    if isinstance(val, str):
        try:
            val = json.loads(val)
        except (ValueError, TypeError):
            return {}
    return val if isinstance(val, dict) else {}


def _set_clinician_jsonb(user_id: str, column: str, blob: dict[str, Any]) -> dict[str, Any]:
    """Write a JSONB column on staff_users for one clinician (self-scoped)."""
    if not user_id:
        raise ValueError("user_id required")
    try:
        from db import DbConfigError, get_conn
    except ImportError as exc:
        raise ValueError("staff store unavailable") from exc
    try:
        with get_conn(autocommit=True) as conn, conn.cursor() as cur:
            cur.execute(
                f"UPDATE staff_users SET {column} = %s::jsonb WHERE user_id = %s",
                (json.dumps(blob), user_id),
            )
            updated = cur.rowcount
    except DbConfigError as exc:
        raise ValueError("staff store unavailable") from exc
    if not updated:
        raise ValueError("clinician record not found")
    return blob


# Clinician review-alert preferences (stored only; no delivery in v1).
CLINICIAN_NOTIF_PREF_KEYS = ("new_review_drafts", "high_severity_flags")
_DEFAULT_CLINICIAN_NOTIF_PREFS = {
    "new_review_drafts": True,
    "high_severity_flags": True,
}


def get_clinician_notif_prefs(user_id: str) -> dict[str, bool]:
    """Return the clinician's review-alert prefs, merged over defaults."""
    prefs = dict(_DEFAULT_CLINICIAN_NOTIF_PREFS)
    stored = _get_clinician_jsonb(user_id, "notif_prefs")
    for key in CLINICIAN_NOTIF_PREF_KEYS:
        if key in stored:
            prefs[key] = bool(stored[key])
    return prefs


def set_clinician_notif_prefs(user_id: str, prefs: dict[str, Any]) -> dict[str, bool]:
    """Persist the clinician's review-alert prefs (JSONB on staff_users)."""
    clean = {
        key: bool(prefs.get(key, _DEFAULT_CLINICIAN_NOTIF_PREFS[key]))
        for key in CLINICIAN_NOTIF_PREF_KEYS
    }
    _set_clinician_jsonb(user_id, "notif_prefs", clean)
    return clean


# Per-payer default goal-language templates. Generic clinician free text used
# as cheap planner style guidance — NEVER patient-specific (becomes
# Anthropic-bound). Keyed by payer model.
GOAL_TEMPLATE_KEYS = PAYER_MODELS  # ("insurance", "medicare", "cash")


def get_clinician_goal_templates(user_id: str) -> dict[str, str]:
    """Return the clinician's per-payer goal templates, or empty strings."""
    templates = {key: "" for key in GOAL_TEMPLATE_KEYS}
    stored = _get_clinician_jsonb(user_id, "goal_templates")
    for key in GOAL_TEMPLATE_KEYS:
        if key in stored and isinstance(stored[key], str):
            templates[key] = stored[key]
    return templates


def set_clinician_goal_templates(user_id: str, templates: dict[str, Any]) -> dict[str, str]:
    """Persist the clinician's per-payer goal templates (JSONB on staff_users)."""
    clean = {
        key: str(templates.get(key) or "").strip()
        for key in GOAL_TEMPLATE_KEYS
    }
    _set_clinician_jsonb(user_id, "goal_templates", clean)
    return clean


# -- Flare-escalation phone resolution (clinic profile -> env -> None) -------
#
# coach_chat surfaces a "call your clinic" escalation when a symptom is flagged
# clinician-attention. v1 is single-clinic (Andre / Nikki), so we resolve the
# first non-null staff_users.clinic_phone, then fall back to the CLINIC_PHONE
# env (today's behavior, so no regression when no DB / no row), then None. The
# patient-side coach_chat has no per-patient clinician link, hence single-clinic
# resolution — per-patient routing is flagged phased. Never log the value.


def resolve_clinic_phone() -> str | None:
    """Resolve the flare-escalation phone: clinic profile -> CLINIC_PHONE -> None.

    Postgres-only for the clinic-profile leg; degrades to the env value when the
    DB is unavailable (the pre-Settings-v2 behavior, so no regression). Returns
    None when neither a clinic phone nor the env var is set.
    """
    env_phone = (os.getenv("CLINIC_PHONE", "") or "").strip() or None
    try:
        from db import DbConfigError, get_conn
    except ImportError:
        return env_phone
    try:
        with get_conn(autocommit=True) as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT clinic_phone FROM staff_users "
                "WHERE clinic_phone IS NOT NULL AND clinic_phone <> '' LIMIT 1",
            )
            row = cur.fetchone()
    except DbConfigError:
        return env_phone
    except Exception as exc:
        logger.warning(
            "resolve_clinic_phone lookup failed: %s: %s", type(exc).__name__, exc,
        )
        return env_phone
    if row:
        phone = str(row.get("clinic_phone") or "").strip()
        if phone:
            return phone
    return env_phone


def save_protocol_state(token: str, state: dict) -> None:
    return _pick(
        _flat_save_protocol_state, _sql_save_protocol_state, _pg_save_protocol_state,
        token, state,
    )


def save_checkin(token: str, checkin: dict) -> None:
    return _pick(
        _flat_save_checkin, _sql_save_checkin, _pg_save_checkin, token, checkin,
    )


def get_session_history(token: str, limit: int = 10) -> list[dict]:
    return _pick(
        _flat_get_session_history, _sql_get_session_history, _pg_get_session_history,
        token, limit,
    )


def get_last_set_completion(
    token: str, exercise_id: str | None = None
) -> dict | None:
    """Return the most recent live-set checkin payload for the user, or None.

    Used by /chat to surface "patient just finished a set of X" into Maya's
    system prompt, and by /pose/last-set for direct UI fetches.
    """
    return _pick(
        _flat_get_last_set_completion,
        _sql_get_last_set_completion,
        _pg_get_last_set_completion,
        token, exercise_id,
    )


# -- Display-name resolver (Supabase-canonical) -----------------------------
#
# The chat prompt and any other "Hi <name>" surface MUST resolve the patient
# name on every request from authoritative sources, never from a denormalized
# `protocol.patient` field. The protocol JSON is a snapshot whose `patient`
# field can drift from a prior account/run; using it caused Maya to greet
# the patient by a stale name (Andre -> "Christian"). See PR-A.
#
# Resolution order (most authoritative first):
#   1. intake_records.payload.name      - patient typed it during intake.
#   2. auth.users.raw_user_meta_data->>'full_name'  - set by some sign-up flows.
#   3. email local-part (auth.users.email)          - better than nothing.
#   4. None                                         - caller falls back to "the patient".
#
# Steps 2 and 3 require a DB read against `auth.users` which only the
# Postgres backend can do. The flat-file / sqlite backends only have step 1.


def get_display_name(token: str) -> str | None:
    """Return the patient's display name, sourced from Supabase tables.

    Source order: intake_records.payload.name -> auth.users.raw_user_meta_data
    .full_name -> email local-part -> None.

    Never returns a name pulled from a denormalized protocol payload - that
    column drifts when accounts churn.
    """
    if not token:
        return None
    name = _pick(
        _flat_get_display_name,
        _sql_get_display_name,
        _pg_get_display_name,
        token,
    )
    if isinstance(name, str):
        name = name.strip()
        return name or None
    return None


def _flat_get_display_name(token: str) -> str | None:
    user = _flat_load_user(token) or {}
    intake = user.get("intake") or {}
    candidate = intake.get("name") or user.get("patient_name")
    return (candidate or "").strip() or None


def _sql_get_display_name(token: str) -> str | None:
    with _sql_conn() as c:
        row = c.execute(
            "SELECT json_extract(payload, '$.name') AS name "
            "FROM intake_records WHERE token = ?",
            (token,),
        ).fetchone()
        if row and row["name"]:
            return str(row["name"]).strip() or None
        urow = c.execute(
            "SELECT patient_name FROM users WHERE token = ?", (token,)
        ).fetchone()
        if urow and urow["patient_name"]:
            return str(urow["patient_name"]).strip() or None
    return None


def _pg_get_display_name(token: str) -> str | None:
    with _pg_conn() as c, c.cursor() as cur:
        # 1. Intake record (patient-typed during onboarding).
        cur.execute(
            "SELECT payload->>'name' AS name FROM intake_records WHERE token = %s",
            (token,),
        )
        row = cur.fetchone()
        if row and row.get("name"):
            cleaned = str(row["name"]).strip()
            if cleaned:
                return cleaned

        # 2. auth.users.raw_user_meta_data.full_name (set by some sign-up flows).
        # 3. Email local-part - last-resort warm anonymous fallback.
        try:
            cur.execute(
                "SELECT raw_user_meta_data->>'full_name' AS full_name, "
                "email FROM auth.users WHERE id::text = %s",
                (token,),
            )
            au = cur.fetchone()
        except Exception as exc:
            # auth.users is in a separate schema; if the role lacks SELECT
            # we silently skip rather than 500ing the chat call. Fall back
            # to public.users.patient_name below.
            logger.warning("auth.users lookup failed for display_name: %s", exc)
            au = None
        if au:
            full_name = (au.get("full_name") or "").strip()
            if full_name:
                return full_name
            email = (au.get("email") or "").strip()
            if email and "@" in email:
                local = email.split("@", 1)[0].strip()
                if local:
                    return local

        # 4. public.users.patient_name (legacy mirror; usually equals intake.name).
        cur.execute(
            "SELECT patient_name FROM users WHERE token = %s", (token,)
        )
        urow = cur.fetchone()
        if urow and urow.get("patient_name"):
            return str(urow["patient_name"]).strip() or None
    return None
