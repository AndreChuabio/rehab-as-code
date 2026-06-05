"""Tests for agents.planner.compose().

Anthropic mocked end-to-end. Behavior contract:
  * happy path: combines candidates + signal into a save_pending payload
  * concerns retry: prompt forwards safety concerns to the model
  * Anthropic error: raises PlannerError
  * malformed output: raises PlannerError
  * payload shape matches what protocol_repo.save_pending expects
"""
from __future__ import annotations

from typing import Any

import pytest


class _FakeToolUseBlock:
    def __init__(self, name: str, input_data: dict[str, Any]) -> None:
        self.type = "tool_use"
        self.name = name
        self.input = input_data


class _FakeUsage:
    def __init__(self) -> None:
        self.input_tokens = 200
        self.output_tokens = 80


class _FakeResponse:
    def __init__(self, blocks: list[Any]) -> None:
        self.content = blocks
        self.usage = _FakeUsage()


class _FakeClient:
    def __init__(
        self,
        response_blocks: list[Any] | None = None,
        raise_exc: Exception | None = None,
    ) -> None:
        self._response_blocks = response_blocks or []
        self._raise_exc = raise_exc
        self.last_kwargs: dict[str, Any] | None = None

        class _Messages:
            def create(inner, **kwargs):
                self.last_kwargs = kwargs
                if self._raise_exc is not None:
                    raise self._raise_exc
                return _FakeResponse(self._response_blocks)

        self.messages = _Messages()


def _stub_anthropic(monkeypatch, **kwargs) -> _FakeClient:
    import anthropic
    fake = _FakeClient(**kwargs)
    monkeypatch.setattr(anthropic, "Anthropic", lambda api_key=None: fake)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key-not-real")
    return fake


def _draft_block(payload: dict) -> _FakeToolUseBlock:
    return _FakeToolUseBlock("compose_protocol", payload)


SAMPLE_DRAFT = {
    "patient": "Test Patient",
    "phase": "subacute",
    "week": 4,
    "session_targets": {"frequency_per_week": 4, "duration_min": 35},
    "exercises": [
        {
            "name": "mini_squats",
            "sets": 3,
            "reps": 12,
            "load": "bodyweight",
            "progression_criteria": "pain-free 3 sessions in a row",
            "regression_criteria": "any sharp pain",
            "references": ["protocols/protocol-library/knee/post-acl-week-4.yaml"],
        },
        {
            "name": "single_leg_balance",
            "sets": 3,
            "reps": 30,
            "load": "bodyweight",
            "references": [
                "protocols/protocol-library/knee/post-acl-week-4.yaml",
            ],
        },
    ],
}


def test_compose_happy_path_returns_save_pending_payload(monkeypatch):
    fake = _stub_anthropic(monkeypatch, response_blocks=[_draft_block(SAMPLE_DRAFT)])

    from agents.planner import compose
    out = compose(
        candidates=[
            {"exercise_id": "mini_squats", "rationale": "closed-chain loading"},
            {"exercise_id": "single_leg_balance", "rationale": "neuromuscular"},
        ],
        signal={"decision": "progress", "reasons": ["pain stable"], "confidence": 0.85},
        intake={"injury_type": "knee", "name": "Test Patient"},
        phase="subacute",
        week=4,
        token="t",
    )

    # Payload matches protocol_repo.save_pending shape exactly.
    assert out["patient"] == "Test Patient"
    assert out["phase"] == "subacute"
    assert out["week"] == 4
    assert isinstance(out["exercises"], list) and len(out["exercises"]) == 2
    for ex in out["exercises"]:
        # Each exercise has the required references field (auto-filled
        # by _normalize_exercise if the model omits it).
        assert "references" in ex and ex["references"]
    assert out["session_targets"]["frequency_per_week"] == 4

    # Sonnet 4.6 with the right tool gate.
    assert fake.last_kwargs["model"] == "claude-sonnet-4-6"
    assert fake.last_kwargs["tool_choice"]["name"] == "compose_protocol"


def test_compose_with_concerns_passes_them_into_prompt(monkeypatch):
    fake = _stub_anthropic(monkeypatch, response_blocks=[_draft_block(SAMPLE_DRAFT)])

    from agents.planner import compose
    concerns = [
        {"check": "pain_ceiling", "severity": "med", "detail": "load too high for week 4"},
    ]
    compose(
        candidates=[],
        signal={"decision": "hold", "reasons": [], "confidence": 0.5},
        intake={"name": "P"},
        phase="subacute",
        week=4,
        concerns=concerns,
        token="t",
    )

    user_msg = fake.last_kwargs["messages"][0]["content"]
    assert "Safety concerns" in user_msg
    assert "pain_ceiling" in user_msg


