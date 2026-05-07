"""Manual smoke runner for PR-C sub-agents.

Not a pytest - intentionally outside the normal test suite. Run with:
    python backend/tests/manual_smoke_pr_c.py

It hits real Anthropic with a stub patient context and prints sample
outputs from each sub-agent. Use for the PR body sample-output section
and to sanity-check end-to-end behavior pre-merge. Do NOT enable in CI.

DELETES no data and writes nothing to Supabase - protocol_repo.save_pending
is monkey-patched out so we never persist a stub draft.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

# Make backend/ importable when run from repo root.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent.parent.parent / ".env")


def main() -> None:
    if not os.getenv("ANTHROPIC_API_KEY"):
        print("ANTHROPIC_API_KEY not set; refusing to run smoke.")
        return

    intake = {
        "name": "Smoke Patient",
        "injury_type": "shoulder",
        "phase": "acute",
        "week": 1,
        "surgery_date": None,
        "pain_at_rest": 4,
    }

    print("=" * 60)
    print("RESEARCHER")
    print("=" * 60)
    from agents.researcher import candidates
    rs = candidates(
        injury_type="shoulder",
        phase="acute",
        week=1,
        intake=intake,
        token="smoke-token",
    )
    print(json.dumps(rs, indent=2)[:1500])

    print()
    print("=" * 60)
    print("TREND ANALYST (insufficient data path)")
    print("=" * 60)
    from agents.trend_analyst import analyze
    ta = analyze(token="smoke-token", checkins=[], sessions=[])
    print(ta)

    # Manufactured 8 weeks of pain data so we can hit the live model.
    from datetime import datetime, timedelta, timezone
    now = datetime.now(timezone.utc)
    fake_checkins = [
        {
            "kind": "checkin",
            "pain_level": max(0, 5 - i // 4),
            "recovery_score": 60 + i,
            "recorded_at": (now - timedelta(days=21 - i)).isoformat(),
        }
        for i in range(20)
    ]
    print()
    print("=" * 60)
    print("TREND ANALYST (real call)")
    print("=" * 60)
    ta = analyze(token="smoke-token", checkins=fake_checkins, sessions=[], intake=intake)
    print(json.dumps(ta, indent=2))

    print()
    print("=" * 60)
    print("EVALUATOR")
    print("=" * 60)
    from agents.evaluator import signal
    sig = signal(
        intake=intake,
        health={"hrv": 55, "recovery_score": 70, "sleep_score": 72},
        history=fake_checkins,
        trend_summary=ta,
        token="smoke-token",
    )
    print(json.dumps(sig, indent=2))

    print()
    print("=" * 60)
    print("PLANNER")
    print("=" * 60)
    from agents.planner import compose
    draft = compose(
        candidates=rs,
        signal=sig,
        intake=intake,
        phase="acute",
        week=1,
        token="smoke-token",
    )
    print(json.dumps(draft, indent=2)[:2500])

    print()
    print("=" * 60)
    print("SAFETY REVIEWER")
    print("=" * 60)
    from agents.safety_reviewer import review
    verdict = review(draft=draft, intake=intake, trend_summary=ta, token="smoke-token")
    print(json.dumps(verdict, indent=2))


if __name__ == "__main__":
    asyncio.run(main()) if False else main()
