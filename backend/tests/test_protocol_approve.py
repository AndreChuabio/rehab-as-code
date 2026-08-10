"""POST /protocols/{id}/approve — happy path + auth-rejected.

The endpoint promotes a pending_review protocol to active. We mock
protocol_repo.approve so the test doesn't need a live DB; the mock
returns the same dict shape the real function returns on success.
"""
from __future__ import annotations
from datetime import datetime, timezone


def test_protocol_approve_happy_path(authed_client, fake_user_id, monkeypatch):
    captured: dict = {}

    def _approve(protocol_id, reviewed_by, notes):
        captured["protocol_id"] = protocol_id
        captured["reviewed_by"] = reviewed_by
        captured["notes"] = notes
        return {
            "id": protocol_id,
            "token": "patient-token-abc",
            "status": "active",
            "reviewed_at": datetime(2026, 5, 6, 12, 0, 0, tzinfo=timezone.utc),
        }

    # main.py imports protocol_repo lazily inside the handler — patch the
    # module attribute directly so the lazy import resolves to our stub.
    import protocol_repo
    monkeypatch.setattr(protocol_repo, "approve", _approve)

    # approve is clinician-gated (require_clinician_id). This test exercises the
    # endpoint's write logic, so open the gate for the JWT-derived patient id;
    # the non-clinician rejection is covered by its own test below.
    import main
    from auth import require_clinician_id
    monkeypatch.setitem(
        main.app.dependency_overrides, require_clinician_id, lambda: fake_user_id
    )

    resp = authed_client.post(
        "/protocols/fake-protocol-id/approve",
        json={"notes": "looks reasonable"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["approved"] is True
    assert body["protocol_id"] == "fake-protocol-id"
    assert body["status"] == "active"

    # Reviewer must be the JWT-derived user id, never client-provided.
    assert captured["reviewed_by"] == fake_user_id
    assert captured["notes"] == "looks reasonable"


def test_protocol_approve_rejects_unauthenticated(unauthed_client):
    resp = unauthed_client.post(
        "/protocols/fake-protocol-id/approve",
        json={"notes": "noop"},
    )
    assert resp.status_code == 401, resp.text


def test_protocol_approve_rejects_non_clinician(authed_client, monkeypatch):
    """A patient (authenticated, NOT staff) must not self-approve their own
    AI-drafted protocol — the clinician gate is the product's trust boundary.

    authed_client overrides current_user_id but NOT require_clinician_id, so the
    real clinician dependency runs and rejects. approve is mocked to a success
    row: if the gate ever regressed back to Depends(current_user_id) this would
    return 200 and fail the test."""
    import protocol_repo
    monkeypatch.setattr(
        protocol_repo,
        "approve",
        lambda **k: {"id": "x", "token": "t", "status": "active", "reviewed_at": None},
    )
    resp = authed_client.post(
        "/protocols/fake-protocol-id/approve",
        json={"notes": "sneaky self-approve"},
    )
    assert resp.status_code in (401, 403), resp.text
