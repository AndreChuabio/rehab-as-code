"""Tests for the clinician Patients/History endpoints.

  GET /clinician/patients                 roster: one row per token, clinician-gated.
  GET /clinician/patient/{token}/history  protocol timeline + summary, PHI-minimized.

Repo + user_store reads are monkeypatched so the suite stays in-process
(sqlite test env; no pipeline_runs / protocols table needed).
"""
from __future__ import annotations

from datetime import datetime, timezone


def _dt(day: int) -> datetime:
    return datetime(2026, 5, day, 12, 0, tzinfo=timezone.utc)


# ── /clinician/patients ───────────────────────────────────────────────────────

def test_list_patients_returns_roster(authed_clinician_client, monkeypatch):
    import protocol_repo
    import user_store

    monkeypatch.setattr(protocol_repo, "list_patient_tokens", lambda limit=200: [
        {"token": "tok-a", "latest_status": "active", "latest_created_at": _dt(20),
         "body_region": "knee", "phase": "subacute", "week": "5"},
        {"token": "tok-b", "latest_status": "pending_review", "latest_created_at": _dt(19),
         "body_region": "ankle", "phase": "acute", "week": "1"},
    ])
    users = {
        "tok-a": {"patient_name": "Andre", "intake": {"name": "Andre"}},
        "tok-b": {"patient_name": None, "intake": {"name": "Christian"}},
    }
    monkeypatch.setattr(user_store, "load_user", lambda t: users.get(t))

    res = authed_clinician_client.get("/clinician/patients")
    assert res.status_code == 200, res.text
    patients = res.json()["patients"]
    assert len(patients) == 2
    a = patients[0]
    assert a["token"] == "tok-a"
    assert a["patient_name"] == "Andre"
    assert a["body_region"] == "knee"
    assert a["latest_status"] == "active"
    assert a["latest_created_at"] == "2026-05-20T12:00:00+00:00"
    # name falls back to intake.name when patient_name is null
    assert patients[1]["patient_name"] == "Christian"


def test_list_patients_requires_clinician(authed_client, unauthed_client):
    # A patient (current_user_id overridden, but NOT require_clinician_id) is rejected.
    assert authed_client.get("/clinician/patients").status_code in (401, 403)
    # No auth at all is rejected.
    assert unauthed_client.get("/clinician/patients").status_code in (401, 403)


# ── /clinician/patient/{token}/history ────────────────────────────────────────

def test_patient_history_returns_all_statuses(authed_clinician_client, monkeypatch):
    import protocol_repo
    import user_store

    rows = [
        {"id": "p3", "parent_id": "p2", "status": "active",
         "payload": {"phase": "subacute", "week": 5, "body_region": "ankle"},
         "created_by_agent": "chat:weekly_plan", "created_at": _dt(28),
         "reviewed_by": "clin-1", "reviewed_at": _dt(28), "review_notes": "looks good"},
        {"id": "p2", "parent_id": "p1", "status": "superseded",
         "payload": {"phase": "subacute", "week": 4, "body_region": "ankle"},
         "created_by_agent": "chat:weekly_plan", "created_at": _dt(21),
         "reviewed_by": "clin-1", "reviewed_at": _dt(21), "review_notes": None},
        {"id": "p1", "parent_id": None, "status": "rejected",
         "payload": {"phase": "acute", "week": 1, "body_region": "ankle"},
         "created_by_agent": "chat:weekly_plan", "created_at": _dt(20),
         "reviewed_by": "clin-1", "reviewed_at": _dt(20), "review_notes": "x" * 150},
    ]
    monkeypatch.setattr(protocol_repo, "list_by_token", lambda t: rows)
    monkeypatch.setattr(user_store, "load_user", lambda t: {
        "patient_name": "Christian",
        "intake": {"name": "Christian", "injury_type": "lateral ankle sprain", "age": 27},
    })
    monkeypatch.setattr(user_store, "get_session_history", lambda t, limit=20: [])
    monkeypatch.setattr(user_store, "get_display_name", lambda uid: "Nikki Hu")

    res = authed_clinician_client.get("/clinician/patient/tok-x/history")
    assert res.status_code == 200, res.text
    body = res.json()
    tl = body["timeline"]
    assert [t["status"] for t in tl] == ["active", "superseded", "rejected"]
    assert tl[0]["reviewer_initials"] == "NH"
    assert tl[0]["body_region"] == "ankle"
    # notes_excerpt truncated to ~100 chars + ellipsis
    assert tl[2]["notes_excerpt"].endswith("…")
    assert len(tl[2]["notes_excerpt"]) <= 101
    assert body["patient_summary"]["injury_type"] == "lateral ankle sprain"


def test_patient_history_requires_clinician(authed_client, unauthed_client):
    assert authed_client.get("/clinician/patient/tok-x/history").status_code in (401, 403)
    assert unauthed_client.get("/clinician/patient/tok-x/history").status_code in (401, 403)


def test_patient_history_no_raw_intake_in_payload(authed_clinician_client, monkeypatch):
    """PHI lock: the response carries the structured summary but NOT a raw
    intake dump. A non-summary intake field must never reach the wire."""
    import protocol_repo
    import user_store

    monkeypatch.setattr(protocol_repo, "list_by_token", lambda t: [])
    monkeypatch.setattr(user_store, "load_user", lambda t: {
        "patient_name": "Christian",
        "intake": {"name": "Christian", "injury_type": "lateral ankle sprain",
                   "secret_note": "DO-NOT-LEAK"},
    })
    monkeypatch.setattr(user_store, "get_session_history", lambda t, limit=20: [])

    res = authed_clinician_client.get("/clinician/patient/tok-x/history")
    assert res.status_code == 200, res.text
    assert "DO-NOT-LEAK" not in res.text     # raw intake field not echoed
    body = res.json()
    assert "intake" not in body              # no top-level raw intake dump
    # The structured summary still surfaces the clinically-relevant fields.
    assert body["patient_summary"]["injury_type"] == "lateral ankle sprain"
