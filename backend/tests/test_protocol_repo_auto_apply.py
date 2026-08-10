"""Task 3 — save_active_auto / revert / list_auto_applied_open in protocol_repo.

Tests exercise:
  - save_active_auto promotes a draft straight to active and supersedes the
    prior active row atomically.
  - revert re-activates the parent row and stamps reverted_at/by.
  - list_auto_applied_open excludes already-reverted rows.

All tests use the `db` fixture which patches protocol_repo._conn with a
sqlite3-backed adapter. This avoids any live DATABASE_URL dependency,
matching the pattern used across the protocol_repo test suite.
"""
from __future__ import annotations

import contextlib
import json
import re
import sqlite3
import sys
from pathlib import Path

import pytest

_BACKEND_DIR = Path(__file__).resolve().parent.parent
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

import protocol_repo as pr  # noqa: E402

TOKEN = "00000000-0000-0000-0000-000000000abc"

_PROTOCOLS_DDL = """
CREATE TABLE IF NOT EXISTS protocols (
    id            TEXT PRIMARY KEY DEFAULT (lower(hex(randomblob(16)))),
    token         TEXT NOT NULL,
    parent_id     TEXT,
    payload       TEXT NOT NULL,
    status        TEXT NOT NULL DEFAULT 'pending_review',
    created_by_agent TEXT,
    created_at    TEXT DEFAULT (strftime('%Y-%m-%d %H:%M:%S','now')),
    reviewed_by   TEXT,
    reviewed_at   TEXT,
    review_notes  TEXT,
    safety_concerns TEXT,
    auto_applied  INTEGER NOT NULL DEFAULT 0,
    reverted_at   TEXT,
    reverted_by   TEXT
)
"""


class _AdaptedCursor:
    """Wraps sqlite3.Cursor to be compatible with the psycopg3 call pattern.

    Handles:
    - %s -> ? parameter-style translation
    - FOR UPDATE removal (not supported by sqlite3)
    - NOW() -> datetime('now') translation
    - psycopg.types.json.Json param unwrapping
    - dict-returning fetchone/fetchall (protocol_repo._normalize_row requires
      a mutable dict, not a sqlite3.Row)
    """

    def __init__(self, raw_cur: sqlite3.Cursor) -> None:
        self._c = raw_cur

    def execute(self, sql: str, params: tuple = ()) -> None:
        try:
            from psycopg.types.json import Json as _Json
            params = tuple(
                json.dumps(p.obj) if isinstance(p, _Json) else p
                for p in params
            )
        except ImportError:
            pass
        sql = sql.replace("%s", "?")
        sql = re.sub(r"\s+FOR\s+UPDATE\b", "", sql, flags=re.IGNORECASE)
        sql = re.sub(r"\bNOW\s*\(\s*\)", "datetime('now')", sql, flags=re.IGNORECASE)
        self._c.execute(sql, params)

    def fetchone(self) -> dict | None:
        row = self._c.fetchone()
        return dict(row) if row is not None else None

    def fetchall(self) -> list[dict]:
        return [dict(r) for r in (self._c.fetchall() or [])]

    @property
    def description(self):
        return self._c.description

    def __enter__(self) -> "_AdaptedCursor":
        return self

    def __exit__(self, *_) -> None:
        self._c.close()


class _AdaptedConn:
    """Wraps sqlite3.Connection to be compatible with the psycopg3 call pattern."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def cursor(self) -> _AdaptedCursor:
        return _AdaptedCursor(self._conn.cursor())

    def commit(self) -> None:
        self._conn.commit()

    def __enter__(self) -> "_AdaptedConn":
        return self

    def __exit__(self, *_) -> None:
        pass


@pytest.fixture
def db(tmp_path, monkeypatch):
    """Sqlite-backed protocol_repo._conn; isolated per test via tmp_path."""
    db_file = tmp_path / "proto_test.db"
    raw_conn = sqlite3.connect(str(db_file))
    raw_conn.row_factory = sqlite3.Row
    raw_conn.execute(_PROTOCOLS_DDL)
    raw_conn.commit()

    @contextlib.contextmanager
    def _fake_conn():
        yield _AdaptedConn(raw_conn)

    monkeypatch.setattr(pr, "_conn", _fake_conn)
    yield
    raw_conn.close()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _seed_active(payload: dict) -> str:
    pid = pr.save_pending(TOKEN, payload, created_by_agent="seed")
    return pr.approve(pid, reviewed_by="clin-1")["id"]


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_save_active_auto_supersedes_prior(db):
    prior_id = _seed_active({"body_region": "knee", "exercises": [{"exercise_id": "a"}]})
    new_id = pr.save_active_auto(
        TOKEN, {"body_region": "knee", "exercises": [{"exercise_id": "b"}]},
        created_by_agent="coach_swap")
    active = pr.get_active(TOKEN)
    assert active["id"] == new_id
    assert active["auto_applied"] is True
    assert pr.get(prior_id)["status"] == "superseded"


def test_revert_reactivates_parent(db):
    prior_id = _seed_active({"body_region": "knee", "exercises": [{"exercise_id": "a"}]})
    new_id = pr.save_active_auto(
        TOKEN, {"body_region": "knee", "exercises": [{"exercise_id": "b"}]},
        created_by_agent="coach_swap")
    pr.revert(new_id, reverted_by="clin-1")
    active = pr.get_active(TOKEN)
    assert active["id"] == prior_id
    assert pr.get(new_id)["reverted_at"] is not None


def test_list_auto_applied_open_excludes_reverted(db):
    _seed_active({"body_region": "knee", "exercises": [{"exercise_id": "a"}]})
    new_id = pr.save_active_auto(
        TOKEN, {"body_region": "knee", "exercises": [{"exercise_id": "b"}]},
        created_by_agent="coach_swap")
    assert any(r["id"] == new_id for r in pr.list_auto_applied_open())
    pr.revert(new_id, reverted_by="clin-1")
    assert all(r["id"] != new_id for r in pr.list_auto_applied_open())
