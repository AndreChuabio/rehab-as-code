"""
trend_analyst.py - Phase C: longitudinal pattern detection over 4-8 weeks.

Aggregates the patient's check-in history, protocol versions, and
completed sessions over the last 4-8 weeks (server-side, before
prompting), then asks Sonnet 4.6 to classify the trajectory:

    plateau       - metrics stable, no clear direction
    breakthrough  - sustained improvement (recovery up, pain down, completion up)
    regression    - sustained decline (pain up, missed sessions, recovery down)
    steady        - normal week-to-week variation, no signal

The aggregation is the key: we send the model numerical summaries
(7-day moving averages, rolling pain trend, completion rates) rather
than raw rows. This keeps the prompt tight and the model's reasoning
focused on patterns, not on parsing checkin schemas.

Output is consumed by EvaluatorAgent (whose prompt incorporates
trend_summary) and exposed in logs / future clinician views.

No silent fallbacks: any Anthropic error raises TrendAnalystError. The
orchestrator runs the trend analyst in parallel with the researcher; if
it fails the orchestrator can choose to proceed with an empty trend
summary (evaluator handles it gracefully) or surface the error - we
return the typed exception so the caller decides.
"""
from __future__ import annotations

import json
import logging
import os
import time
from datetime import datetime, timedelta, timezone
from typing import Any

logger = logging.getLogger(__name__)


_DEFAULT_MODEL = "claude-sonnet-4-6"


class TrendAnalystError(RuntimeError):
    """Raised when the trend analyst cannot produce a pattern."""


_SYSTEM_PROMPT = (
    "You are a rehabilitation trend analyst. You receive pre-aggregated "
    "longitudinal numerical summaries spanning 4-8 weeks of a patient's "
    "rehab journey (rolling pain, recovery score, sleep, completion rate, "
    "missed sessions). Classify the trajectory into one of:\n\n"
    "  plateau      - metrics held steady at a level the patient has not "
    "                 progressed past.\n"
    "  breakthrough - sustained improvement across two or more axes.\n"
    "  regression   - sustained decline across two or more axes.\n"
    "  steady      - normal week-to-week noise; no actionable signal.\n\n"
    "Cite specific numbers in `evidence`. Keep `implication_for_next_week` "
    "to one short clinical sentence the evaluator agent can act on. Output "
    "only via the propose_pattern tool."
)


_TOOL = {
    "name": "propose_pattern",
    "description": "Submit the longitudinal pattern classification.",
    "input_schema": {
        "type": "object",
        "properties": {
            "pattern": {
                "type": "string",
                "enum": ["plateau", "breakthrough", "regression", "steady"],
            },
            "evidence": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "Specific numerical observations that support the "
                    "pattern. Each item is one short sentence."
                ),
            },
            "implication_for_next_week": {
                "type": "string",
                "description": (
                    "One sentence for the evaluator: what this trend "
                    "implies about the next prescription."
                ),
            },
        },
        "required": ["pattern", "evidence", "implication_for_next_week"],
    },
}


def _model() -> str:
    return os.getenv("TREND_ANALYST_MODEL", _DEFAULT_MODEL)


def _round_avg(values: list[float | int]) -> float | None:
    if not values:
        return None
    return round(sum(values) / len(values), 2)


