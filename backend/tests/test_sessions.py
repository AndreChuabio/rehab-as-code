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
