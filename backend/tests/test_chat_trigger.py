"""POST /chat trigger executor — pending-protocol write path + auth gate.

Replaces test_invoke_fallback.py, which pinned the dead PR-bus surface
(_invoke_with_fallback / cursor_sdk / cached_replay). Post-PR-#62 the chat
tools call chat_protocol_drafter.draft_and_save_pending, which writes a
`pending_review` row to the `protocols` table.

Coverage:
  1. Happy path: trigger executor invokes the drafter with the JWT-derived
     user_id and returns the persisted pending_protocol_id + summary.
  2. Drafter failure surfaces as an exception so coach_chat._dispatch_tool
     can render a tool_result error event (never a silent success).
  3. /chat itself rejects unauthenticated callers with 401, same as the
     other patient-scoped endpoints.
"""
from __future__ import annotations

import asyncio


def test_chat_trigger_executor_writes_pending_protocol(
    authed_client, fake_user_id, monkeypatch,
):
    """The factory-built executor should call the drafter with the JWT user
    and return the pending_protocol_id + summary verbatim."""
    import patient_context

    captured: dict = {}

    def _fake_drafter(token, flow, payload, prior_protocol):
        captured["token"] = token
        captured["flow"] = flow
        captured["payload"] = payload
        captured["prior_protocol"] = prior_protocol
        return {
            "pending_protocol_id": "draft-uuid-abc",
            "summary": "Regressed single-leg squats to step-ups while pain reduces.",
            "phase": "subacute",
            "week": 4,
        }

    monkeypatch.setattr(
        patient_context.chat_protocol_drafter, "draft_and_save_pending", _fake_drafter,
    )
    # Avoid hitting GitHub for the prior-protocol read; the executor only
    # needs *something* serializable to forward.
    monkeypatch.setattr(
        patient_context, "fetch_protocol_for_user",
        lambda _uid: {"patient": "Andre", "week": 3},
    )

    executor = patient_context._chat_trigger_executor_factory(fake_user_id)
    result = asyncio.run(executor(
        "symptom_adjustment",
        {"symptom_text": "knee felt tweaky on single-leg squats"},
    ))

    assert result["pending_protocol_id"] == "draft-uuid-abc"
    assert "single-leg squats" in result["summary"]
    assert result["phase"] == "subacute"
    assert result["week"] == 4
    assert result["flow"] == "symptom_adjustment"

    # Auth boundary: the drafter must be invoked with the JWT-derived
    # user_id, never a client-supplied token.
    assert captured["token"] == fake_user_id
    assert captured["flow"] == "symptom_adjustment"
    assert captured["payload"]["symptom_text"].startswith("knee")
    # Prior protocol forwarded verbatim so the model can edit-in-place.
    assert captured["prior_protocol"] == {"patient": "Andre", "week": 3}


def test_chat_trigger_executor_propagates_drafter_failure(
    authed_client, fake_user_id, monkeypatch,
):
    """Drafter raises -> executor raises. coach_chat catches and renders an
    error tool_result; nothing pretends success or silently swaps in a stub."""
    import patient_context
    from chat_protocol_drafter import ProtocolDraftError

    def _boom(token, flow, payload, prior_protocol):
        raise ProtocolDraftError("anthropic 503 from upstream")

    monkeypatch.setattr(
        patient_context.chat_protocol_drafter, "draft_and_save_pending", _boom,
    )
    monkeypatch.setattr(patient_context, "fetch_protocol_for_user", lambda _uid: None)

    executor = patient_context._chat_trigger_executor_factory(fake_user_id)

    import pytest
    with pytest.raises(ProtocolDraftError) as exc_info:
        asyncio.run(executor("checkin", {"checkin_text": "felt heavy today"}))
    assert "anthropic 503" in str(exc_info.value)


def test_chat_rejects_unauthenticated(unauthed_client):
    """The /chat endpoint itself must require a Bearer JWT; the trigger
    executor is only ever instantiated for an authenticated patient."""
    resp = unauthed_client.post(
        "/chat",
        json={"session_id": "default", "message": "hi", "history": []},
    )
    assert resp.status_code == 401, resp.text
