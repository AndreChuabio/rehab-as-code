"""Task 1 — protocol auto_applied / revert provenance columns.

Verifies that the sqlite test backend exposes the three new columns added
by migration 20260628120000_protocol_auto_apply.sql, and that
protocol_repo._column_names() returns them.

The test monkeypatches protocol_repo._conn to a temporary sqlite3 connection
so no live DATABASE_URL is required, matching the pattern used by the rest of
the protocol_repo test suite.
"""
from __future__ import annotations

import contextlib
import sqlite3
import sys
from pathlib import Path

import pytest

_BACKEND_DIR = Path(__file__).resolve().parent.parent
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

import protocol_repo  # noqa: E402

# DDL that mirrors user_store._SCHEMA for the protocols table including the
# three columns added by the auto-apply migration.
_PROTOCOLS_DDL = """
CREATE TABLE IF NOT EXISTS protocols (
    id TEXT PRIMARY KEY,
    token TEXT NOT NULL,
    parent_id TEXT,
    payload TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending_review',
    created_by_agent TEXT,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    reviewed_by TEXT,
    reviewed_at TEXT,
    review_notes TEXT,
    safety_concerns TEXT,
    auto_applied INTEGER NOT NULL DEFAULT 0,
    reverted_at TEXT,
    reverted_by TEXT
)
"""


def _make_sqlite_conn_factory(db_path: Path):
    """Return a _conn-compatible context-manager factory backed by sqlite3."""

    @contextlib.contextmanager
    def _conn():
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        try:
            yield conn
        finally:
            conn.close()

    return _conn


def test_protocols_table_has_auto_apply_columns(tmp_path, monkeypatch):
    """The sqlite test backend should expose the new columns."""
    db_path = tmp_path / "test.db"
    with sqlite3.connect(str(db_path)) as conn:
        conn.execute(_PROTOCOLS_DDL)

    monkeypatch.setattr(
        protocol_repo, "_conn", _make_sqlite_conn_factory(db_path)
    )

    cols = protocol_repo._column_names("protocols")
    assert "auto_applied" in cols
    assert "reverted_at" in cols
    assert "reverted_by" in cols
