"""
Loader + writer for the rehab-protocols-andre repo.

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

PROTOCOL_REPO = os.getenv("PROTOCOL_REPO", "AndreChuabio/rehab-protocols-andre")
DEFAULT_BRANCH = os.getenv("PROTOCOL_BRANCH", "main")


def _raw_url(repo: str, branch: str, path: str) -> str:
    return f"https://raw.githubusercontent.com/{repo}/{branch}/{path}"


def fetch_protocol(branch: str = DEFAULT_BRANCH) -> dict:
    """Fetch the current protocol.yaml from GitHub raw, return parsed dict.

    Falls back to a local stub if the repo isn't reachable yet (e.g., before
    seeding). Keeps the rest of the app working in offline dev.
    """
    url = _raw_url(PROTOCOL_REPO, branch, "protocol.yaml")
    try:
        r = httpx.get(url, timeout=10.0)
        r.raise_for_status()
        # PyYAML import deferred so missing dep doesn't crash app startup
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
    """
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    suffix = "" if flow == "weekly_plan" else f"-{flow.replace('_', '-')}"
    files = {
        f"data/wearables-{today}.json": json.dumps(wearables, indent=2),
        f"data/symptoms-{today}{suffix}.md": _format_symptom_log(symptom_log),
    }
    return files


def _format_symptom_log(text: str) -> str:
    ts = datetime.now(timezone.utc).isoformat()
    return f"# Symptom log\n\n_Recorded: {ts}_\n\n{text.strip()}\n"


def _stub_protocol() -> dict:
    """Local fallback when the GitHub repo isn't reachable."""
    return {
        "patient": "Andre",
        "phase": "post-ACL reconstruction",
        "week": 3,
        "exercises": [
            {
                "name": "quad_sets",
                "sets": 3, "reps": 15,
                "ROM_target_deg": 90,
                "progression_criteria": "pain-free, no swelling",
                "references": ["protocol-library/post-acl-week-3.yaml"],
            },
            {
                "name": "heel_slides",
                "sets": 3, "reps": 10,
                "ROM_target_deg": 100,
                "progression_criteria": "ROM > 100 deg",
                "references": ["protocol-library/post-acl-week-3.yaml"],
            },
        ],
    }
