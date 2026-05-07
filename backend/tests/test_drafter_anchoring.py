"""Tests for chat_protocol_drafter injury anchoring (the load-bearing fix
for the cross-region exercise bug Andre caught on 2026-05-06).

Three behaviors under test:

  1. The drafter's user prompt includes the patient's injury_type and
     resolved body_region so the LLM is anchored at the prompt level.
  2. The deterministic post-LLM validator raises CrossRegionExerciseError
     when the model proposes an exercise outside the patient's body_region.
     We never auto-substitute - the clinician (or a fresh draft) is the
     recovery path.
  3. The fetch_protocol_for_user empty-state path returns no exercises for
     authenticated patients with no active row (no YAML phantom).
"""
from __future__ import annotations

from typing import Any

import pytest


# --- Fake Anthropic plumbing -------------------------------------------------

class _FakeToolUseBlock:
    def __init__(self, name: str, input_data: dict[str, Any]) -> None:
        self.type = "tool_use"
        self.name = name
        self.input = input_data


class _FakeUsage:
    input_tokens = 100
    output_tokens = 40


class _FakeResponse:
    def __init__(self, blocks: list[Any]) -> None:
        self.content = blocks
        self.usage = _FakeUsage()


class _FakeClient:
    def __init__(self, blocks: list[Any]) -> None:
        self._blocks = blocks
        self.last_kwargs: dict[str, Any] | None = None

        class _Messages:
            def create(inner, **kwargs: Any) -> _FakeResponse:
                self.last_kwargs = kwargs
                return _FakeResponse(self._blocks)

        self.messages = _Messages()


def _stub_anthropic(monkeypatch: pytest.MonkeyPatch, blocks: list[Any]) -> _FakeClient:
    import anthropic
    fake = _FakeClient(blocks)
    monkeypatch.setattr(anthropic, "Anthropic", lambda api_key=None: fake)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key-not-real")
    return fake


def _propose_block(payload: dict[str, Any]) -> _FakeToolUseBlock:
    return _FakeToolUseBlock("propose_protocol", payload)


# --- Common ankle-patient intake ---------------------------------------------

ANKLE_INTAKE = {
    "name": "Test Ankle Patient",
    "age": 32,
    "injury_type": "lateral ankle sprain",
    "surgery_date": "2026-04-20",
    "pain_level": 4,
    "symptoms": ["swelling", "limited dorsiflexion"],
    "goals": ["return to running"],
}


def _setup_drafter_env(
    monkeypatch: pytest.MonkeyPatch,
    intake: dict[str, Any],
) -> None:
    """Stub user_store + protocol_repo + anthropic for a drafter run."""
    import user_store

    monkeypatch.setattr(user_store, "get_intake", lambda token: intake)
    monkeypatch.setattr(user_store, "get_display_name", lambda token: intake["name"])

    def _save_pending(token, payload, created_by_agent=None):
        return "pending-id-stub"

    import protocol_repo
    monkeypatch.setattr(protocol_repo, "save_pending", _save_pending)


# --- Tests -------------------------------------------------------------------

