"""Trust-loop integration test (PR-H, Phase S4).

Walks the round trip from the patient's perspective:

    1. /patient/me/intake-status -> review_status.state = "pending_review"
       (immediately after a chat draft writes a row)
    2. clinician approves the row
    3. /patient/me/intake-status -> review_status.state = "recently_approved"
       (with reviewer initials populated)

Both steps run against the FastAPI TestClient. We mock protocol_repo's
`get_review_status` directly so the test doesn't need a live Postgres - the
goal here is to verify the wiring (endpoint -> repo helper -> response
shape), not the SQL itself; the SQL is exercised in test_review_status.

The pre-existing test_intake_status / test_protocol_approve files cover
the pieces in isolation; this file confirms they compose correctly when
the patient's frontend round-trips through both.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone


def test_review_status_pending_then_approved_round_trip(
    authed_client, fake_user_id, monkeypatch,
):
    """Same patient session: pending -> approve -> recently_approved.

    Tracks an in-test "fake DB" via a closure so the second call returns the
    post-approval state without us having to wire a real Postgres. The
    `protocol_repo.get_review_status` and `protocol_repo.approve` mocks both
    flip the same shared dict.
    """
    # In-test state: starts in "pending_review", flips to "recently_approved"
    # after the approve endpoint fires.
    fake_db = {
        "state": "pending_review",
        "protocol_id": "11111111-1111-1111-1111-aaaaaaaaaaaa",
        "submitted_at": datetime(2026, 5, 7, 9, 0, 0, tzinfo=timezone.utc).isoformat(),
        "reviewed_at": None,
        "reviewer_initials": None,
        "notes_excerpt": None,
    }

    monkeypatch.setattr("main.ensure_user", lambda token, slack_user_id=None: token)
    monkeypatch.setattr(
        "main.user_store.load_user",
        lambda token: {
            "token": token,
            "patient_name": "Andre",
            "intake": {"name": "Andre", "injury": "knee"},
            "protocol_state": {
                "current_phase": "subacute",
                "current_week": 4,
                "last_pr_url": "https://github.com/x/y/pull/1",
            },
            "session_history": [],
        },
    )

    import protocol_repo
    monkeypatch.setattr(
        protocol_repo,
        "get_review_status",
        lambda token: dict(fake_db),
    )

    # Step 1: patient hits /patient/me/intake-status. Pending_review surfaces.
    resp = authed_client.get("/patient/me/intake-status")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["state"] == "ready"
    assert body["review_status"] is not None
    assert body["review_status"]["state"] == "pending_review"
    assert body["review_status"]["protocol_id"] == "11111111-1111-1111-1111-aaaaaaaaaaaa"
    assert body["review_status"]["reviewer_initials"] is None

    # Step 2: clinician approves the pending row. The approve endpoint is
    # patient-scoped here (Andre approves his own row in this test) but the
    # production path runs with a clinician JWT.
    reviewed_at = datetime.now(timezone.utc) - timedelta(minutes=5)

    def _approve(protocol_id, reviewed_by, notes):
        # Flip the shared "DB" so step 3 sees recently_approved.
        fake_db.update({
            "state": "recently_approved",
            "protocol_id": protocol_id,
            "submitted_at": None,
            "reviewed_at": reviewed_at.isoformat(),
            "reviewer_initials": "NH",
            "notes_excerpt": None,
        })
        return {
            "id": protocol_id,
            "token": "patient-token-abc",
            "status": "active",
            "reviewed_at": reviewed_at,
        }

    monkeypatch.setattr(protocol_repo, "approve", _approve)

    resp = authed_client.post(
        "/protocols/11111111-1111-1111-1111-aaaaaaaaaaaa/approve",
        json={"notes": "looks reasonable"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["approved"] is True

    # Step 3: patient re-fetches intake-status. State is now recently_approved
    # with reviewer initials surfaced.
    resp = authed_client.get("/patient/me/intake-status")
    assert resp.status_code == 200
    body = resp.json()
    rs = body["review_status"]
    assert rs["state"] == "recently_approved"
    assert rs["reviewer_initials"] == "NH"
    assert rs["reviewed_at"] is not None
    assert rs["notes_excerpt"] is None


def test_review_status_pending_then_rejected_round_trip(
    authed_client, fake_user_id, monkeypatch,
):
    """Pending -> reject -> recently_rejected with notes_excerpt.

    Same shape as the approve test, but the post-decision state carries a
    redacted excerpt of the clinician's review notes so the patient can
    open the panel and see why.
    """
    fake_db = {
        "state": "pending_review",
        "protocol_id": "22222222-2222-2222-2222-bbbbbbbbbbbb",
        "submitted_at": datetime(2026, 5, 7, 9, 0, 0, tzinfo=timezone.utc).isoformat(),
        "reviewed_at": None,
        "reviewer_initials": None,
        "notes_excerpt": None,
    }

    monkeypatch.setattr("main.ensure_user", lambda token, slack_user_id=None: token)
    monkeypatch.setattr(
        "main.user_store.load_user",
        lambda token: {
            "token": token,
            "patient_name": "Andre",
            "intake": {"name": "Andre", "injury": "knee"},
            "protocol_state": {
                "current_phase": "subacute",
                "current_week": 4,
                "last_pr_url": "https://github.com/x/y/pull/1",
            },
            "session_history": [],
        },
    )

    import protocol_repo
    monkeypatch.setattr(
        protocol_repo, "get_review_status", lambda token: dict(fake_db),
    )

    reviewed_at = datetime.now(timezone.utc) - timedelta(minutes=2)

    def _reject(protocol_id, reviewed_by, notes):
        fake_db.update({
            "state": "recently_rejected",
            "protocol_id": protocol_id,
            "submitted_at": None,
            "reviewed_at": reviewed_at.isoformat(),
            "reviewer_initials": "NH",
            "notes_excerpt": notes[:100],
        })
        return {
            "id": protocol_id,
            "token": "patient-token-abc",
            "status": "rejected",
            "reviewed_at": reviewed_at,
        }

    monkeypatch.setattr(protocol_repo, "reject", _reject)

    long_notes = (
        "Single-leg squat is too aggressive at week 3 post-op. Regress to "
        "wall-supported sit-to-stand for two weeks then re-evaluate."
    )
    resp = authed_client.post(
        "/protocols/22222222-2222-2222-2222-bbbbbbbbbbbb/reject",
        json={"notes": long_notes},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["rejected"] is True

    resp = authed_client.get("/patient/me/intake-status")
    assert resp.status_code == 200
    body = resp.json()
    rs = body["review_status"]
    assert rs["state"] == "recently_rejected"
    assert rs["reviewer_initials"] == "NH"
    assert rs["notes_excerpt"] is not None
    assert len(rs["notes_excerpt"]) <= 100


def test_review_status_none_when_helper_returns_none(
    authed_client, fake_user_id, monkeypatch,
):
    """get_review_status() returning None (DB error) -> review_status: null.

    Frontend renders no pill. Caller does NOT 5xx.
    """
    monkeypatch.setattr("main.ensure_user", lambda token, slack_user_id=None: token)
    monkeypatch.setattr(
        "main.user_store.load_user",
        lambda token: {
            "token": token,
            "patient_name": "Andre",
            "intake": {"name": "Andre"},
            "protocol_state": {"last_pr_url": "x"},
            "session_history": [],
        },
    )

    import protocol_repo
    monkeypatch.setattr(protocol_repo, "get_review_status", lambda token: None)

    resp = authed_client.get("/patient/me/intake-status")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert "review_status" in body
    assert body["review_status"] is None


def test_review_status_handles_repo_config_error_as_null(
    authed_client, fake_user_id, monkeypatch,
):
    """ProtocolRepoError (e.g. DATABASE_URL missing in dev) -> review_status: null.

    Same surface as a generic DB error - graceful degrade, no toast.
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
        },
    )

    import protocol_repo

    def _raise(_token):
        raise protocol_repo.ProtocolRepoError("DATABASE_URL not set")

    monkeypatch.setattr(protocol_repo, "get_review_status", _raise)

    resp = authed_client.get("/patient/me/intake-status")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["review_status"] is None
