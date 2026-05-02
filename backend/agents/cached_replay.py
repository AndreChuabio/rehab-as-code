"""
CachedReplayAgent — replays a pre-captured agent trace from disk.

This is the demo path. The real Cursor cloud agent runs are slow (30s-5min)
and can fail live; we capture them this morning into JSON, then replay them
during the demo with paced timing so the UI feels live but is deterministic.

The capture format is the same TraceEvent shape live agents produce, plus
top-level pr_url / branch / artifacts. See cached_runs/ for examples.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from pathlib import Path
from typing import AsyncIterator

from .base import AgentInvocation, CodingAgent, InvocationRequest, TraceEvent

logger = logging.getLogger(__name__)

CACHE_DIR = Path(__file__).parent.parent / "cached_runs"


class CachedReplayAgent(CodingAgent):
    """Replays a JSON-captured agent trace, paced by recorded timestamps."""

    name = "cached_replay"

    def __init__(self, cache_dir: Path | None = None, speed: float = 1.0) -> None:
        """
        Parameters
        ----------
        cache_dir : Path | None
            Directory containing {flow}.json files. Defaults to backend/cached_runs/.
        speed : float
            Playback rate multiplier. >1 = faster than live, <1 = slower.
            Useful for keeping the demo within a time budget.
        """
        self.cache_dir = cache_dir or CACHE_DIR
        self.speed = speed
        self._invocations: dict[str, dict] = {}

    async def invoke(self, request: InvocationRequest) -> AgentInvocation:
        cache_path = self.cache_dir / f"{request.flow}.json"
        if not cache_path.exists():
            raise FileNotFoundError(
                f"No cached run for flow {request.flow!r} at {cache_path}. "
                f"Run scripts/capture_run.py against the live agent first."
            )

        with cache_path.open() as f:
            cached = json.load(f)

        invocation_id = str(uuid.uuid4())
        self._invocations[invocation_id] = cached

        return AgentInvocation(
            invocation_id=invocation_id,
            pr_url=cached.get("pr_url"),
            branch=cached.get("branch"),
            artifacts=cached.get("artifacts", []),
        )

    async def stream_trace(self, invocation_id: str) -> AsyncIterator[TraceEvent]:
        cached = self._invocations.get(invocation_id)
        if cached is None:
            raise KeyError(f"unknown invocation_id {invocation_id!r}")

        events = cached.get("events", [])
        if not events:
            return

        start = time.monotonic()
        first_ts = events[0].get("timestamp", 0.0)

        for raw in events:
            event_ts = raw.get("timestamp", 0.0) - first_ts
            target = start + (event_ts / max(self.speed, 0.01))
            now = time.monotonic()
            if target > now:
                await asyncio.sleep(target - now)
            yield TraceEvent(
                type=raw["type"],
                timestamp=event_ts,
                label=raw["label"],
                payload=raw.get("payload", {}),
            )
