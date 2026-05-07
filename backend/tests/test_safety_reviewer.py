"""Tests for agents.safety_reviewer.review().

Anthropic mocked end-to-end. Behavior contract:
  * ok branch: model returns ok=True with no concerns
  * med-severity branch: returns concerns with overall_severity='med'
  * high-severity branch: returns concerns with overall_severity='high'
  * Anthropic error: raises SafetyReviewError (fails closed)
  * malformed verdict: raises SafetyReviewError
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
        self.output_tokens = 100


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


def _verdict_block(ok: bool, concerns: list[dict], overall: str) -> _FakeToolUseBlock:
    return _FakeToolUseBlock(
        "submit_verdict",
        {"ok": ok, "concerns": concerns, "overall_severity": overall},
    )


SAMPLE_DRAFT = {
    "patient": "P",
    "phase": "subacute",
    "week": 4,
    "exercises": [
        {"name": "mini_squats", "sets": 3, "reps": 12, "load": "bodyweight"},
    ],
}


def test_review_ok_branch(monkeypatch):
    _stub_anthropic(
        monkeypatch,
        response_blocks=[_verdict_block(True, [], "low")],
    )
    from agents.safety_reviewer import review
    out = review(
        draft=SAMPLE_DRAFT,
        intake={"injury_type": "knee"},
        trend_summary={"pattern": "steady"},
        token="t",
    )
    assert out["ok"] is True
    assert out["concerns"] == []
    assert out["overall_severity"] == "low"


def test_review_med_branch(monkeypatch):
    concerns = [
        {"check": "pain_ceiling", "severity": "med", "detail": "load too high for week 4"},
    ]
    _stub_anthropic(
        monkeypatch,
        response_blocks=[_verdict_block(False, concerns, "med")],
    )
    from agents.safety_reviewer import review
    out = review(
        draft=SAMPLE_DRAFT,
        intake={"injury_type": "knee"},
        trend_summary=None,
        token="t",
    )
    assert out["ok"] is False
    assert out["overall_severity"] == "med"
    assert len(out["concerns"]) == 1
    assert out["concerns"][0]["check"] == "pain_ceiling"


def test_review_high_branch(monkeypatch):
    concerns = [
        {
            "check": "contraindication",
            "severity": "high",
            "detail": "overhead pressing on torn rotator cuff",
        },
    ]
    _stub_anthropic(
        monkeypatch,
        response_blocks=[_verdict_block(False, concerns, "high")],
    )
    from agents.safety_reviewer import review
    out = review(
        draft=SAMPLE_DRAFT,
        intake={"injury_type": "shoulder"},
        trend_summary=None,
        token="t",
    )
    assert out["overall_severity"] == "high"
    assert out["concerns"][0]["severity"] == "high"


def test_review_raises_on_anthropic_error(monkeypatch):
    """No silent fallbacks on the safety gate - we fail closed."""
    _stub_anthropic(monkeypatch, raise_exc=RuntimeError("anthropic 500"))
    from agents.safety_reviewer import SafetyReviewError, review
    with pytest.raises(SafetyReviewError):
        review(draft=SAMPLE_DRAFT, intake=None, trend_summary=None)


def test_review_raises_on_invalid_severity(monkeypatch):
    bad = _FakeToolUseBlock("submit_verdict", {
        "ok": False, "concerns": [], "overall_severity": "critical",
    })
    _stub_anthropic(monkeypatch, response_blocks=[bad])
    from agents.safety_reviewer import SafetyReviewError, review
    with pytest.raises(SafetyReviewError):
        review(draft=SAMPLE_DRAFT, intake=None, trend_summary=None)


def test_review_raises_when_api_key_missing(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    from agents.safety_reviewer import SafetyReviewError, review
    with pytest.raises(SafetyReviewError):
        review(draft=SAMPLE_DRAFT, intake=None, trend_summary=None)


def test_review_reconciles_no_concerns_with_ok_false(monkeypatch):
    """Edge case: if the model returns ok=False with empty concerns,
    trust the absence of concerns and treat the verdict as ok."""
    _stub_anthropic(
        monkeypatch,
        response_blocks=[_verdict_block(False, [], "low")],
    )
    from agents.safety_reviewer import review
    out = review(draft=SAMPLE_DRAFT, intake=None, trend_summary=None)
    assert out["ok"] is True
    assert out["concerns"] == []
