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
    """Patient with intake AND an active protocol row — fully onboarded.

    "ready" derives from protocol_repo.get_active (the canonical signal), not
    protocol_state.last_pr_url (a dead PR-bus field). current_phase/week come
    from the active protocol payload. Also asserts the PR-R additions
    (display_name + prior last_active).
    """
    monkeypatch.setattr("main.ensure_user", lambda token, slack_user_id=None: token)
    monkeypatch.setattr(
        "main.user_store.load_user",
        lambda token: {
            "token": token,
            "patient_name": "Andre",
            "intake": {"name": "Andre", "injury": "knee"},
            "protocol_state": {"current_phase": "acute", "current_week": 2},
            "session_history": [{"id": 1}, {"id": 2}],
            "last_active": "2026-05-01T12:00:00Z",
        },
    )
    monkeypatch.setattr(
        "main.user_store.get_display_name", lambda token: "Andre",
    )
    monkeypatch.setattr(
        "protocol_repo.get_active",
        lambda token: {
            "id": "p1",
            "token": token,
            "status": "active",
            "payload": {"phase": "acute", "week": 2},
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


def test_intake_status_ready_from_active_protocol_not_pr_url(
    authed_client, fake_user_id, monkeypatch,
):
    """Regression: a returning patient with an ACTIVE protocol row but no
    vestigial protocol_state.last_pr_url must resolve to "ready" — not
    "needs_plan".

    last_pr_url is a dead PR-bus field (always None now); keying state off it
    made Maya greet every returning patient as if drafting their FIRST plan,
    even at subacute week 5. State must derive from protocol_repo.get_active,
    and current_phase/week must come from the active payload.
    """
    monkeypatch.setattr("main.ensure_user", lambda token, slack_user_id=None: token)
    monkeypatch.setattr(
        "main.user_store.load_user",
        lambda token: {
            "token": token,
            "patient_name": "Christian",
            "intake": {"name": "Christian", "injury": "ankle"},
            # Real post-PR-bus shape: protocol_state carries NO last_pr_url.
            "protocol_state": {"current_phase": None, "current_week": None},
            "session_history": [{"id": 1}],
            "last_active": "2026-05-20T12:00:00Z",
        },
    )
    monkeypatch.setattr("main.user_store.get_display_name", lambda token: "Christian")
    # Canonical signal: an active protocol row exists for this patient.
    monkeypatch.setattr(
        "protocol_repo.get_active",
        lambda token: {
            "id": "p1",
            "token": token,
            "status": "active",
            "payload": {"phase": "subacute", "week": 5},
        },
    )

    resp = authed_client.get("/patient/me/intake-status")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["state"] == "ready"
    assert body["has_protocol"] is True
    assert body["current_phase"] == "subacute"
    assert body["current_week"] == 5


def test_intake_status_needs_plan_when_no_active_protocol(
    authed_client, fake_user_id, monkeypatch,
):
    """Intake present but get_active returns None -> needs_plan (the
    legitimate pre-first-plan state). Pins the new derivation so a future
    edit can't silently flip an intake-only patient to "ready"."""
    monkeypatch.setattr("main.ensure_user", lambda token, slack_user_id=None: token)
    monkeypatch.setattr(
        "main.user_store.load_user",
        lambda token: {
            "token": token,
            "patient_name": "Christian",
            "intake": {"name": "Christian", "injury": "ankle"},
            "protocol_state": {"last_pr_url": "https://stale/pr/1"},  # ignored now
            "session_history": [],
            "last_active": None,
        },
    )
    monkeypatch.setattr("main.user_store.get_display_name", lambda token: "Christian")
    monkeypatch.setattr("protocol_repo.get_active", lambda token: None)

    resp = authed_client.get("/patient/me/intake-status")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    # No active row -> needs_plan, even though the stale last_pr_url is truthy.
    assert body["state"] == "needs_plan"
    assert body["has_protocol"] is False


def test_intake_status_pending_intake_placeholder_is_not_ready(
    authed_client, fake_user_id, monkeypatch,
):
    """Regression: a degenerate active protocol stuck at phase 'pending_intake'
    (a pre-onboarding sentinel row) must NOT count as has_protocol.

    Otherwise the patient resolves to "ready", the intake modal never opens,
    and "Start intake" dead-ends. The account must read as needs_intake (no
    intake row) / needs_plan (intake present) so the real intake path reopens.
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
    monkeypatch.setattr("main.user_store.get_display_name", lambda token: None)
    monkeypatch.setattr(
        "protocol_repo.get_active",
        lambda token: {
            "id": "placeholder-1",
            "token": token,
            "status": "active",
            "payload": {
                "phase": "pending_intake",
                "week": 0,
                "exercises": [{"name": "clinician_review_required"}],
            },
        },
    )

    resp = authed_client.get("/patient/me/intake-status")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["has_protocol"] is False
    # No intake row + placeholder active -> needs_intake (intake reopens).
    assert body["state"] == "needs_intake"


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
