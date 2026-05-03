"""
AG2Agent: multi-agent pipeline using the AG2 (AutoGen) GroupChat pattern.

Three specialized agents handle distinct stages of updating the rehab protocol:

  repo_reader      — reads the repo (list_files, read_file)
  protocol_editor  — writes YAML changes (write_file)
  git_publisher    — commits and opens the PR (commit_and_pr)

A GroupChatManager (Claude-powered) routes messages between them.
An ExecutorProxy (UserProxyAgent, no LLM) runs all tool calls.

Set AGENT_PROVIDER=ag2. Requires ANTHROPIC_API_KEY and GITHUB_TOKEN.
"""

from __future__ import annotations

import asyncio
import logging
import os
import subprocess
import tempfile
import time
import uuid
from pathlib import Path
from typing import AsyncIterator

import autogen

from .base import AgentInvocation, CodingAgent, InvocationRequest, TraceEvent

logger = logging.getLogger(__name__)


# ── per-agent system prompts ──────────────────────────────────────────────────

REPO_READER_PROMPT = """You are the Repo Reader.
Your only job: explore the cloned repository and report what you find.

Steps:
1. Call list_files with pattern "**/*.yaml" to find protocol files.
2. Call read_file on the most relevant one (usually the current active protocol).
3. Output a concise summary of the file path and its current content.

Do NOT make edits. Finish with: "Repo read complete."
"""

PROTOCOL_EDITOR_PROMPT = """You are the Protocol Editor.
You receive the current protocol YAML (from the Repo Reader) and the original task.

Steps:
1. Identify exactly which fields to change based on the task.
2. Call write_file with the updated YAML — keep the same file path the Repo Reader reported.
3. Only change what was asked; preserve all other fields and structure.

Finish with: "Protocol edit complete — <file_path> updated."
"""

GIT_PUBLISHER_PROMPT = """You are the Git Publisher.
You run after the Protocol Editor confirms the file is written.

Steps:
1. Call commit_and_pr with:
   - new_branch: a short kebab-case name like "protocol/<brief-description>"
   - commit_msg: a one-line message describing what changed
   - pr_title:   a clear title for the GitHub PR
   - pr_body:    2-3 sentence summary of the change and why

When the PR is created, say exactly: DONE: <pr_url>
"""

MANAGER_PROMPT = """You are the workflow coordinator for a rehabilitation protocol update pipeline.

Route messages in this strict order:
  1. repo_reader   — reads the repository
  2. protocol_editor — writes the updated protocol file
  3. git_publisher — commits changes and opens the PR

Never skip a step. Never repeat a step. Stop after git_publisher says "DONE:".
"""


class AG2AgentError(RuntimeError):
    """Raised when the AG2 invocation cannot complete."""


