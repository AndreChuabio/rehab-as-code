"""protocol_repo.get_review_status — five-state resolution tests.

Drives the trust-loop pill on the patient header (PR-H, Phase S4). Covers
each state enum the helper can return:

  pending_review            (latest row is pending_review)
  needs_clinician_review    (latest row is high-severity flag)
  recently_approved         (active, reviewed within RECENT_REVIEW_WINDOW_HOURS)
  recently_rejected         (rejected, reviewed within window)
  none                      (no rows; or active/rejected outside window)

We mock the psycopg connection and auth.users lookup with a scripted-cursor
harness so the test stays in-process - same pattern test_display_name uses.
"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest

_BACKEND_DIR = Path(__file__).resolve().parent.parent
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

import protocol_repo  # noqa: E402


# ---------------------------------------------------------------------------
# Fake psycopg cursor/conn — scripted responses keyed by SQL substring.
# Each test queues the (substring, response) tuples it expects to see.
# ---------------------------------------------------------------------------


class _FakeCursor:
    def __init__(self, scripted: list[tuple[str, Any]]):
        self._scripted = list(scripted)
        self._last: Any = None

    def execute(self, sql: str, params: tuple = ()) -> None:  # noqa: ARG002
        if not self._scripted:
            raise AssertionError(
                f"unexpected query (none scripted): {sql!r}"
            )
        expected_substr, response = self._scripted.pop(0)
        if expected_substr not in sql:
            raise AssertionError(
                f"query mismatch.\n  expected substring: {expected_substr!r}\n"
                f"  got:                {sql!r}"
            )
        if isinstance(response, Exception):
            self._last = None
            raise response
        self._last = response

    def fetchone(self) -> Any:
        return self._last

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


def _patch_conn(monkeypatch: pytest.MonkeyPatch, scripted: list[tuple[str, Any]]) -> None:
    monkeypatch.setattr(protocol_repo, "_conn", lambda: _FakeConn(scripted))


# ---------------------------------------------------------------------------
# Tests — one per state enum, plus edge cases
# ---------------------------------------------------------------------------


def test_empty_token_returns_none() -> None:
    assert protocol_repo.get_review_status("") is None
    assert protocol_repo.get_review_status(None) is None  # type: ignore[arg-type]


def test_no_rows_returns_state_none(monkeypatch: pytest.MonkeyPatch) -> None:
    """First-time patient: no protocols row at all."""
    _patch_conn(monkeypatch, [
        ("FROM protocols WHERE token", None),
    ])
    out = protocol_repo.get_review_status("token-abc")
    assert out == {
        "state": "none",
        "protocol_id": None,
        "submitted_at": None,
        "reviewed_at": None,
        "reviewer_initials": None,
        "notes_excerpt": None,
    }


def test_pending_review_state(monkeypatch: pytest.MonkeyPatch) -> None:
    """Latest row is pending_review — no reviewer fields surfaced."""
    pid = uuid4()
    submitted = datetime(2026, 5, 7, 10, 0, 0, tzinfo=timezone.utc)
    _patch_conn(monkeypatch, [
        ("FROM protocols WHERE token", {
            "id": pid,
            "status": "pending_review",
            "created_at": submitted,
            "reviewed_at": None,
            "reviewed_by": None,
            "review_notes": None,
        }),
    ])

    out = protocol_repo.get_review_status("token-abc")
    assert out is not None
    assert out["state"] == "pending_review"
    assert out["protocol_id"] == str(pid)
    assert out["submitted_at"] == submitted.isoformat()
    assert out["reviewed_at"] is None
    assert out["reviewer_initials"] is None
    assert out["notes_excerpt"] is None


def test_needs_clinician_review_state(monkeypatch: pytest.MonkeyPatch) -> None:
    """High-severity symptom flag — same shape as pending_review."""
    pid = uuid4()
    submitted = datetime(2026, 5, 7, 11, 30, 0, tzinfo=timezone.utc)
    _patch_conn(monkeypatch, [
        ("FROM protocols WHERE token", {
            "id": pid,
            "status": "needs_clinician_review",
            "created_at": submitted,
            "reviewed_at": None,
            "reviewed_by": None,
            "review_notes": None,
        }),
    ])

    out = protocol_repo.get_review_status("token-abc")
    assert out is not None
    assert out["state"] == "needs_clinician_review"
    assert out["protocol_id"] == str(pid)
    assert out["reviewer_initials"] is None


def test_recently_approved_state_with_initials(monkeypatch: pytest.MonkeyPatch) -> None:
    """Active row, reviewed 1h ago, reviewer has full_name in auth metadata."""
    pid = uuid4()
    reviewer_id = "9d8e0e6c-1111-2222-3333-aaaaaaaaaaaa"
    reviewed = datetime.now(timezone.utc) - timedelta(hours=1)
    _patch_conn(monkeypatch, [
        ("FROM protocols WHERE token", {
            "id": pid,
            "status": "active",
            "created_at": reviewed - timedelta(minutes=30),
            "reviewed_at": reviewed,
            "reviewed_by": reviewer_id,
            "review_notes": None,
        }),
        ("FROM auth.users", {"full_name": "Nikki Hu", "email": "nikki@example.com"}),
    ])

    out = protocol_repo.get_review_status("token-abc")
    assert out is not None
    assert out["state"] == "recently_approved"
    assert out["protocol_id"] == str(pid)
    assert out["reviewer_initials"] == "NH"
    assert out["notes_excerpt"] is None


def test_recently_approved_falls_back_to_PT_when_no_name(monkeypatch: pytest.MonkeyPatch) -> None:
    """auth.users lookup empty -> generic PT placeholder, not None."""
    pid = uuid4()
    reviewed = datetime.now(timezone.utc) - timedelta(minutes=15)
    _patch_conn(monkeypatch, [
        ("FROM protocols WHERE token", {
            "id": pid,
            "status": "active",
            "created_at": reviewed - timedelta(minutes=10),
            "reviewed_at": reviewed,
            "reviewed_by": "deadbeef-deadbeef",
            "review_notes": None,
        }),
        ("FROM auth.users", None),
    ])

    out = protocol_repo.get_review_status("token-abc")
    assert out is not None
    assert out["state"] == "recently_approved"
    assert out["reviewer_initials"] == "PT"


def test_recently_approved_outside_window_collapses_to_none(monkeypatch: pytest.MonkeyPatch) -> None:
    """Active row reviewed > 72h ago — pill stops nagging."""
    pid = uuid4()
    reviewed = datetime.now(timezone.utc) - timedelta(hours=80)
    _patch_conn(monkeypatch, [
        ("FROM protocols WHERE token", {
            "id": pid,
            "status": "active",
            "created_at": reviewed - timedelta(minutes=5),
            "reviewed_at": reviewed,
            "reviewed_by": "anything",
            "review_notes": None,
        }),
    ])

    out = protocol_repo.get_review_status("token-abc")
    assert out is not None
    assert out["state"] == "none"


def test_recently_rejected_state_with_excerpt(monkeypatch: pytest.MonkeyPatch) -> None:
    """Rejected row within 72h — excerpt is first 100 chars of review_notes."""
    pid = uuid4()
    reviewer_id = "9d8e0e6c-2222-3333-4444-bbbbbbbbbbbb"
    reviewed = datetime.now(timezone.utc) - timedelta(hours=2)
    long_notes = (
        "Single-leg squat is too aggressive at week 3 post-op. Regress to "
        "wall-supported sit-to-stand for two weeks then re-evaluate. Reach "
        "out if pain stays above 4/10."
    )
    _patch_conn(monkeypatch, [
        ("FROM protocols WHERE token", {
            "id": pid,
            "status": "rejected",
            "created_at": reviewed - timedelta(minutes=20),
            "reviewed_at": reviewed,
            "reviewed_by": reviewer_id,
            "review_notes": long_notes,
        }),
        ("FROM auth.users", {"full_name": "Nikki Hu", "email": None}),
    ])

    out = protocol_repo.get_review_status("token-abc")
    assert out is not None
    assert out["state"] == "recently_rejected"
    assert out["reviewer_initials"] == "NH"
    assert out["notes_excerpt"] is not None
    assert len(out["notes_excerpt"]) <= 100
    assert out["notes_excerpt"] == long_notes[:100]


def test_recently_rejected_short_notes_passes_through(monkeypatch: pytest.MonkeyPatch) -> None:
    """Notes shorter than 100 chars: excerpt equals full string."""
    pid = uuid4()
    reviewed = datetime.now(timezone.utc) - timedelta(hours=3)
    _patch_conn(monkeypatch, [
        ("FROM protocols WHERE token", {
            "id": pid,
            "status": "rejected",
            "created_at": reviewed - timedelta(minutes=20),
            "reviewed_at": reviewed,
            "reviewed_by": "x",
            "review_notes": "Too aggressive for week 3.",
        }),
        ("FROM auth.users", {"full_name": None, "email": "pt@clinic.com"}),
    ])

    out = protocol_repo.get_review_status("token-abc")
    assert out is not None
    assert out["state"] == "recently_rejected"
    assert out["notes_excerpt"] == "Too aggressive for week 3."
    # Email-fallback initial: "P" from "pt@..."
    assert out["reviewer_initials"] == "P"


def test_recently_rejected_outside_window_collapses_to_none(monkeypatch: pytest.MonkeyPatch) -> None:
    pid = uuid4()
    reviewed = datetime.now(timezone.utc) - timedelta(hours=200)
    _patch_conn(monkeypatch, [
        ("FROM protocols WHERE token", {
            "id": pid,
            "status": "rejected",
            "created_at": reviewed - timedelta(minutes=20),
            "reviewed_at": reviewed,
            "reviewed_by": "x",
            "review_notes": "stale",
        }),
    ])
    out = protocol_repo.get_review_status("token-abc")
    assert out is not None
    assert out["state"] == "none"


def test_active_row_with_no_reviewed_at_collapses_to_none(monkeypatch: pytest.MonkeyPatch) -> None:
    """Active row missing reviewed_at (legacy/backfilled) -> 'none' state."""
    pid = uuid4()
    _patch_conn(monkeypatch, [
        ("FROM protocols WHERE token", {
            "id": pid,
            "status": "active",
            "created_at": datetime.now(timezone.utc),
            "reviewed_at": None,
            "reviewed_by": None,
            "review_notes": None,
        }),
    ])
    out = protocol_repo.get_review_status("token-abc")
    assert out is not None
    assert out["state"] == "none"


def test_db_error_returns_none_not_raise(monkeypatch: pytest.MonkeyPatch) -> None:
    """Generic DB exception -> None (frontend renders no pill).

    Caller never 5xxs because of a missing pill query.
    """
    _patch_conn(monkeypatch, [
        ("FROM protocols WHERE token", RuntimeError("connection lost")),
    ])
    out = protocol_repo.get_review_status("token-abc")
    assert out is None


def test_initials_helper_handles_edge_cases() -> None:
    fn = protocol_repo._initials_from_name
    assert fn(None) is None
    assert fn("") is None
    assert fn("   ") is None
    assert fn("Nikki") == "N"
    assert fn("Nikki Hu") == "NH"
    assert fn("Nikki Marie Hu") == "NM"   # only first two
    assert fn("nikki hu") == "NH"
