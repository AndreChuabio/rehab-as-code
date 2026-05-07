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
    """Open a connection. Caller is responsible for `with` / close."""
    import psycopg
    from psycopg.rows import dict_row

    _pg_init()
    return psycopg.connect(_pg_dsn(), row_factory=dict_row, autocommit=True)


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
