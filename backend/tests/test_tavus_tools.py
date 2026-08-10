"""Coverage of the Tavus LLM-tool delivery.api callback (/tavus/tools/dispatch).

Hermetic under the sqlite test env: tavus_repo is a sys.modules fake,
protocol_loader / session_repo reads are monkeypatched, and exercise_kb runs
against the real on-disk library (knee + ankle in scope). No DATABASE_URL,
OpenAI, Anthropic, or Tavus network is touched.

The HMAC contract under test (verified from docs.tavus.io pal/llm-tool-delivery):
  X-Tavus-Signature = hmac_sha256(secret, RAW_BODY).hexdigest(), lowercase hex,
  signed over the exact bytes sent. Tests sign the literal request bytes so the
  verification path exercises the same raw-body discipline as production.

Covered:
  (a) empty secret env -> 503
  (b) bad / missing signature -> 401
  (c) valid signature + each tool -> 200 + minimized shape
  (d) unresolved conversation_id -> 404; missing name/conversation_id -> 400
  (e) unknown tool name -> 400
  (f) `arguments` JSON-string is decoded; `days` is clamped to 1..14
  (g) get_active_protocol alias; empty protocol -> no_active_protocol
"""
from __future__ import annotations

import hashlib
import hmac
import json
import sys

import pytest
from fastapi.testclient import TestClient

import main

_SECRET = "tool-s3cret"
_CONV = "c123456789"
_TOKEN = "11111111-1111-1111-1111-111111111111"


class _FakeRepo:
    """sys.modules stand-in for tavus_repo (dispatch does `import tavus_repo`)."""

    def __init__(self, by_conv=None):
        self._by_conv = by_conv or {}

    def get_token_by_conversation_id(self, conversation_id):
        return self._by_conv.get(conversation_id)


def _install_fake_repo(monkeypatch, *, by_conv=None):
    monkeypatch.setitem(sys.modules, "tavus_repo", _FakeRepo(by_conv=by_conv))


def _sign(raw: bytes, secret: str = _SECRET) -> str:
    return hmac.new(secret.encode("utf-8"), raw, hashlib.sha256).hexdigest()


def _post(client, body, *, secret=_SECRET, signature=None):
    """POST the EXACT serialized bytes we signed (raw-body discipline)."""
    raw = json.dumps(body).encode("utf-8")
    sig = signature if signature is not None else _sign(raw, secret)
    return client.post(
        "/tavus/tools/dispatch",
        content=raw,
        headers={"X-Tavus-Signature": sig, "Content-Type": "application/json"},
    )


@pytest.fixture
def client():
    return TestClient(main.app)


@pytest.fixture
def configured(monkeypatch):
    """Secret set + conversation maps to a patient."""
    monkeypatch.setenv("TAVUS_TOOL_HMAC_SECRET", _SECRET)
    _install_fake_repo(monkeypatch, by_conv={_CONV: _TOKEN})


# ---------------------------------------------------------------------------
# (a) / (b) auth boundary
# ---------------------------------------------------------------------------


def test_empty_secret_returns_503(client, monkeypatch):
    monkeypatch.setenv("TAVUS_TOOL_HMAC_SECRET", "")
    resp = _post(client, {"name": "get_patient_protocols", "conversation_id": _CONV})
    assert resp.status_code == 503, resp.text


def test_bad_signature_returns_401(client, configured):
    resp = _post(
        client,
        {"name": "get_patient_protocols", "conversation_id": _CONV},
        signature="deadbeef",
    )
    assert resp.status_code == 401, resp.text


def test_missing_signature_returns_401(client, configured):
    raw = json.dumps({"name": "get_patient_protocols", "conversation_id": _CONV}).encode()
    resp = client.post("/tavus/tools/dispatch", content=raw)
    assert resp.status_code == 401, resp.text


def test_signature_over_different_bytes_returns_401(client, configured):
    """A signature valid for a DIFFERENT body must not authorize this one."""
    other = json.dumps({"name": "x", "conversation_id": _CONV}).encode()
    resp = _post(
        client,
        {"name": "get_patient_protocols", "conversation_id": _CONV},
        signature=_sign(other),
    )
    assert resp.status_code == 401, resp.text


# ---------------------------------------------------------------------------
# (d) patient mapping + request validation
# ---------------------------------------------------------------------------


def test_unresolved_conversation_returns_404(client, monkeypatch):
    monkeypatch.setenv("TAVUS_TOOL_HMAC_SECRET", _SECRET)
    _install_fake_repo(monkeypatch, by_conv={})  # no mapping
    resp = _post(client, {"name": "get_patient_protocols", "conversation_id": _CONV})
    assert resp.status_code == 404, resp.text


def test_missing_conversation_id_returns_400(client, configured):
    resp = _post(client, {"name": "get_patient_protocols"})
    assert resp.status_code == 400, resp.text


def test_missing_name_returns_400(client, configured):
    resp = _post(client, {"conversation_id": _CONV})
    assert resp.status_code == 400, resp.text


def test_unknown_tool_returns_400(client, configured):
    resp = _post(client, {"name": "drop_tables", "conversation_id": _CONV})
    assert resp.status_code == 400, resp.text


