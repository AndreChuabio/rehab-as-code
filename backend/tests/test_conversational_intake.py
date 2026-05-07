"""PR-K — conversational intake via Maya's start_intake_tool.

Covers two layers:

  1. agents.intake_agent.capture_intake_from_chat — the persistence helper.
     * mode="new" inserts a fresh intake_records payload
     * mode="update" deep-merges into the latest row's payload (does NOT
       overwrite keys the patient hasn't re-provided; lists union)
     * missing required keys are surfaced via fields_missing
     * validation failure raises IntakeCaptureError (no silent fallback)

  2. coach_chat._dispatch_tool ("start_intake_tool") — the routing layer.
     * forwards the tool args verbatim into capture_intake_from_chat
     * 401-equivalent path: missing user_token short-circuits with an
       error tool_result (the only auth boundary inside the chat dispatcher;
       /chat itself is auth-gated and covered by test_chat_trigger.py)
     * IntakeCaptureError surfaces as an error tool_result, not a fake
       success — Maya can then ask the patient again instead of pretending

The /chat endpoint's own 401 gating is exercised in
test_chat_trigger.test_chat_rejects_unauthenticated; we don't duplicate
that here.
"""
from __future__ import annotations

import asyncio


# ---------------------------------------------------------------------------
# Layer 1: capture_intake_from_chat (helper)
# ---------------------------------------------------------------------------


def _patch_user_store(monkeypatch, initial: dict | None = None):
    """In-memory stand-in for user_store.save_intake / get_intake."""
    state: dict[str, dict] = {}
    if initial is not None:
        state["row"] = dict(initial)

    import user_store

    def _save(token: str, intake: dict) -> None:
        # Mirror the real backend: save_intake is upsert per token; the row
        # under PRIMARY KEY token is replaced wholesale.
        state["row"] = dict(intake)

    def _get(token: str) -> dict | None:
        return dict(state["row"]) if "row" in state else None

    monkeypatch.setattr(user_store, "save_intake", _save)
    monkeypatch.setattr(user_store, "get_intake", _get)
    return state


def test_capture_new_mode_inserts_full_payload(monkeypatch):
    state = _patch_user_store(monkeypatch)
    from agents.intake_agent import capture_intake_from_chat

    result = capture_intake_from_chat(
        token="patient-uuid",
        fields={
            "injury_type": "lateral ankle sprain",
            "pain_level": 4,
            "symptoms": ["lateral pain", "stiffness on dorsiflexion"],
            "goals": ["return to running"],
            "surgery_date": "no surgery",
        },
        mode="new",
    )

    assert result["intake_id"] == "patient-uuid"
    assert result["mode"] == "new"
    assert set(result["fields_captured"]) >= {
        "injury_type", "pain_level", "symptoms", "goals", "surgery_date",
    }
    # All required keys present — nothing missing.
    assert result["fields_missing"] == []
    # Persisted payload matches what we sent.
    persisted = state["row"]
    assert persisted["injury_type"] == "lateral ankle sprain"
    assert persisted["pain_level"] == 4
    assert persisted["symptoms"] == ["lateral pain", "stiffness on dorsiflexion"]


def test_capture_new_mode_partial_reports_missing(monkeypatch):
    """Just enough to satisfy the chat tool's required={injury_type, mode}
    but not enough for a complete intake — fields_missing surfaces the gap."""
    _patch_user_store(monkeypatch)
    from agents.intake_agent import capture_intake_from_chat

    result = capture_intake_from_chat(
        token="patient-uuid",
        fields={
            "injury_type": "ankle sprain",
            "pain_level": 3,
        },
        mode="new",
    )

    assert "symptoms" in result["fields_missing"]
    assert "goals" in result["fields_missing"]
    assert "surgery_date" in result["fields_missing"]
    # injury_type and pain_level were captured, so they should NOT appear in missing
    assert "injury_type" not in result["fields_missing"]
    assert "pain_level" not in result["fields_missing"]


