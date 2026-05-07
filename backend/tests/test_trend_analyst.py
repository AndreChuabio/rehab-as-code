"""Tests for agents.trend_analyst.analyze().

Anthropic mocked end-to-end. Behavior contract:
  * happy path: enough history, model returns a pattern -> dict back
  * insufficient history fallback: returns None without calling model
  * Anthropic error: raises TrendAnalystError
  * server-side aggregation produces the right summary shape
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
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
        self.output_tokens = 40


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


def _pattern_block(pattern: str, evidence: list[str], implication: str) -> _FakeToolUseBlock:
    return _FakeToolUseBlock(
        "propose_pattern",
        {
            "pattern": pattern,
            "evidence": evidence,
            "implication_for_next_week": implication,
        },
    )


def _make_checkins(n: int, base_pain: int = 3) -> list[dict]:
    """Generate n recent check-ins with ascending dates, oldest-first."""
    out = []
    now = datetime.now(timezone.utc)
    for i in range(n):
        ts = now - timedelta(days=(n - i))
        out.append({
            "kind": "checkin",
            "pain_level": base_pain,
            "recovery_score": 70,
            "recorded_at": ts.isoformat(),
        })
    return out


def test_analyze_happy_path_returns_pattern(monkeypatch):
    fake = _stub_anthropic(
        monkeypatch,
        response_blocks=[_pattern_block(
            "breakthrough",
            ["pain dropped 4 -> 2 over 3 weeks", "completion 90%"],
            "Progress load on tolerated exercises.",
        )],
    )
    from agents.trend_analyst import analyze
    out = analyze(
        token="test-token",
        checkins=_make_checkins(8, base_pain=3),
        sessions=[],
        intake={"injury_type": "knee"},
        weeks=4,
    )
    assert out is not None
    assert out["pattern"] == "breakthrough"
    assert any("pain" in e for e in out["evidence"])
    assert "implication_for_next_week" in out

    # The aggregated payload must be in the user prompt; we shouldn't
    # be sending raw rows.
    user_msg = fake.last_kwargs["messages"][0]["content"]
    assert "Aggregated longitudinal data" in user_msg
    assert "pain" in user_msg


def test_analyze_returns_none_on_insufficient_history(monkeypatch):
    """Fewer than 4 check-ins / completed sessions in the window -> the
    model isn't called and None is returned."""
    fake = _stub_anthropic(monkeypatch, response_blocks=[])
    from agents.trend_analyst import analyze
    out = analyze(
        token="test-token",
        checkins=_make_checkins(2, base_pain=3),
        sessions=[],
    )
    assert out is None
    assert fake.last_kwargs is None


def test_analyze_uses_completed_sessions_when_no_checkins(monkeypatch):
    """4+ completed sessions trigger the call even without check-ins."""
    now = datetime.now(timezone.utc)
    sessions = [
        {
            "status": "completed",
            "exercise_id": "x",
            "created_at": (now - timedelta(days=i)).isoformat(),
        }
        for i in range(1, 6)
    ]
    fake = _stub_anthropic(
        monkeypatch,
        response_blocks=[_pattern_block("steady", [], "no change")],
    )
    from agents.trend_analyst import analyze
    out = analyze(token="t", checkins=[], sessions=sessions)
    assert out is not None
    assert out["pattern"] == "steady"
    assert fake.last_kwargs is not None


def test_analyze_raises_on_anthropic_error(monkeypatch):
    _stub_anthropic(monkeypatch, raise_exc=RuntimeError("502"))
    from agents.trend_analyst import TrendAnalystError, analyze
    with pytest.raises(TrendAnalystError):
        analyze(
            token="t",
            checkins=_make_checkins(8),
            sessions=[],
        )


def test_analyze_raises_on_invalid_pattern(monkeypatch):
    _stub_anthropic(
        monkeypatch,
        response_blocks=[_pattern_block("ascending", [], "x")],
    )
    from agents.trend_analyst import TrendAnalystError, analyze
    with pytest.raises(TrendAnalystError):
        analyze(
            token="t",
            checkins=_make_checkins(8),
            sessions=[],
        )


def test_analyze_raises_when_api_key_missing(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    from agents.trend_analyst import TrendAnalystError, analyze
    with pytest.raises(TrendAnalystError):
        analyze(token="t", checkins=_make_checkins(8), sessions=[])
