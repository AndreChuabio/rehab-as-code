"""POST /start-session, GET /tavus/sessions/recent, POST /tavus/sessions/{id}/end.

PR-P hardens the Tavus integration with auth + identity + persistence + a
no-silent-fallback posture. These tests cover the FastAPI surface:

  * /start-session
      - happy path (auth ok, keys ok, conversation persisted)
      - auth-rejected returns 401
      - missing TAVUS_API_KEY raises 503
      - missing TAVUS_REPLICA_ID raises 503
      - upstream Tavus 5xx raises 502

  * /tavus/sessions/recent
      - returns >=0 rows ordered newest-first
      - auth-rejected returns 401
      - active vs expired computed via tavus_repo.is_active

  * /tavus/sessions/{id}/end
      - sets status='ended' + ended_at
      - 404 when row not found / not patient's

Both tavus_client.create_conversation and tavus_repo.* are stubbed so the
tests don't depend on a live Tavus account or DATABASE_URL.
"""
from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _stub_context(monkeypatch):
    """Replace context_builder + health/calendar/protocol fetches.

    All four are exercised on the success path and are not the unit under
    test here. Stubbing them keeps the test focused on the auth + tavus +
    persistence wiring.
    """
    monkeypatch.setattr(
        "main.get_health_data",
        lambda: {
            "sleep_score": 80,
            "hrv_ms": 60,
            "recovery_score": 75,
            "sleep_hours": 7.5,
            "hrv_7day_avg": 60,
        },
    )
    monkeypatch.setattr(
        "main.get_calendar_events",
        lambda: [{"time": "10:00", "title": "PT visit", "type": "low"}],
    )
    monkeypatch.setattr(
        "main.fetch_protocol_for_user",
        lambda _uid: {"phase": "post-op", "week": 4, "exercises": []},
    )
    monkeypatch.setattr(
        "main.fetch_protocol",
        lambda: {"phase": "post-op", "week": 4, "exercises": []},
    )
    monkeypatch.setattr(
        "main.build_system_prompt",
        lambda health, events, protocol=None, patient_name=None: {
            "system_prompt": "stub system prompt",
            "greeting": "Hi from Maya.",
            "recommendations": [
                {"priority": "high", "category": "form", "title": "Knee tracking",
                 "detail": "Stay aligned over the second toe."},
            ],
        },
    )
    monkeypatch.setattr("main.ensure_user", lambda token: token)
    monkeypatch.setattr(
        "user_store.get_display_name", lambda _uid: "Andre"
    )


def _stub_tavus_create(monkeypatch, *, replica_id="rep_1", persona_id="per_1"):
    captured: dict = {}

    def _create(system_prompt, greeting, user_name="there"):
        captured["system_prompt"] = system_prompt
        captured["greeting"] = greeting
        captured["user_name"] = user_name
        return {
            "conversation_url": "https://tavus.daily.co/abc123",
            "conversation_id": "conv_abc123",
            "status": "active",
            "replica_id": replica_id,
            "persona_id": persona_id,
            "expires_at": (
                datetime.now(timezone.utc) + timedelta(minutes=6)
            ).isoformat(),
        }

    monkeypatch.setattr("main.create_conversation", _create)
    return captured


class _FakeTavusRepo:
    """In-memory stand-in for tavus_repo with the same surface main.py uses.

    Re-imported by main.py at call time via `import tavus_repo`, so we install
    it via monkeypatch.setitem on sys.modules. Keeps state per-instance so
    tests don't bleed into each other.
    """

    class TavusRepoError(RuntimeError):
        pass

    def __init__(self):
        self.rows: list[dict] = []
        # Make the inner exception class available as an attribute the
        # endpoint code does `tavus_repo.TavusRepoError`.

    def insert_active(self, *, token, conversation_id, conversation_url,
                      replica_id, persona_id, expires_at):
        row = {
            "id": f"tavus-{len(self.rows) + 1}",
            "token": token,
            "conversation_id": conversation_id,
            "conversation_url": conversation_url,
            "replica_id": replica_id,
            "persona_id": persona_id,
            "status": "active",
            "created_at": datetime.now(timezone.utc).isoformat(),
            "expires_at": expires_at,
            "ended_at": None,
        }
        self.rows.append(row)
        return row

    def list_recent(self, token, *, limit=5):
        own = [r for r in self.rows if r["token"] == token]
        own.sort(key=lambda r: r["created_at"], reverse=True)
        return own[:limit]

    def end_session(self, *, session_id, token):
        for r in self.rows:
            if r["id"] == session_id and r["token"] == token:
                r["status"] = "ended"
                r["ended_at"] = datetime.now(timezone.utc).isoformat()
                return r
        raise self.TavusRepoError("not found")

    def is_active(self, row):
        if (row.get("status") or "").lower() != "active":
            return False
        expires_raw = row.get("expires_at")
        if not expires_raw:
            return True
        try:
            expires = datetime.fromisoformat(expires_raw.replace("Z", "+00:00"))
            if expires.tzinfo is None:
                expires = expires.replace(tzinfo=timezone.utc)
        except (TypeError, ValueError):
            return False
        return expires > datetime.now(timezone.utc)


