"""Tests for diff_narrator.summarize() and the GET /protocols/{id}
narrator_summary integration.

Anthropic is mocked end-to-end — we never hit the real API. The goal of
these tests is the behavior contract:

  * happy path: returns the model's text
  * empty diff: returns None without calling the model
  * Anthropic error: returns None (caller falls back), no exception
  * endpoint shape: clinician sees narrator_summary, patient self-fetch does not
  * endpoint auth: 401 without bearer
"""
from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Any

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _FakeTextBlock:
    """Mimic anthropic.types.TextBlock just enough for diff_narrator."""

    def __init__(self, text: str) -> None:
        self.type = "text"
        self.text = text


class _FakeUsage:
    def __init__(self, in_tokens: int = 100, out_tokens: int = 50) -> None:
        self.input_tokens = in_tokens
        self.output_tokens = out_tokens


class _FakeResponse:
    def __init__(self, text: str) -> None:
        self.content = [_FakeTextBlock(text)]
        self.usage = _FakeUsage()


class _FakeAnthropicClient:
    """Stand-in for anthropic.Anthropic. Captures the last call args
    and either returns a stub response or raises a configured exception."""

    def __init__(self, response_text: str | None = None, raise_exc: Exception | None = None) -> None:
        self._response_text = response_text
        self._raise_exc = raise_exc
        self.last_kwargs: dict[str, Any] | None = None

        class _Messages:
            def __init__(inner) -> None:
                pass

            def create(inner, **kwargs):
                self.last_kwargs = kwargs
                if self._raise_exc is not None:
                    raise self._raise_exc
                return _FakeResponse(self._response_text or "")

        self.messages = _Messages()


def _stub_anthropic(monkeypatch, *, response_text: str | None = None, raise_exc: Exception | None = None) -> _FakeAnthropicClient:
    """Patch anthropic.Anthropic to return our fake. Each test gets a
    fresh client so call captures don't bleed across tests."""
    import anthropic

    fake = _FakeAnthropicClient(response_text=response_text, raise_exc=raise_exc)
    monkeypatch.setattr(anthropic, "Anthropic", lambda api_key=None: fake)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key-not-real")
    return fake


def _clear_narrator_cache() -> None:
    """Wipe the module-level cache so tests don't observe each other's
    narrations across runs."""
    import diff_narrator
    diff_narrator._CACHE.clear()


# ---------------------------------------------------------------------------
# diff_narrator.summarize() unit tests
# ---------------------------------------------------------------------------


def test_summarize_happy_path_returns_model_text(monkeypatch):
    _clear_narrator_cache()
    expected = (
        "Bumps stationary bike from 8 to 12 minutes and adds a yellow-band "
        "TKE progression. Justified by recovery score climbing 65 to 78 and "
        "pain-free completion last week."
    )
    fake = _stub_anthropic(monkeypatch, response_text=expected)

    import diff_narrator
    result = diff_narrator.summarize(
        active_payload={"week": 1, "exercises": [{"name": "bike", "sets": 1, "reps": 8}]},
        proposed_payload={"week": 2, "exercises": [{"name": "bike", "sets": 1, "reps": 12}]},
        intake_payload={"injury_type": "knee", "name": "Test Patient"},
        last_5_checkins=[{"pain_level": 2, "recorded_at": "2026-05-05"}],
        recent_sessions=[{"exercise_id": "bike", "status": "completed"}],
        active_id="active-1",
        proposed_id="proposed-1",
        protocol_id="proposed-1",
        clinician_id="clinician-uuid",
    )

    assert result == expected
    # Confirm we hit Haiku 4.5 with the right system prompt and a single
    # user message that includes both payloads.
    assert fake.last_kwargs is not None
    assert fake.last_kwargs["model"] == "claude-haiku-4-5-20251001"
    assert "physical therapists" in fake.last_kwargs["system"]
    user_msg = fake.last_kwargs["messages"][0]["content"]
    assert "ACTIVE PROTOCOL" in user_msg
    assert "PROPOSED PROTOCOL" in user_msg