def test_compose_normalizes_missing_references(monkeypatch):
    """If the model omits `references` on an exercise, the planner
    synthesizes the auto-generated one so save_pending validation
    passes (mirrors chat_protocol_drafter behavior)."""
    draft = {
        "patient": "P",
        "phase": "acute",
        "week": 1,
        "exercises": [
            {"name": "ex_a", "sets": 1, "reps": 5},  # no references
        ],
    }
    _stub_anthropic(monkeypatch, response_blocks=[_draft_block(draft)])
    from agents.planner import compose
    out = compose(
        candidates=[],
        signal={"decision": "hold", "reasons": [], "confidence": 0.5},
        intake=None,
        phase="acute",
        week=1,
    )
    assert out["exercises"][0]["references"] == ["protocol-library/auto-generated.yaml"]


def test_compose_raises_on_anthropic_error(monkeypatch):
    _stub_anthropic(monkeypatch, raise_exc=RuntimeError("502"))
    from agents.planner import PlannerError, compose
    with pytest.raises(PlannerError):
        compose(
            candidates=[],
            signal={"decision": "hold", "reasons": [], "confidence": 0.5},
            intake=None,
            phase="acute",
            week=1,
        )


def test_compose_raises_on_invalid_payload(monkeypatch):
    """Tool-use schema is enforced by Anthropic, but a missing required
    field still surfaces as PlannerError rather than crashing in psycopg
    later."""
    bad = {"phase": "acute", "week": 1, "exercises": [{"name": "x", "sets": 1, "reps": 1}]}
    _stub_anthropic(monkeypatch, response_blocks=[_draft_block(bad)])
    from agents.planner import PlannerError, compose
    with pytest.raises(PlannerError):
        compose(
            candidates=[],
            signal={"decision": "hold", "reasons": [], "confidence": 0.5},
            intake=None,
            phase="acute",
            week=1,
        )


def test_compose_raises_on_empty_exercises(monkeypatch):
    bad = {"patient": "P", "phase": "acute", "week": 1, "exercises": []}
    _stub_anthropic(monkeypatch, response_blocks=[_draft_block(bad)])
    from agents.planner import PlannerError, compose
    with pytest.raises(PlannerError):
        compose(
            candidates=[],
            signal={"decision": "hold", "reasons": [], "confidence": 0.5},
            intake=None,
            phase="acute",
            week=1,
        )


