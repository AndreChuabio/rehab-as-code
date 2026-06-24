"""Guard the context_builder name + doctrine fixes.

Two regressions this locks down:

  1. The banned `protocol.patient` read (the "Christian" leak). The name must
     come from the threaded `display_name`, never the protocol payload.
  2. The retired "Cursor cloud agent opens a PR" behavior rule. The doctrine
     must describe the current clinician-review-gated trust loop instead.

Runs with ANTHROPIC_API_KEY unset so build_system_prompt takes the
deterministic _fallback_context path (no live Haiku call).
"""
from __future__ import annotations

import context_builder


_HEALTH = {
    "sleep_hours": 7.5,
    "sleep_score": 82,
    "hrv_ms": 58,
    "hrv_7day_avg": 60,
    "resting_hr": 54,
    "recovery_score": 71,
    "steps_yesterday": 4200,
}

_EVENTS = [{"time": "10:00", "title": "PT visit", "duration_min": 45, "type": "low"}]

# Poisoned protocol: protocol.patient is the BANNED drift-prone field. If the
# builder ever reads it, "Christian" leaks into Andre's session.
_POISONED = {
    "patient": "Christian",
    "phase": "subacute",
    "week": 4,
    "exercises": [
        {"name": "Quad Sets", "sets": 3, "reps": 10, "ROM_target_deg": 90},
    ],
    "body_region": "knee",
}


def _build(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    return context_builder.build_system_prompt(
        _HEALTH, _EVENTS, protocol=_POISONED, display_name="Andre",
    )


def test_display_name_used_not_protocol_patient(monkeypatch):
    result = _build(monkeypatch)
    system_prompt = result["system_prompt"]
    greeting = result["greeting"]

    # The banned field must never surface.
    assert "Christian" not in system_prompt
    assert "Christian" not in greeting

    # The threaded display name must be used in both surfaces.
    assert "Andre" in system_prompt
    assert "Andre" in greeting


def test_cursor_pr_doctrine_replaced_with_clinician_review(monkeypatch):
    result = _build(monkeypatch)
    system_prompt = result["system_prompt"]

    # The retired PR-bus doctrine must be gone.
    assert "Cursor" not in system_prompt
    assert "opens a PR" not in system_prompt

    # The current clinician-review trust loop must be present.
    assert "clinician" in system_prompt.lower()


def test_live_protocol_exercise_wired_into_context(monkeypatch):
    result = _build(monkeypatch)
    # The patient's actual protocol exercise should appear in the data block.
    assert "Quad Sets" in result["system_prompt"]
