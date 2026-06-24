"""
context_builder.py - Build the Tavus avatar's persona context for RehabAsCode.

Maps wearable signals + the current rehab protocol into:
  - a factual data block (appended to the Tavus persona's system prompt)
  - a personalized greeting from "Coach Maya, post-op rehab specialist"
  - a list of session focus items the avatar should reference

build_system_prompt takes (health, events, protocol=None, display_name=None).
The protocol is fetched internally via protocol_loader.fetch_protocol() when
not supplied; display_name is resolved by the caller via
user_store.get_display_name and must never be read from protocol.patient
(that field drifts and once leaked "Christian" into a chat for Andre).
"""

import logging
import os

import anthropic

from calendar_fetch import summarize_calendar
from protocol_loader import fetch_protocol

logger = logging.getLogger(__name__)

client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))


def analyze_rehab_signals(health: dict, protocol: dict) -> list[dict]:
    """
    Map wearable signals + current protocol state into rehab session focus items.

    Returns up to 3 ranked focus items the avatar will reference proactively.
    Each item: {priority, category, title, detail}.
    """
    focus: list[dict] = []
    hrv = health.get("hrv_ms", 0)
    hrv_avg = health.get("hrv_7day_avg", 60)
    sleep_score = health.get("sleep_score", 80)
    recovery = health.get("recovery_score", 75)

    week = protocol.get("week", "n/a")
    phase = protocol.get("phase", "post-op recovery")

    # HRV trend gates load progression for tissue recovery
    if hrv < hrv_avg - 8:
        focus.append({
            "priority": "high",
            "category": "load_management",
            "title": "Hold load today",
            "detail": (
                f"HRV is {hrv}ms, well below your 7-day average of {hrv_avg}ms. "
                "Tissue is still under recovery load. Stay at the current "
                "protocol intensity, do not progress."
            ),
        })
    elif hrv >= hrv_avg + 5 and recovery >= 80:
        focus.append({
            "priority": "high",
            "category": "load_management",
            "title": "Cleared to progress",
            "detail": (
                f"HRV up to {hrv}ms (avg {hrv_avg}). Recovery score {recovery}. "
                "Today is a good day to test the next progression in your protocol."
            ),
        })

    # Sleep adequacy for connective tissue remodeling
    if sleep_score < 70:
        focus.append({
            "priority": "high",
            "category": "recovery",
            "title": "Sleep deficit affecting tissue repair",
            "detail": (
                f"Sleep score {sleep_score}/100. Collagen synthesis peaks during "
                "deep sleep — drop intensity 20% today and prioritize a nap."
            ),
        })

    # Default focus tied to current protocol phase
    if not focus:
        focus.append({
            "priority": "medium",
            "category": "session_focus",
            "title": f"Week {week} {phase} session",
            "detail": (
                "Wearable signals look stable. Run the current protocol as written "
                "and log any compensations you feel during single-leg work."
            ),
        })

    return focus[:3]


def _build_context_block(
    health: dict,
    events: list[dict],
    focus: list[dict],
    cal_summary: dict,
    protocol: dict,
    display_name: str | None = None,
) -> str:
    """Format wearable + calendar + protocol state into the Tavus data block.

    Tavus appends this to the persona's existing system prompt; the persona's
    personality is set in the Tavus dashboard. This block is factual data only.
    """
    hrv_delta = health.get("hrv_ms", 0) - health.get("hrv_7day_avg", 60)
    hrv_trend = "below" if hrv_delta < 0 else "above"

    event_lines = "\n".join(
        f"  - {e['time']}: {e['title']} ({e.get('duration_min', 60)} min)"
        + (" [HIGH STAKES]" if e.get("type") == "high_stakes" else "")
        for e in events
    ) or "  (none scheduled)"

    focus_lines = "\n".join(
        f"  {i + 1}. [{f['category'].upper()}] {f['title']}: {f['detail']}"
        for i, f in enumerate(focus)
    )

    exercises = protocol.get("exercises", [])
    exercise_lines = "\n".join(
        f"  - {e.get('name', 'unnamed')}: "
        f"{e.get('sets', '?')}x{e.get('reps', '?')}, "
        f"ROM target {e.get('ROM_target_deg', '?')} deg"
        for e in exercises
    ) or "  (none in current protocol.yaml)"

    trend = health.get("trend", {})
    trend_block = ""
    if trend:
        trend_block = (
            f"\nWeekly trend:\n"
            f"- {trend.get('hrv_trend_summary', '')}\n"
            f"- Sleep score 7-day avg: {trend.get('sleep_score_7day_avg', 'N/A')}/100\n"
            f"- Recovery 7-day avg: {trend.get('recovery_7day_avg', 'N/A')}/100"
        )

    return f"""--- PATIENT REHAB CONTEXT ---

Patient: {display_name or 'the patient'}
Phase: {protocol.get('phase', 'post-op recovery')}
Week: {protocol.get('week', 'n/a')}

Today's wearable metrics:
- Sleep: {health['sleep_hours']} hrs, score {health['sleep_score']}/100
- HRV: {health['hrv_ms']}ms ({hrv_trend} 7-day average of {health['hrv_7day_avg']}ms)
- Resting HR: {health['resting_hr']} bpm
- Recovery score: {health['recovery_score']}/100
- Steps yesterday: {health['steps_yesterday']}{trend_block}

Current protocol exercises (from rehab-protocols-andre/protocol.yaml):
{exercise_lines}

Today's schedule ({cal_summary.get('total_events', 0)} events):
{event_lines}

Session focus items (reference these proactively):
{focus_lines}

BEHAVIOR RULES:
- You are Coach Maya, a rehab specialist. Warm, precise, evidence-cited.
- Refer to the protocol by week and exercise name. Do not invent exercises.
- If the patient reports a symptom, acknowledge it and tell them a draft
  revision will be queued for clinician review; do not modify the protocol
  yourself. A clinician approves the draft on /clinician before it goes active.
- Reference HRV / sleep / recovery numbers when they justify a recommendation.
- Keep responses to 2-3 sentences unless actively guiding an exercise.
- Never decrease ROM target unless the patient explicitly reports pain.

EXERCISE PACING RULES:
- When guiding holds or counts, say each number slowly with a full second pause.
- Match the energy of a careful clinician supervising form work.
--- END REHAB CONTEXT ---"""


