"""Tests for the /sessions/* surface (POST, PATCH, GET today, GET recent).

Mocks session_repo at the module-attribute level so we exercise the FastAPI
auth boundary, request shape, and error mapping without needing a live
DATABASE_URL. Mirrors the pattern in test_pose_session.py.
"""
from __future__ import annotations

from typing import Any

import pytest


def test_create_session_happy_path(authed_client, fake_user_id, monkeypatch):
    captured: dict[str, Any] = {}

    def _ensure_user(token, slack_user_id=None):
        captured["ensure_token"] = token
        return token

    class _FakeSessionRepoModule:
        class SessionRepoError(RuntimeError):
            pass

        @staticmethod
        def create_planned(token, exercise_id, *, planned_sets=None,
                           planned_reps=None, protocol_id=None):
            captured["create"] = {
                "token": token,
                "exercise_id": exercise_id,
                "planned_sets": planned_sets,
                "planned_reps": planned_reps,
                "protocol_id": protocol_id,
            }
            return {
                "id": "sess-1",
                "token": token,
                "exercise_id": exercise_id,
                "protocol_id": protocol_id,
                "planned_sets": planned_sets,
                "planned_reps": planned_reps,
                "status": "planned",
                "created_at": "2026-05-07T00:00:00Z",
            }

    class _FakeProtocolRepoModule:
        @staticmethod
        def get_active(token):
            return {"id": "active-protocol-id", "token": token}

    import sys
    monkeypatch.setitem(sys.modules, "session_repo", _FakeSessionRepoModule)
    monkeypatch.setitem(sys.modules, "protocol_repo", _FakeProtocolRepoModule)
    monkeypatch.setattr("main.ensure_user", _ensure_user)

    resp = authed_client.post(
        "/sessions",
        json={"exercise_id": "wall_squat", "planned_sets": 3, "planned_reps": 10},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["id"] == "sess-1"
    assert body["status"] == "planned"
    assert body["protocol_id"] == "active-protocol-id"

    # Server captured the JWT-derived token, not anything the client claimed.
    assert captured["ensure_token"] == fake_user_id
    assert captured["create"]["token"] == fake_user_id
    assert captured["create"]["exercise_id"] == "wall_squat"
    assert captured["create"]["protocol_id"] == "active-protocol-id"


def test_create_session_rejects_unauthenticated(unauthed_client):
    resp = unauthed_client.post(
        "/sessions",
        json={"exercise_id": "wall_squat"},
    )
    assert resp.status_code == 401


def test_patch_session_happy_path(authed_client, fake_user_id, monkeypatch):
    captured: dict[str, Any] = {}

    class _FakeSessionRepoModule:
        class SessionRepoError(RuntimeError):
            pass

        @staticmethod
        def patch(*, session_id, token, status=None, completed_sets=None,
                  completed_reps=None, pose_metrics=None, started_at=None,
                  completed_at=None):
            captured["patch"] = {
                "session_id": session_id, "token": token, "status": status,
                "completed_sets": completed_sets, "completed_reps": completed_reps,
                "pose_metrics": pose_metrics, "started_at": started_at,
                "completed_at": completed_at,
            }
            return {
                "id": session_id, "token": token, "exercise_id": "wall_squat",
                "status": status or "planned",
                "completed_sets": completed_sets,
                "completed_reps": completed_reps,
            }

    import sys
    monkeypatch.setitem(sys.modules, "session_repo", _FakeSessionRepoModule)

    resp = authed_client.patch(
        "/sessions/sess-abc",
        json={"status": "completed", "completed_sets": 3, "completed_reps": 10},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["id"] == "sess-abc"
    assert body["status"] == "completed"
    assert captured["patch"]["token"] == fake_user_id
    assert captured["patch"]["status"] == "completed"


def test_patch_session_rejects_unauthenticated(unauthed_client):
    resp = unauthed_client.patch(
        "/sessions/sess-abc",
        json={"status": "completed"},
    )
    assert resp.status_code == 401


def test_patch_session_404_when_not_found(authed_client, monkeypatch):
    class _FakeSessionRepoModule:
        class SessionRepoError(RuntimeError):
            pass

        @staticmethod
        def patch(**kwargs):  # noqa: ARG004
            raise _FakeSessionRepoModule.SessionRepoError(
                "session sess-x not found for this user"
            )

    import sys
    monkeypatch.setitem(sys.modules, "session_repo", _FakeSessionRepoModule)

    resp = authed_client.patch("/sessions/sess-x", json={"status": "completed"})
    assert resp.status_code == 404
    assert "not found" in resp.json()["detail"]


def test_today_sessions_happy_path(authed_client, fake_user_id, monkeypatch):
    captured: dict[str, Any] = {}

    class _FakeSessionRepoModule:
        class SessionRepoError(RuntimeError):
            pass

        @staticmethod
        def list_today(*, token, tz_name=None):
            captured["list_today"] = {"token": token, "tz_name": tz_name}
            return [
                {"id": "s1", "token": token, "exercise_id": "wall_squat",
                 "status": "planned"},
            ]

    import sys
    monkeypatch.setitem(sys.modules, "session_repo", _FakeSessionRepoModule)
    monkeypatch.setattr("main.ensure_user", lambda t, slack_user_id=None: t)

    resp = authed_client.get(
        "/sessions/today",
        headers={"X-Timezone": "America/New_York"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert isinstance(body["sessions"], list)
    assert body["sessions"][0]["id"] == "s1"
    assert captured["list_today"]["token"] == fake_user_id
    assert captured["list_today"]["tz_name"] == "America/New_York"


def test_today_sessions_rejects_unauthenticated(unauthed_client):
    resp = unauthed_client.get("/sessions/today")
    assert resp.status_code == 401


def test_recent_sessions_self_fetch(authed_client, fake_user_id, monkeypatch):
    captured: dict[str, Any] = {}

    class _FakeSessionRepoModule:
        class SessionRepoError(RuntimeError):
            pass

        @staticmethod
        def list_recent(*, token, days=7):
            captured["list_recent"] = {"token": token, "days": days}
            return [{"id": "s1", "token": token}]

    import sys
    monkeypatch.setitem(sys.modules, "session_repo", _FakeSessionRepoModule)

    resp = authed_client.get("/sessions/recent?days=7")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["token"] == fake_user_id
    assert body["days"] == 7
    assert captured["list_recent"]["token"] == fake_user_id


def test_recent_sessions_clinician_can_read_other_patient(
    authed_clinician_client, fake_clinician_id, fake_user_id, monkeypatch,
):
    """A clinician can pass ?token=<patient_uuid> to read that patient's sessions."""
    captured: dict[str, Any] = {}

    class _FakeSessionRepoModule:
        class SessionRepoError(RuntimeError):
            pass

        @staticmethod
        def list_recent(*, token, days=7):
            captured["list_recent"] = {"token": token, "days": days}
            return [{"id": "s2", "token": token}]

    import sys
    monkeypatch.setitem(sys.modules, "session_repo", _FakeSessionRepoModule)
    monkeypatch.setattr("main.is_clinician", lambda uid: uid == fake_clinician_id)

    resp = authed_clinician_client.get(f"/sessions/recent?days=7&token={fake_user_id}")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["token"] == fake_user_id
    assert captured["list_recent"]["token"] == fake_user_id


def test_recent_sessions_patient_cannot_read_other_patient(
    authed_client, fake_user_id, monkeypatch,
):
    """A non-clinician passing ?token=<other> gets 403, not data."""
    monkeypatch.setattr("main.is_clinician", lambda uid: False)

    other = "99999999-9999-9999-9999-999999999999"
    resp = authed_client.get(f"/sessions/recent?days=7&token={other}")
    assert resp.status_code == 403
    assert resp.json()["detail"] == "clinician role required"


def test_recent_sessions_rejects_unauthenticated(unauthed_client):
    resp = unauthed_client.get("/sessions/recent")
    assert resp.status_code == 401


# ---------------------------------------------------------------------------
# PR-T2: region enrichment on /sessions/today and /sessions/recent
# ---------------------------------------------------------------------------
#
# Both endpoints enrich each row with body_region + is_current_region so
# the frontends can dim sessions tied to a body region the patient isn't
# currently rehabbing. We never drop rows — adherence-as-history. The
# response also echoes active_body_region so the client can render the
# context without re-fetching the protocol.


def _stub_session_recent(monkeypatch, rows: list[dict[str, Any]]) -> None:
    class _FakeSessionRepo:
        class SessionRepoError(RuntimeError):
            pass

        @staticmethod
        def list_recent(*, token, days=7):
            # Return a fresh list each call — the endpoint mutates rows
            # in place, and we don't want test-to-test bleed.
            return [dict(r) for r in rows]

    import sys
    monkeypatch.setitem(sys.modules, "session_repo", _FakeSessionRepo)


def _stub_session_today(monkeypatch, rows: list[dict[str, Any]]) -> None:
    class _FakeSessionRepo:
        class SessionRepoError(RuntimeError):
            pass

        @staticmethod
        def list_today(*, token, tz_name=None):
            return [dict(r) for r in rows]

    import sys
    monkeypatch.setitem(sys.modules, "session_repo", _FakeSessionRepo)


def _stub_protocol_active(monkeypatch, payload: dict[str, Any] | None) -> None:
    """payload is the protocol payload (or None for "no active protocol").
    The repo wrapper mimics protocol_repo.get_active's shape: None or
    {id, token, payload, ...}."""

    class _FakeProtocolRepo:
        @staticmethod
        def get_active(token):
            if payload is None:
                return None
            return {
                "id": "active-id",
                "token": token,
                "payload": payload,
                "status": "active",
            }

    import sys
    monkeypatch.setitem(sys.modules, "protocol_repo", _FakeProtocolRepo)


def _stub_body_region_lookup(monkeypatch, mapping: dict[str, str | None]) -> None:
    """Force exercise_kb.body_region_for to honor the test's mapping
    instead of the real knowledge/exercise-library.json. None values
    simulate exercises missing from the kb."""
    import exercise_kb

    def _resolver(exercise_id):
        if exercise_id is None:
            return None
        return mapping.get(exercise_id)

    monkeypatch.setattr(exercise_kb, "body_region_for", _resolver)


def test_today_enriches_with_body_region_and_is_current_region(
    authed_client, fake_user_id, monkeypatch,
):
    """Patient on an ankle protocol; sessions table has a mix of ankle
    and elbow rows. Both render — elbow is marked
    is_current_region=False so the sidebar can dim it."""
    _stub_session_today(monkeypatch, [
        {"id": "s1", "exercise_id": "ankle_alphabet", "status": "planned"},
        {"id": "s2", "exercise_id": "calf_raise", "status": "completed"},
        {"id": "s3", "exercise_id": "tricep_extension", "status": "skipped"},
    ])
    _stub_protocol_active(monkeypatch, {"body_region": "ankle"})
    _stub_body_region_lookup(monkeypatch, {
        "ankle_alphabet": "ankle",
        "calf_raise": "ankle",
        "tricep_extension": "elbow",
    })
    monkeypatch.setattr("main.ensure_user", lambda t, slack_user_id=None: t)

    resp = authed_client.get("/sessions/today", headers={"X-Timezone": "America/New_York"})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["active_body_region"] == "ankle"
    rows = {r["id"]: r for r in body["sessions"]}
    assert len(rows) == 3, "no row dropping — adherence-as-history"
    assert rows["s1"]["body_region"] == "ankle"
    assert rows["s1"]["is_current_region"] is True
    assert rows["s2"]["is_current_region"] is True
    assert rows["s3"]["body_region"] == "elbow"
    assert rows["s3"]["is_current_region"] is False


def test_today_no_active_protocol_marks_all_rows_not_current(
    authed_client, fake_user_id, monkeypatch,
):
    """No active protocol -> active_body_region is None and every row
    is is_current_region=False (nothing is "current"). Rows still
    return so the patient can see what they had been doing."""
    _stub_session_today(monkeypatch, [
        {"id": "s1", "exercise_id": "ankle_alphabet", "status": "planned"},
    ])
    _stub_protocol_active(monkeypatch, None)
    _stub_body_region_lookup(monkeypatch, {"ankle_alphabet": "ankle"})
    monkeypatch.setattr("main.ensure_user", lambda t, slack_user_id=None: t)

    resp = authed_client.get("/sessions/today", headers={"X-Timezone": "UTC"})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["active_body_region"] is None
    assert body["sessions"][0]["is_current_region"] is False
    assert body["sessions"][0]["body_region"] == "ankle"


def test_today_unknown_exercise_marks_body_region_null(
    authed_client, fake_user_id, monkeypatch,
):
    """Exercise not in the kb -> body_region: None, is_current_region:
    False (an unknown region cannot match the active region)."""
    _stub_session_today(monkeypatch, [
        {"id": "s1", "exercise_id": "ankle_alphabet", "status": "planned"},
        {"id": "s2", "exercise_id": "totally_unknown_id", "status": "completed"},
    ])
    _stub_protocol_active(monkeypatch, {"body_region": "ankle"})
    _stub_body_region_lookup(monkeypatch, {
        "ankle_alphabet": "ankle",
        "totally_unknown_id": None,
    })
    monkeypatch.setattr("main.ensure_user", lambda t, slack_user_id=None: t)

    resp = authed_client.get("/sessions/today", headers={"X-Timezone": "UTC"})
    assert resp.status_code == 200, resp.text
    rows = {r["id"]: r for r in resp.json()["sessions"]}
    assert rows["s1"]["is_current_region"] is True
    assert rows["s2"]["body_region"] is None
    assert rows["s2"]["is_current_region"] is False


def test_recent_enriches_with_body_region_and_is_current_region(
    authed_clinician_client, fake_clinician_id, fake_user_id, monkeypatch,
):
    """Clinician fetching another patient's last 7 days. Mix of ankle
    + elbow rows. All survive; elbow rows are flagged
    is_current_region=False so the dashboard can dim them."""
    _stub_session_recent(monkeypatch, [
        {"id": "r1", "exercise_id": "ankle_alphabet", "status": "completed",
         "created_at": "2026-05-05T10:00:00Z"},
        {"id": "r2", "exercise_id": "tricep_extension", "status": "skipped",
         "created_at": "2026-05-04T10:00:00Z"},
    ])
    _stub_protocol_active(monkeypatch, {"body_region": "ankle"})
    _stub_body_region_lookup(monkeypatch, {
        "ankle_alphabet": "ankle",
        "tricep_extension": "elbow",
    })
    monkeypatch.setattr("main.is_clinician", lambda uid: uid == fake_clinician_id)

    resp = authed_clinician_client.get(
        f"/sessions/recent?days=7&token={fake_user_id}",
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["token"] == fake_user_id
    assert body["active_body_region"] == "ankle"
    rows = {r["id"]: r for r in body["sessions"]}
    assert len(rows) == 2
    assert rows["r1"]["is_current_region"] is True
    assert rows["r2"]["is_current_region"] is False
    assert rows["r2"]["body_region"] == "elbow"


def test_recent_legacy_active_no_body_region_marks_rows_not_current(
    authed_client, fake_user_id, monkeypatch,
):
    """Legacy active protocol without a body_region key (pre-injury-
    anchoring rows). Defensive fallback: every row is_current_region=
    False so we don't accidentally claim something as 'current' when we
    don't actually know what the active region is."""
    _stub_session_recent(monkeypatch, [
        {"id": "r1", "exercise_id": "ankle_alphabet", "status": "completed"},
    ])
    # Active protocol exists but its payload has no body_region.
    _stub_protocol_active(monkeypatch, {"week": 3, "phase": "subacute"})
    _stub_body_region_lookup(monkeypatch, {"ankle_alphabet": "ankle"})

    resp = authed_client.get("/sessions/recent?days=7")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["active_body_region"] is None
    assert body["sessions"][0]["is_current_region"] is False
    # body_region still resolved on the row itself even when active is unknown
    assert body["sessions"][0]["body_region"] == "ankle"
