"""Tests for backend.clinical_taxonomy.

The deterministic body_region() must resolve every common injury_type the
intake modal collects. The freetext classifier is exercised with a stubbed
Anthropic client so we don't need a live API key in CI.
"""
from __future__ import annotations

from typing import Any

import pytest

import clinical_taxonomy


# ---------------------------------------------------------------------------
# body_region() - deterministic mapping
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    ("injury_type", "expected"),
    [
        ("lateral ankle sprain", "ankle"),
        ("Lateral Ankle Sprain", "ankle"),
        ("  ACL reconstruction  ", "knee"),
        ("Post-ACL Reconstruction", "knee"),
        ("rotator cuff repair", "shoulder"),
        ("rotator cuff strain", "shoulder"),
        ("hamstring strain", "hamstring"),
        ("low back pain", "low_back"),
        ("LBP", "low_back"),
        ("tennis elbow", "elbow"),
        ("lateral epicondylitis", "elbow"),
    ],
)
def test_body_region_explicit_map(injury_type: str, expected: str) -> None:
    """Common intake values resolve via the explicit dict, no LLM needed."""
    assert clinical_taxonomy.body_region(injury_type) == expected


@pytest.mark.parametrize(
    ("injury_type", "expected"),
    [
        # Free-text variants resolve via the substring rules.
        ("twisted my ankle yesterday", "ankle"),
        ("torn meniscus", "knee"),
        ("acl reconstructed in march", "knee"),
        ("achilles tear", "ankle"),
        ("severe lumbar pain", "low_back"),
        ("hamstring pull", "hamstring"),
        ("painful elbow tendinopathy", "elbow"),
    ],
)
def test_body_region_substring_fallback(injury_type: str, expected: str) -> None:
    """Free-text containing a region keyword resolves via _SUBSTRING_RULES."""
    assert clinical_taxonomy.body_region(injury_type) == expected


def test_body_region_returns_none_when_no_match() -> None:
    assert clinical_taxonomy.body_region("complete mystery") is None
    assert clinical_taxonomy.body_region(None) is None
    assert clinical_taxonomy.body_region("") is None


# ---------------------------------------------------------------------------
# classify_freetext() - LLM fallback (stubbed)
# ---------------------------------------------------------------------------

class _FakeTextBlock:
    def __init__(self, text: str) -> None:
        self.type = "text"
        self.text = text


class _FakeResponse:
    def __init__(self, text: str) -> None:
        self.content = [_FakeTextBlock(text)]


class _FakeClient:
    def __init__(self, text: str) -> None:
        class _Messages:
            def create(inner, **kwargs: Any) -> _FakeResponse:
                return _FakeResponse(text)
        self.messages = _Messages()


@pytest.fixture(autouse=True)
def _clear_cache() -> None:
    """Reset the module-level classify cache between tests."""
    clinical_taxonomy._CLASSIFY_CACHE.clear()


def test_classify_freetext_returns_region_when_model_responds_canonically(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-not-real")
    import anthropic
    monkeypatch.setattr(
        anthropic,
        "Anthropic",
        lambda api_key=None: _FakeClient("ankle"),
    )
    out = clinical_taxonomy.classify_freetext("rolled my foot, swollen ankle")
    assert out == "ankle"


def test_classify_freetext_caches_results(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-not-real")
    import anthropic

    call_count = {"n": 0}

    class _CountingClient(_FakeClient):
        def __init__(self) -> None:
            class _Messages:
                def create(inner, **kwargs: Any) -> _FakeResponse:
                    call_count["n"] += 1
                    return _FakeResponse("knee")
            self.messages = _Messages()

    monkeypatch.setattr(anthropic, "Anthropic", lambda api_key=None: _CountingClient())
    a = clinical_taxonomy.classify_freetext("weird kneecap pop")
    b = clinical_taxonomy.classify_freetext("weird kneecap pop")
    assert a == b == "knee"
    assert call_count["n"] == 1, "classify_freetext should cache by normalized input"


def test_classify_freetext_returns_none_on_unrecognized_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-not-real")
    import anthropic
    monkeypatch.setattr(
        anthropic,
        "Anthropic",
        lambda api_key=None: _FakeClient("garbage_region"),
    )
    assert clinical_taxonomy.classify_freetext("???") is None


def test_classify_freetext_returns_none_without_api_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    assert clinical_taxonomy.classify_freetext("torn rotator cuff") is None


# ---------------------------------------------------------------------------
# resolve_body_region() - the public entrypoint
# ---------------------------------------------------------------------------

def test_resolve_body_region_short_circuits_on_deterministic(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When the deterministic map hits, the LLM is never called."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-not-real")

    def _explode(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("LLM should not be invoked when the map hits")

    import anthropic
    monkeypatch.setattr(anthropic, "Anthropic", _explode)
    assert clinical_taxonomy.resolve_body_region("lateral ankle sprain") == "ankle"