@pytest.fixture
def fake_tavus_repo(monkeypatch):
    fake = _FakeTavusRepo()
    monkeypatch.setitem(__import__("sys").modules, "tavus_repo", fake)
    return fake


# ---------------------------------------------------------------------------
# /start-session
# ---------------------------------------------------------------------------


def test_start_session_happy_path(authed_client, fake_user_id, fake_tavus_repo, monkeypatch):
    _stub_context(monkeypatch)
    captured = _stub_tavus_create(monkeypatch)

    resp = authed_client.post("/start-session", json={})
    assert resp.status_code == 200, resp.text
    data = resp.json()

    # Identity is the JWT-derived display name, not anything client-provided.
    assert captured["user_name"] == "Andre"

    # Response shape: includes the conversation handles + a tavus_session_id
    # pointing at the persisted row.
    assert data["conversation_url"] == "https://tavus.daily.co/abc123"
    assert data["conversation_id"] == "conv_abc123"
    assert data["tavus_session_id"], "tavus_session_id should be populated on success"
    assert data["status"] == "active"
    assert data["greeting"] == "Hi from Maya."
    assert data["recommendations"][0]["title"] == "Knee tracking"

    # Persistence: a row was written for this patient.
    assert len(fake_tavus_repo.rows) == 1
    row = fake_tavus_repo.rows[0]
    assert row["token"] == fake_user_id
    assert row["conversation_id"] == "conv_abc123"
    assert row["status"] == "active"


def test_start_session_rejects_unauthenticated(unauthed_client):
    resp = unauthed_client.post("/start-session", json={})
    assert resp.status_code == 401, resp.text


def test_start_session_missing_api_key_returns_503(authed_client, fake_tavus_repo, monkeypatch):
    _stub_context(monkeypatch)

    # Force the real tavus_client._get_keys via TavusConfigError. Easiest path:
    # have main.create_conversation raise TavusConfigError directly.
    from tavus_client import TavusConfigError

    def _raise_config(*_a, **_kw):
        raise TavusConfigError("Tavus is not configured: missing env vars ['TAVUS_API_KEY']")

    monkeypatch.setattr("main.create_conversation", _raise_config)

    resp = authed_client.post("/start-session", json={})
    assert resp.status_code == 503, resp.text
    assert "Video call temporarily unavailable" in resp.json()["detail"]
    # No row should have been written when the upstream call never happened.
    assert fake_tavus_repo.rows == []


def test_start_session_missing_replica_id_returns_503(authed_client, fake_tavus_repo, monkeypatch):
    _stub_context(monkeypatch)
    from tavus_client import TavusConfigError

    def _raise_config(*_a, **_kw):
        raise TavusConfigError("Tavus is not configured: missing env vars ['TAVUS_REPLICA_ID']")

    monkeypatch.setattr("main.create_conversation", _raise_config)

    resp = authed_client.post("/start-session", json={})
    assert resp.status_code == 503, resp.text
    assert fake_tavus_repo.rows == []


def test_start_session_upstream_api_error_returns_502(authed_client, fake_tavus_repo, monkeypatch):
    _stub_context(monkeypatch)
    from tavus_client import TavusAPIError

    def _raise_api(*_a, **_kw):
        raise TavusAPIError("Tavus returned HTTP 502")

    monkeypatch.setattr("main.create_conversation", _raise_api)

    resp = authed_client.post("/start-session", json={})
    assert resp.status_code == 502, resp.text
    assert fake_tavus_repo.rows == []


# ---------------------------------------------------------------------------
# /tavus/sessions/recent
# ---------------------------------------------------------------------------


