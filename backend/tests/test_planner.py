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
