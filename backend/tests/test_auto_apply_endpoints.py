"""Task 9 - clinician auto-applied feed + revert endpoints.

  GET  /protocols/auto-applied  -> {"auto_applied": [...]}   (clinician-guarded)
  POST /protocols/{id}/revert   -> {"ok": True, "reverted_to": <parent_id>}

Both routes are clinician-only via the same require_clinician_id guard that
/protocols/pending uses. protocol_repo is monkeypatched directly (the handler
imports it lazily) so no live DATABASE_URL is needed - this mirrors
test_protocol_approve.py.
"""
from __future__ import annotations


def test_auto_applied_feed_requires_clinician(unauthed_client):
    resp = unauthed_client.get("/protocols/auto-applied")
    assert resp.status_code in (401, 403), resp.text


def test_revert_requires_clinician(unauthed_client):
    resp = unauthed_client.post("/protocols/fake-id/revert")
    assert resp.status_code in (401, 403), resp.text


def test_auto_applied_feed_returns_rows(authed_clinician_client, monkeypatch):
    rows = [
        {
            "id": "auto-1",
            "token": "patient-abc",
            "auto_applied": True,
            "reverted_at": None,
            "created_by_agent": "coach_swap",
        }
    ]
    import protocol_repo

    monkeypatch.setattr(protocol_repo, "list_auto_applied_open", lambda: rows)

    resp = authed_clinician_client.get("/protocols/auto-applied")
    assert resp.status_code == 200, resp.text
    assert resp.json()["auto_applied"] == rows


def test_revert_round_trip(authed_clinician_client, fake_clinician_id, monkeypatch):
    captured: dict = {}

    def _revert(protocol_id, reverted_by):
        captured["protocol_id"] = protocol_id
        captured["reverted_by"] = reverted_by
        return {"id": protocol_id, "token": "patient-abc", "reverted_at": None}

    import protocol_repo

    monkeypatch.setattr(protocol_repo, "revert", _revert)
    monkeypatch.setattr(
        protocol_repo,
        "get_active",
        lambda token: {"id": "parent-9", "token": token, "status": "active"},
    )

    resp = authed_clinician_client.post("/protocols/auto-7/revert")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["ok"] is True
    # reverted_to is the protocol that is active again after the revert.
    assert body["reverted_to"] == "parent-9"
    # Reverter must be the JWT-derived clinician id, never client-supplied.
    assert captured["reverted_by"] == fake_clinician_id
    assert captured["protocol_id"] == "auto-7"


def test_revert_conflict_returns_409(authed_clinician_client, monkeypatch):
    import protocol_repo

    def _revert(protocol_id, reverted_by):
        raise protocol_repo.ProtocolRepoError("not an open auto-applied row")

    monkeypatch.setattr(protocol_repo, "revert", _revert)

    resp = authed_clinician_client.post("/protocols/stale-1/revert")
    assert resp.status_code == 409, resp.text


def test_revert_succeeds_when_parent_lookup_fails(authed_clinician_client, monkeypatch):
    """revert() committed; a transient failure on the secondary get_active read
    must degrade to reverted_to=None, not surface a misleading 500."""
    import protocol_repo

    monkeypatch.setattr(
        protocol_repo,
        "revert",
        lambda protocol_id, reverted_by: {
            "id": protocol_id,
            "token": "patient-abc",
            "reverted_at": None,
        },
    )

    def _boom(token):
        raise RuntimeError("connection dropped")

    monkeypatch.setattr(protocol_repo, "get_active", _boom)

    resp = authed_clinician_client.post("/protocols/auto-7/revert")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["ok"] is True
    assert body["reverted_to"] is None