def test_compose_raises_when_api_key_missing(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    from agents.planner import PlannerError, compose
    with pytest.raises(PlannerError):
        compose(
            candidates=[],
            signal={"decision": "hold", "reasons": [], "confidence": 0.5},
            intake=None,
            phase="acute",
            week=1,
        )


# --- Payer-aware goals -------------------------------------------------------

def _draft_with_goals(goals: list[dict]) -> dict:
    return {**SAMPLE_DRAFT, "goals": goals}


def test_compose_emits_payer_aware_goals_cash(monkeypatch):
    """Cash patient: goals carry payer_mode=cash and a load-mgmt/performance
    tied_to bucket, and survive into the saved payload."""
    draft = _draft_with_goals([
        {
            "text": "Return to pain-free recreational cycling — 25 of 30 miles",
            "measurable_target": "25 miles, pain <=2/10",
            "tied_to": "performance",
            "payer_mode": "cash",
            "references": ["protocols/protocol-library/knee/post-acl-week-4.yaml"],
        },
    ])
    _stub_anthropic(monkeypatch, response_blocks=[_draft_block(draft)])
    from agents.planner import compose
    out = compose(
        candidates=[{"exercise_id": "mini_squats"}],
        signal={"decision": "progress", "reasons": [], "confidence": 0.8},
        intake={"injury_type": "knee", "name": "Test Patient", "payer_model": "cash"},
        phase="subacute",
        week=4,
        token="t",
    )
    assert len(out["goals"]) == 1
    g = out["goals"][0]
    assert g["payer_mode"] == "cash"
    assert g["tied_to"] in ("performance", "load_mgmt")
    assert g["text"]


def test_compose_goal_mode_conflation_is_coerced(monkeypatch):
    """BLOCKER guard: a cash patient whose goal arrives tagged with an
    insurance bucket (adl) or payer_mode is deterministically forced back to
    the resolved cash mode — the LLM's self-report is never trusted."""
    draft = _draft_with_goals([
        {
            "text": "Independent stair negotiation without rail",  # insurance-y text
            "measurable_target": "full flight",
            "tied_to": "adl",          # insurance bucket
            "payer_mode": "insurance",  # wrong — patient is cash
            "references": ["protocols/protocol-library/knee/post-acl-week-4.yaml"],
        },
    ])
    _stub_anthropic(monkeypatch, response_blocks=[_draft_block(draft)])
    from agents.planner import compose
    out = compose(
        candidates=[{"exercise_id": "mini_squats"}],
        signal={"decision": "hold", "reasons": [], "confidence": 0.5},
        intake={"injury_type": "knee", "name": "Test Patient", "payer_model": "cash"},
        phase="subacute",
        week=4,
        token="t",
    )
    g = out["goals"][0]
    assert g["payer_mode"] == "cash"          # forced to resolved mode
    assert g["tied_to"] in ("performance", "load_mgmt")  # coerced out of adl


def test_compose_insurance_mode_in_prompt_and_buckets(monkeypatch):
    """Insurance patient: the user prompt names the payer model and goals are
    constrained to the adl/fall_risk buckets."""
    draft = _draft_with_goals([
        {
            "text": "Achieve 110 deg knee flexion for independent stairs",
            "measurable_target": "110 degrees",
            "tied_to": "performance",  # cash bucket — should be coerced to adl/fall_risk
            "payer_mode": "cash",
            "references": ["protocols/protocol-library/knee/post-acl-week-4.yaml"],
        },
    ])
    fake = _stub_anthropic(monkeypatch, response_blocks=[_draft_block(draft)])
    from agents.planner import compose
    out = compose(
        candidates=[{"exercise_id": "mini_squats"}],
        signal={"decision": "progress", "reasons": [], "confidence": 0.8},
        intake={"injury_type": "knee", "name": "P", "payer_model": "insurance"},
        phase="subacute",
        week=4,
        token="t",
    )
    user_msg = fake.last_kwargs["messages"][0]["content"]
    assert "Payer model" in user_msg and "insurance" in user_msg
    g = out["goals"][0]
    assert g["payer_mode"] == "insurance"
    assert g["tied_to"] in ("adl", "fall_risk")


def test_compose_flags_register_mismatch_and_coercion(monkeypatch):
    """Review fixes: an insurance goal whose TEXT uses performance vocab is
    flagged text_register_warning + needs_clinician_review; an out-of-mode
    tied_to is coerced AND flagged tied_to_coerced (not silently buried)."""
    draft = _draft_with_goals([
        {
            "text": "Ride 25 miles pain-free by week 8",  # performance vocab under insurance
            "measurable_target": "25 miles",
            "tied_to": "performance",  # cash bucket -> must coerce under insurance
            "payer_mode": "insurance",
            "references": ["protocols/protocol-library/knee/post-acl-week-4.yaml"],
        },
    ])
    _stub_anthropic(monkeypatch, response_blocks=[_draft_block(draft)])
    from agents.planner import compose
    out = compose(
        candidates=[{"exercise_id": "mini_squats"}],
        signal={"decision": "hold", "reasons": [], "confidence": 0.5},
        intake={"injury_type": "knee", "name": "P", "payer_model": "insurance"},
        phase="subacute",
        week=4,
        token="t",
    )
    g = out["goals"][0]
    assert g["tied_to"] in ("adl", "fall_risk")
    assert g["tied_to_coerced"] is True
    assert g["text_register_warning"] is True
    assert g["needs_clinician_review"] is True


def test_compose_flags_missing_citation_without_fabricating(monkeypatch):
    """Review fix 7: a goal with no references gets an empty list + a
    citation_missing flag — never a fabricated 'auto-generated.yaml' path."""
    draft = _draft_with_goals([
        {"text": "Ride 25 miles", "tied_to": "load_mgmt", "payer_mode": "cash"},
    ])
    _stub_anthropic(monkeypatch, response_blocks=[_draft_block(draft)])
    from agents.planner import compose
    out = compose(
        candidates=[{"exercise_id": "mini_squats"}],
        signal={"decision": "hold", "reasons": [], "confidence": 0.5},
        intake={"injury_type": "knee", "name": "P", "payer_model": "cash"},
        phase="subacute",
        week=4,
        token="t",
    )
    g = out["goals"][0]
    assert g["references"] == []
    assert g["citation_missing"] is True


def test_compose_defaults_to_cash_when_payer_model_unset(monkeypatch):
    """No payer_model on intake -> resolves to cash (the GTM default)."""
    draft = _draft_with_goals([
        {"text": "Ride 25 miles", "tied_to": "load_mgmt", "payer_mode": "cash"},
    ])
    fake = _stub_anthropic(monkeypatch, response_blocks=[_draft_block(draft)])
    from agents.planner import compose
    out = compose(
        candidates=[{"exercise_id": "mini_squats"}],
        signal={"decision": "hold", "reasons": [], "confidence": 0.5},
        intake={"injury_type": "knee", "name": "P"},  # no payer_model
        phase="subacute",
        week=4,
        token="t",
    )
    assert "Payer model (HARD constraint" in fake.last_kwargs["messages"][0]["content"]
    assert "cash" in fake.last_kwargs["messages"][0]["content"]
    assert out["goals"][0]["payer_mode"] == "cash"