# ---------------------------------------------------------------------------
# (c) get_active_protocol / get_patient_protocols
# ---------------------------------------------------------------------------


def _stub_protocol(monkeypatch, payload):
    monkeypatch.setattr("protocol_loader.fetch_protocol_for_user", lambda t: payload)


def test_get_patient_protocols_happy_path(client, configured, monkeypatch):
    _stub_protocol(
        monkeypatch,
        {
            "week": 5,
            "phase": "subacute",
            "body_region": "ankle",
            "exercises": [
                {"id": "ankle_calf_raises_double_leg", "name": "Double-Leg Calf Raises",
                 "sets": 3, "reps": 12, "ROM_target_deg": 20},
                {"id": "ankle_alphabet", "name": "Ankle Alphabet", "sets": 2, "reps": 10},
            ],
        },
    )
    resp = _post(client, {"name": "get_patient_protocols", "conversation_id": _CONV,
                          "arguments": ""})
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["status"] == "ok"
    assert data["week"] == 5 and data["phase"] == "subacute"
    assert data["body_region"] == "ankle"
    names = [e["name"] for e in data["exercises"]]
    assert "Double-Leg Calf Raises" in names
    first = data["exercises"][0]
    assert first["sets"] == 3 and first["reps"] == 12
    assert first["rom_target_deg"] == 20  # ROM_target_deg mapped


def test_get_active_protocol_alias(client, configured, monkeypatch):
    _stub_protocol(monkeypatch, {"week": 1, "phase": "acute",
                                 "exercises": [{"name": "Quad Sets"}]})
    resp = _post(client, {"name": "get_active_protocol", "conversation_id": _CONV})
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "ok"


def test_no_active_protocol(client, configured, monkeypatch):
    _stub_protocol(monkeypatch, {})  # pending / empty payload
    resp = _post(client, {"name": "get_patient_protocols", "conversation_id": _CONV})
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "no_active_protocol"


# ---------------------------------------------------------------------------
# (f) get_recent_history: arguments parsing + clamping + shape
# ---------------------------------------------------------------------------


def test_recent_history_parses_json_string_args(client, configured, monkeypatch):
    captured = {}

    def _fake_list_recent(token, days=7):
        captured["token"] = token
        captured["days"] = days
        return [
            {"created_at": "2026-06-28T10:00:00+00:00", "exercise_id": "quad_sets",
             "completed_reps": 30, "planned_reps": 45, "status": "complete",
             "checkin_pain_level": 2},
        ]

    monkeypatch.setattr("session_repo.list_recent", _fake_list_recent)
    # arguments arrives as a JSON-encoded STRING per the Tavus contract.
    resp = _post(client, {"name": "get_recent_history", "conversation_id": _CONV,
                          "arguments": json.dumps({"days": 3})})
    assert resp.status_code == 200, resp.text
    assert captured["token"] == _TOKEN
    assert captured["days"] == 3
    data = resp.json()
    assert data["status"] == "ok" and data["days"] == 3 and data["count"] == 1
    s = data["sessions"][0]
    assert s["date"] == "2026-06-28" and s["exercise"] == "quad_sets"
    assert s["completed_reps"] == 30 and s["pain_level"] == 2


@pytest.mark.parametrize("requested,expected", [(99, 14), (0, 1), (-5, 1), (7, 7)])
def test_recent_history_clamps_days(client, configured, monkeypatch, requested, expected):
    captured = {}

    def _fake_list_recent(token, days=7):
        captured["days"] = days
        return []

    monkeypatch.setattr("session_repo.list_recent", _fake_list_recent)
    resp = _post(client, {"name": "get_recent_history", "conversation_id": _CONV,
                          "arguments": json.dumps({"days": requested})})
    assert resp.status_code == 200, resp.text
    assert captured["days"] == expected


def test_recent_history_caps_rows(client, configured, monkeypatch):
    rows = [{"created_at": f"2026-06-{d:02d}T00:00:00+00:00", "exercise_id": "x",
             "status": "complete"} for d in range(1, 28)]
    monkeypatch.setattr("session_repo.list_recent", lambda token, days=7: rows)
    resp = _post(client, {"name": "get_recent_history", "conversation_id": _CONV})
    assert resp.status_code == 200, resp.text
    assert resp.json()["count"] == 20  # _HISTORY_MAX_ROWS


# ---------------------------------------------------------------------------
# (c) list_approved_exercises against the REAL library
# ---------------------------------------------------------------------------


def test_list_approved_exercises_in_scope_only(client, configured):
    resp = _post(client, {"name": "list_approved_exercises", "conversation_id": _CONV})
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["status"] == "ok" and data["count"] > 0
    regions = {e["region"] for e in data["exercises"]}
    assert regions and regions.issubset({"knee", "ankle"})


def test_list_approved_exercises_region_filter(client, configured):
    resp = _post(client, {"name": "list_approved_exercises", "conversation_id": _CONV,
                          "arguments": json.dumps({"body_region": "ankle"})})
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["count"] > 0
    assert all(e["region"] == "ankle" for e in data["exercises"])
