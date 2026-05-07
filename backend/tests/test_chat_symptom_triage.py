"""Phase F wire-in: coach_chat.chat_stream + symptom triage.

Tests cover the orchestration layer (NOT Anthropic; that's covered in
test_symptom_classifier). Both `agents.symptom_classifier.classify` and
the OpenAI chat completion are stubbed so we can assert behaviour
deterministically:

  * pain-keyword message -> classifier fires
  * non-pain message -> classifier does NOT fire
  * classifier output appears in the system prompt as [SYMPTOM_TRIAGE]
  * severity='clinician-attention' -> writer is called (writes a
    needs_clinician_review row in main.py); a tool_result event surfaces
  * severity='hold-load' -> NO writer call (no protocol row)
  * severity='minor' -> NO writer call
  * de-dup: same (session_id, message) within a session classifies once
  * classifier error -> Maya still answers without the triage block
    (no silent fake-minor fallback)
"""
from __future__ import annotations

import asyncio
from typing import Any

import pytest


# ---------------------------------------------------------------------------
# Fake OpenAI streaming response
# ---------------------------------------------------------------------------


class _FakeDelta:
    def __init__(self, content: str | None = None, tool_calls=None):
        self.content = content
        self.tool_calls = tool_calls


class _FakeChoice:
    def __init__(self, delta: _FakeDelta, finish_reason: str | None = None):
        self.delta = delta
        self.finish_reason = finish_reason


class _FakeChunk:
    def __init__(self, choices: list[_FakeChoice]):
        self.choices = choices


def _fake_stream(text: str):
    """Yield a stream that emits one text chunk and then finishes - no tools."""
    yield _FakeChunk([_FakeChoice(_FakeDelta(content=text))])
    yield _FakeChunk([_FakeChoice(_FakeDelta(content=None), finish_reason="stop")])


class _FakeCompletions:
    def __init__(self, captured_kwargs: dict):
        self._captured = captured_kwargs

    def create(self, **kwargs):
        # Capture every call so the test can assert on the system prompt.
        self._captured.setdefault("calls", []).append(kwargs)
        return _fake_stream("ok.")


class _FakeOpenAIClient:
    def __init__(self, captured_kwargs: dict):
        self.chat = type(
            "_Chat", (), {"completions": _FakeCompletions(captured_kwargs)},
        )()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _drain(agen) -> list[dict]:
    """Collect all events yielded by an async generator into a list."""
    async def _runner():
        out = []
        async for ev in agen:
            out.append(ev)
        return out
    return asyncio.run(_runner())


def _patch_openai(monkeypatch) -> dict:
    """Stub coach_chat._client so chat_stream uses a fake OpenAI client.
    Returns a dict that captures every kwargs passed to create()."""
    import coach_chat
    captured: dict[str, Any] = {}
    monkeypatch.setattr(
        coach_chat, "_client", lambda: _FakeOpenAIClient(captured),
    )
    return captured


def _patch_classifier(monkeypatch, return_value=None, raise_exc=None):
    """Stub agents.symptom_classifier.classify."""
    import agents.symptom_classifier as sc

    def _fake(*args, **kwargs):
        if raise_exc is not None:
            raise raise_exc
        return return_value

    monkeypatch.setattr(sc, "classify", _fake)
    return sc


def _reset_seen_dedup():
    """Each test gets a clean de-dup map; the dict is module-level state."""
    import coach_chat
    coach_chat._TRIAGE_SEEN.clear()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_pain_keyword_message_fires_classifier(monkeypatch):
    _reset_seen_dedup()
    captured = _patch_openai(monkeypatch)

    classifier_calls: list[dict] = []
    import agents.symptom_classifier as sc

    def _fake_classify(message, **kwargs):
        classifier_calls.append({"message": message, **kwargs})
        return {
            "severity": "minor",
            "reasoning": "mild soreness, no red flags.",
            "suggested_response": "Continue the plan, ice if needed.",
            "regression_exercise_id": None,
        }

    monkeypatch.setattr(sc, "classify", _fake_classify)

    import coach_chat

    async def _no_executor(*a, **kw):
        return {}

    events = _drain(coach_chat.chat_stream(
        messages=[{"role": "user", "content": "my knee hurts a bit today"}],
        health={"hrv_ms": 50},
        protocol={"phase": "subacute", "week": 4, "exercises": []},
        trigger_executor=_no_executor,
        user_token="patient-uuid",
        session_id="sess-1",
        last_pose_metrics=None,
        clinician_attention_writer=None,
    ))

    assert len(classifier_calls) == 1
    assert "knee" in classifier_calls[0]["message"]
    # The system prompt sent to OpenAI should contain the triage block.
    sys_prompt = captured["calls"][0]["messages"][0]["content"]
    assert "[SYMPTOM_TRIAGE]" in sys_prompt
    assert "severity: minor" in sys_prompt
    assert "[/SYMPTOM_TRIAGE]" in sys_prompt
    # Done event must appear; no error event.
    assert any(e.get("type") == "done" for e in events)
    assert not any(e.get("type") == "error" for e in events)


