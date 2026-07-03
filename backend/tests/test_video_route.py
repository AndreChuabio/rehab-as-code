"""Deterministic exercise-video pre-route (coach_chat._route_exercise_video).

Regression coverage for the observed prod bug: "pull up my calf raises video"
was answered in text with a fabricated link and needed a second prompt before
the card appeared. The pre-route now resolves the exercise + emits the card
itself. These are pure-function tests (no OpenAI) that pin the intent gate, the
protocol-scoped resolution, and — critically — the false-positive guards so the
route never forces a card on an unrelated message.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import coach_chat  # noqa: E402

_PROTOCOL = {
    "body_region": "ankle",
    "exercises": [
        {"id": "ankle_calf_raises_double_leg", "name": "Double-Leg Calf Raises"},
        {"id": "ankle_alphabet", "name": "Ankle Alphabet"},
    ],
}


def test_pull_up_named_exercise_resolves_the_card():
    hit = coach_chat._route_exercise_video(
        "pull up my double-leg calf raises video so i can do the exercise",
        _PROTOCOL,
    )
    assert hit is not None
    assert hit["id"] == "ankle_calf_raises_double_leg"


def test_show_by_id_words_resolves():
    hit = coach_chat._route_exercise_video("can you show me ankle alphabet", _PROTOCOL)
    assert hit is not None
    assert hit["id"] == "ankle_alphabet"


def test_watch_verb_resolves():
    hit = coach_chat._route_exercise_video(
        "let me watch the double-leg calf raises", _PROTOCOL
    )
    assert hit is not None
    assert hit["id"] == "ankle_calf_raises_double_leg"


# ---- false-positive guards: the route must NOT force a card here ----

def test_no_retrieval_verb_returns_none():
    # "how do i do X" is a how-to (rule 3 / recommend_exercise), not a retrieval
    # request — the pre-route must stay out of it.
    assert coach_chat._route_exercise_video("how do i do my calf raises", _PROTOCOL) is None


def test_intent_verb_but_no_exercise_returns_none():
    # "show me my progress" has an intent verb but references no exercise.
    assert coach_chat._route_exercise_video("show me my progress", _PROTOCOL) is None


def test_intent_verb_but_off_protocol_exercise_returns_none():
    # references an exercise not in the patient's protocol -> don't fabricate.
    assert coach_chat._route_exercise_video("pull up the bench press video", _PROTOCOL) is None


def test_empty_and_no_protocol():
    assert coach_chat._route_exercise_video("", _PROTOCOL) is None
    assert coach_chat._route_exercise_video("pull up my calf raises video", {}) is None
    assert coach_chat._route_exercise_video("pull up my calf raises video", None) is None