def test_summarize_caches_by_id_pair(monkeypatch):
    _clear_narrator_cache()
    fake = _stub_anthropic(monkeypatch, response_text="cached narration")

    import diff_narrator
    args = dict(
        active_payload={"week": 1, "exercises": []},
        proposed_payload={"week": 2, "exercises": [{"name": "x", "sets": 1, "reps": 1}]},
        intake_payload=None,
        last_5_checkins=None,
        recent_sessions=None,
        active_id="A",
        proposed_id="B",
    )
    first = diff_narrator.summarize(**args)
    # Mutate fake to prove cache hit (would return different text if called)
    fake._response_text = "should-not-appear"
    second = diff_narrator.summarize(**args)

    assert first == "cached narration"
    assert second == "cached narration"


def test_summarize_returns_none_for_empty_diff(monkeypatch):
    _clear_narrator_cache()
    fake = _stub_anthropic(monkeypatch, response_text="should not be called")

    import diff_narrator
    # Identical payloads -> no diff -> no narration, no model call.
    payload = {"week": 1, "exercises": [{"name": "x", "sets": 1, "reps": 1}]}
    result = diff_narrator.summarize(
        active_payload=payload,
        proposed_payload=payload,
        intake_payload=None,
        last_5_checkins=None,
        recent_sessions=None,
        active_id="A",
        proposed_id="A",
    )
    assert result is None
    assert fake.last_kwargs is None


def test_summarize_returns_none_when_anthropic_raises(monkeypatch):
    _clear_narrator_cache()
    _stub_anthropic(monkeypatch, raise_exc=RuntimeError("boom"))

    import diff_narrator
    result = diff_narrator.summarize(
        active_payload={"week": 1},
        proposed_payload={"week": 2, "exercises": [{"name": "x", "sets": 1, "reps": 1}]},
        intake_payload=None,
        last_5_checkins=None,
        recent_sessions=None,
        active_id="A",
        proposed_id="B",
    )
    assert result is None


def test_summarize_returns_none_for_overlong_response(monkeypatch):
    _clear_narrator_cache()
    too_long = "x" * 600
    _stub_anthropic(monkeypatch, response_text=too_long)

    import diff_narrator
    result = diff_narrator.summarize(
        active_payload={"week": 1},
        proposed_payload={"week": 2, "exercises": [{"name": "x", "sets": 1, "reps": 1}]},
        intake_payload=None,
        last_5_checkins=None,
        recent_sessions=None,
        active_id="A",
        proposed_id="B",
    )
    assert result is None


def test_summarize_returns_none_when_api_key_missing(monkeypatch):
    _clear_narrator_cache()
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

    import diff_narrator
    result = diff_narrator.summarize(
        active_payload={"week": 1},
        proposed_payload={"week": 2, "exercises": [{"name": "x", "sets": 1, "reps": 1}]},
        intake_payload=None,
        last_5_checkins=None,
        recent_sessions=None,
        active_id="A",
        proposed_id="B",
    )
    assert result is None


# ---------------------------------------------------------------------------
# GET /protocols/{id} integration: narrator_summary visibility
# ---------------------------------------------------------------------------


_PATIENT_TOKEN = "11111111-1111-1111-1111-111111111111"
_CLINICIAN_ID = "22222222-2222-2222-2222-222222222222"
_PROTO_ID = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"


