"""POST /checkins - auto check-in endpoint (PR-N).

Covers happy path, validation (out-of-range pain, missing pain), auth
rejection, and silent truncation of notes >500 chars. The save_checkin call
is stubbed because the row insert is exercised by user_store's own tests;
here we want the FastAPI surface (validation, auth, response shape).
"""
from __future__ import annotations


def _stub_save(monkeypatch):
    """Patch ensure_user + save_checkin and return the captured call dict.

    save_checkin in real flat-store mode mutates the payload to add
    session_id and recorded_at. We mirror that here so the response-shape
    assertions work without a live store.
    """
    captured: dict = {}

    def _ensure_user(token, slack_user_id=None):
        captured["ensure_token"] = token
        return token

    def _save_checkin(token, payload):
        captured["save_token"] = token
        # Mimic flat-store: assign session_id if not provided. The endpoint
        # does not assign it, so this matches real save_checkin behavior.
        payload.setdefault("session_id", "checkin-stub-uuid")
        captured["save_payload"] = payload

    monkeypatch.setattr("main.ensure_user", _ensure_user)
    monkeypatch.setattr("main.save_checkin", _save_checkin)
    return captured


def test_create_checkin_happy_path(authed_client, fake_user_id, monkeypatch):
    captured = _stub_save(monkeypatch)
    body = {
        "pain_level": 4,
        "rpe": 6,
        "notes": "Felt a small twinge on rep 8 but otherwise good.",
        "associated_session_id": "sess-abc-123",
    }
    resp = authed_client.post("/checkins", json=body)
    assert resp.status_code == 201, resp.text
    out = resp.json()
    assert out["checkin_id"] == "checkin-stub-uuid"
    assert out["created_at"], "created_at should be a non-empty ISO timestamp"

    assert captured["ensure_token"] == fake_user_id
    assert captured["save_token"] == fake_user_id
    saved = captured["save_payload"]
    assert saved["kind"] == "auto_checkin"
    assert saved["pain_level"] == 4
    assert saved["rpe"] == 6
    assert saved["notes"] == "Felt a small twinge on rep 8 but otherwise good."
    assert saved["associated_session_id"] == "sess-abc-123"


def test_create_checkin_missing_pain_level_returns_422(authed_client, monkeypatch):
    _stub_save(monkeypatch)
    resp = authed_client.post("/checkins", json={"rpe": 5})
    # Pydantic catches missing required field with a 422.
    assert resp.status_code == 422, resp.text


def test_create_checkin_pain_level_negative_returns_422(authed_client, monkeypatch):
    _stub_save(monkeypatch)
    resp = authed_client.post("/checkins", json={"pain_level": -1})
    assert resp.status_code == 422, resp.text


def test_create_checkin_pain_level_too_high_returns_422(authed_client, monkeypatch):
    _stub_save(monkeypatch)
    resp = authed_client.post("/checkins", json={"pain_level": 11})
    assert resp.status_code == 422, resp.text


def test_create_checkin_rpe_out_of_range_returns_422(authed_client, monkeypatch):
    _stub_save(monkeypatch)
    resp = authed_client.post(
        "/checkins",
        json={"pain_level": 3, "rpe": 0},
    )
    assert resp.status_code == 422, resp.text


def test_create_checkin_rejects_unauthenticated(unauthed_client):
    resp = unauthed_client.post("/checkins", json={"pain_level": 3})
    assert resp.status_code == 401, resp.text


def test_create_checkin_truncates_long_notes(authed_client, monkeypatch):
    captured = _stub_save(monkeypatch)
    long_notes = "x" * 750  # 250 over the limit
    resp = authed_client.post(
        "/checkins",
        json={"pain_level": 2, "notes": long_notes},
    )
    assert resp.status_code == 201, resp.text
    saved = captured["save_payload"]
    assert saved["notes"] is not None
    assert len(saved["notes"]) == 500
    assert saved["notes"] == "x" * 500


def test_create_checkin_strips_control_chars(authed_client, monkeypatch):
    captured = _stub_save(monkeypatch)
    # Mix of allowed (\n, \t) and disallowed control chars (\x00, \x07).
    dirty = "Hello\x00world\x07\nfine\there"
    resp = authed_client.post(
        "/checkins",
        json={"pain_level": 1, "notes": dirty},
    )
    assert resp.status_code == 201, resp.text
    saved = captured["save_payload"]
    # \x00 and \x07 stripped; \n and \t preserved.
    assert saved["notes"] == "Helloworld\nfine\there"


def test_create_checkin_pain_zero_and_ten_are_valid(authed_client, monkeypatch):
    _stub_save(monkeypatch)
    for level in (0, 10):
        resp = authed_client.post("/checkins", json={"pain_level": level})
        assert resp.status_code == 201, f"pain_level={level} should be allowed"


def test_create_checkin_optional_fields_can_be_omitted(authed_client, monkeypatch):
    captured = _stub_save(monkeypatch)
    resp = authed_client.post("/checkins", json={"pain_level": 5})
    assert resp.status_code == 201, resp.text
    saved = captured["save_payload"]
    assert saved["rpe"] is None
    assert saved["notes"] is None
    assert saved["associated_session_id"] is None
