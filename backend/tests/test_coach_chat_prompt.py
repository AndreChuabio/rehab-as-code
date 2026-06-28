"""Coach Maya system-prompt assertions for coach_chat.build_system_prompt.

coach_chat.build_system_prompt is the SHARED brain prompt for both the typed
/chat endpoint and the Tavus voice path (coach_chat.chat_stream calls it).
These tests pin the live-rep-counting behavior line (rule 10) and confirm the
prompt still builds cleanly with and without a recent set, since the live
rep-counting feature relies on this prompt reading sanely in both modes.

No network: build_system_prompt is a pure string builder.
"""
from __future__ import annotations

import coach_chat


HEALTH = {"hrv_ms": 60, "sleep_score": 80, "recovery_score": 75}
PROTOCOL = {
    "week": 2,
    "phase": "rehab",
    "exercises": [{"name": "Double-leg calf raise"}],
}


def test_live_rep_count_behavior_line_present():
    prompt = coach_chat.build_system_prompt(HEALTH, PROTOCOL, display_name="Andre")
    # The app speaks the count; Maya must not count herself.
    assert "spoken automatically" in prompt
    assert "do not try to count reps yourself" in prompt
    # Wording is conditional so it does not assume a set is in progress.
    assert "about to start" in prompt


def test_prompt_builds_without_recent_set():
    # The `if recent:` guard means no recent-set line when _recent_set absent.
    prompt = coach_chat.build_system_prompt(HEALTH, PROTOCOL, display_name="Andre")
    assert "Live set just finished" not in prompt
    # Behavior rules still render.
    assert "Behavior rules:" in prompt
    assert "10." in prompt


def test_prompt_builds_with_recent_set():
    protocol = {
        **PROTOCOL,
        "_recent_set": {
            "rep_count": 12,
            "exercise_name": "Double-leg calf raise",
            "worst_status": "good",
            "best_depth": 78,
            "warnings": [],
        },
    }
    prompt = coach_chat.build_system_prompt(protocol=protocol, health=HEALTH)
    assert "Live set just finished" in prompt
    assert "12 reps" in prompt
    # Behavior line still present alongside the recent-set acknowledgment.
    assert "do not try to count reps yourself" in prompt


def test_do_start_intent_rule_present():
    """Rule 3 must steer Maya to surface the actionable card (with the Start
    exercise launcher) when the patient signals do/start/begin intent, so she
    calls recommend_exercise instead of text-replying with no card."""
    prompt = coach_chat.build_system_prompt(HEALTH, PROTOCOL, display_name="Andre")
    assert "START" in prompt  # the DO / START / BEGIN steering
    assert "recommend_exercise" in prompt
    assert "actionable card" in prompt
    assert "Start exercise" in prompt
    # Never-invent fallback for an off-library spoken name is preserved.
    assert "never invent one" in prompt
    # Renumbering guard: the live-rep-count rule 10 still renders.
    assert "10." in prompt


def test_recommend_exercise_tool_description_covers_action_intent():
    """The recommend_exercise tool description (not just the prompt) must
    advertise do/start intent so gpt-4o-mini's tool selector calls it when the
    patient wants to begin an exercise."""
    rec = next(
        t for t in coach_chat.TOOLS
        if t["function"]["name"] == "recommend_exercise"
    )
    desc = rec["function"]["description"].lower()
    assert "start" in desc
    assert "let's do" in desc or "let" in desc
    assert "never invent" in desc
