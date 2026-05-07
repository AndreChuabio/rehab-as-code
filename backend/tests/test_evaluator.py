"""Tests for agents.evaluator.signal().

Anthropic mocked end-to-end. We exercise each decision branch
(progress / hold / regress) plus the failure modes that must NOT
silently degrade.
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
        self.input_tokens = 100
        self.output_tokens = 30


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


def _decision_block(decision: str, reasons: list[str], confidence: float) -> _FakeToolUseBlock:
    return _FakeToolUseBlock(
        "propose_decision",
        {"decision": decision, "reasons": reasons, "confidence": confidence},
    )


def test_signal_progress_branch(monkeypatch):
    _stub_anthropic(
        monkeypatch,
        response_blocks=[_decision_block(
            "progress",
            ["pain held at 2/10 for 7 days", "completion 100%"],
            0.9,
        )],
    )
    from agents.evaluator import signal
    out = signal(
        intake={"injury_type": "knee", "surgery_date": "2026-04-01"},
        health={"hrv": 65, "recovery_score": 78},
        history=[
            {"kind": "checkin", "pain_level": 2, "recorded_at": "2026-05-01"},
            {"kind": "checkin", "pain_level": 2, "recorded_at": "2026-05-04"},
        ],
        trend_summary={"pattern": "breakthrough", "evidence": ["recovery up"]},
        token="t",
    )
    assert out["decision"] == "progress"
    assert out["confidence"] == pytest.approx(0.9)
    assert any("pain" in r for r in out["reasons"])


def test_signal_hold_branch(monkeypatch):
    _stub_anthropic(
        monkeypatch,
        response_blocks=[_decision_block(
            "hold",
            ["sleep score 58 for 4 days", "metrics flat"],
            0.55,
        )],
    )
    from agents.evaluator import signal
    out = signal(
        intake={"injury_type": "knee"},
        health=None,
        history=None,
        trend_summary={"pattern": "plateau"},
    )
    assert out["decision"] == "hold"
    assert out["confidence"] == pytest.approx(0.55)


def test_signal_regress_branch(monkeypatch):
    _stub_anthropic(
        monkeypatch,
        response_blocks=[_decision_block(
            "regress",
            ["pain 3 -> 5 over 4 days", "two missed sessions"],
            0.8,
        )],
    )
    from agents.evaluator import signal
    out = signal(
        intake={"injury_type": "shoulder"},
        health={"recovery_score": 55},
        history=[
            {"kind": "checkin", "pain_level": 3, "recorded_at": "2026-05-01"},
            {"kind": "checkin", "pain_level": 5, "recorded_at": "2026-05-04"},
        ],
        trend_summary={"pattern": "regression"},
    )
    assert out["decision"] == "regress"


def test_signal_raises_on_anthropic_error(monkeypatch):
    _stub_anthropic(monkeypatch, raise_exc=RuntimeError("anthropic 502"))
    from agents.evaluator import EvaluatorError, signal
    with pytest.raises(EvaluatorError):
        signal(intake={}, health=None, history=None, trend_summary=None)


def test_signal_raises_on_invalid_decision(monkeypatch):
    _stub_anthropic(
        monkeypatch,
        response_blocks=[_decision_block("escalate", [], 0.5)],
    )
    from agents.evaluator import EvaluatorError, signal
    with pytest.raises(EvaluatorError):
        signal(intake={}, health=None, history=None, trend_summary=None)


def test_signal_raises_when_api_key_missing(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    from agents.evaluator import EvaluatorError, signal
    with pytest.raises(EvaluatorError):
        signal(intake={}, health=None, history=None, trend_summary=None)
