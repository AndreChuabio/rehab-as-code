"""
test_db_pool - sanity checks for the connection-pool singleton.

These tests don't hit a real database. They patch psycopg_pool.ConnectionPool
with a fake so we can assert (a) the singleton is reused across calls,
(b) DATABASE_URL absence raises a clean DbConfigError, and (c) get_conn
flips conn.autocommit deterministically based on the keyword.
"""
from __future__ import annotations

import sys
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest

_BACKEND_DIR = Path(__file__).resolve().parent.parent
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))


class _FakeConn:
    def __init__(self) -> None:
        self.autocommit = False

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _FakePool:
    instances = 0
    last: "_FakePool | None" = None

    def __init__(self, **kwargs: Any) -> None:
        type(self).instances += 1
        type(self).last = self
        self.kwargs = kwargs
        self.conn = _FakeConn()
        self.opened = False
        self.closed = False
        self.min_size = kwargs.get("min_size", 0)
        self.max_size = kwargs.get("max_size", 0)
        self.checkouts = 0

    def open(self) -> None:
        self.opened = True

    def close(self) -> None:
        self.closed = True

    @contextmanager
    def connection(self):
        self.checkouts += 1
        yield self.conn


@pytest.fixture(autouse=True)
def _reset(monkeypatch):
    """Force a clean pool singleton for each test."""
    import db

    db.close_pool()
    _FakePool.instances = 0
    _FakePool.last = None

    monkeypatch.setenv("DATABASE_URL", "postgresql://fake:fake@localhost:6543/postgres")
    monkeypatch.setattr("psycopg_pool.ConnectionPool", _FakePool, raising=True)
    yield
    db.close_pool()


def test_get_pool_returns_singleton():
    import db

    p1 = db.get_pool()
    p2 = db.get_pool()
    assert p1 is p2
    assert _FakePool.instances == 1


def test_get_pool_passes_pgbouncer_safe_kwargs():
    import db

    db.get_pool()
    kw = _FakePool.last.kwargs["kwargs"]
    assert kw["prepare_threshold"] is None
    assert "statement_timeout=8000" in kw["options"]
    assert kw["connect_timeout"] == 3


def test_missing_database_url_raises():
    import db
    db.close_pool()

    import os
    os.environ.pop("DATABASE_URL", None)

    with pytest.raises(db.DbConfigError):
        db.get_pool()


def test_get_conn_sets_autocommit_true():
    import db

    with db.get_conn(autocommit=True) as conn:
        assert conn.autocommit is True


def test_get_conn_sets_autocommit_false():
    import db

    with db.get_conn(autocommit=False) as conn:
        assert conn.autocommit is False


def test_get_conn_reuses_pool_across_calls():
    import db

    with db.get_conn() as _:
        pass
    with db.get_conn() as _:
        pass
    assert _FakePool.instances == 1
    assert _FakePool.last.checkouts == 2


def test_close_pool_is_idempotent():
    import db

    db.get_pool()
    db.close_pool()
    db.close_pool()
