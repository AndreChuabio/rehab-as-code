"""Tests for agents.symptom_classifier.classify().

Anthropic mocked end-to-end. Behavior contract:
  * minor branch: pain <= 4/10, no red flags -> severity='minor', no regression
  * hold-load branch: pain >= 5/10, movement-specific -> severity='hold-load',
    regression_exercise_id set
  * clinician-attention branch: red flag -> severity='clinician-attention',
    regression cleared even if model returned one
  * Anthropic error: raises SymptomClassifierError (no silent fallback)
  * malformed verdict: raises SymptomClassifierError
  * empty reasoning / suggested_response: raises SymptomClassifierError
  * missing API key: raises SymptomClassifierError
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
        self.input_tokens = 180
        self.output_tokens = 60


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


def _verdict_block(
    severity: str,
    reasoning: str = "test reasoning",
    suggested: str = "Try ice and gentle stretching.",
    regression: str | None = None,
) -> _FakeToolUseBlock:
    return _FakeToolUseBlock(
        "classify_symptom",
        {
            "severity": severity,
            "reasoning": reasoning,
            "suggested_response": suggested,
            "regression_exercise_id": regression,
        },
    )


SAMPLE_WEARABLES = {"hrv_ms": 55, "sleep_score": 78, "recovery_score": 72}
SAMPLE_PROTOCOL = {
    "patient": "P",
    "phase": "subacute",
    "week": 4,
    "exercises": [
        {"id": "single_leg_squat", "name": "single-leg squats", "sets": 3, "reps": 10},
    ],
}


def test_classify_minor(monkeypatch):
    fake = _stub_anthropic(
        monkeypatch,
        response_blocks=[_verdict_block(
            "minor",
            reasoning="mild post-exercise soreness, pain ~3/10, no red flags.",
            suggested="That sounds like normal post-rehab soreness - keep going at this load.",
        )],
    )
    from agents.symptom_classifier import classify
    out = classify(
        message="my knee is a bit sore after yesterday's session",
        wearables=SAMPLE_WEARABLES,
        protocol=SAMPLE_PROTOCOL,
        last_pose_metrics=None,
        token="patient-uuid-1",
    )
    assert out["severity"] == "minor"
    assert out["regression_exercise_id"] is None
    assert "soreness" in out["reasoning"].lower()
    assert out["suggested_response"]
    # System + tool wiring sanity check
    assert fake.last_kwargs["tool_choice"]["name"] == "classify_symptom"


def test_classify_hold_load_with_regression(monkeypatch):
    _stub_anthropic(
        monkeypatch,
        response_blocks=[_verdict_block(
            "hold-load",
            reasoning="pain 6/10 isolated to lateral knee on single-leg squats.",
            suggested="Drop single-leg squats and try step-ups today instead.",
            regression="step_up",
        )],
    )
    from agents.symptom_classifier import classify
    out = classify(
        message="single leg squats give me sharp pain about 6/10 today",
        wearables=SAMPLE_WEARABLES,
        protocol=SAMPLE_PROTOCOL,
        last_pose_metrics=None,
        token="patient-uuid-2",
    )
    assert out["severity"] == "hold-load"
    assert out["regression_exercise_id"] == "step_up"


def test_classify_clinician_attention_clears_regression(monkeypatch):
    """Even if the model spuriously sets a regression on clinician-attention,
    the classifier wipes it - prescribing on a red flag is exactly what the
    orchestrator must NOT do."""
    _stub_anthropic(
        monkeypatch,
        response_blocks=[_verdict_block(
            "clinician-attention",
            reasoning="knee locking and giving way - red flag, possible meniscal involvement.",
            suggested="I'm flagging this for your clinician - they'll review and reach out shortly.",
            regression="step_up",  # model bug; must be wiped
        )],
    )
    from agents.symptom_classifier import classify
    out = classify(
        message="my knee keeps locking and gave way going down stairs",
        wearables=SAMPLE_WEARABLES,
        protocol=SAMPLE_PROTOCOL,
        last_pose_metrics={"avg_depth_deg": 78, "warnings": []},
        token="patient-uuid-3",
    )
    assert out["severity"] == "clinician-attention"
    assert out["regression_exercise_id"] is None


def test_classify_raises_on_anthropic_error(monkeypatch):
    """No silent fallback to a fake 'minor'. Caller (coach_chat) catches
    and skips the [SYMPTOM_TRIAGE] block for this turn."""
    _stub_anthropic(monkeypatch, raise_exc=RuntimeError("anthropic 503"))
    from agents.symptom_classifier import SymptomClassifierError, classify
    with pytest.raises(SymptomClassifierError):
        classify(
            message="my knee hurts",
            wearables=None,
            protocol=None,
            last_pose_metrics=None,
        )


def test_classify_raises_on_invalid_severity(monkeypatch):
    bad = _FakeToolUseBlock("classify_symptom", {
        "severity": "critical",
        "reasoning": "bad",
        "suggested_response": "bad",
        "regression_exercise_id": None,
    })
    _stub_anthropic(monkeypatch, response_blocks=[bad])
    from agents.symptom_classifier import SymptomClassifierError, classify
    with pytest.raises(SymptomClassifierError):
        classify(message="hurt", wearables=None, protocol=None)


def test_classify_raises_on_empty_reasoning(monkeypatch):
    """The classifier must emit a populated reasoning + suggested_response;
    Maya needs both to compose her reply."""
    bad = _FakeToolUseBlock("classify_symptom", {
        "severity": "minor",
        "reasoning": "",
        "suggested_response": "ok",
        "regression_exercise_id": None,
    })
    _stub_anthropic(monkeypatch, response_blocks=[bad])
    from agents.symptom_classifier import SymptomClassifierError, classify
    with pytest.raises(SymptomClassifierError):
        classify(message="hurt", wearables=None, protocol=None)


def test_classify_raises_on_no_tool_call(monkeypatch):
    """A reply with no classify_symptom tool block must fail loudly."""
    _stub_anthropic(monkeypatch, response_blocks=[])
    from agents.symptom_classifier import SymptomClassifierError, classify
    with pytest.raises(SymptomClassifierError):
        classify(message="hurt", wearables=None, protocol=None)


def test_classify_raises_when_api_key_missing(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    from agents.symptom_classifier import SymptomClassifierError, classify
    with pytest.raises(SymptomClassifierError):
        classify(message="hurt", wearables=None, protocol=None)


def test_classify_minor_clears_regression_even_if_model_returns_one(monkeypatch):
    """Defense in depth: only hold-load may carry a regression suggestion.
    If the model returns a regression on a 'minor' verdict, drop it."""
    _stub_anthropic(
        monkeypatch,
        response_blocks=[_verdict_block(
            "minor",
            reasoning="mild soreness.",
            suggested="Continue the plan.",
            regression="step_up",  # spurious - must be wiped
        )],
    )
    from agents.symptom_classifier import classify
    out = classify(message="bit sore", wearables=None, protocol=None)
    assert out["severity"] == "minor"
    assert out["regression_exercise_id"] is None
