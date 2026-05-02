"""
Capture a live orchestrated run from the Cursor SDK and write it into
backend/cached_runs/{flow}.json for use by CachedReplayAgent.

Why:
    The demo uses CachedReplayAgent for deterministic timing. But we want
    those cached traces to reflect what the real parent + sub-agent
    orchestrator actually does — not hand-authored events. This script is
    the one-shot that runs the live orchestrator once and pins its output.

Prereqs:
    1. CURSOR_API_KEY set (sponsor-table key).
    2. orchestrator/ npm-installed (see orchestrator/README.md).
    3. PROTOCOL_REPO exists on GitHub with protocol.yaml, protocol-library/,
       .cursorrules committed.

Usage:
    cd backend
    python3 -m scripts.capture_run weekly_plan
    python3 -m scripts.capture_run symptom_adjustment
    python3 -m scripts.capture_run intake
    python3 -m scripts.capture_run checkin

    # overwrite all four in one go
    python3 -m scripts.capture_run --all

Output:
    backend/cached_runs/{flow}.json (overwrites prior hand-authored version).
"""

from __future__ import annotations

import argparse
import asyncio
import dataclasses
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# Load .env from the project root so CURSOR_API_KEY / PROTOCOL_REPO are
# available without having to export them manually in the shell.
try:
    from dotenv import load_dotenv
    _project_root = Path(__file__).resolve().parent.parent.parent
    load_dotenv(_project_root / ".env", override=False)
except ImportError:
    pass

from agents.cursor_sdk import CursorSdkAgent  # noqa: E402
from agents.base import InvocationRequest  # noqa: E402
from protocol_loader import write_context_files  # noqa: E402


FLOWS = ["weekly_plan", "symptom_adjustment", "intake", "checkin"]

SAMPLE_PROMPTS = {
    "weekly_plan": (
        "Wearables snapshot: HRV trend 32 -> 41 ms (recovered), "
        "sleep_score 78. Generate next week's progression."
    ),
    "symptom_adjustment": (
        "Patient mid-session report: 'knee felt tweaky on single-leg squats "
        "yesterday.' Adjust protocol.yaml minimally."
    ),
    "intake": (
        "New intake: Andre, 26, ACL reconstruction 3 weeks ago, mild pain "
        "at 110 flexion. Initialize protocol.yaml from post-acl-week-3."
    ),
    "checkin": (
        "Today's check-in: hit 3x10 heel slides, quad set felt stronger "
        "than yesterday. Pain 0/10. Append to log.yaml."
    ),
}


async def capture(flow: str, out_path: Path, repo: str) -> None:
    """Run the live orchestrator for one flow; write its trace to disk."""
    print(f"[{flow}] capturing (repo={repo}) ...", file=sys.stderr)

    agent = CursorSdkAgent()
    context = write_context_files(
        flow=flow,
        wearables={"sleep_score": 78, "hrv_ms": 41, "recovery_score": 82},
        symptom_log=SAMPLE_PROMPTS[flow],
    )
    request = InvocationRequest(
        repo=repo,
        prompt=SAMPLE_PROMPTS[flow],
        context_files=context,
        flow=flow,  # type: ignore[arg-type]
    )

    invocation = await agent.invoke(request)

    # stream_trace returns all events with original pacing; dump them raw
    events: list[dict] = []
    async for evt in agent.stream_trace(invocation.invocation_id):
        events.append(dataclasses.asdict(evt))

    payload = {
        "flow": flow,
        "pr_url": invocation.pr_url,
        "branch": invocation.branch,
        "artifacts": invocation.artifacts,
        "events": events,
    }

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(payload, indent=2) + "\n")
    print(
        f"[{flow}] wrote {len(events)} events -> {out_path.name} "
        f"(PR: {invocation.pr_url})",
        file=sys.stderr,
    )


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("flow", nargs="?", choices=FLOWS, help="flow to capture")
    parser.add_argument("--all", action="store_true", help="capture all 4 flows")
    parser.add_argument(
        "--out-dir",
        default=str(Path(__file__).resolve().parent.parent / "cached_runs"),
        help="output directory (default: backend/cached_runs)",
    )
    parser.add_argument(
        "--repo",
        default=os.getenv("PROTOCOL_REPO", "AndreChuabio/rehab-protocols-andre"),
        help="target repo (owner/name)",
    )
    args = parser.parse_args()

    if not args.all and not args.flow:
        parser.error("pass a flow name or --all")

    if not os.getenv("CURSOR_API_KEY"):
        print("ERROR: CURSOR_API_KEY is not set.", file=sys.stderr)
        return 1

    out_dir = Path(args.out_dir)
    flows = FLOWS if args.all else [args.flow]  # type: ignore[list-item]

    for flow in flows:
        try:
            await capture(flow, out_dir / f"{flow}.json", args.repo)
        except Exception as exc:
            print(f"[{flow}] FAILED: {exc}", file=sys.stderr)
            if not args.all:
                return 2
            # when --all, keep going and refresh whatever succeeds

    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
