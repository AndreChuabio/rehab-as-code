"""GET /patient/me/intake-status — happy path + auth-rejected.

The endpoint returns server-derived patient state (needs_intake / needs_plan
/ ready) used by the frontend state machine to decide which modal to open
on auth-ready. We mock load_user / ensure_user so the test stays in-process.
"""
from __future__ import annotations


def test_intake_status_needs_intake_state(authed_client, fake_user_id, monkeypatch):
    """Brand-new patient — no intake row, no protocol_state row."""
    monkeypatch.setattr("main.ensure_user", lambda token, slack_user_id=None: token)
    monkeypatch.setattr(
        "main.user_store.load_user",
        lambda token: {
            "token": token,
            "patient_name": None,
            "intake": None,
            "protocol_state": None,
            "session_history": [],
        },
    )

    resp = authed_client.get("/patient/me/intake-status")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["state"] == "needs_intake"
    assert body["has_intake"] is False
    assert body["has_protocol"] is False


def test_intake_status_ready_state(authed_client, fake_user_id, monkeypatch):
    """Patient with intake AND a protocol PR — fully onboarded."""
    monkeypatch.setattr("main.ensure_user", lambda token, slack_user_id=None: token)
    monkeypatch.setattr(
        "main.user_store.load_user",
        lambda token: {
            "token": token,
            "patient_name": "Andre",
            "intake": {"name": "Andre", "injury": "knee"},
            "protocol_state": {
                "current_phase": "acute",
                "current_week": 2,
                "last_pr_url": "https://github.com/x/y/pull/1",
            },
            "session_history": [{"id": 1}, {"id": 2}],
        },
    )

    resp = authed_client.get("/patient/me/intake-status")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["state"] == "ready"
    assert body["has_intake"] is True
    assert body["has_protocol"] is True
    assert body["current_phase"] == "acute"
    assert body["current_week"] == 2
    assert body["session_count"] == 2


def test_intake_status_rejects_unauthenticated(unauthed_client):
    resp = unauthed_client.get("/patient/me/intake-status")
    assert resp.status_code == 401, resp.text
