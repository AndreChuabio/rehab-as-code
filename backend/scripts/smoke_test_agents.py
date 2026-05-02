"""
Smoke test: exercise the modular agent layer end-to-end without hitting any
external services. Confirms the abstraction works and provider swapping is
just a config change.

Run:
    cd backend && python -m scripts.smoke_test_agents
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agents import InvocationRequest, get_agent  # noqa: E402


async def exercise(provider: str, request: InvocationRequest) -> None:
    print(f"\n=== provider: {provider} ===")
    agent = get_agent(provider)
    invocation = await agent.invoke(request)
    print(f"  pr_url:        {invocation.pr_url}")
    print(f"  branch:        {invocation.branch}")
    print(f"  invocation_id: {invocation.invocation_id}")
    print(f"  trace events:")
    async for event in agent.stream_trace(invocation.invocation_id):
        print(f"    [{event.timestamp:5.2f}s] {event.type:18s} {event.label}")


async def main() -> None:
    request = InvocationRequest(
        repo="AndreChuabio/rehab-protocols-andre",
        prompt="generate next week's protocol for post-ACL week 4",
        context_files={
            "data/wearables-2026-05-02.json": '{"hrv_ms": 41, "sleep_hours": 7.2}',
            "data/symptoms-2026-05-02.md": "# Symptom log\n\nFeeling stronger.",
        },
        flow="weekly_plan",
    )

    # Exercise both demo-safe providers. Live providers (cursor_github,
    # cursor_api) are skipped here — they need GitHub auth + Cursor access.
    for provider in ("mock", "cached_replay"):
        try:
            await exercise(provider, request)
        except Exception as exc:
            print(f"  FAILED: {exc}")


if __name__ == "__main__":
    asyncio.run(main())