def test_non_pain_message_does_not_fire_classifier(monkeypatch):
    _reset_seen_dedup()
    captured = _patch_openai(monkeypatch)

    classifier_calls: list[dict] = []
    import agents.symptom_classifier as sc
    monkeypatch.setattr(
        sc, "classify",
        lambda *a, **k: classifier_calls.append({}) or {},
    )

    import coach_chat

    async def _no_executor(*a, **kw):
        return {}

    _drain(coach_chat.chat_stream(
        messages=[{"role": "user", "content": "what should I do for week 5?"}],
        health={},
        protocol={},
        trigger_executor=_no_executor,
        user_token="patient-uuid",
        session_id="sess-2",
    ))

    assert classifier_calls == []
    sys_prompt = captured["calls"][0]["messages"][0]["content"]
    assert "[SYMPTOM_TRIAGE]" not in sys_prompt


def test_clinician_attention_calls_writer_and_emits_tool_result(monkeypatch):
    _reset_seen_dedup()
    _patch_openai(monkeypatch)
    _patch_classifier(monkeypatch, return_value={
        "severity": "clinician-attention",
        "reasoning": "knee locking, possible meniscal injury.",
        "suggested_response": "I'm flagging this for your clinician now.",
        "regression_exercise_id": None,
    })

    writer_calls: list[dict] = []

    async def _writer(triage, message_text):
        writer_calls.append({"triage": triage, "message": message_text})
        return "pending-uuid-cliniclock-1"

    import coach_chat

    async def _no_executor(*a, **kw):
        return {}

    events = _drain(coach_chat.chat_stream(
        messages=[{"role": "user", "content": "my knee keeps locking and giving way"}],
        health={},
        protocol={"phase": "subacute", "week": 4},
        trigger_executor=_no_executor,
        user_token="patient-uuid",
        session_id="sess-3",
        clinician_attention_writer=_writer,
    ))

    assert len(writer_calls) == 1
    assert "locking" in writer_calls[0]["message"]
    assert writer_calls[0]["triage"]["severity"] == "clinician-attention"

    # tool_result event with name='symptom_triage' should appear before done.
    triage_events = [
        e for e in events
        if e.get("type") == "tool_result" and e.get("name") == "symptom_triage"
    ]
    assert len(triage_events) == 1
    assert triage_events[0]["result"]["severity"] == "clinician-attention"
    assert triage_events[0]["result"]["pending_protocol_id"] == "pending-uuid-cliniclock-1"

    # PR-H: a triage_alert event should also fire so the patient frontend
    # can render a system message ("Your message about a [keyword] was
    # flagged for your PT"). Severity is the only required field; phone
    # may be None (CLINIC_PHONE env not configured in tests).
    alert_events = [e for e in events if e.get("type") == "triage_alert"]
    assert len(alert_events) == 1
    assert alert_events[0]["severity"] == "clinician-attention"
    # symptom_keyword should match one of the regex hits in "knee keeps locking"
    assert alert_events[0]["symptom_keyword"] in {"locking", "giving way"}


def test_hold_load_does_not_call_writer(monkeypatch):
    """hold-load is steered via the prompt only - no protocols row written."""
    _reset_seen_dedup()
    _patch_openai(monkeypatch)
    _patch_classifier(monkeypatch, return_value={
        "severity": "hold-load",
        "reasoning": "pain 6/10 isolated to lateral knee.",
        "suggested_response": "Try step-ups instead today.",
        "regression_exercise_id": "step_up",
    })

    writer_calls: list[dict] = []

    async def _writer(triage, message_text):
        writer_calls.append({"triage": triage, "message": message_text})
        return "should-not-be-called"

    import coach_chat

    async def _no_executor(*a, **kw):
        return {}

    _drain(coach_chat.chat_stream(
        messages=[{"role": "user", "content": "single leg squats give me sharp pain today"}],
        health={},
        protocol={},
        trigger_executor=_no_executor,
        user_token="patient-uuid",
        session_id="sess-4",
        clinician_attention_writer=_writer,
    ))

    assert writer_calls == []


def test_hold_load_does_not_emit_triage_alert(monkeypatch):
    """PR-H: triage_alert is clinician-attention-only. hold-load steers
    via prompt only; no patient-facing system receipt needed."""
    _reset_seen_dedup()
    _patch_openai(monkeypatch)
    _patch_classifier(monkeypatch, return_value={
        "severity": "hold-load",
        "reasoning": "pain isolated.",
        "suggested_response": "Try the regression.",
        "regression_exercise_id": "step_up",
    })

    import coach_chat

    async def _no_executor(*a, **kw):
        return {}

    events = _drain(coach_chat.chat_stream(
        messages=[{"role": "user", "content": "single leg squats hurt today"}],
        health={},
        protocol={},
        trigger_executor=_no_executor,
        user_token="patient-uuid",
        session_id="sess-holdload-noalert",
    ))
    alert_events = [e for e in events if e.get("type") == "triage_alert"]
    assert alert_events == []


