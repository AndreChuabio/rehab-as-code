"""user_store.get_display_name resolution-order tests.

Validates the source-of-truth chain that prevents Maya from greeting a
patient by a stale name pulled from a prior protocol payload (the "Christian"
bug). Order under test:

    intake_records.payload.name
      -> auth.users.raw_user_meta_data->>'full_name'
      -> auth.users.email local-part
      -> public.users.patient_name (legacy fallback)
      -> None

Postgres path is exercised with a fake psycopg connection; sqlite/flat-file
paths are smoke-tested through the public dispatcher to keep contract parity.
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest

# user_store imports psycopg lazily; we only need the module loadable here.
_BACKEND_DIR = Path(__file__).resolve().parent.parent
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

import user_store  # noqa: E402


# ---------------------------------------------------------------------------
# Fake psycopg connection harness
#
# `_pg_get_display_name` runs three SQL queries in order. We script the
# response stream by giving the fake cursor a deque of (sql_substring,
# response_dict) pairs and asserting each query matches its expected form.
# ---------------------------------------------------------------------------


class _FakeCursor:
    def __init__(self, scripted: list[tuple[str, Any]]):
        self._scripted = list(scripted)
        self._last_response: Any = None

    def execute(self, sql: str, params: tuple = ()) -> None:  # noqa: ARG002
        if not self._scripted:
            raise AssertionError(
                f"unexpected query (no scripted responses left): {sql!r}"
            )
        expected_substr, response = self._scripted.pop(0)
        if expected_substr not in sql:
            raise AssertionError(
                f"query mismatch.\n  expected substring: {expected_substr!r}\n"
                f"  got:                {sql!r}"
            )
        if isinstance(response, Exception):
            self._last_response = None
            raise response
        self._last_response = response

    def fetchone(self) -> Any:
        return self._last_response

    def __enter__(self) -> "_FakeCursor":
        return self

    def __exit__(self, *exc: object) -> None:
        return None


class _FakeConn:
    def __init__(self, scripted: list[tuple[str, Any]]):
        self._cursor = _FakeCursor(scripted)

    def cursor(self) -> _FakeCursor:
        return self._cursor

    def __enter__(self) -> "_FakeConn":
        return self

    def __exit__(self, *exc: object) -> None:
        return None


def _patch_pg_conn(monkeypatch: pytest.MonkeyPatch, scripted: list[tuple[str, Any]]) -> None:
    monkeypatch.setattr("user_store._backend_name", lambda: "postgres")
    monkeypatch.setattr("user_store._pg_init", lambda: None)
    monkeypatch.setattr("user_store._pg_conn", lambda: _FakeConn(scripted))


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_returns_none_for_empty_token() -> None:
    assert user_store.get_display_name("") is None
    assert user_store.get_display_name(None) is None  # type: ignore[arg-type]


def test_pg_resolves_intake_name_first(monkeypatch: pytest.MonkeyPatch) -> None:
    """When intake_records.payload.name exists, that wins; auth.users is not consulted."""
    _patch_pg_conn(
        monkeypatch,
        [
            ("FROM intake_records", {"name": "Andre"}),
        ],
    )
    assert user_store.get_display_name("user-uuid") == "Andre"


def test_pg_falls_back_to_auth_full_name(monkeypatch: pytest.MonkeyPatch) -> None:
    """No intake -> auth.users.raw_user_meta_data full_name."""
    _patch_pg_conn(
        monkeypatch,
        [
            ("FROM intake_records", {"name": None}),
            ("FROM auth.users", {"full_name": "Andre Chuabio", "email": "andre@example.com"}),
        ],
    )
    assert user_store.get_display_name("user-uuid") == "Andre Chuabio"


def test_pg_falls_back_to_email_local_part(monkeypatch: pytest.MonkeyPatch) -> None:
    """No intake, no full_name -> email local-part."""
    _patch_pg_conn(
        monkeypatch,
        [
            ("FROM intake_records", None),
            ("FROM auth.users", {"full_name": None, "email": "andre102599@gmail.com"}),
            ("FROM users", None),
        ],
    )
    assert user_store.get_display_name("user-uuid") == "andre102599"


def test_pg_returns_none_when_all_sources_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_pg_conn(
        monkeypatch,
        [
            ("FROM intake_records", None),
            ("FROM auth.users", {"full_name": None, "email": None}),
            ("FROM users", None),
        ],
    )
    assert user_store.get_display_name("user-uuid") is None


def test_pg_swallows_auth_users_permission_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """If the role can't read auth.users, fall back to public.users.patient_name."""
    _patch_pg_conn(
        monkeypatch,
        [
            ("FROM intake_records", None),
            ("FROM auth.users", PermissionError("no SELECT on auth.users")),
            ("FROM users", {"patient_name": "Andre Legacy"}),
        ],
    )
    assert user_store.get_display_name("user-uuid") == "Andre Legacy"


def test_pg_strips_whitespace(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_pg_conn(
        monkeypatch,
        [
            ("FROM intake_records", {"name": "   Andre  "}),
        ],
    )
    assert user_store.get_display_name("user-uuid") == "Andre"


def test_pg_blank_intake_name_falls_through(monkeypatch: pytest.MonkeyPatch) -> None:
    """A whitespace-only intake.name is treated as missing, not as a name."""
    _patch_pg_conn(
        monkeypatch,
        [
            ("FROM intake_records", {"name": "   "}),
            ("FROM auth.users", {"full_name": "Andre", "email": None}),
        ],
    )
    assert user_store.get_display_name("user-uuid") == "Andre"


def test_never_pulls_from_protocol_payload(monkeypatch: pytest.MonkeyPatch) -> None:
    """Regression guard: the resolver MUST NOT touch the protocols table.

    The "Christian" bug came from `protocol.get('patient')` reads. If
    get_display_name ever starts querying protocols for the name, this
    test should fail because no scripted response covers that query.
    """
    queries_seen: list[str] = []

    class _SpyCursor(_FakeCursor):
        def execute(self, sql: str, params: tuple = ()) -> None:  # noqa: ARG002
            queries_seen.append(sql)
            super().execute(sql, params)

    class _SpyConn(_FakeConn):
        def __init__(self) -> None:
            self._cursor = _SpyCursor(
                [
                    ("FROM intake_records", {"name": "Andre"}),
                ]
            )

    monkeypatch.setattr("user_store._backend_name", lambda: "postgres")
    monkeypatch.setattr("user_store._pg_init", lambda: None)
    monkeypatch.setattr("user_store._pg_conn", lambda: _SpyConn())

    user_store.get_display_name("user-uuid")
    for sql in queries_seen:
        assert "protocols" not in sql.lower(), (
            f"display name resolver leaked into protocols table: {sql!r}"
        )
