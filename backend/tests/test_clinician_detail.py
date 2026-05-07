"""Tests for the GET /protocols/{id} clinician-only response shape:
patient_summary card + pain_trend list.

These were added in PR-G to replace the old <pre>JSON</pre> dump in the
clinician dashboard. The card is computed server-side so the frontend
doesn't denormalize. The same role gate that protects narrator_summary
protects these fields — patients self-fetching their own protocol see
neither.

Anthropic is mocked (the narrator path runs but we don't care about its
output here). The protocol_repo + user_store are stubbed in-process so
tests don't require Postgres.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

import pytest


# ---------------------------------------------------------------------------
# Local fakes — duplicated from test_diff_narrator on purpose so the two
# files stay independently runnable. If we end up with a third clinician-
# detail test file we'll factor these into conftest.
# ---------------------------------------------------------------------------


class _FakeTextBlock:
    def __init__(self, text: str) -> None:
        self.type = "text"
        self.text = text


class _FakeUsage:
    def __init__(self) -> None:
        self.input_tokens = 100
        self.output_tokens = 50


class _FakeResponse:
    def __init__(self, text: str) -> None:
        self.content = [_FakeTextBlock(text)]
        self.usage = _FakeUsage()


class _FakeAnthropicClient:
    def __init__(self, response_text: str = "stub narration") -> None:
        self._response_text = response_text

        class _Messages:
            def create(inner, **kwargs):
                return _FakeResponse(self._response_text)

        self.messages = _Messages()


def _stub_anthropic(monkeypatch, *, response_text: str = "stub narration") -> None:
    import anthropic
    fake = _FakeAnthropicClient(response_text=response_text)
    monkeypatch.setattr(anthropic, "Anthropic", lambda api_key=None: fake)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key-not-real")


def _clear_narrator_cache() -> None:
    import diff_narrator
    diff_narrator._CACHE.clear()


_PATIENT_TOKEN = "11111111-1111-1111-1111-111111111111"
_PROTO_ID = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"


def _build_target(payload: dict[str, Any], token: str = _PATIENT_TOKEN) -> dict:
    return {
        "id": _PROTO_ID,
        "token": token,
        "parent_id": None,
        "payload": payload,
        "status": "pending_review",
        "created_by_agent": "test",
        "created_at": datetime(2026, 5, 6, 12, 0, tzinfo=timezone.utc),
        "reviewed_by": None,
        "reviewed_at": None,
        "review_notes": None,
    }


def _build_active(payload: dict[str, Any], token: str = _PATIENT_TOKEN) -> dict:
    return {
        "id": "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb",
        "token": token,
        "parent_id": None,
        "payload": payload,
        "status": "active",
        "created_by_agent": "test",
        "created_at": datetime(2026, 4, 29, 12, 0, tzinfo=timezone.utc),
        "reviewed_by": None,
        "reviewed_at": None,
        "review_notes": None,
    }


def _stub_repo(monkeypatch, target: dict, active: dict | None) -> None:
    import protocol_repo
    monkeypatch.setattr(
        protocol_repo, "get",
        lambda pid: target if pid == _PROTO_ID else None,
    )
    monkeypatch.setattr(
        protocol_repo, "get_active",
        lambda tok: active if (active and tok == active["token"]) else None,
    )


def _stub_user(monkeypatch, *, token: str, intake: dict | None,
               history: list[dict] | None = None) -> None:
    import user_store
    monkeypatch.setattr(
        user_store, "load_user",
        lambda t: ({"patient_name": (intake or {}).get("name"), "intake": intake}
                   if t == token else None),
    )
    monkeypatch.setattr(
        user_store, "get_session_history",
        lambda t, limit=10: (history or []) if t == token else [],
    )


# ---------------------------------------------------------------------------
# patient_summary tests
# ---------------------------------------------------------------------------


def test_patient_summary_present_for_clinician(authed_clinician_client, monkeypatch):
    """GET /protocols/{id} returns patient_summary with all the fields the
    at-a-glance card needs."""
    _clear_narrator_cache()
    target = _build_target({"phase": "subacute", "week": 4,
                            "exercises": [{"name": "bike", "sets": 1, "reps": 12}]})
    active = _build_active({"phase": "acute", "week": 1,
                            "exercises": [{"name": "bike", "sets": 1, "reps": 8}]})
    _stub_repo(monkeypatch, target, active)
    # 28 days post-op: surgery_date 2026-04-09, today 2026-05-07.
    surgery_date = "2026-04-09"
    intake = {
        "name": "Christian Reyes",
        "age": 34,
        "injury_type": "ACL reconstruction",
        "surgery_date": surgery_date,
        "pain_level": 4,
        "symptoms": ["swelling", "limited dorsiflexion"],
        "goals": ["return to soccer", "stop limping"],
    }
    _stub_user(monkeypatch, token=_PATIENT_TOKEN, intake=intake, history=[
        {"kind": "checkin", "pain_level": 6, "recorded_at": "2026-05-01"},
        {"kind": "checkin", "pain_level": 5, "recorded_at": "2026-05-03"},
        {"kind": "checkin", "pain_level": 4, "recorded_at": "2026-05-05"},
    ])
    _stub_anthropic(monkeypatch)
    monkeypatch.setattr("main.is_clinician", lambda uid: True)

    resp = authed_clinician_client.get(f"/protocols/{_PROTO_ID}")
    assert resp.status_code == 200, resp.text
    body = resp.json()

    summary = body.get("patient_summary")
    assert summary is not None, "clinician response should include patient_summary"
    assert summary["display_name"] == "Christian Reyes"
    assert summary["age"] == 34
    assert summary["injury_type"] == "ACL reconstruction"
    assert summary["body_region"] == "knee", (
        "ACL reconstruction must resolve to body_region=knee via the "
        "deterministic taxonomy (no LLM call needed for the card)"
    )
    assert summary["phase"] == "subacute"
    assert summary["week"] == 4
    # post_op_days computed from surgery_date 2026-04-09 vs today's date.
    # Don't pin a specific day count — just check it's a non-negative int
    # so this test stays reproducible past 2026-05-07.
    assert isinstance(summary["post_op_days"], int)
    assert summary["post_op_days"] >= 0
    assert summary["symptoms"] == ["swelling", "limited dorsiflexion"]
    assert summary["goals"] == ["return to soccer", "stop limping"]


def test_patient_summary_handles_missing_fields(authed_clinician_client, monkeypatch):
    """If intake is sparse (no surgery_date, no symptoms), the card still
    builds — missing fields surface as None / empty list, not a 500."""
    _clear_narrator_cache()
    target = _build_target({"phase": "acute", "week": 1, "exercises": []})
    _stub_repo(monkeypatch, target, active=None)
    _stub_user(monkeypatch, token=_PATIENT_TOKEN,
               intake={"name": "Alex", "injury_type": "knee pain"})
    _stub_anthropic(monkeypatch)
    monkeypatch.setattr("main.is_clinician", lambda uid: True)

    resp = authed_clinician_client.get(f"/protocols/{_PROTO_ID}")
    assert resp.status_code == 200, resp.text
    summary = resp.json()["patient_summary"]
    assert summary["display_name"] == "Alex"
    assert summary["age"] is None
    assert summary["injury_type"] == "knee pain"
    assert summary["body_region"] == "knee"
    assert summary["post_op_days"] is None
    assert summary["symptoms"] == []
    assert summary["goals"] == []


def test_patient_summary_handles_malformed_surgery_date(
    authed_clinician_client, monkeypatch,
):
    """A free-text surgery_date doesn't crash the endpoint; post_op_days
    is None and the rest of the card renders."""
    _clear_narrator_cache()
    target = _build_target({"phase": "acute", "week": 1, "exercises": []})
    _stub_repo(monkeypatch, target, active=None)
    _stub_user(monkeypatch, token=_PATIENT_TOKEN, intake={
        "name": "Test", "injury_type": "ACL tear", "surgery_date": "last tuesday",
    })
    _stub_anthropic(monkeypatch)
    monkeypatch.setattr("main.is_clinician", lambda uid: True)

    resp = authed_clinician_client.get(f"/protocols/{_PROTO_ID}")
    assert resp.status_code == 200, resp.text
    summary = resp.json()["patient_summary"]
    assert summary["post_op_days"] is None
    assert summary["display_name"] == "Test"


def test_pain_trend_returns_last_5_oldest_first(authed_clinician_client, monkeypatch):
    """pain_trend is the last 5 entries with a numeric pain_level, ordered
    same as the underlying session_history (oldest-first by convention)."""
    _clear_narrator_cache()
    target = _build_target({"phase": "subacute", "week": 4, "exercises": []})
    _stub_repo(monkeypatch, target, active=None)
    history = [
        {"kind": "checkin", "pain_level": 7, "recorded_at": "2026-04-25"},
        {"kind": "checkin", "pain_level": 6, "recorded_at": "2026-04-28"},
        {"kind": "set_completion", "exercise_id": "bike", "completed_at": "2026-04-29"},
        {"kind": "checkin", "pain_level": 5, "recorded_at": "2026-05-01"},
        {"kind": "checkin", "pain_level": 5, "recorded_at": "2026-05-03"},
        {"kind": "checkin", "pain_level": 4, "recorded_at": "2026-05-05"},
        {"kind": "checkin", "pain_level": 3, "recorded_at": "2026-05-07"},
    ]
    _stub_user(monkeypatch, token=_PATIENT_TOKEN,
               intake={"name": "Test", "injury_type": "knee"},
               history=history)
    _stub_anthropic(monkeypatch)
    monkeypatch.setattr("main.is_clinician", lambda uid: True)

    resp = authed_clinician_client.get(f"/protocols/{_PROTO_ID}")
    assert resp.status_code == 200, resp.text
    trend = resp.json()["pain_trend"]
    assert len(trend) == 5, f"expected 5 entries, got {len(trend)}: {trend}"
    levels = [t["level"] for t in trend]
    # Most recent five pain check-ins are 6, 5, 5, 4, 3 (after dropping the
    # 7-pain on 04-25 because it's the oldest of six).
    assert levels == [6, 5, 5, 4, 3]
    # Dates surface as YYYY-MM-DD prefixes.
    assert all(len(t["date"]) == 10 for t in trend)


def test_pain_trend_empty_when_no_checkins(authed_clinician_client, monkeypatch):
    _clear_narrator_cache()
    target = _build_target({"phase": "acute", "week": 1, "exercises": []})
    _stub_repo(monkeypatch, target, active=None)
    _stub_user(monkeypatch, token=_PATIENT_TOKEN,
               intake={"name": "Test", "injury_type": "knee"},
               history=[
                   {"kind": "set_completion", "exercise_id": "bike",
                    "completed_at": "2026-05-01"},
               ])
    _stub_anthropic(monkeypatch)
    monkeypatch.setattr("main.is_clinician", lambda uid: True)

    resp = authed_clinician_client.get(f"/protocols/{_PROTO_ID}")
    assert resp.status_code == 200, resp.text
    assert resp.json()["pain_trend"] == []


# ---------------------------------------------------------------------------
# Patient self-fetch must NOT receive any of these fields
# ---------------------------------------------------------------------------


def test_patient_self_fetch_omits_clinician_only_fields(
    authed_client, fake_user_id, monkeypatch,
):
    _clear_narrator_cache()
    target = _build_target({"phase": "acute", "week": 1, "exercises": []},
                            token=fake_user_id)
    _stub_repo(monkeypatch, target, active=None)
    _stub_user(monkeypatch, token=fake_user_id,
               intake={"name": "Self", "injury_type": "knee"})
    monkeypatch.setattr("main.is_clinician", lambda uid: False)

    resp = authed_client.get(f"/protocols/{_PROTO_ID}")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert "patient_summary" not in body
    assert "pain_trend" not in body
    assert "narrator_summary" not in body
    assert "narrator_status" not in body


# ---------------------------------------------------------------------------
# /audit/raw-context-revealed
# ---------------------------------------------------------------------------


def test_audit_raw_context_revealed_clinician_logs_and_succeeds(
    authed_clinician_client, monkeypatch, caplog,
):
    monkeypatch.setattr("main.is_clinician", lambda uid: True)
    with caplog.at_level("INFO", logger="main"):
        resp = authed_clinician_client.post(
            "/audit/raw-context-revealed",
            json={"target_token": _PATIENT_TOKEN},
        )
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"logged": True}
    # Confirm the log line carries token UUIDs only — no patient names
    # ever pass through this endpoint, so we just check the endpoint
    # logged something with the expected structure.
    matched = [
        r for r in caplog.records
        if "clinician_revealed_raw_context" in r.getMessage()
        and _PATIENT_TOKEN in r.getMessage()
    ]
    assert matched, f"audit log line missing; got: {[r.getMessage() for r in caplog.records]}"


def test_audit_raw_context_revealed_403_for_patients(authed_client, monkeypatch):
    monkeypatch.setattr("main.is_clinician", lambda uid: False)
    resp = authed_client.post(
        "/audit/raw-context-revealed",
        json={"target_token": _PATIENT_TOKEN},
    )
    assert resp.status_code == 403


def test_audit_raw_context_revealed_401_unauthed(unauthed_client):
    resp = unauthed_client.post(
        "/audit/raw-context-revealed",
        json={"target_token": _PATIENT_TOKEN},
    )
    assert resp.status_code == 401
