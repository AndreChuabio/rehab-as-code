"""Tests for agents.researcher.candidates().

Anthropic is mocked end-to-end. Behavior contract:
  * happy path: returns the model's candidate list
  * empty library (no injury dir match): returns [] without calling the model
  * Anthropic error: raises ResearcherError (no silent fallbacks)
  * missing API key: raises ResearcherError
  * malformed model output: raises ResearcherError
"""
from __future__ import annotations

from typing import Any

import pytest


class _FakeToolUseBlock:
    """Mimic anthropic.types.ToolUseBlock for tool-call responses."""

    def __init__(self, name: str, input_data: dict[str, Any]) -> None:
        self.type = "tool_use"
        self.name = name
        self.input = input_data


class _FakeUsage:
    def __init__(self, in_tokens: int = 100, out_tokens: int = 50) -> None:
        self.input_tokens = in_tokens
        self.output_tokens = out_tokens


class _FakeResponse:
    def __init__(self, blocks: list[Any]) -> None:
        self.content = blocks
        self.usage = _FakeUsage()


class _FakeAnthropicClient:
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


def _stub_anthropic(
    monkeypatch,
    *,
    response_blocks: list[Any] | None = None,
    raise_exc: Exception | None = None,
) -> _FakeAnthropicClient:
    import anthropic
    fake = _FakeAnthropicClient(response_blocks=response_blocks, raise_exc=raise_exc)
    monkeypatch.setattr(anthropic, "Anthropic", lambda api_key=None: fake)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key-not-real")
    return fake


def _candidate_block(candidates: list[dict]) -> _FakeToolUseBlock:
    return _FakeToolUseBlock("propose_candidates", {"candidates": candidates})


def test_candidates_happy_path_returns_model_output(monkeypatch):
    expected_candidates = [
        {
            "exercise_id": "shoulder_pendulum",
            "citation_path": "protocols/protocol-library/shoulder/rotator-cuff-strain-week-1.yaml",
            "citation_line": 14,
            "rationale": "Pain-free passive ROM appropriate for week 1.",
            "progression_options": [],
        },
        {
            "exercise_id": "shoulder_isometric_er",
            "citation_path": "protocols/protocol-library/shoulder/rotator-cuff-strain-week-1.yaml",
            "citation_line": 19,
            "rationale": "Sub-maximal activation without provoking pain.",
            "progression_options": ["shoulder_scapular_retraction"],
        },
    ]
    fake = _stub_anthropic(
        monkeypatch,
        response_blocks=[_candidate_block(expected_candidates)],
    )

    from agents.researcher import candidates
    result = candidates(
        injury_type="shoulder",
        phase="acute",
        week=1,
        intake={"injury_type": "shoulder", "name": "Test"},
        token="test-token-uuid",
    )

    assert result == expected_candidates
    # Confirm Sonnet 4.6 was hit with the right tool gate.
    assert fake.last_kwargs is not None
    assert fake.last_kwargs["model"] == "claude-sonnet-4-6"
    assert fake.last_kwargs["tool_choice"]["name"] == "propose_candidates"
    # The library YAML must end up in the user prompt so the model can
    # cite line numbers back. Don't assert the full text - just that the
    # path was referenced.
    user_msg = fake.last_kwargs["messages"][0]["content"]
    assert "rotator-cuff-strain-week-1.yaml" in user_msg


def test_candidates_returns_empty_when_no_library_dir(monkeypatch):
    """An injury type that doesn't map to a library subdir is a known
    case (general conditioning, undocumented injury). Empty-list, no
    Anthropic call - no error raised."""
    fake = _stub_anthropic(monkeypatch, response_blocks=[])

    from agents.researcher import candidates
    result = candidates(
        injury_type="unmapped-injury-xyz",
        phase="acute",
        week=1,
        intake=None,
        token="test-token-uuid",
    )
    assert result == []
    # The model must NOT have been called.
    assert fake.last_kwargs is None


def test_candidates_raises_on_anthropic_error(monkeypatch):
    """Per the no-silent-fallback rule: an Anthropic 5xx propagates as
    ResearcherError so the orchestrator surfaces it as a 5xx upstream."""
    _stub_anthropic(monkeypatch, raise_exc=RuntimeError("anthropic 503"))

    from agents.researcher import ResearcherError, candidates
    with pytest.raises(ResearcherError) as exc_info:
        candidates(
            injury_type="knee",
            phase="acute",
            week=3,
            intake={"injury_type": "knee"},
            token="test-token-uuid",
        )
    assert "anthropic 503" in str(exc_info.value)


def test_candidates_raises_when_api_key_missing(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

    from agents.researcher import ResearcherError, candidates
    with pytest.raises(ResearcherError):
        candidates(
            injury_type="knee",
            phase="acute",
            week=3,
            intake=None,
        )


def test_candidates_raises_on_missing_tool_call(monkeypatch):
    """Anthropic returned content but no propose_candidates tool call -
    treat as malformed output, not silent ok."""
    # An empty content block list is malformed for tool_choice forced.
    _stub_anthropic(monkeypatch, response_blocks=[])

    from agents.researcher import ResearcherError, candidates
    with pytest.raises(ResearcherError):
        candidates(
            injury_type="knee",
            phase="acute",
            week=3,
            intake=None,
        )
