"""
CursorSdkAgent: live path via @cursor/sdk TypeScript SDK.

The SDK is TypeScript-only; the Python backend shells out to the Node
orchestrator under orchestrator/src/orchestrator.ts (run via npx tsx). The
orchestrator wraps Agent.create with named sub-agents defined in a YAML
config file and streams NDJSON trace events back on stdout.

Contract with the Node helper:
    stdin  : JSON {config, flow, repoUrl, extraPrompt, contextFiles}
    stdout : newline-delimited JSON
        {"type": "trace",  "event": {...}}
        {"type": "result", "agentId": ..., "runId": ..., "prUrl": ...,
         "branch": ..., "status": "finished" | "error" | "cancelled"}
    stderr : human-readable diagnostics (for logs only)
    exit   : 0 finished, 1 startup failure, 2 run error

Demo posture: this is the live path. Demo default is cached_replay. The
caller (main.py) decides which provider to invoke per request; on failure
here, FastAPI auto-falls back to cached_replay.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
import uuid
from pathlib import Path
from typing import AsyncIterator

from .base import AgentInvocation, CodingAgent, InvocationRequest, TraceEvent

logger = logging.getLogger(__name__)


class CursorSdkError(RuntimeError):
    """Raised when the Node orchestrator fails to start or complete."""


class CursorSdkAgent(CodingAgent):
    """Live Cursor cloud agent via the TypeScript SDK (Node helper)."""

    name = "cursor_sdk"

    def __init__(
        self,
        orchestrator_dir: Path | None = None,
        config_name: str = "care-plan",
    ) -> None:
        """
        Parameters
        ----------
        orchestrator_dir : Path | None
            Path to the orchestrator/ directory. Defaults to the sibling
            orchestrator/ folder in the repo.
        config_name : str
            Name of the orchestrator config to load (orchestrator/configs/
            {config_name}.yaml). A sibling orchestrator (for example
            nutrition-coordinator) is a new config file plus a new
            CursorSdkAgent instance with a different config_name.
        """
        if orchestrator_dir is None:
            orchestrator_dir = (
                Path(__file__).resolve().parent.parent.parent / "orchestrator"
            )
        self.orchestrator_dir = orchestrator_dir
        self.config_name = config_name
        self.tsx_binary = orchestrator_dir / "node_modules" / ".bin" / "tsx"
        self.orchestrator_script = orchestrator_dir / "src" / "orchestrator.ts"
        self._invocations: dict[str, dict] = {}

    async def invoke(self, request: InvocationRequest) -> AgentInvocation:
        if not self.tsx_binary.exists() or not self.orchestrator_script.exists():
            raise CursorSdkError(
                f"orchestrator not installed at {self.orchestrator_dir}. "
                f"Run `cd orchestrator && npm install` first."
            )
        if not os.getenv("CURSOR_API_KEY"):
            raise CursorSdkError("CURSOR_API_KEY not set")

        repo_url = (
            request.repo
            if request.repo.startswith("http")
            else f"https://github.com/{request.repo}"
        )
        payload = {
            "config": self.config_name,
            "flow": request.flow,
            "repoUrl": repo_url,
            "extraPrompt": request.prompt,
            "contextFiles": request.context_files,
        }

        proc = await asyncio.create_subprocess_exec(
            str(self.tsx_binary),
            str(self.orchestrator_script),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=str(self.orchestrator_dir),
            env={**os.environ},
        )
        assert proc.stdin is not None and proc.stdout is not None and proc.stderr is not None

        proc.stdin.write(json.dumps(payload).encode() + b"\n")
        await proc.stdin.drain()
        proc.stdin.close()

        events: list[TraceEvent] = []
        meta: dict = {
            "pr_url": None,
            "branch": None,
            "agent_id": None,
            "run_id": None,
            "status": "running",
        }
        start = time.monotonic()

        async for raw in proc.stdout:
            line = raw.decode(errors="replace").strip()
            if not line:
                continue
            try:
                parsed = json.loads(line)
            except json.JSONDecodeError:
                logger.debug("orchestrator non-JSON line: %s", line[:200])
                continue
            msg_type = parsed.get("type")
            if msg_type == "trace":
                e = parsed.get("event") or {}
                events.append(
                    TraceEvent(
                        type=e.get("type", "tool_call"),
                        timestamp=float(e.get("timestamp", 0.0)),
                        label=str(e.get("label", "")),
                        payload=e.get("payload") or {},
                    )
                )
            elif msg_type == "result":
                meta["pr_url"] = parsed.get("prUrl")
                meta["branch"] = parsed.get("branch")
                meta["agent_id"] = parsed.get("agentId")
                meta["run_id"] = parsed.get("runId")
                meta["status"] = parsed.get("status") or "unknown"
            else:
                logger.debug("orchestrator unknown message type: %s", msg_type)

        stderr_bytes = await proc.stderr.read()
        returncode = await proc.wait()
        elapsed = time.monotonic() - start

        stderr_text = stderr_bytes.decode(errors="replace").strip()
        if stderr_text:
            logger.info(
                "orchestrator stderr (exit=%s, %.1fs): %s",
                returncode,
                elapsed,
                stderr_text[-1500:],
            )

        if returncode != 0:
            reason = stderr_text.splitlines()[-1] if stderr_text else f"exit {returncode}"
            raise CursorSdkError(
                f"orchestrator exited {returncode}: {reason}. "
                f"Captured {len(events)} trace events before failure."
            )

        invocation_id = str(uuid.uuid4())
        self._invocations[invocation_id] = {
            "events": events,
            **meta,
        }

        artifacts: list[dict] = []
        if meta["agent_id"]:
            artifacts.append({"type": "agent", "id": meta["agent_id"]})
        if meta["run_id"]:
            artifacts.append({"type": "run", "id": meta["run_id"]})

        return AgentInvocation(
            invocation_id=invocation_id,
            pr_url=meta["pr_url"],
            branch=meta["branch"],
            artifacts=artifacts,
        )

    async def stream_trace(self, invocation_id: str) -> AsyncIterator[TraceEvent]:
        entry = self._invocations.get(invocation_id)
        if entry is None:
            raise KeyError(f"unknown invocation_id {invocation_id!r}")

        events: list[TraceEvent] = entry["events"]
        if not events:
            return

        start = time.monotonic()
        first_ts = events[0].timestamp

        for event in events:
            target = start + (event.timestamp - first_ts)
            now = time.monotonic()
            if target > now:
                await asyncio.sleep(target - now)
            yield event