def test_capture_update_mode_preserves_other_fields(monkeypatch):
    """Patching the row should preserve fields the patient hasn't re-provided."""
    _patch_user_store(monkeypatch, initial={
        "injury_type": "ankle sprain",
        "pain_level": 6,
        "symptoms": ["lateral pain"],
        "goals": ["walk to the bus stop"],
        "surgery_date": "no surgery",
    })
    from agents.intake_agent import capture_intake_from_chat

    result = capture_intake_from_chat(
        token="patient-uuid",
        fields={
            "injury_type": "ankle sprain",  # required by schema
            "pain_level": 4,                # the only field the patient is updating
        },
        mode="update",
    )

    # Update reports only what was patched as captured.
    assert set(result["fields_captured"]) == {"injury_type", "pain_level"}
    # Nothing missing — the prior row already had everything.
    assert result["fields_missing"] == []

    # Importantly, the OTHER fields are still present in the persisted payload.
    import user_store
    row = user_store.get_intake("patient-uuid")
    assert row["pain_level"] == 4               # updated
    assert row["symptoms"] == ["lateral pain"]   # preserved
    assert row["goals"] == ["walk to the bus stop"]  # preserved
    assert row["surgery_date"] == "no surgery"  # preserved


def test_capture_update_unions_list_fields(monkeypatch):
    """When the patient adds a symptom mid-chat, the list should grow,
    not get replaced (otherwise 'and also stiffness' would clobber pain)."""
    _patch_user_store(monkeypatch, initial={
        "injury_type": "ankle sprain",
        "pain_level": 4,
        "symptoms": ["lateral pain"],
        "goals": ["return to running"],
        "surgery_date": "no surgery",
    })
    from agents.intake_agent import capture_intake_from_chat

    capture_intake_from_chat(
        token="patient-uuid",
        fields={
            "injury_type": "ankle sprain",
            "symptoms": ["stiffness on dorsiflexion"],
        },
        mode="update",
    )

    import user_store
    row = user_store.get_intake("patient-uuid")
    assert row["symptoms"] == ["lateral pain", "stiffness on dorsiflexion"]


def test_capture_invalid_pain_level_raises(monkeypatch):
    _patch_user_store(monkeypatch)
    from agents.intake_agent import IntakeCaptureError, capture_intake_from_chat

    import pytest
    with pytest.raises(IntakeCaptureError):
        capture_intake_from_chat(
            token="patient-uuid",
            fields={"injury_type": "ankle sprain", "pain_level": 42},
            mode="new",
        )


def test_capture_unsupported_mode_raises(monkeypatch):
    _patch_user_store(monkeypatch)
    from agents.intake_agent import IntakeCaptureError, capture_intake_from_chat

    import pytest
    with pytest.raises(IntakeCaptureError):
        capture_intake_from_chat(
            token="patient-uuid",
            fields={"injury_type": "ankle sprain"},
            mode="reset",  # type: ignore[arg-type]
        )


def test_capture_db_failure_surfaces(monkeypatch):
    """A save_intake exception should raise IntakeCaptureError so the
    chat dispatcher renders a tool_result error, not a fake success."""
    import user_store
    monkeypatch.setattr(user_store, "save_intake", lambda *_: (_ for _ in ()).throw(
        RuntimeError("postgres timeout")
    ))
    monkeypatch.setattr(user_store, "get_intake", lambda token: None)

    from agents.intake_agent import IntakeCaptureError, capture_intake_from_chat

    import pytest
    with pytest.raises(IntakeCaptureError) as exc_info:
        capture_intake_from_chat(
            token="patient-uuid",
            fields={"injury_type": "ankle sprain", "pain_level": 4},
            mode="new",
        )
    assert "postgres timeout" in str(exc_info.value)


# ---------------------------------------------------------------------------
# Layer 2: coach_chat._dispatch_tool routing
# ---------------------------------------------------------------------------


async def _noop_executor(flow, payload):
    return {}


