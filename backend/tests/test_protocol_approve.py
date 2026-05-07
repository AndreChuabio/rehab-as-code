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
