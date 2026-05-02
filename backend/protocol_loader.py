"""
Loader + writer for the protocols/ subdirectory of the rehab-as-code repo.

The repo is the message bus between backend and Cursor cloud agents:
  - Backend WRITES context files (wearables snapshot, symptom log) before
    invoking an agent.
  - Backend READS the latest protocol.yaml after the agent's PR is merged
    (or directly from the PR branch for preview).

Anything Cursor needs has to be in the repo when the agent clones it; the
agent has no other channel back to us.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path

import httpx

logger = logging.getLogger(__name__)

PROTOCOL_REPO = os.getenv("PROTOCOL_REPO", "AndreChuabio/rehab-as-code")
DEFAULT_BRANCH = os.getenv("PROTOCOL_BRANCH", "main")
PROTOCOL_SUBDIR = os.getenv("PROTOCOL_SUBDIR", "protocols")


def _raw_url(repo: str, branch: str, path: str) -> str:
    return f"https://raw.githubusercontent.com/{repo}/{branch}/{path}"


def _in_subdir(path: str) -> str:
    """Prefix a path with the protocol subdir. Safe to call with absolute paths."""
    if path.startswith(f"{PROTOCOL_SUBDIR}/"):
        return path
    return f"{PROTOCOL_SUBDIR}/{path}" if PROTOCOL_SUBDIR else path


def fetch_protocol(branch: str = DEFAULT_BRANCH) -> dict:
    """Fetch the current protocol.yaml from GitHub, return parsed dict.

    Uses the GitHub contents API instead of raw.githubusercontent.com because
    the latter has a CDN cache (~5min TTL) — fatal for the demo workflow chain
    where each approve flips main and the next /protocol call must see fresh
    state. The API returns the latest commit's content immediately.

    Falls back to a local stub if the repo isn't reachable yet.
    """
    api_url = (
        f"https://api.github.com/repos/{PROTOCOL_REPO}/contents/"
        f"{_in_subdir('protocol.yaml')}?ref={branch}"
    )
    headers = {"Accept": "application/vnd.github.v3.raw"}
    token = os.getenv("GITHUB_TOKEN") or os.getenv("GH_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    try:
        r = httpx.get(api_url, headers=headers, timeout=10.0)
        r.raise_for_status()
        import yaml
        return yaml.safe_load(r.text)
    except Exception as exc:
        logger.warning("protocol fetch failed (%s); using local stub", exc)
        return _stub_protocol()


def write_context_files(
    flow: str,
    wearables: dict,
    symptom_log: str,
) -> dict[str, str]:
    """Build the dict of {path: content} the backend will push to the protocol
    repo before invoking an agent.

    Returns the mapping (caller decides how to push: gh CLI, API, etc).
    All paths are under PROTOCOL_SUBDIR so the agent's file tool stays
    scoped to the protocols/ area of the repo.
    """
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    suffix = "" if flow == "weekly_plan" else f"-{flow.replace('_', '-')}"
    files = {
        _in_subdir(f"data/wearables-{today}.json"): json.dumps(wearables, indent=2),
        _in_subdir(f"data/symptoms-{today}{suffix}.md"): _format_symptom_log(symptom_log),
    }
    return files


def _format_symptom_log(text: str) -> str:
    ts = datetime.now(timezone.utc).isoformat()
    return f"# Symptom log\n\n_Recorded: {ts}_\n\n{text.strip()}\n"


def _stub_protocol() -> dict:
    """Local fallback when the GitHub repo isn't reachable."""
    return {
        "patient": None,
        "phase": "pending_intake",
        "week": 0,
        "exercises": [],
    }
