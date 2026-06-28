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


# ---------------------------------------------------------------------------
# recommend_exercise dispatch: actionable-card surfacing on do/start intent
# ---------------------------------------------------------------------------
#
# The launcher infra (card event -> renderExerciseCard -> Start exercise) is
# fully wired; the do/start fix is that recommend_exercise now ALSO fires on a
# "let's do X" intent. These tests pin the dispatch contract: a valid id emits
# a card, and a loosely-named exercise resolves to a real in-library card via
# the conservative resolver (never invents one).


def _noop_executor(*_args, **_kwargs):
    raise AssertionError("trigger executor must not be called for recommend_exercise")


def test_dispatch_recommend_exercise_valid_id_emits_card():
    import coach_chat

    result, extras = asyncio.run(coach_chat._dispatch_tool(
        name="recommend_exercise",
        arguments={"exercise_id": "ankle_alphabet"},
        trigger_executor=_noop_executor,
    ))

    assert result["ok"] is True
    assert result["exercise"]["id"] == "ankle_alphabet"
    cards = [e for e in extras if e.get("type") == "card"]
    assert len(cards) == 1
    assert cards[0]["card"]["id"] == "ankle_alphabet"


def test_dispatch_recommend_exercise_resolves_loose_name():
    """A do/start intent like 'seated ankle pumps' (not a library id) must
    resolve to a real in-library ankle card via resolve_to_library scoped to
    the patient's body_region, NOT fall through to an unknown-exercise error."""
    import coach_chat

    result, extras = asyncio.run(coach_chat._dispatch_tool(
        name="recommend_exercise",
        arguments={"exercise_id": "seated ankle pumps"},
        trigger_executor=_noop_executor,
        body_region="ankle",
    ))

    assert result.get("ok") is True
    assert "error" not in result
    cards = [e for e in extras if e.get("type") == "card"]
    assert len(cards) == 1
    # Resolved to a real ankle library entry, not invented.
    import exercise_kb
    assert cards[0]["card"]["id"] in exercise_kb.list_ids()
    assert (cards[0]["card"].get("body_region") or "").lower() == "ankle"


def test_dispatch_recommend_exercise_off_library_returns_no_card():
    """A true off-library name with no keyword match yields no card (the
    resolver is conservative). Maya's prompt then offers the closest option;
    she never invents an exercise."""
    import coach_chat

    result, extras = asyncio.run(coach_chat._dispatch_tool(
        name="recommend_exercise",
        arguments={"exercise_id": "xkcdz vbnmq"},
        trigger_executor=_noop_executor,
        body_region="ankle",
    ))

    assert result == {"error": "unknown exercise_id"}
    assert [e for e in extras if e.get("type") == "card"] == []


def test_dispatch_recommend_exercise_no_region_does_not_cross_regions():
    """Pre-intake / legacy patient with NO active protocol has body_region=None.
    A loose do/start name must NOT keyword-search the whole 6-region library and
    surface an out-of-scope, never-prescribed card. The fuzzy resolver is gated
    on a known region, so the loose name falls through to no card and Maya
    offers the closest in-scope option in text instead."""
    import coach_chat

    result, extras = asyncio.run(coach_chat._dispatch_tool(
        name="recommend_exercise",
        arguments={"exercise_id": "seated heel raises"},
        trigger_executor=_noop_executor,
        body_region=None,
    ))

    assert result == {"error": "unknown exercise_id"}
    assert [e for e in extras if e.get("type") == "card"] == []


def test_dispatch_recommend_exercise_out_of_scope_exact_id_dropped():
    """An exact-match library id from an out-of-scope region (e.g. shoulder)
    must NOT be surfaced as an actionable card even with no active protocol;
    only knee + ankle are in scope."""
    import coach_chat
    import exercise_kb

    shoulder_ids = [
        e["id"] for e in exercise_kb._EXERCISES
        if (e.get("body_region") or "").lower() == "shoulder"
    ]
    assert shoulder_ids, "fixture expects at least one shoulder exercise in the library"

    result, extras = asyncio.run(coach_chat._dispatch_tool(
        name="recommend_exercise",
        arguments={"exercise_id": shoulder_ids[0]},
        trigger_executor=_noop_executor,
        body_region=None,
    ))

    assert result == {"error": "out_of_scope_exercise"}
    assert [e for e in extras if e.get("type") == "card"] == []


def test_dispatch_recommend_exercise_out_of_region_for_active_protocol_dropped():
    """An in-scope but cross-region exact id (ankle exercise while the active
    protocol is a knee plan) must be dropped; the surfaced card has to match the
    patient's protocol region."""
    import coach_chat

    result, extras = asyncio.run(coach_chat._dispatch_tool(
        name="recommend_exercise",
        arguments={"exercise_id": "ankle_alphabet"},
        trigger_executor=_noop_executor,
        body_region="knee",
    ))

    assert result == {"error": "out_of_region_exercise"}
    assert [e for e in extras if e.get("type") == "card"] == []