def _aggregate(
    checkins: list[dict[str, Any]] | None,
    sessions: list[dict[str, Any]] | None,
    weeks: int,
) -> dict[str, Any]:
    """Produce a numerical snapshot from raw history.

    The shapes mirror what user_store.get_session_history and
    session_repo.list_recent return. Aggregation is bucketed by ISO
    week so the model sees a clean rolling series, not raw timestamps.
    """
    now = datetime.now(timezone.utc)
    window_start = now - timedelta(weeks=weeks)

    pain_levels: list[int] = []
    recovery_scores: list[int] = []
    sleep_scores: list[int] = []

    weekly_pain: dict[str, list[int]] = {}
    weekly_recovery: dict[str, list[int]] = {}

    for entry in checkins or []:
        ts = _parse_ts(entry.get("recorded_at"))
        if ts is None or ts < window_start:
            continue
        wk = ts.strftime("%G-W%V")
        if (p := entry.get("pain_level")) is not None:
            pain_levels.append(p)
            weekly_pain.setdefault(wk, []).append(p)
        # Some checkin payloads embed wearable metrics inline.
        for k, target, weekly in (
            ("recovery_score", recovery_scores, weekly_recovery),
            ("sleep_score", sleep_scores, None),
        ):
            v = entry.get(k)
            if isinstance(v, (int, float)):
                target.append(int(v))
                if weekly is not None:
                    weekly.setdefault(wk, []).append(int(v))

    completed = 0
    planned = 0
    skipped = 0
    in_progress = 0
    for s in sessions or []:
        ts = _parse_ts(s.get("created_at"))
        if ts is None or ts < window_start:
            continue
        st = s.get("status")
        if st == "completed":
            completed += 1
        elif st == "planned":
            planned += 1
        elif st == "skipped":
            skipped += 1
        elif st == "in_progress":
            in_progress += 1

    total_sessions = completed + planned + skipped + in_progress
    completion_rate = round(completed / total_sessions, 2) if total_sessions else None

    weekly_pain_series = [
        {"week": wk, "avg_pain": _round_avg(vals)}
        for wk, vals in sorted(weekly_pain.items())
    ]
    weekly_recovery_series = [
        {"week": wk, "avg_recovery": _round_avg(vals)}
        for wk, vals in sorted(weekly_recovery.items())
    ]

    return {
        "window_weeks": weeks,
        "pain": {
            "n": len(pain_levels),
            "avg": _round_avg([float(p) for p in pain_levels]),
            "min": min(pain_levels) if pain_levels else None,
            "max": max(pain_levels) if pain_levels else None,
            "weekly": weekly_pain_series,
        },
        "recovery": {
            "n": len(recovery_scores),
            "avg": _round_avg([float(p) for p in recovery_scores]),
            "weekly": weekly_recovery_series,
        },
        "sleep": {
            "n": len(sleep_scores),
            "avg_7d": _round_avg([float(p) for p in sleep_scores[-7:]]),
        },
        "sessions": {
            "completed": completed,
            "planned": planned,
            "in_progress": in_progress,
            "skipped": skipped,
            "completion_rate": completion_rate,
        },
    }


def _parse_ts(value: Any) -> datetime | None:
    if not value:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, str):
        try:
            ts = datetime.fromisoformat(value.replace("Z", "+00:00"))
            return ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)
        except ValueError:
            return None
    return None


def _has_enough_history(agg: dict[str, Any]) -> bool:
    """Heuristic: at least 4 pain check-ins OR 4 completed sessions.

    Below that threshold the model can't say anything meaningful and
    we'd rather skip the call than produce noise. The evaluator handles
    a None trend_summary fine.
    """
    return (
        (agg.get("pain") or {}).get("n", 0) >= 4
        or (agg.get("sessions") or {}).get("completed", 0) >= 4
    )


def _summarize_trend(result: dict[str, Any] | None) -> dict[str, Any]:
    """PHI-safe summary: pattern label + numeric deltas only."""
    if not isinstance(result, dict):
        return {"_skipped": True}
    return {
        "pattern": result.get("pattern"),
        "data_completeness": result.get("data_completeness"),
        "n_checkins": (result.get("counts") or {}).get("checkins"),
        "n_sessions": (result.get("counts") or {}).get("sessions"),
    }


def _decision_from_trend(result: dict[str, Any] | None) -> str | None:
    return (result or {}).get("pattern") if isinstance(result, dict) else None


from observability import trace_sync


