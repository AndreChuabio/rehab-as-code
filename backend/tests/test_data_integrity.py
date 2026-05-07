"""Tests for the GET /protocols/{id} `data_integrity` field (PR-I).

The data-integrity diagnostic compares the patient's expected body_region
(derived from intake.injury_type via clinical_taxonomy.body_region) against
the body_region of every exercise in the active and proposed payloads.
status == "region_mismatch" when any exercise targets the wrong region;
"ok" otherwise.

Reuses the same in-process stubs that test_clinician_detail.py uses so we
don't need a live Postgres or Anthropic. The narrator path is mocked.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import pytest


# ---------------------------------------------------------------------------
# Local fakes — mirror test_clinician_detail.py so this file is independently
# runnable. The two stay in sync deliberately; if a third file lands we'll
# pull these into conftest.
# ---------------------------------------------------------------------------


class _FakeTextBlock:
    def __init__(self, text: str) -> None:
        self.type = "text"
        self.text = text


class _FakeUsage:
    def __init__(self) -> None:
        self.input_tokens = 100
        self.output_tokens = 50


class _FakeResponse:
    def __init__(self, text: str) -> None:
        self.content = [_FakeTextBlock(text)]
        self.usage = _FakeUsage()


class _FakeAnthropicClient:
    def __init__(self, response_text: str = "stub") -> None:
        self._response_text = response_text

        class _Messages:
            def create(inner, **kwargs):
                return _FakeResponse(self._response_text)

        self.messages = _Messages()


def _stub_anthropic(monkeypatch) -> None:
    import anthropic
    fake = _FakeAnthropicClient()
    monkeypatch.setattr(anthropic, "Anthropic", lambda api_key=None: fake)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key-not-real")


def _clear_narrator_cache() -> None:
    import diff_narrator
    diff_narrator._CACHE.clear()


_PATIENT_TOKEN = "11111111-1111-1111-1111-111111111111"
_PROTO_ID = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"


def _build_target(payload: dict[str, Any], token: str = _PATIENT_TOKEN) -> dict:
    return {
        "id": _PROTO_ID,
        "token": token,
        "parent_id": None,
        "payload": payload,
        "status": "pending_review",
        "created_by_agent": "test",
        "created_at": datetime(2026, 5, 6, 12, 0, tzinfo=timezone.utc),
        "reviewed_by": None,
        "reviewed_at": None,
        "review_notes": None,
    }


def _build_active(payload: dict[str, Any], token: str = _PATIENT_TOKEN) -> dict:
    return {
        "id": "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb",
        "token": token,
        "parent_id": None,
        "payload": payload,
        "status": "active",
        "created_by_agent": "test",
        "created_at": datetime(2026, 4, 29, 12, 0, tzinfo=timezone.utc),
        "reviewed_by": None,
        "reviewed_at": None,
        "review_notes": None,
    }


def _stub_repo(monkeypatch, target: dict, active: dict | None) -> None:
    import protocol_repo
    monkeypatch.setattr(
        protocol_repo, "get",
        lambda pid: target if pid == _PROTO_ID else None,
    )
    monkeypatch.setattr(
        protocol_repo, "get_active",
        lambda tok: active if (active and tok == active["token"]) else None,
    )


def _stub_user(monkeypatch, *, token: str, intake: dict | None,
               history: list[dict] | None = None) -> None:
    import user_store
    monkeypatch.setattr(
        user_store, "load_user",
        lambda t: ({"patient_name": (intake or {}).get("name"), "intake": intake}
                   if t == token else None),
    )
    monkeypatch.setattr(
        user_store, "get_session_history",
        lambda t, limit=10: (history or []) if t == token else [],
    )


# ---------------------------------------------------------------------------
# Happy path: regions match
# ---------------------------------------------------------------------------


def test_data_integrity_ok_when_regions_match(authed_clinician_client, monkeypatch):
    """Ankle injury + ankle exercises in BOTH active and proposed -> ok."""
    _clear_narrator_cache()
    target = _build_target({
        "phase": "subacute", "week": 4,
        "exercises": [{"id": "ankle_alphabet", "sets": 1, "reps": 12}],
    })
    active = _build_active({
        "phase": "acute", "week": 1,
        "exercises": [{"id": "ankle_dorsiflexion_band", "sets": 1, "reps": 8}],
    })
    _stub_repo(monkeypatch, target, active)
    _stub_user(monkeypatch, token=_PATIENT_TOKEN, intake={
        "name": "Test", "injury_type": "lateral ankle sprain",
    })
    _stub_anthropic(monkeypatch)
    monkeypatch.setattr("main.is_clinician", lambda uid: True)

    resp = authed_clinician_client.get(f"/protocols/{_PROTO_ID}")
    assert resp.status_code == 200, resp.text
    integrity = resp.json().get("data_integrity")
    assert integrity is not None, "clinician fetch must include data_integrity"
    assert integrity["status"] == "ok"
    assert integrity["expected_region"] == "ankle"
    assert integrity["mismatches"] == []
    assert "ankle" in integrity["active_regions"]
    assert "ankle" in integrity["proposed_regions"]


# ---------------------------------------------------------------------------
# Mismatch: knee Wall Sit on an ankle patient
# ---------------------------------------------------------------------------


def test_data_integrity_flags_region_mismatch(authed_clinician_client, monkeypatch):
    """Ankle injury + active contains a knee Wall Sit -> region_mismatch
    with a structured entry the frontend can render."""
    _clear_narrator_cache()
    target = _build_target({
        "phase": "subacute", "week": 4,
        "exercises": [{"id": "ankle_calf_raises_double_leg", "sets": 2, "reps": 10}],
    })
    # Active payload has a knee exercise (wall_sit) — the kind of historical
    # inconsistency PR-F prevents on new drafts but didn't backfill.
    active = _build_active({
        "phase": "acute", "week": 1,
        "exercises": [{"id": "wall_sit", "sets": 3, "duration_s": 30}],
    })
    _stub_repo(monkeypatch, target, active)
    _stub_user(monkeypatch, token=_PATIENT_TOKEN, intake={
        "name": "Christian Reyes", "injury_type": "lateral ankle sprain",
    })
    _stub_anthropic(monkeypatch)
    monkeypatch.setattr("main.is_clinician", lambda uid: True)

    resp = authed_clinician_client.get(f"/protocols/{_PROTO_ID}")
    assert resp.status_code == 200, resp.text
    integrity = resp.json()["data_integrity"]
    assert integrity["status"] == "region_mismatch"
    assert integrity["expected_region"] == "ankle"
    # The knee Wall Sit is recorded as a mismatch on the active side.
    mismatches = integrity["mismatches"]
    assert len(mismatches) >= 1
    assert any(
        m["location"] == "active"
        and m["exercise_id"] == "wall_sit"
        and m["actual_region"] == "knee"
        for m in mismatches
    ), f"expected wall_sit/knee mismatch on active side; got {mismatches}"
    assert "knee" in integrity["active_regions"]


def test_data_integrity_flags_proposed_side_mismatch(authed_clinician_client, monkeypatch):
    """Mismatch on the proposed payload alone is also caught."""
    _clear_narrator_cache()
    target = _build_target({
        "phase": "subacute", "week": 4,
        "exercises": [{"id": "wall_sit", "sets": 3, "duration_s": 30}],
    })
    _stub_repo(monkeypatch, target, active=None)
    _stub_user(monkeypatch, token=_PATIENT_TOKEN, intake={
        "name": "Christian", "injury_type": "lateral ankle sprain",
    })
    _stub_anthropic(monkeypatch)
    monkeypatch.setattr("main.is_clinician", lambda uid: True)

    resp = authed_clinician_client.get(f"/protocols/{_PROTO_ID}")
    assert resp.status_code == 200, resp.text
    integrity = resp.json()["data_integrity"]
    assert integrity["status"] == "region_mismatch"
    locations = {m["location"] for m in integrity["mismatches"]}
    assert "proposed" in locations


# ---------------------------------------------------------------------------
# multi-region intake: never flag
# ---------------------------------------------------------------------------


def test_data_integrity_multi_region_never_flags(authed_clinician_client, monkeypatch):
    """When the patient's intake spans multiple regions (multi), per-exercise
    enforcement would over-block. status stays ok regardless of contents."""
    _clear_narrator_cache()
    # Mix knee + ankle exercises on a patient classified as multi.
    target = _build_target({
        "phase": "acute", "week": 1,
        "exercises": [
            {"id": "wall_sit", "sets": 1, "duration_s": 20},
            {"id": "ankle_alphabet", "sets": 1, "reps": 10},
        ],
    })
    _stub_repo(monkeypatch, target, active=None)

    import clinical_taxonomy
    # Force the deterministic resolver to multi for this test. The
    # intake string itself doesn't matter once the resolver returns multi.
    monkeypatch.setattr(clinical_taxonomy, "body_region",
                        lambda inj: "multi" if inj == "multi-region polytrauma" else None)

    _stub_user(monkeypatch, token=_PATIENT_TOKEN, intake={
        "name": "Test", "injury_type": "multi-region polytrauma",
    })
    _stub_anthropic(monkeypatch)
    monkeypatch.setattr("main.is_clinician", lambda uid: True)

    resp = authed_clinician_client.get(f"/protocols/{_PROTO_ID}")
    assert resp.status_code == 200, resp.text
    integrity = resp.json()["data_integrity"]
    assert integrity["status"] == "ok"
    assert integrity["expected_region"] == "multi"
    assert integrity["mismatches"] == []


# ---------------------------------------------------------------------------
# Unknown / missing intake: don't manufacture false positives
# ---------------------------------------------------------------------------


def test_data_integrity_missing_intake_stays_ok(authed_clinician_client, monkeypatch):
    """Intake missing entirely -> expected_region None -> status ok with
    no mismatches, regardless of what the protocol contains."""
    _clear_narrator_cache()
    target = _build_target({
        "phase": "acute", "week": 1,
        "exercises": [{"id": "wall_sit", "sets": 1, "duration_s": 20}],
    })
    _stub_repo(monkeypatch, target, active=None)
    _stub_user(monkeypatch, token=_PATIENT_TOKEN, intake=None)
    _stub_anthropic(monkeypatch)
    monkeypatch.setattr("main.is_clinician", lambda uid: True)

    resp = authed_clinician_client.get(f"/protocols/{_PROTO_ID}")
    assert resp.status_code == 200, resp.text
    integrity = resp.json()["data_integrity"]
    assert integrity["status"] == "ok"
    assert integrity["expected_region"] is None
    assert integrity["mismatches"] == []


# ---------------------------------------------------------------------------
# Patient self-fetch: field omitted entirely
# ---------------------------------------------------------------------------


def test_data_integrity_omitted_for_patient_self_fetch(
    authed_client, fake_user_id, monkeypatch,
):
    """Patient fetching their own protocol does NOT see data_integrity —
    it's a clinician-only diagnostic."""
    _clear_narrator_cache()
    target = _build_target({
        "phase": "acute", "week": 1,
        "exercises": [{"id": "wall_sit", "sets": 1, "duration_s": 20}],
    }, token=fake_user_id)
    _stub_repo(monkeypatch, target, active=None)
    _stub_user(monkeypatch, token=fake_user_id, intake={
        "name": "Self", "injury_type": "lateral ankle sprain",
    })
    monkeypatch.setattr("main.is_clinician", lambda uid: False)

    resp = authed_client.get(f"/protocols/{_PROTO_ID}")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert "data_integrity" not in body, (
        "patient self-fetch must not receive the clinician-only "
        "data_integrity field"
    )


def test_data_integrity_present_for_clinician(authed_clinician_client, monkeypatch):
    """Smoke test: the clinician fetch always carries the field, even on
    the happy path. (Pairs with the patient-self-fetch omission test.)"""
    _clear_narrator_cache()
    target = _build_target({"phase": "acute", "week": 1, "exercises": []})
    _stub_repo(monkeypatch, target, active=None)
    _stub_user(monkeypatch, token=_PATIENT_TOKEN, intake={
        "name": "Test", "injury_type": "lateral ankle sprain",
    })
    _stub_anthropic(monkeypatch)
    monkeypatch.setattr("main.is_clinician", lambda uid: True)

    resp = authed_clinician_client.get(f"/protocols/{_PROTO_ID}")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert "data_integrity" in body
    assert body["data_integrity"]["status"] == "ok"