class AG2Agent(CodingAgent):
    """Multi-agent coding pipeline powered by AG2 GroupChat + Claude."""

    name = "ag2"

    def __init__(self) -> None:
        self._invocations: dict[str, dict] = {}

    def _llm_config(self, temperature: float = 0.0) -> dict:
        api_key = os.getenv("ANTHROPIC_API_KEY")
        if not api_key:
            raise AG2AgentError("ANTHROPIC_API_KEY not set")
        return {
            "config_list": [
                {
                    "model": "claude-opus-4-6",
                    "api_key": api_key,
                    "api_type": "anthropic",
                }
            ],
            "temperature": temperature,
            "timeout": 180,
            "cache_seed": None,
        }

    def _run_invocation(
        self,
        request: InvocationRequest,
        events: list[TraceEvent],
    ) -> dict:
        """Synchronous AG2 GroupChat run. Called via run_in_executor."""
        workdir = tempfile.mkdtemp(prefix="rehab-ag2-")
        pr_url: str | None = None
        result_branch: str | None = None

        # ── clone repo ────────────────────────────────────────────────────────
        repo_url = (
            request.repo
            if request.repo.startswith("http")
            else f"https://github.com/{request.repo}"
        )
        gh_token = os.getenv("GITHUB_TOKEN") or os.getenv("GH_TOKEN", "")
        auth_url = (
            repo_url.replace("https://github.com/", f"https://{gh_token}@github.com/")
            if gh_token and repo_url.startswith("https://github.com/")
            else repo_url
        )

        clone = subprocess.run(
            ["git", "clone", "--depth", "1", auth_url, workdir],
            capture_output=True, text=True, timeout=120,
        )
        if clone.returncode != 0:
            raise AG2AgentError(f"git clone failed: {clone.stderr.strip()}")

        events.append(TraceEvent(
            type="agent_started",
            timestamp=time.monotonic(),
            label=f"cloned {request.repo}",
            payload={"repo": request.repo},
        ))

        for path, content in (request.context_files or {}).items():
            fp = Path(workdir) / path
            fp.parent.mkdir(parents=True, exist_ok=True)
            fp.write_text(content)

        # ── tool implementations ──────────────────────────────────────────────

        def list_files(pattern: str) -> str:
            """List repo files matching a glob pattern (e.g. '**/*.yaml')."""
            found = [str(p.relative_to(workdir)) for p in Path(workdir).glob(pattern)]
            return "\n".join(found) if found else "(no files matched)"

        def read_file(file_path: str) -> str:
            """Read a file from the cloned repo by its relative path."""
            fp = Path(workdir) / file_path
            if not fp.exists():
                return f"ERROR: {file_path} not found"
            return fp.read_text()

        def write_file(file_path: str, content: str) -> str:
            """Write content to a file in the cloned repo."""
            fp = Path(workdir) / file_path
            fp.parent.mkdir(parents=True, exist_ok=True)
            fp.write_text(content)
            events.append(TraceEvent(
                type="file_edit",
                timestamp=time.monotonic(),
                label=f"wrote {file_path}",
                payload={"path": file_path},
            ))
            return f"OK: wrote {file_path}"

        def commit_and_pr(
            new_branch: str,
            commit_msg: str,
            pr_title: str,
            pr_body: str,
        ) -> str:
            """Create a branch, commit all changes, push, and open a GitHub PR."""
            nonlocal pr_url, result_branch
            result_branch = new_branch

            for cmd in [
                ["git", "checkout", "-b", new_branch],
                ["git", "add", "-A"],
                ["git", "commit", "-m", commit_msg],
                ["git", "push", "-u", "origin", new_branch],
            ]:
                r = subprocess.run(cmd, cwd=workdir, capture_output=True, text=True, timeout=60)
                if r.returncode != 0:
                    return f"ERROR during '{cmd[1]}': {r.stderr.strip()}"

            events.append(TraceEvent(
                type="commit_created",
                timestamp=time.monotonic(),
                label=commit_msg,
                payload={},
            ))

            pr_r = subprocess.run(
                ["gh", "pr", "create", "--title", pr_title, "--body", pr_body, "--head", new_branch],
                cwd=workdir, capture_output=True, text=True, timeout=60,
            )
            if pr_r.returncode != 0:
                return f"ERROR creating PR: {pr_r.stderr.strip()}"

            pr_url = pr_r.stdout.strip()
            events.append(TraceEvent(
                type="pr_opened",
                timestamp=time.monotonic(),
                label=f"PR opened: {pr_url}",
                payload={"pr_url": pr_url},
            ))
            return f"OK: PR created at {pr_url}"

        # ── build agents ──────────────────────────────────────────────────────

        executor = autogen.UserProxyAgent(
            name="executor",
            human_input_mode="NEVER",
            max_consecutive_auto_reply=20,
            code_execution_config=False,
            is_termination_msg=lambda x: "DONE:" in (x.get("content") or ""),
        )

        repo_reader = autogen.AssistantAgent(
            name="repo_reader",
            llm_config=self._llm_config(),
            system_message=REPO_READER_PROMPT,
        )

        protocol_editor = autogen.AssistantAgent(
            name="protocol_editor",
            llm_config=self._llm_config(),
            system_message=PROTOCOL_EDITOR_PROMPT,
        )

        git_publisher = autogen.AssistantAgent(
            name="git_publisher",
            llm_config=self._llm_config(),
            system_message=GIT_PUBLISHER_PROMPT,
        )

        # Each tool is registered only on the agent that should call it
        autogen.register_function(
            list_files,
            caller=repo_reader, executor=executor,
            name="list_files",
            description="List repo files matching a glob pattern (e.g. '**/*.yaml')",
        )
        autogen.register_function(
            read_file,
            caller=repo_reader, executor=executor,
            name="read_file",
            description="Read a file from the cloned repo by its relative path",
        )
        autogen.register_function(
            write_file,
            caller=protocol_editor, executor=executor,
            name="write_file",
            description="Write content to a file in the cloned repo",
        )
        autogen.register_function(
            commit_and_pr,
            caller=git_publisher, executor=executor,
            name="commit_and_pr",
            description="Create a branch, commit all changes, push, and open a GitHub PR",
        )

        # ── GroupChat ─────────────────────────────────────────────────────────

        groupchat = autogen.GroupChat(
            agents=[executor, repo_reader, protocol_editor, git_publisher],
            messages=[],
            max_round=25,
            speaker_selection_method="auto",
        )
        manager = autogen.GroupChatManager(
            groupchat=groupchat,
            llm_config=self._llm_config(),
            system_message=MANAGER_PROMPT,
        )

        events.append(TraceEvent(
            type="agent_started",
            timestamp=time.monotonic(),
            label="GroupChat started (reader → editor → publisher)",
            payload={},
        ))

        executor.initiate_chat(manager, message=request.prompt)

        events.append(TraceEvent(
            type="agent_completed",
            timestamp=time.monotonic(),
            label="GroupChat completed",
            payload={"pr_url": pr_url},
        ))

        return {"pr_url": pr_url, "branch": result_branch}

    async def invoke(self, request: InvocationRequest) -> AgentInvocation:
        if not os.getenv("ANTHROPIC_API_KEY"):
            raise AG2AgentError("ANTHROPIC_API_KEY not set")

        events: list[TraceEvent] = []
        loop = asyncio.get_event_loop()
        result = await loop.run_in_executor(
            None, self._run_invocation, request, events
        )

        inv_id = str(uuid.uuid4())
        self._invocations[inv_id] = {"events": events, **result}

        return AgentInvocation(
            invocation_id=inv_id,
            pr_url=result["pr_url"],
            branch=result["branch"],
            artifacts=(
                [{"type": "pr", "url": result["pr_url"]}] if result["pr_url"] else []
            ),
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
            delay = start + (event.timestamp - first_ts) - time.monotonic()
            if delay > 0:
                await asyncio.sleep(delay)
            yield event
