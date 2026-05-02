"""
Primary Cursor cloud agent invocation: GitHub @cursor mention via gh CLI.

Flow:
  1. Write context files (wearable data, symptom log) into the protocol repo
     on a fresh branch via gh api commits — repo is the message bus.
  2. Open a GitHub issue in the protocol repo with @cursor mention + the
     structured prompt.
  3. Poll the issue/repo for a PR opened by the Cursor agent referencing
     our issue.
  4. Stream trace events back to the UI as we observe each milestone.

Live latency is 30s-5min — this path is for the morning's pre-cache run, not
for the demo itself. Demo uses CachedReplayAgent.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from typing import AsyncIterator

from .base import (
    AgentInvocation,
    CodingAgent,
    InvocationRequest,
    TraceEvent,
)

logger = logging.getLogger(__name__)


class CursorGitHubAgent(CodingAgent):
    """Invoke Cursor cloud agent via GitHub @cursor issue mention."""

    name = "cursor_github"

    def __init__(self, poll_interval_sec: int = 5, timeout_sec: int = 600) -> None:
        self.poll_interval_sec = poll_interval_sec
        self.timeout_sec = timeout_sec
        self._traces: dict[str, list[TraceEvent]] = {}

    async def invoke(self, request: InvocationRequest) -> AgentInvocation:
        invocation_id = str(uuid.uuid4())
        start = time.monotonic()
        trace: list[TraceEvent] = []
        self._traces[invocation_id] = trace

        def emit(event_type, label, payload=None):
            trace.append(TraceEvent(
                type=event_type,
                timestamp=time.monotonic() - start,
                label=label,
                payload=payload or {},
            ))

        emit("agent_started", f"Cursor agent invocation started for {request.flow}",
             {"flow": request.flow, "repo": request.repo})

        # 1. Push context files to a working branch in the protocol repo
        branch = await self._push_context_files(request, emit)

        # 2. Open GitHub issue with @cursor mention
        issue_number = await self._open_cursor_issue(request, branch, emit)

        # 3. Poll for the PR Cursor opens in response
        pr_url = await self._wait_for_pr(request.repo, issue_number, emit)

        emit("agent_completed", f"PR ready: {pr_url}", {"pr_url": pr_url})

        return AgentInvocation(
            invocation_id=invocation_id,
            pr_url=pr_url,
            branch=branch,
            artifacts=[{"type": "issue", "number": issue_number}],
        )

    async def stream_trace(self, invocation_id: str) -> AsyncIterator[TraceEvent]:
        trace = self._traces.get(invocation_id, [])
        for event in trace:
            yield event

    # ----- internal -------------------------------------------------------

    async def _push_context_files(self, request: InvocationRequest, emit) -> str:
        """Write context_files into a new branch via gh api. Returns branch name."""
        branch = f"agent-input/{request.flow}-{int(time.time())}"
        emit("branch_created", f"git checkout -b {branch}", {"branch": branch})

        for path, content in request.context_files.items():
            emit("file_edit", f"write {path} ({len(content)} bytes)",
                 {"path": path, "size": len(content)})
            cmd = [
                "gh", "api",
                f"repos/{request.repo}/contents/{path}",
                "-X", "PUT",
                "-f", f"message=context: {request.flow} {path}",
                "-f", f"branch={branch}",
                "-f", f"content={_b64(content)}",
            ]
            await _run(cmd)

        emit("commit_created", f"context committed to {branch}",
             {"branch": branch, "files": list(request.context_files.keys())})
        return branch

    async def _open_cursor_issue(
        self, request: InvocationRequest, branch: str, emit
    ) -> int:
        """Open issue with @cursor mention, return issue number."""
        body = (
            f"@cursor please update `protocol.yaml` for flow `{request.flow}`.\n\n"
            f"Context branch: `{branch}`\n\n"
            f"## Task\n{request.prompt}\n\n"
            f"## Files to consult\n"
            f"- `data/` (just-pushed context — wearable + symptom data)\n"
            f"- `protocol.yaml` (current week's plan)\n"
            f"- `protocol-library/` (evidence-based reference)\n"
            f"- `.cursorrules` (schema + clinical guardrails)\n\n"
            f"Open a PR following the conventions in `.cursorrules`."
        )
        cmd = [
            "gh", "issue", "create",
            "--repo", request.repo,
            "--title", f"[agent] {request.flow} update",
            "--body", body,
        ]
        result = await _run(cmd)
        # gh prints the issue URL on success — extract number
        issue_number = int(result.strip().rstrip("/").rsplit("/", 1)[-1])
        emit("tool_call", f"opened issue #{issue_number} with @cursor mention",
             {"issue": issue_number})
        return issue_number

    async def _wait_for_pr(self, repo: str, issue_number: int, emit) -> str:
        """Poll for a PR that references the issue. Returns PR URL."""
        deadline = time.monotonic() + self.timeout_sec
        while time.monotonic() < deadline:
            cmd = [
                "gh", "pr", "list",
                "--repo", repo,
                "--state", "open",
                "--json", "number,url,title,body",
                "--limit", "20",
            ]
            try:
                out = await _run(cmd)
                prs = json.loads(out)
                for pr in prs:
                    if f"#{issue_number}" in (pr.get("body") or "") or \
                       f"closes #{issue_number}" in (pr.get("body") or "").lower():
                        emit("pr_opened", f"PR #{pr['number']} opened", pr)
                        return pr["url"]
            except Exception as e:
                logger.warning("pr poll failed: %s", e)
            await asyncio.sleep(self.poll_interval_sec)
        raise TimeoutError(f"No PR opened within {self.timeout_sec}s")


# ----- helpers ------------------------------------------------------------

async def _run(cmd: list[str]) -> str:
    """Run a subprocess, return stdout. Raises on non-zero exit."""
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()
    if proc.returncode != 0:
        raise RuntimeError(
            f"command failed ({proc.returncode}): {' '.join(cmd)}\n"
            f"stderr: {stderr.decode()}"
        )
    return stdout.decode()


def _b64(s: str) -> str:
    import base64
    return base64.b64encode(s.encode()).decode()