def test_drafter_user_prompt_includes_injury_anchoring(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The model receives injury_type + body_region as a HARD constraint
    in its user prompt. This is the prompt-level safety layer."""
    _setup_drafter_env(monkeypatch, ANKLE_INTAKE)
    fake = _stub_anthropic(monkeypatch, [_propose_block({
        "summary": "Maintain current ankle protocol.",
        "patient": "Test Ankle Patient",
        "phase": "subacute",
        "week": 2,
        "exercises": [
            {
                "name": "ankle_alphabet",
                "sets": 3,
                "reps": 26,
                "load": "bodyweight",
            },
        ],
    })])

    import chat_protocol_drafter as drafter
    drafter.draft_and_save_pending(
        token="test-token", flow="checkin", payload={},
    )

    user_msg = fake.last_kwargs["messages"][0]["content"]  # type: ignore[index]
    assert "lateral ankle sprain" in user_msg
    assert "body_region" in user_msg
    assert "ankle" in user_msg


def test_drafter_rejects_cross_region_exercises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Deterministic validator: model proposes a wrist exercise for an ankle
    patient -> raises CrossRegionExerciseError. No silent auto-substitution."""
    _setup_drafter_env(monkeypatch, ANKLE_INTAKE)
    _stub_anthropic(monkeypatch, [_propose_block({
        "summary": "Ankle protocol with stray wrist work.",
        "patient": "Test Ankle Patient",
        "phase": "subacute",
        "week": 2,
        "exercises": [
            {"name": "ankle_alphabet", "sets": 3, "reps": 26, "load": "bodyweight"},
            # Cross-region: this is an elbow exercise.
            {"name": "elbow_eccentric_wrist_extension", "sets": 3, "reps": 15, "load": "1lb"},
        ],
    })])

    import chat_protocol_drafter as drafter
    with pytest.raises(drafter.CrossRegionExerciseError) as exc_info:
        drafter.draft_and_save_pending(
            token="test-token", flow="checkin", payload={},
        )
    detail = str(exc_info.value)
    assert "ankle" in detail
    assert "elbow_eccentric_wrist_extension" in detail


def test_drafter_allows_clinician_review_required_refusal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The sentinel `clinician_review_required` exercise is the explicit
    refusal path; the validator must not flag it as cross-region."""
    _setup_drafter_env(monkeypatch, ANKLE_INTAKE)
    _stub_anthropic(monkeypatch, [_propose_block({
        "summary": "No appropriate ankle exercise for this phase; flag.",
        "patient": "Test Ankle Patient",
        "phase": "acute",
        "week": 1,
        "exercises": [
            {"name": "clinician_review_required", "sets": 0, "reps": 0},
        ],
    })])

    import chat_protocol_drafter as drafter
    out = drafter.draft_and_save_pending(
        token="test-token", flow="checkin", payload={},
    )
    assert out["pending_protocol_id"] == "pending-id-stub"


def test_drafter_passes_for_matched_region(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Happy path: ankle patient gets ankle exercises; validator stays quiet."""
    _setup_drafter_env(monkeypatch, ANKLE_INTAKE)
    _stub_anthropic(monkeypatch, [_propose_block({
        "summary": "Continued band-resisted ankle work this week.",
        "patient": "Test Ankle Patient",
        "phase": "subacute",
        "week": 2,
        "exercises": [
            {"name": "ankle_dorsiflexion_band", "sets": 3, "reps": 15, "load": "yellow band"},
            {"name": "ankle_eversion_band", "sets": 3, "reps": 15, "load": "yellow band"},
            {"name": "ankle_calf_raises_double_leg", "sets": 3, "reps": 15, "load": "bodyweight"},
        ],
    })])

    import chat_protocol_drafter as drafter
    out = drafter.draft_and_save_pending(
        token="test-token", flow="checkin", payload={},
    )
    assert out["pending_protocol_id"] == "pending-id-stub"
    assert out["phase"] == "subacute"


def test_drafter_does_not_enforce_when_intake_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No intake -> no body_region -> validator is skipped (not loud).

    The drafter has no anchor to enforce against; the safer fallback is
    let the draft through and let the clinician spot-check (consistent
    with how the orchestrator handles unresolved injuries already)."""
    _setup_drafter_env(monkeypatch, intake=None)  # type: ignore[arg-type]
    _stub_anthropic(monkeypatch, [_propose_block({
        "summary": "Generic plan; intake not yet captured.",
        "patient": "Test Patient",
        "phase": "acute",
        "week": 1,
        "exercises": [
            {"name": "wall_sit", "sets": 3, "reps": 1, "load": "30s hold"},
        ],
    })])

    import chat_protocol_drafter as drafter
    out = drafter.draft_and_save_pending(
        token="test-token", flow="checkin", payload={},
    )
    assert out["pending_protocol_id"] == "pending-id-stub"


# --- protocol_loader empty-state for authenticated patients ------------------

def test_fetch_protocol_for_user_returns_empty_state_no_active_row(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An authenticated patient with no active row gets an explicit
    pending_intake empty payload - NOT the legacy single-tenant YAML.

    This is the second layer of defense for the bug Andre caught: even if
    the drafter tried to inherit a phantom protocol, the loader now refuses
    to surface one for an authenticated patient with no Supabase row."""
    import protocol_loader

    monkeypatch.setenv("PROTOCOL_SOURCE", "supabase")
    monkeypatch.setattr(
        protocol_loader, "_fetch_active_from_supabase", lambda token: None
    )

    out = protocol_loader.fetch_protocol_for_user("auth-user-without-row")
    assert out == {
        "patient": None,
        "phase": "pending_intake",
        "week": 0,
        "exercises": [],
    }
