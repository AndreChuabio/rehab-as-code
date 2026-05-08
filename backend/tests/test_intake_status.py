"""GET /patient/me/intake-status — happy path + auth-rejected.

The endpoint returns server-derived patient state (needs_intake / needs_plan
/ ready) used by the frontend state machine to decide which modal to open
on auth-ready. We mock load_user / ensure_user so the test stays in-process.
"""
from __future__ import annotations


def test_intake_status_needs_intake_state(authed_client, fake_user_id, monkeypatch):
    """Brand-new patient — no intake row, no protocol_state row.

    Also asserts the PR-R additions: display_name + last_active are present
    in the response shape (both None for a brand-new user).
    """
    monkeypatch.setattr("main.ensure_user", lambda token, slack_user_id=None: token)
    monkeypatch.setattr(
        "main.user_store.load_user",
        lambda token: {
            "token": token,
            "patient_name": None,
            "intake": None,
            "protocol_state": None,
            "session_history": [],
            "last_active": None,
        },
    )
    monkeypatch.setattr(
        "main.user_store.get_display_name", lambda token: None,
    )

    resp = authed_client.get("/patient/me/intake-status")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["state"] == "needs_intake"
    assert body["has_intake"] is False
    assert body["has_protocol"] is False
    # PR-R: new fields present, both None for a never-seen user.
    assert "display_name" in body and body["display_name"] is None
    assert "last_active" in body and body["last_active"] is None


def test_intake_status_ready_state(authed_client, fake_user_id, monkeypatch):
    """Patient with intake AND a protocol PR — fully onboarded.

    Asserts display_name is the resolved string and last_active is the
    *prior* ISO timestamp (captured before ensure_user mutated it).
    """
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
            "last_active": "2026-05-01T12:00:00Z",
        },
    )
    monkeypatch.setattr(
        "main.user_store.get_display_name", lambda token: "Andre",
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
    # PR-R: display_name resolves; last_active is the prior visit's ISO ts.
    assert body["display_name"] == "Andre"
    assert body["last_active"] == "2026-05-01T12:00:00Z"


def test_intake_status_display_name_resolver_failure_is_graceful(
    authed_client, fake_user_id, monkeypatch,
):
    """If get_display_name raises (e.g. auth.users SELECT denied), the
    endpoint must still return 200 with display_name=None — no 5xx, no
    silent crash. Greeting bubble elides the name in that case."""
    monkeypatch.setattr("main.ensure_user", lambda token, slack_user_id=None: token)
    monkeypatch.setattr(
        "main.user_store.load_user",
        lambda token: {
            "token": token,
            "patient_name": "Andre",
            "intake": {"name": "Andre"},
            "protocol_state": None,
            "session_history": [],
            "last_active": "2026-05-01T12:00:00Z",
        },
    )

    def _boom(token):
        raise RuntimeError("auth.users SELECT denied")

    monkeypatch.setattr("main.user_store.get_display_name", _boom)

    resp = authed_client.get("/patient/me/intake-status")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["display_name"] is None
    assert body["last_active"] == "2026-05-01T12:00:00Z"


def test_intake_status_rejects_unauthenticated(unauthed_client):
    resp = unauthed_client.get("/patient/me/intake-status")
    assert resp.status_code == 401, resp.text


def test_intake_status_degrades_to_unknown_on_load_user_failure(
    authed_client, fake_user_id, monkeypatch,
):
    """PR-W1: pooler exhaustion / DB blip in load_user must not 5xx the
    endpoint. Degrade to 200 with state="unknown" + all flags False so the
    frontend's review-pill / today-CTA helpers can still render instead of
    the red "Patient state check failed (500)" toast.

    Pins the don't-5xx contract for the core load/ensure path.
    """
    def _boom(token):
        raise RuntimeError("EMAXCONNSESSION: pooler exhausted")

    monkeypatch.setattr("main.user_store.load_user", _boom)
    # ensure_user shouldn't be reached, but stub it so a regression doesn't
    # accidentally hit a real DB.
    monkeypatch.setattr("main.ensure_user", lambda token, slack_user_id=None: token)

    resp = authed_client.get("/patient/me/intake-status")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["state"] == "unknown"
    assert body["has_intake"] is False
    assert body["has_protocol"] is False
    assert body["display_name"] is None
    assert body["last_active"] is None
    assert body["session_count"] == 0
    assert body["review_status"] is None
