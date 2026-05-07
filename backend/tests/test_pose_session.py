"""POST /pose/session — happy path + auth-rejected.

This endpoint logs a completed pose-form-check set into the `checkins`
table for the authenticated patient. We mock save_checkin / ensure_user
because they hit a real DB; the goal of these tests is the FastAPI surface
(auth gating, payload roll-up, response shape), not the DB driver.
"""
from __future__ import annotations


def _payload():
    return {
        "exercise_id": "wall_squat",
        "exercise_name": "Wall squat",
        "started_at": "2026-05-06T10:00:00Z",
        "ended_at": "2026-05-06T10:01:30Z",
        "target_dose": "3x10",
        "reps": [
            {"rep": 1, "depth_min": 0.80, "status": "good", "msg": None},
            {"rep": 2, "depth_min": 0.78, "status": "good", "msg": None},
            {"rep": 3, "depth_min": 0.95, "status": "warn", "msg": "shallow"},
        ],
        "warnings": [],
        "client": "web/pose-v1-test",
    }


def test_pose_session_happy_path(authed_client, fake_user_id, monkeypatch):
    captured: dict = {}

    def _ensure_user(token, slack_user_id=None):
        captured["ensure_token"] = token
        return token

    def _save_checkin(token, payload):
        captured["save_token"] = token
        captured["save_payload"] = payload

    monkeypatch.setattr("main.ensure_user", _ensure_user)
    monkeypatch.setattr("main.save_checkin", _save_checkin)

    resp = authed_client.post("/pose/session", json=_payload())
    assert resp.status_code == 200, resp.text
    body = resp.json()

    # Server-side roll-up must reflect the rep array, not whatever the
    # client claimed. Worst status is "warn" (rep 3); best depth is 0.78.
    assert body["rep_count"] == 3
    assert body["best_depth"] == 0.78
    assert body["worst_status"] == "warn"

    # ensure_user + save_checkin both received the JWT-derived user id.
    assert captured["ensure_token"] == fake_user_id
    assert captured["save_token"] == fake_user_id
    saved = captured["save_payload"]
    assert saved["kind"] == "set_completion"
    assert saved["exercise_id"] == "wall_squat"


def test_pose_session_rejects_unauthenticated(unauthed_client):
    resp = unauthed_client.post("/pose/session", json=_payload())
    assert resp.status_code == 401, resp.text
