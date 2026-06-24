"""End-to-end coverage of the BYO-LLM Tavus proxy.

All hermetic under the sqlite test env: coach_chat.chat_stream is stubbed to a
deterministic async generator, the repo is a sys.modules fake, and every live
Supabase read on the proxy module is monkeypatched. No OpenAI / Anthropic /
DATABASE_URL is touched.

Covered:
  (a) missing / wrong secret -> 401
  (b) empty TAVUS_PROXY_SECRET env -> 503
  (c) happy path via conversation_id -> 200, OpenAI SSE shape, card dropped,
      first chunk delta.role='assistant', terminal data: [DONE]
  (d) session_ref fallback resolves the same patient
  (e) unresolvable patient -> 404, no patient data streamed
  (f) the messages handed to chat_stream carry no system role and no
      RAC_SESSION_REF string
"""
from __future__ import annotations

import json
import sys

import pytest
from fastapi.testclient import TestClient

import main


_FAKE_USER_ID = "11111111-1111-1111-1111-111111111111"


class _FakeRepo:
    """sys.modules stand-in for tavus_repo (the proxy does `import tavus_repo`)."""

    def __init__(self, by_conv=None, by_ref=None):
        self._by_conv = by_conv or {}
        self._by_ref = by_ref or {}

    def get_token_by_conversation_id(self, conversation_id):
        return self._by_conv.get(conversation_id)

    def get_token_by_session_ref(self, session_ref):
        return self._by_ref.get(session_ref)


def _install_fake_repo(monkeypatch, *, by_conv=None, by_ref=None):
    monkeypatch.setitem(
        sys.modules, "tavus_repo", _FakeRepo(by_conv=by_conv, by_ref=by_ref)
    )


def _stub_context(monkeypatch, captured):
    """Stub every live read on the proxy module + capture the brain inputs."""
    monkeypatch.setattr("api.tavus_proxy.ensure_user", lambda t: t)
    monkeypatch.setattr(
        "api.tavus_proxy.get_health_data", lambda user_token=None: {"hrv_ms": 60}
    )
    monkeypatch.setattr(
        "api.tavus_proxy.fetch_protocol_for_user",
        lambda t: {"phase": "subacute", "week": 4, "exercises": []},
    )
    monkeypatch.setattr(
        "api.tavus_proxy.get_last_set_completion", lambda t: None
    )
    monkeypatch.setattr("api.tavus_proxy.get_display_name", lambda t: "Andre")
    monkeypatch.setattr("api.tavus_proxy._last_pose_metrics", lambda t: None)
    monkeypatch.setattr(
        "api.tavus_proxy._chat_trigger_executor_factory", lambda t: ("exec", t)
    )
    monkeypatch.setattr(
        "api.tavus_proxy._clinician_attention_writer_factory",
        lambda t: ("writer", t),
    )

    async def _fake_chat_stream(**kwargs):
        captured["kwargs"] = kwargs
        # token, then a muted card, then more token, then done.
        for ev in [
            {"type": "token", "delta": "Hi "},
            {"type": "card", "card": {"id": "should-be-dropped"}},
            {"type": "token", "delta": "Andre"},
            {"type": "done"},
        ]:
            yield ev

    monkeypatch.setattr("coach_chat.chat_stream", _fake_chat_stream)


def _parse_sse(text):
    """Return the list of JSON chunk objects from an SSE body (excl. [DONE])."""
    chunks = []
    saw_done = False
    for line in text.splitlines():
        if not line.startswith("data: "):
            continue
        payload = line[len("data: "):]
        if payload.strip() == "[DONE]":
            saw_done = True
            continue
        chunks.append(json.loads(payload))
    return chunks, saw_done


@pytest.fixture
def client():
    return TestClient(main.app)


# ---------------------------------------------------------------------------
# (a) / (b) auth
# ---------------------------------------------------------------------------


def test_wrong_secret_returns_401(client, monkeypatch):
    monkeypatch.setenv("TAVUS_PROXY_SECRET", "s3cret")
    resp = client.post(
        "/tavus/llm/chat/completions",
        headers={"Authorization": "Bearer nope"},
        json={"messages": [{"role": "user", "content": "hi"}]},
    )
    assert resp.status_code == 401, resp.text


def test_missing_secret_header_returns_401(client, monkeypatch):
    monkeypatch.setenv("TAVUS_PROXY_SECRET", "s3cret")
    resp = client.post(
        "/tavus/llm/chat/completions",
        json={"messages": [{"role": "user", "content": "hi"}]},
    )
    assert resp.status_code == 401, resp.text


def test_empty_env_secret_returns_503(client, monkeypatch):
    monkeypatch.setenv("TAVUS_PROXY_SECRET", "")
    resp = client.post(
        "/tavus/llm/chat/completions",
        headers={"Authorization": "Bearer anything"},
        json={"messages": [{"role": "user", "content": "hi"}]},
    )
    assert resp.status_code == 503, resp.text


# ---------------------------------------------------------------------------
# (c) happy path via conversation_id
# ---------------------------------------------------------------------------


