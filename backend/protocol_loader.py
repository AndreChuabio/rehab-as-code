"""
Loader + writer for the rehab protocol.

Two read paths, switched by the PROTOCOL_SOURCE env var:

  PROTOCOL_SOURCE=github   (default, legacy)
      Fetch protocols/protocol.yaml from GitHub via the contents API.
      Single-tenant: every caller sees the same protocol regardless of
      who's asking. The repo IS the message bus.

  PROTOCOL_SOURCE=supabase (Phase-1+ target)
      Query the `protocols` table for the row WHERE token=$1 AND
      status='active'. Per-patient. Set this once the read path has
      been live behind the flag for a release cycle and the backfill
      has been run for every existing user.

Either path falls back to a local stub if it fails. Callers in code
paths that have a Supabase JWT (and therefore a user_id) should call
fetch_protocol_for_user(token); endpoints without auth keep calling
fetch_protocol().
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


def _protocol_source() -> str:
    """Read PROTOCOL_SOURCE each call so tests / runtime flips take effect."""
    return os.getenv("PROTOCOL_SOURCE", "github").strip().lower() or "github"


def _raw_url(repo: str, branch: str, path: str) -> str:
    return f"https://raw.githubusercontent.com/{repo}/{branch}/{path}"


def _in_subdir(path: str) -> str:
    """Prefix a path with the protocol subdir. Safe to call with absolute paths."""
    if path.startswith(f"{PROTOCOL_SUBDIR}/"):
        return path
    return f"{PROTOCOL_SUBDIR}/{path}" if PROTOCOL_SUBDIR else path


def fetch_protocol(branch: str = DEFAULT_BRANCH) -> dict:
    """Fetch the current protocol from GitHub. Single-tenant, no user context.

    Always queries GitHub regardless of PROTOCOL_SOURCE — this function is
    the legacy path used by unauthenticated endpoints (`/protocol`,
    `/protocol/exercises` when called without a JWT). Endpoints that have
    a user_id should call fetch_protocol_for_user() instead, which respects
    the PROTOCOL_SOURCE flag.

    Uses the GitHub contents API instead of raw.githubusercontent.com because
    the latter has a CDN cache (~5min TTL) — fatal for the demo workflow chain
    where each approve flips main and the next /protocol call must see fresh
    state. The API returns the latest commit's content immediately.

    Falls back to a local stub if the repo isn't reachable yet.
    """
    return _fetch_from_github(branch)


def fetch_protocol_for_user(token: str, branch: str = DEFAULT_BRANCH) -> dict:
    """Fetch the active protocol for a specific patient.

    When PROTOCOL_SOURCE=supabase, queries the `protocols` table for the
    row where token=$1 AND status='active'. Falls back to the GitHub fetch
    if the patient has no active row yet (e.g., new user pre-backfill) so
    the demo doesn't break mid-rollout.

    When PROTOCOL_SOURCE=github (default), behaves identically to
    fetch_protocol() — ignores the token and reads from the repo.

    A live release cycle on PROTOCOL_SOURCE=supabase, with backfill run
    for every existing patient, is the gate to dropping the GitHub path
    entirely (Phase E of the migration plan).
    """
    source = _protocol_source()
    if source == "supabase" and token:
        payload = _fetch_active_from_supabase(token)
        if payload is not None:
            return payload
        logger.info(
            "no active supabase protocol for token=%s; falling back to github read",
            token,
        )
    return _fetch_from_github(branch)


def _fetch_from_github(branch: str) -> dict:
    api_url = (
        f"https://api.github.com/repos/{PROTOCOL_REPO}/contents/"
        f"{_in_subdir('protocol.yaml')}?ref={branch}"
    )
    headers = {"Accept": "application/vnd.github.v3.raw"}
    gh_token = os.getenv("GITHUB_TOKEN") or os.getenv("GH_TOKEN")
    if gh_token:
        headers["Authorization"] = f"Bearer {gh_token}"
    try:
        r = httpx.get(api_url, headers=headers, timeout=10.0)
        r.raise_for_status()
        import yaml
        return yaml.safe_load(r.text)
    except Exception as exc:
        logger.warning("protocol fetch failed (%s); using local stub", exc)
        return _stub_protocol()


def _fetch_active_from_supabase(token: str) -> dict | None:
    """Return the JSONB payload of the patient's active protocol, or None.

    None signals "no row" — the caller decides whether to fall back to
    GitHub or surface the absence. Hard errors (DB unreachable, malformed
    payload) are logged and treated as None so the request path keeps
    working under the flag.
    """
    dsn = os.getenv("DATABASE_URL", "").strip()
    if not dsn:
        logger.warning(
            "PROTOCOL_SOURCE=supabase but DATABASE_URL not set; falling back"
        )
        return None
    try:
        import psycopg
        from psycopg.rows import dict_row
    except ImportError:
        logger.warning("psycopg not installed; supabase read path disabled")
        return None
    try:
        with psycopg.connect(dsn, row_factory=dict_row, autocommit=True) as conn, \
                conn.cursor() as cur:
            cur.execute(
                "SELECT payload FROM protocols "
                "WHERE token = %s AND status = 'active' "
                "LIMIT 1",
                (token,),
            )
            row = cur.fetchone()
    except Exception as exc:
        logger.exception("supabase protocol fetch failed: %s", exc)
        return None
    if not row:
        return None
    payload = row["payload"]
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except json.JSONDecodeError as exc:
            logger.warning("active protocol payload not valid JSON: %s", exc)
            return None
    return payload


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
