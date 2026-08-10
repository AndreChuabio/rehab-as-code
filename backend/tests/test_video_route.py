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


# ---- shared region gate: the pre-route is a card-emitting path like the tools ----

def test_out_of_scope_protocol_row_is_gated():
    """A drifted/legacy protocol carrying an out-of-scope exercise must not turn
    into an actionable card just because it is 'the patient's own plan'. The
    pre-route shares _card_gate_reason with recommend_exercise +
    list_phase_exercises so no card path can drift out of the knee/ankle scope."""
    import exercise_kb

    off_scope = [
        e for e in exercise_kb._EXERCISES
        if (e.get("body_region") or "").lower() not in {"knee", "ankle"}
    ]
    assert off_scope, "fixture expects at least one out-of-scope library exercise"
    row = off_scope[0]

    protocol = {
        "body_region": row.get("body_region"),
        "exercises": [{"id": row["id"], "name": row.get("name")}],
    }
    message = f"pull up my {(row.get('name') or '').lower()} video"

    assert coach_chat._route_exercise_video(message, protocol) is None


def test_regionless_protocol_cannot_cross_regions():
    """With no body_region on the protocol, resolve_to_library keyword-searches
    all six regions. The gate still holds the card to in-scope regions."""
    protocol = {
        "exercises": [{"id": "ankle_alphabet", "name": "Ankle Alphabet"}],
    }
    hit = coach_chat._route_exercise_video("show me ankle alphabet", protocol)
    assert hit is not None
    assert (hit.get("body_region") or "").lower() in {"knee", "ankle"}