def build_system_prompt(
    health: dict,
    events: list[dict],
    protocol: dict | None = None,
    display_name: str | None = None,
) -> dict:
    """
    Build the Tavus conversational_context block + a Claude-generated greeting.

    Originally signed `(health, events)` for the wellness-coach scaffold; PR-P
    added an optional `protocol` kwarg so the caller can pass a per-user
    protocol resolved via fetch_protocol_for_user(user_id). When omitted we
    fall back to the legacy single-tenant fetch_protocol() so older callers
    keep working.

    `display_name` is the patient's name resolved fresh from Supabase by the
    caller (user_store.get_display_name). It MUST be threaded in here instead
    of reading protocol.patient — that field drifts between account runs and
    once leaked "Christian" into a chat for Andre. When None the prompt and
    greeting address the patient generically rather than inventing a name.
    """
    cal_summary = summarize_calendar(events)
    if protocol is None:
        protocol = fetch_protocol()
    focus = analyze_rehab_signals(health, protocol)
    context_block = _build_context_block(
        health, events, focus, cal_summary, protocol, display_name=display_name)

    if not os.getenv("ANTHROPIC_API_KEY"):
        logger.warning("no ANTHROPIC_API_KEY; using fallback greeting")
        return _fallback_context(
            health, focus, context_block, protocol, display_name=display_name)

    greeting_prompt = _greeting_prompt(
        health, focus, protocol, display_name=display_name)
    response = client.messages.create(
        model="claude-haiku-4-5",
        max_tokens=200,
        messages=[{"role": "user", "content": greeting_prompt}],
    )
    greeting = response.content[0].text.strip()

    return {
        "system_prompt": context_block,
        "greeting": greeting,
        "recommendations": focus,
    }


def _greeting_prompt(
    health: dict, focus: list[dict], protocol: dict, display_name: str | None = None
) -> str:
    hrv_delta = health.get("hrv_ms", 0) - health.get("hrv_7day_avg", 60)
    hrv_trend = "below" if hrv_delta < 0 else "above"
    top_focus = focus[0]["title"] if focus else "running the current protocol"
    return f"""Write a 2-3 sentence spoken greeting for Coach Maya, a rehab specialist AI avatar.

Coach Maya is warm, precise, evidence-cited. She knows the patient's wearable data and current rehab protocol.

Patient state:
- Name: {display_name or 'the patient'}
- Phase: {protocol.get('phase', 'post-op recovery')}, week {protocol.get('week', 'n/a')}
- Sleep last night: {health['sleep_hours']}h (score {health['sleep_score']}/100)
- HRV: {health['hrv_ms']}ms ({hrv_trend} 7-day average)
- Recovery: {health['recovery_score']}/100

Today's session focus: {top_focus}

Write a greeting that:
- Opens warmly without sounding like a chatbot intro
- Names the patient and the rehab phase/week specifically
- Mentions one wearable observation that matters for today's session
- Hints at the session focus without being preachy

Return only the greeting text, no quotes, no JSON wrapper."""


def _fallback_context(
    health: dict,
    focus: list[dict],
    context_block: str,
    protocol: dict,
    display_name: str | None = None,
) -> dict:
    week = protocol.get("week", "n/a")
    patient = display_name or "there"
    top = focus[0]["title"] if focus else "session focus"
    greeting = (
        f"Good morning, {patient}. Week {week} of your rehab — "
        f"HRV is at {health['hrv_ms']}ms with a recovery score of "
        f"{health['recovery_score']} today. Focus for this session: {top}."
    )
    return {
        "system_prompt": context_block,
        "greeting": greeting,
        "recommendations": focus,
    }
