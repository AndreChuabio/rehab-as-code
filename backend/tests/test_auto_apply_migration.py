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
    reverted_by TEXT,
    acknowledged_at TEXT,
    acknowledged_by TEXT
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


# ---------------------------------------------------------------------------
# Static assertions on the migration SQL itself.
#
# The whole suite runs on sqlite (conftest forces STORAGE_BACKEND=sqlite), which
# is permissive about types -- so a Postgres-only type mismatch passes here
# silently. It already nearly shipped once: reverted_by was declared `uuid`
# while these tests write reverted_by="clin-1", which Postgres rejects with
# `invalid input syntax for type uuid`. These read the migration file directly
# so the two backends cannot drift apart again unnoticed.

_MIGRATIONS = Path(__file__).resolve().parents[2] / "supabase" / "migrations"
_MIGRATION = _MIGRATIONS / "20260628120000_protocol_auto_apply.sql"
_CORRECTIONS = _MIGRATIONS / "20260810230000_protocol_auto_apply_corrections.sql"


def _sql_of(path: Path) -> str:
    """A migration's executable SQL, lowercased with -- comments stripped.

    Comments are stripped because they discuss the very constructs these tests
    forbid (e.g. the note explaining why CONCURRENTLY is not used).
    """
    assert path.exists(), f"migration missing at {path}"
    lines = [line.split("--", 1)[0] for line in path.read_text().splitlines()]
    return "\n".join(lines).lower()


def _migration_sql() -> str:
    return _sql_of(_MIGRATION)


def test_reverted_by_is_text_not_uuid():
    """reverted_by must be TEXT to match protocols.reviewed_by and
    staff_users.user_id -- every actor column in this schema is TEXT, and the
    revert path is handed plain strings."""
    sql = _migration_sql()
    assert "reverted_by  text" in sql or "reverted_by text" in sql
    assert "reverted_by  uuid" not in sql and "reverted_by uuid" not in sql


def test_auto_applied_feed_index_matches_the_query():
    """list_auto_applied_open is cross-patient and orders by created_at alone.
    An index leading on token cannot be walked for that ORDER BY, so it degrades
    to a full partial-index scan plus a Sort and costs writes for nothing."""
    sql = _migration_sql()
    assert "protocols_auto_applied_open_idx" in sql
    assert "(created_at desc)" in sql
    assert "(token, created_at desc)" not in sql


def test_no_create_index_concurrently():
    """Supabase CLI >= 2.92.1 wraps each migration file in an implicit pipeline;
    CREATE INDEX CONCURRENTLY cannot run inside one (SQLSTATE 25001) and would
    fail the deploy on the merge-to-main path. lock_timeout is the guard here."""
    sql = _migration_sql()
    assert "concurrently" not in sql
    assert "set lock_timeout" in sql


def test_corrections_migration_reconciles_prod():
    """20260628120000 was ghost-applied to prod in its pre-audit form on
    2026-06-28, so its version is recorded and it can never re-run -- and every
    statement in it is IF NOT EXISTS-guarded, so even a forced replay no-ops
    against the existing column and index. The three corrections (reverted_by
    uuid -> text, feed index reshape, parent_id index) plus the two missing
    column comments only reach prod through this append-only file. Without this
    test the guards above pass while describing a file that never runs again."""
    sql = _sql_of(_CORRECTIONS)
    assert "alter column reverted_by type text" in sql
    assert "drop index if exists public.protocols_auto_applied_open_idx" in sql
    assert "(created_at desc)" in sql
    assert "(token, created_at desc)" not in sql
    assert "protocols_parent_id_idx" in sql
    assert "comment on column public.protocols.reverted_at" in sql
    assert "comment on column public.protocols.reverted_by" in sql
    assert "concurrently" not in sql
    assert "set lock_timeout" in sql