def _stub_protocol_repo(monkeypatch, *, target_token: str = _PATIENT_TOKEN) -> None:
    """Wire protocol_repo.get / get_active to return a deterministic
    pending vs active pair for the patient."""
    import protocol_repo

    target = {
        "id": _PROTO_ID,
        "token": target_token,
        "parent_id": None,
        "payload": {"week": 2, "phase": "subacute",
                    "exercises": [{"name": "bike", "sets": 1, "reps": 12}]},
        "status": "pending_review",
        "created_by_agent": "test",
        "created_at": datetime(2026, 5, 6, 12, 0, tzinfo=timezone.utc),
        "reviewed_by": None,
        "reviewed_at": None,
        "review_notes": None,
    }
    active = {
        "id": "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb",
        "token": target_token,
        "parent_id": None,
        "payload": {"week": 1, "phase": "acute",
                    "exercises": [{"name": "bike", "sets": 1, "reps": 8}]},
        "status": "active",
        "created_by_agent": "test",
        "created_at": datetime(2026, 4, 29, 12, 0, tzinfo=timezone.utc),
        "reviewed_by": None,
        "reviewed_at": None,
        "review_notes": None,
    }

    def _get(pid):
        return target if pid == _PROTO_ID else None

    def _get_active(token):
        return active if token == target_token else None

    monkeypatch.setattr(protocol_repo, "get", _get)
    monkeypatch.setattr(protocol_repo, "get_active", _get_active)


def _stub_user_store(monkeypatch, token: str = _PATIENT_TOKEN) -> None:
    import user_store

    def _load_user(t):
        if t != token:
            return None
        return {"patient_name": "Test Patient", "intake": {"injury_type": "knee"}}

    def _get_session_history(t, limit=10):
        return [
            {"kind": "checkin", "pain_level": 3, "recorded_at": "2026-05-04"},
            {"kind": "set_completion", "exercise_id": "bike", "completed_at": "2026-05-05"},
        ]

    monkeypatch.setattr(user_store, "load_user", _load_user)
    monkeypatch.setattr(user_store, "get_session_history", _get_session_history)


def test_protocol_detail_includes_narrator_for_clinician(
    authed_clinician_client, monkeypatch,
):
    _clear_narrator_cache()
    _stub_protocol_repo(monkeypatch)
    _stub_user_store(monkeypatch)
    _stub_anthropic(monkeypatch, response_text="Bike 8->12 min reflects pain trending down.")

    # The clinician fixture sets user_id to _FAKE_CLINICIAN_ID; we still
    # need is_clinician(user_id) to return True for the new endpoint
    # logic to include narrator_summary.
    monkeypatch.setattr("main.is_clinician", lambda uid: True)

    resp = authed_clinician_client.get(f"/protocols/{_PROTO_ID}")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert "narrator_summary" in body
    assert body["narrator_summary"] == "Bike 8->12 min reflects pain trending down."


def test_protocol_detail_omits_narrator_for_patient_self_fetch(
    authed_client, fake_user_id, monkeypatch,
):
    _clear_narrator_cache()
    # Patient self-fetch: the protocol's token == the JWT user_id.
    _stub_protocol_repo(monkeypatch, target_token=fake_user_id)
    _stub_user_store(monkeypatch, token=fake_user_id)
    # If summarize() were ever called, this would error — so this also
    # asserts we don't pay Haiku cost on patient self-fetch.
    _stub_anthropic(monkeypatch, raise_exc=AssertionError("must not call Haiku for patients"))
    monkeypatch.setattr("main.is_clinician", lambda uid: False)

    resp = authed_client.get(f"/protocols/{_PROTO_ID}")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert "narrator_summary" not in body, (
        "patient self-fetch should not include the AI-generated summary"
    )


def test_protocol_detail_rejects_unauthenticated(unauthed_client):
    resp = unauthed_client.get(f"/protocols/{_PROTO_ID}")
    assert resp.status_code == 401, resp.text


def test_protocol_detail_rejects_other_patient(authed_client, monkeypatch):
    """A patient cannot fetch another patient's protocol — only their own
    or via the clinician role."""
    _clear_narrator_cache()
    # The protocol belongs to a different token than the authed user.
    _stub_protocol_repo(monkeypatch, target_token="other-patient-token")
    _stub_user_store(monkeypatch, token="other-patient-token")
    monkeypatch.setattr("main.is_clinician", lambda uid: False)

    resp = authed_client.get(f"/protocols/{_PROTO_ID}")
    assert resp.status_code == 403, resp.text