@trace_sync(
    "trend_analyst",
    model="claude-sonnet-4-6",
    summarize=_summarize_trend,
    decision_from=_decision_from_trend,
)
def analyze(
    *,
    token: str,
    checkins: list[dict[str, Any]] | None = None,
    sessions: list[dict[str, Any]] | None = None,
    intake: dict[str, Any] | None = None,
    weeks: int = 4,
) -> dict[str, Any] | None:
    """Classify the patient's longitudinal pattern.

    Parameters
    ----------
    token : str
        Patient auth.uid(). For logging only.
    checkins : list[dict] | None
        Recent symptom / pain check-ins. Same shape as
        user_store.get_session_history (mixed kinds; the aggregator
        filters on pain_level presence).
    sessions : list[dict] | None
        Recent rows from session_repo.list_recent.
    intake : dict | None
        Optional baseline so the model can frame the trend (e.g.
        post-op week 6 plateau is different from chronic-LBP plateau).
    weeks : int
        Lookback window in ISO weeks. 4-8 is the sweet spot.

    Returns
    -------
    dict | None
        {"pattern": ..., "evidence": [...], "implication_for_next_week": ...}
        when there is enough data and the model returns a valid response.
        None when there is insufficient history (orchestrator passes None
        to the evaluator, which handles it).

    Raises
    ------
    TrendAnalystError
        On Anthropic API failures, missing API key, or malformed output.
    """
    api_key = os.getenv("ANTHROPIC_API_KEY", "").strip()
    if not api_key:
        raise TrendAnalystError("ANTHROPIC_API_KEY is not configured")

    agg = _aggregate(checkins, sessions, weeks)
    if not _has_enough_history(agg):
        logger.info(
            "trend_analyst: insufficient history token=%s pain_n=%s sessions_completed=%s",
            token,
            (agg.get("pain") or {}).get("n"),
            (agg.get("sessions") or {}).get("completed"),
        )
        return None

    try:
        import anthropic
    except ImportError as exc:
        raise TrendAnalystError(f"anthropic SDK not installed: {exc}") from exc

    client = anthropic.Anthropic(api_key=api_key)
    parts: list[str] = [
        "Aggregated longitudinal data:\n" + json.dumps(agg, indent=2, default=str),
    ]
    if intake:
        parts.append("Patient intake (baseline):\n" + json.dumps(intake, indent=2, default=str))
    parts.append("Classify the pattern via propose_pattern.")
    prompt = "\n\n".join(parts)

    started = time.monotonic()
    try:
        resp = client.messages.create(
            model=_model(),
            max_tokens=600,
            system=_SYSTEM_PROMPT,
            tools=[_TOOL],
            tool_choice={"type": "tool", "name": "propose_pattern"},
            messages=[{"role": "user", "content": prompt}],
        )
    except Exception as exc:
        elapsed_ms = int((time.monotonic() - started) * 1000)
        logger.warning(
            "trend_analyst anthropic call failed in %dms token=%s: %s",
            elapsed_ms, token, exc,
        )
        raise TrendAnalystError(f"trend analyst unavailable: {exc}") from exc

    elapsed_ms = int((time.monotonic() - started) * 1000)
    usage = getattr(resp, "usage", None)
    in_tokens = getattr(usage, "input_tokens", None) if usage else None
    out_tokens = getattr(usage, "output_tokens", None) if usage else None

    for block in resp.content or []:
        if (
            getattr(block, "type", None) == "tool_use"
            and getattr(block, "name", "") == "propose_pattern"
        ):
            data = dict(block.input or {})
            pattern = data.get("pattern")
            if pattern not in ("plateau", "breakthrough", "regression", "steady"):
                raise TrendAnalystError(
                    f"trend analyst returned invalid pattern: {pattern!r}"
                )
            out = {
                "pattern": pattern,
                "evidence": list(data.get("evidence") or []),
                "implication_for_next_week": data.get("implication_for_next_week", ""),
            }
            logger.info(
                "trend_analyst ok in %dms in_tokens=%s out_tokens=%s "
                "pattern=%s token=%s",
                elapsed_ms, in_tokens, out_tokens, pattern, token,
            )
            return out

    raise TrendAnalystError("trend analyst returned no propose_pattern tool call")
