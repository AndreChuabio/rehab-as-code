"""_invoke_with_fallback gating — cached_replay must not silently mask
live-provider failures.

Pre-2026-05-06 this helper would catch any provider exception and swap
in cached_replay. That's fine for hackathon-stage demos but hides real
failures once live patients are in the loop. The fix raises HTTPException(502)
with a friendly toast message instead.
"""
from __future__ import annotations

import asyncio

import pytest
from fastapi import HTTPException


def test_live_provider_failure_raises_http_502(monkeypatch):
    import main

    class _BoomAgent:
        name = "boom"

        async def invoke(self, request):  # noqa: ARG002
            raise RuntimeError("provider 503 from upstream")

    def _get_agent(name):  # noqa: ARG001
        return _BoomAgent()

    # Skip the heavy context-build path; we only care about the raise.
    monkeypatch.setattr(main, "get_agent", _get_agent)
    monkeypatch.setattr(main, "get_health_data", lambda: {})
    monkeypatch.setattr(main, "write_context_files", lambda **_: [])
    monkeypatch.setattr(main, "_build_agent_prompt", lambda **_: "stub prompt")

    req = main.AgentInvokeRequest(
        flow="weekly_plan", provider="cursor_api"  # live provider
    )

    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(main._invoke_with_fallback(req))

    assert exc_info.value.status_code == 502
    # User-facing toast text — keep stable so the frontend can render it
    # verbatim without a stack trace leaking through.
    assert "try again" in exc_info.value.detail.lower()


def test_cached_replay_failure_also_raises_not_silent(monkeypatch):
    """Even if cached_replay itself fails, we don't pretend it succeeded.
    The previous fallback was 'on any failure use cached_replay' — which
    couldn't help when cached_replay was the failing provider."""
    import main

    class _BoomAgent:
        name = "cached_replay"

        async def invoke(self, request):  # noqa: ARG002
            raise RuntimeError("replay file missing")

    monkeypatch.setattr(main, "get_agent", lambda _name: _BoomAgent())
    monkeypatch.setattr(main, "get_health_data", lambda: {})
    monkeypatch.setattr(main, "write_context_files", lambda **_: [])
    monkeypatch.setattr(main, "_build_agent_prompt", lambda **_: "stub prompt")

    req = main.AgentInvokeRequest(flow="intake", provider="cached_replay")

    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(main._invoke_with_fallback(req))
    assert exc_info.value.status_code == 502