def test_happy_path_conversation_id(client, monkeypatch):
    monkeypatch.setenv("TAVUS_PROXY_SECRET", "s3cret")
    _install_fake_repo(monkeypatch, by_conv={"conv_1": _FAKE_USER_ID})
    captured: dict = {}
    _stub_context(monkeypatch, captured)

    resp = client.post(
        "/tavus/llm/chat/completions",
        headers={"Authorization": "Bearer s3cret"},
        json={
            "model": "gpt-4o-mini",
            "stream": True,
            "conversation_id": "conv_1",
            "messages": [
                {"role": "system", "content": "stale context"},
                {"role": "user", "content": "how is my knee"},
            ],
        },
    )
    assert resp.status_code == 200, resp.text
    assert resp.headers["content-type"].startswith("text/event-stream")

    chunks, saw_done = _parse_sse(resp.text)
    assert saw_done, "stream must terminate with data: [DONE]"

    # The card event is dropped; only the two token deltas reach TTS.
    content = "".join(
        c["choices"][0]["delta"].get("content", "") for c in chunks
    )
    assert content == "Hi Andre"

    # First chunk carries the assistant role per OpenAI convention.
    assert chunks[0]["choices"][0]["delta"].get("role") == "assistant"
    assert chunks[0]["object"] == "chat.completion.chunk"

    # The terminal chunk is a finish_reason=stop frame.
    assert chunks[-1]["choices"][0]["finish_reason"] == "stop"


# ---------------------------------------------------------------------------
# (d) session_ref fallback
# ---------------------------------------------------------------------------


def test_session_ref_fallback(client, monkeypatch):
    monkeypatch.setenv("TAVUS_PROXY_SECRET", "s3cret")
    _install_fake_repo(monkeypatch, by_ref={"ref_abc": _FAKE_USER_ID})
    captured: dict = {}
    _stub_context(monkeypatch, captured)

    resp = client.post(
        "/tavus/llm/chat/completions",
        headers={"Authorization": "Bearer s3cret"},
        json={
            "messages": [
                {"role": "system", "content": "context\n[RAC_SESSION_REF]: ref_abc\n"},
                {"role": "user", "content": "hello"},
            ],
        },
    )
    assert resp.status_code == 200, resp.text
    content = "".join(
        c["choices"][0]["delta"].get("content", "")
        for c in _parse_sse(resp.text)[0]
    )
    assert content == "Hi Andre"


# ---------------------------------------------------------------------------
# (d2) session_ref fallback when system content is structured parts
# ---------------------------------------------------------------------------


def test_session_ref_fallback_structured_content(client, monkeypatch):
    """Tavus may send conversational_context as list-typed content parts with
    no conversation_id forwarded. The sentinel must still be recovered so the
    avatar does not 404 on every spoken turn."""
    monkeypatch.setenv("TAVUS_PROXY_SECRET", "s3cret")
    _install_fake_repo(monkeypatch, by_ref={"ref_abc": _FAKE_USER_ID})
    captured: dict = {}
    _stub_context(monkeypatch, captured)

    resp = client.post(
        "/tavus/llm/chat/completions",
        headers={"Authorization": "Bearer s3cret"},
        json={
            # No conversation_id anywhere -> the embedded ref is the only key.
            "messages": [
                {
                    "role": "system",
                    "content": [
                        {"type": "text", "text": "doctrine block\n"},
                        {"type": "text", "text": "[RAC_SESSION_REF]: ref_abc\n"},
                    ],
                },
                {"role": "user", "content": "hello"},
            ],
        },
    )
    assert resp.status_code == 200, resp.text
    content = "".join(
        c["choices"][0]["delta"].get("content", "")
        for c in _parse_sse(resp.text)[0]
    )
    assert content == "Hi Andre"
    # Resolved to the correct patient via the structured-content ref.
    assert captured["kwargs"]["user_token"] == _FAKE_USER_ID


# ---------------------------------------------------------------------------
# (e) unresolvable patient
# ---------------------------------------------------------------------------


def test_unresolvable_patient_returns_404(client, monkeypatch):
    monkeypatch.setenv("TAVUS_PROXY_SECRET", "s3cret")
    _install_fake_repo(monkeypatch)  # empty maps
    captured: dict = {}
    _stub_context(monkeypatch, captured)

    resp = client.post(
        "/tavus/llm/chat/completions",
        headers={"Authorization": "Bearer s3cret"},
        json={
            "conversation_id": "unknown",
            "messages": [{"role": "user", "content": "hi"}],
        },
    )
    assert resp.status_code == 404, resp.text
    # The brain was never driven -> no patient data streamed.
    assert "kwargs" not in captured


# ---------------------------------------------------------------------------
# (f) system messages + session_ref never reach the brain
# ---------------------------------------------------------------------------


def test_system_messages_and_ref_stripped_before_brain(client, monkeypatch):
    monkeypatch.setenv("TAVUS_PROXY_SECRET", "s3cret")
    _install_fake_repo(monkeypatch, by_ref={"ref_abc": _FAKE_USER_ID})
    captured: dict = {}
    _stub_context(monkeypatch, captured)

    client.post(
        "/tavus/llm/chat/completions",
        headers={"Authorization": "Bearer s3cret"},
        json={
            "messages": [
                {"role": "system", "content": "doctrine\n[RAC_SESSION_REF]: ref_abc\n"},
                {"role": "user", "content": "my knee aches"},
                {"role": "assistant", "content": "noted"},
            ],
        },
    )

    passed = captured["kwargs"]["messages"]
    # No system role survived.
    assert all(m["role"] != "system" for m in passed)
    # The sentinel never reaches the model.
    serialized = json.dumps(passed)
    assert "RAC_SESSION_REF" not in serialized
    # user + assistant turns preserved in order.
    assert [m["role"] for m in passed] == ["user", "assistant"]
    # Recovered token is the patient, threaded through.
    assert captured["kwargs"]["user_token"] == _FAKE_USER_ID
    assert captured["kwargs"]["display_name"] == "Andre"