def test_dispatch_start_intake_tool_forwards_args(monkeypatch):
    """The dispatcher should pass the tool args through to
    capture_intake_from_chat verbatim (mode stripped from `fields`)."""
    captured: dict = {}

    def _fake_capture(token, fields, mode):
        captured["token"] = token
        captured["fields"] = fields
        captured["mode"] = mode
        return {
            "intake_id": token,
            "fields_captured": sorted(fields.keys()),
            "fields_missing": [],
            "mode": mode,
        }

    import agents.intake_agent as ia
    monkeypatch.setattr(ia, "capture_intake_from_chat", _fake_capture)

    import coach_chat
    result, extras = asyncio.run(coach_chat._dispatch_tool(
        name="start_intake_tool",
        arguments={
            "injury_type": "lateral ankle sprain",
            "pain_level": 4,
            "symptoms": ["lateral pain"],
            "goals": ["return to running"],
            "surgery_date": "3 days ago",
            "mode": "new",
        },
        trigger_executor=_noop_executor,
        user_token="patient-uuid",
    ))

    # Auth boundary: the patient JWT-derived token reaches the helper.
    assert captured["token"] == "patient-uuid"
    # mode is split out from fields — it controls routing, not payload.
    assert captured["mode"] == "new"
    assert "mode" not in captured["fields"]
    assert captured["fields"]["injury_type"] == "lateral ankle sprain"
    assert captured["fields"]["pain_level"] == 4

    # Tool result reflects the helper's return + an ok flag.
    assert result["ok"] is True
    assert result["intake_id"] == "patient-uuid"
    assert result["fields_missing"] == []
    # And a tool_result event was emitted for the frontend.
    tool_results = [e for e in extras if e.get("type") == "tool_result"]
    assert len(tool_results) == 1
    assert tool_results[0]["name"] == "start_intake_tool"


def test_dispatch_start_intake_tool_rejects_missing_user_token():
    """No authenticated patient on the chat session -> the dispatcher
    refuses the call rather than writing intake under an empty token."""
    import coach_chat

    result, extras = asyncio.run(coach_chat._dispatch_tool(
        name="start_intake_tool",
        arguments={
            "injury_type": "lateral ankle sprain",
            "pain_level": 4,
            "mode": "new",
        },
        trigger_executor=_noop_executor,
        user_token=None,
    ))

    assert result["ok"] is False
    assert "authenticated" in result["error"].lower()
    # Error is also surfaced as a tool_result event so the frontend can render it.
    tool_results = [e for e in extras if e.get("type") == "tool_result"]
    assert len(tool_results) == 1
    assert tool_results[0]["result"]["ok"] is False


def test_dispatch_start_intake_tool_rejects_invalid_mode():
    """Mode outside the enum -> dispatcher returns an error tool_result
    without touching the helper (cheap guard before any DB write)."""
    import coach_chat

    result, _extras = asyncio.run(coach_chat._dispatch_tool(
        name="start_intake_tool",
        arguments={
            "injury_type": "ankle sprain",
            "mode": "delete",  # not in the enum
        },
        trigger_executor=_noop_executor,
        user_token="patient-uuid",
    ))

    assert result["ok"] is False
    assert "mode" in result["error"].lower()


def test_dispatch_start_intake_tool_surfaces_capture_error(monkeypatch):
    """When the helper raises IntakeCaptureError, the dispatcher emits
    an error tool_result (no silent success). Maya then sees ok=False
    and surfaces a friendly message instead of pretending intake was
    captured."""
    from agents.intake_agent import IntakeCaptureError

    def _boom(token, fields, mode):
        raise IntakeCaptureError("postgres timeout")

    import agents.intake_agent as ia
    monkeypatch.setattr(ia, "capture_intake_from_chat", _boom)

    import coach_chat
    result, extras = asyncio.run(coach_chat._dispatch_tool(
        name="start_intake_tool",
        arguments={
            "injury_type": "ankle sprain",
            "pain_level": 4,
            "mode": "new",
        },
        trigger_executor=_noop_executor,
        user_token="patient-uuid",
    ))

    assert result["ok"] is False
    assert "postgres timeout" in result["error"]
    tool_results = [e for e in extras if e.get("type") == "tool_result"]
    assert len(tool_results) == 1
    assert tool_results[0]["result"]["ok"] is False
