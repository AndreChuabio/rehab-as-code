"""Tests for chat_protocol_drafter.apply_swap (Coach Maya swap_exercise tool).

apply_swap is a deterministic, no-LLM payload edit: it replaces one exercise
in the active protocol with another, runs the same safety pass + tier
classifier the drafter uses, and either applies live (save_active_auto) or
queues for clinician review (save_pending). A swap whose target is out of the
library or out of the patient's body region is forced to gate.
"""
import asyncio

import chat_protocol_drafter as d

PRIOR = {
    "body_region": "knee",
    "exercises": [{"exercise_id": "knee_sl_squat", "name": "SL squat", "load": 20}],
}


def test_clean_swap_auto_applies(monkeypatch):
    monkeypatch.setattr(d, "_run_safety", lambda payload, region: [])
    monkeypatch.setattr(d, "_in_library_and_region", lambda eid, region: True)
    captured = {}
    monkeypatch.setattr(
        d.protocol_repo,
        "save_active_auto",
        lambda *a, **k: (captured.update(payload=a[1]) or "active-id"),
    )
    out = d.apply_swap(
        "tok", "knee_sl_squat", "knee_step_down", "prefers step-down", PRIOR
    )
    assert out["auto_applied"] is True
    assert out["protocol_id"] == "active-id"
    ids = [e["exercise_id"] for e in captured["payload"]["exercises"]]
    assert "knee_step_down" in ids and "knee_sl_squat" not in ids


def test_out_of_region_swap_gates(monkeypatch):
    monkeypatch.setattr(d, "_run_safety", lambda payload, region: [])
    # Target is in a different region -> target_ok is False -> gate.
    monkeypatch.setattr(
        d, "_in_library_and_region", lambda eid, region: eid != "ankle_circle"
    )
    monkeypatch.setattr(d.protocol_repo, "save_pending", lambda *a, **k: "pend-id")
    out = d.apply_swap("tok", "knee_sl_squat", "ankle_circle", "x", PRIOR)
    assert out["auto_applied"] is False
    assert out["protocol_id"] == "pend-id"


def test_high_severity_swap_routes_to_needs_clinician_review(monkeypatch):
    monkeypatch.setattr(
        d, "_run_safety", lambda payload, region: [{"severity": "high"}]
    )
    monkeypatch.setattr(d, "_in_library_and_region", lambda eid, region: True)
    captured = {}
    monkeypatch.setattr(
        d.protocol_repo,
        "save_pending",
        lambda *a, **k: (captured.update(status=k.get("status")) or "pend-id"),
    )
    out = d.apply_swap(
        "tok", "knee_sl_squat", "knee_step_down", "pain reported", PRIOR
    )
    assert out["auto_applied"] is False
    assert captured["status"] == "needs_clinician_review"


def test_in_library_and_region_matches_region(monkeypatch):
    monkeypatch.setattr(d.exercise_kb, "list_ids", lambda: ["knee_step_down"])
    monkeypatch.setattr(
        d.exercise_kb, "body_region_for", lambda eid: "knee"
    )
    assert d._in_library_and_region("knee_step_down", "knee") is True
    assert d._in_library_and_region("knee_step_down", "ankle") is False
    assert d._in_library_and_region("not_in_library", "knee") is False


def test_executor_routes_swap_flow_to_apply_swap(fake_user_id, monkeypatch):
    """The chat trigger executor must dispatch flow=='swap' to apply_swap with
    the JWT user id, never the LLM drafter, and surface the auto_applied flag."""
    import patient_context

    captured: dict = {}

    def _fake_apply_swap(token, from_id, to_id, reason, prior_protocol):
        captured.update(
            token=token, from_id=from_id, to_id=to_id,
            reason=reason, prior_protocol=prior_protocol,
        )
        return {
            "protocol_id": "swap-uuid",
            "auto_applied": True,
            "summary": "Swapped knee_sl_squat -> knee_step_down (prefers it).",
        }

    monkeypatch.setattr(
        patient_context.chat_protocol_drafter, "apply_swap", _fake_apply_swap,
    )
    monkeypatch.setattr(
        patient_context, "fetch_protocol_for_user",
        lambda _uid: {"body_region": "knee", "exercises": []},
    )

    executor = patient_context._chat_trigger_executor_factory(fake_user_id)
    result = asyncio.run(executor(
        "swap",
        {
            "from_exercise_id": "knee_sl_squat",
            "to_exercise_id": "knee_step_down",
            "reason": "prefers it",
        },
    ))

    assert result["protocol_id"] == "swap-uuid"
    assert result["pending_protocol_id"] == "swap-uuid"
    assert result["auto_applied"] is True
    assert result["flow"] == "swap"
    # Auth boundary: apply_swap is invoked with the JWT-derived user id.
    assert captured["token"] == fake_user_id
    assert captured["from_id"] == "knee_sl_squat"
    assert captured["to_id"] == "knee_step_down"


def test_dispatch_swap_exercise_emits_tool_result():
    """coach_chat._dispatch_tool routes swap_exercise through the executor and
    surfaces a tool_result event with the swap outcome."""
    import coach_chat

    async def _fake_executor(flow, payload):
        assert flow == "swap"
        assert payload == {
            "from_exercise_id": "knee_sl_squat",
            "to_exercise_id": "knee_step_down",
            "reason": "prefers step-down",
        }
        return {
            "protocol_id": "swap-uuid",
            "auto_applied": True,
            "summary": "Swapped knee_sl_squat -> knee_step_down (prefers step-down).",
        }

    result, extras = asyncio.run(coach_chat._dispatch_tool(
        "swap_exercise",
        {
            "from_exercise_id": "knee_sl_squat",
            "to_exercise_id": "knee_step_down",
            "reason": "prefers step-down",
        },
        trigger_executor=_fake_executor,
    ))

    assert result["ok"] is True
    assert result["auto_applied"] is True
    assert any(
        ev.get("type") == "tool_result" and ev.get("name") == "swap_exercise"
        for ev in extras
    )