def test_recent_sessions_returns_active_flag(authed_client, fake_user_id, fake_tavus_repo, monkeypatch):
    _stub_context(monkeypatch)
    monkeypatch.setattr("main.ensure_user", lambda _t: _t)

    # Insert an ACTIVE row (expires in the future) and an EXPIRED row.
    future = (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat()
    past = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
    fake_tavus_repo.rows.extend([
        {
            "id": "tavus-1",
            "token": fake_user_id,
            "conversation_id": "conv1",
            "conversation_url": "https://tavus.daily.co/1",
            "replica_id": "rep_1", "persona_id": "per_1",
            "status": "active",
            "created_at": "2026-05-07T00:00:00+00:00",
            "expires_at": past,
            "ended_at": None,
        },
        {
            "id": "tavus-2",
            "token": fake_user_id,
            "conversation_id": "conv2",
            "conversation_url": "https://tavus.daily.co/2",
            "replica_id": "rep_1", "persona_id": "per_1",
            "status": "active",
            "created_at": "2026-05-07T00:01:00+00:00",
            "expires_at": future,
            "ended_at": None,
        },
    ])

    resp = authed_client.get("/tavus/sessions/recent")
    assert resp.status_code == 200, resp.text
    data = resp.json()

    sessions = data["sessions"]
    assert len(sessions) == 2
    # Newest first.
    assert sessions[0]["id"] == "tavus-2"
    assert sessions[0]["is_active"] is True
    assert sessions[1]["id"] == "tavus-1"
    assert sessions[1]["is_active"] is False  # expired


def test_recent_sessions_caps_at_five_by_default(authed_client, fake_user_id, fake_tavus_repo, monkeypatch):
    _stub_context(monkeypatch)
    future = (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat()
    for i in range(7):
        fake_tavus_repo.rows.append({
            "id": f"tavus-{i}",
            "token": fake_user_id,
            "conversation_id": f"conv{i}",
            "conversation_url": f"https://tavus.daily.co/{i}",
            "replica_id": "rep_1", "persona_id": "per_1",
            "status": "active",
            "created_at": f"2026-05-07T00:0{i}:00+00:00",
            "expires_at": future,
            "ended_at": None,
        })

    resp = authed_client.get("/tavus/sessions/recent")
    assert resp.status_code == 200, resp.text
    assert len(resp.json()["sessions"]) == 5


def test_recent_sessions_rejects_unauthenticated(unauthed_client):
    resp = unauthed_client.get("/tavus/sessions/recent")
    assert resp.status_code == 401, resp.text


# ---------------------------------------------------------------------------
# /tavus/sessions/{id}/end
# ---------------------------------------------------------------------------


def test_end_session_marks_ended(authed_client, fake_user_id, fake_tavus_repo, monkeypatch):
    _stub_context(monkeypatch)
    # Disable the upstream end call so the test doesn't hit the network.
    monkeypatch.setattr(
        "tavus_client.end_conversation", lambda _cid: True
    )
    fake_tavus_repo.rows.append({
        "id": "tavus-1",
        "token": fake_user_id,
        "conversation_id": "conv1",
        "conversation_url": "https://tavus.daily.co/1",
        "replica_id": "rep_1", "persona_id": "per_1",
        "status": "active",
        "created_at": "2026-05-07T00:00:00+00:00",
        "expires_at": None,
        "ended_at": None,
    })

    resp = authed_client.post("/tavus/sessions/tavus-1/end")
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["id"] == "tavus-1"
    assert data["status"] == "ended"
    assert data["ended_at"] is not None


def test_end_session_404_when_not_found(authed_client, fake_tavus_repo, monkeypatch):
    _stub_context(monkeypatch)
    resp = authed_client.post("/tavus/sessions/does-not-exist/end")
    assert resp.status_code == 404, resp.text


def test_end_session_404_when_other_patients_row(authed_client, fake_tavus_repo, monkeypatch):
    _stub_context(monkeypatch)
    monkeypatch.setattr("tavus_client.end_conversation", lambda _cid: True)
    fake_tavus_repo.rows.append({
        "id": "tavus-1",
        "token": "00000000-0000-0000-0000-000000000000",  # different patient
        "conversation_id": "conv1",
        "conversation_url": "https://tavus.daily.co/1",
        "replica_id": "rep_1", "persona_id": "per_1",
        "status": "active",
        "created_at": "2026-05-07T00:00:00+00:00",
        "expires_at": None,
        "ended_at": None,
    })

    resp = authed_client.post("/tavus/sessions/tavus-1/end")
    assert resp.status_code == 404, resp.text


def test_end_session_rejects_unauthenticated(unauthed_client):
    resp = unauthed_client.post("/tavus/sessions/tavus-1/end")
    assert resp.status_code == 401, resp.text