def test_minor_does_not_call_writer(monkeypatch):
    _reset_seen_dedup()
    _patch_openai(monkeypatch)
    _patch_classifier(monkeypatch, return_value={
        "severity": "minor",
        "reasoning": "mild soreness.",
        "suggested_response": "Continue the plan.",
        "regression_exercise_id": None,
    })

    writer_calls: list[dict] = []

    async def _writer(triage, message_text):
        writer_calls.append({})
        return "should-not"

    import coach_chat

    async def _no_executor(*a, **kw):
        return {}

    _drain(coach_chat.chat_stream(
        messages=[{"role": "user", "content": "I'm a bit sore today"}],
        health={},
        protocol={},
        trigger_executor=_no_executor,
        user_token="patient-uuid",
        session_id="sess-5",
        clinician_attention_writer=_writer,
    ))

    assert writer_calls == []


def test_dedup_within_session(monkeypatch):
    """Identical message within the same session classifies once."""
    _reset_seen_dedup()
    _patch_openai(monkeypatch)

    classifier_calls: list[int] = []
    import agents.symptom_classifier as sc

    def _fake_classify(message, **kwargs):
        classifier_calls.append(1)
        return {
            "severity": "minor",
            "reasoning": "mild.",
            "suggested_response": "ok",
            "regression_exercise_id": None,
        }

    monkeypatch.setattr(sc, "classify", _fake_classify)
    import coach_chat

    async def _no_executor(*a, **kw):
        return {}

    msg = "my knee hurts"
    for _ in range(3):
        _drain(coach_chat.chat_stream(
            messages=[{"role": "user", "content": msg}],
            health={},
            protocol={},
            trigger_executor=_no_executor,
            user_token="patient-uuid",
            session_id="sess-dedup",
        ))

    assert len(classifier_calls) == 1


def test_classifier_error_is_swallowed_no_silent_minor(monkeypatch):
    """When Haiku errors, Maya still answers, but the [SYMPTOM_TRIAGE] block
    is OMITTED. We do NOT silently downgrade to a fake 'minor' verdict."""
    _reset_seen_dedup()
    captured = _patch_openai(monkeypatch)

    from agents.symptom_classifier import SymptomClassifierError
    _patch_classifier(
        monkeypatch,
        raise_exc=SymptomClassifierError("anthropic 503"),
    )

    import coach_chat

    async def _no_executor(*a, **kw):
        return {}

    events = _drain(coach_chat.chat_stream(
        messages=[{"role": "user", "content": "my shoulder hurts"}],
        health={},
        protocol={},
        trigger_executor=_no_executor,
        user_token="patient-uuid",
        session_id="sess-err",
    ))

    sys_prompt = captured["calls"][0]["messages"][0]["content"]
    assert "[SYMPTOM_TRIAGE]" not in sys_prompt
    # Maya still returned a response - chat didn't 5xx the user.
    assert any(e.get("type") == "done" for e in events)


def test_clinician_attention_writer_failure_emits_error_tool_result(monkeypatch):
    """Writer raises -> tool_result event ok=False, no silent miss."""
    _reset_seen_dedup()
    _patch_openai(monkeypatch)
    _patch_classifier(monkeypatch, return_value={
        "severity": "clinician-attention",
        "reasoning": "red flag.",
        "suggested_response": "Flagging clinician.",
        "regression_exercise_id": None,
    })

    async def _writer(triage, message_text):
        raise RuntimeError("supabase down")

    import coach_chat

    async def _no_executor(*a, **kw):
        return {}

    events = _drain(coach_chat.chat_stream(
        messages=[{"role": "user", "content": "knee popping and giving way"}],
        health={},
        protocol={},
        trigger_executor=_no_executor,
        user_token="patient-uuid",
        session_id="sess-writerfail",
        clinician_attention_writer=_writer,
    ))

    triage_events = [
        e for e in events
        if e.get("type") == "tool_result" and e.get("name") == "symptom_triage"
    ]
    assert len(triage_events) == 1
    assert triage_events[0]["result"]["ok"] is False
    assert "supabase" in triage_events[0]["result"]["error"].lower()

    # PR-H: triage_alert STILL fires when the writer fails. The patient
    # deserves to know to call urgent care if symptoms are severe even if
    # the backend couldn't queue the row. Receipt-on-failure parity.
    alert_events = [e for e in events if e.get("type") == "triage_alert"]
    assert len(alert_events) == 1
    assert alert_events[0]["severity"] == "clinician-attention"
