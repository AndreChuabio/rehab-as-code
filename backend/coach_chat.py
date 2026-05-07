"""
coach_chat.py - OpenAI-powered Coach Maya chat co-pilot.

Recommends grounded exercises from exercise_kb and drafts protocol
revisions via tool calls. Each `fire_*_trigger` tool runs the LLM-driven
drafter in chat_protocol_drafter.py, which writes a `pending_review` row
to the `protocols` Supabase table. Clinicians approve or reject those rows
from the /clinician dashboard. There is no PR-bus, no GitHub write path.

The drafter is dispatched through a callable injected by main.py so this
module stays free of FastAPI / repo plumbing.

Event protocol yielded by chat_stream():

    {"type": "token",       "delta": str}
    {"type": "card",        "card": dict}                # exercise card
    {"type": "tool_call",   "name": str, "arguments": dict}
    {"type": "tool_result", "name": str, "result": dict} # includes pending_protocol_id
    {"type": "error",       "message": str}
    {"type": "done"}
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any, AsyncIterator, Awaitable, Callable

import exercise_kb

logger = logging.getLogger(__name__)


def _client():
    from openai import OpenAI

    api_key = os.getenv("OPENAI_API_KEY", "").strip()
    if not api_key or api_key == "your_openai_key_here":
        raise RuntimeError("OPENAI_API_KEY is not set")
    return OpenAI(api_key=api_key)


def _model() -> str:
    return os.getenv("OPENAI_MODEL", "gpt-4o-mini")


# ---------------------------------------------------------------------------
# Tool surface
# ---------------------------------------------------------------------------

TOOLS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "recommend_exercise",
            "description": (
                "Return a single exercise from the curated library as a video card. "
                "Use when the patient asks how to perform an exercise, asks for a regression, "
                "or you want to ground a recommendation in a video."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "exercise_id": {
                        "type": "string",
                        "description": "Exercise id - must be one of the library ids.",
                    },
                },
                "required": ["exercise_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_phase_exercises",
            "description": (
                "Return all exercises matching a rehab phase, optionally filtered by "
                "injury category. Use when the patient asks for an overview of what's "
                "appropriate for their week, or asks specifically about exercises for an "
                "injury (e.g. 'show me ankle mobility', 'shoulder week 3 work')."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "phase": {
                        "type": "string",
                        "enum": ["acute", "subacute", "strength"],
                    },
                    "injury_type": {
                        "type": "string",
                        "enum": ["knee", "ankle", "shoulder", "low_back", "hamstring", "elbow"],
                        "description": "Optional. Restrict results to a single injury category. Omit for all injuries.",
                    },
                },
                "required": ["phase"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "fire_symptom_trigger",
            "description": (
                "Draft a protocol revision in response to a new symptom and queue "
                "it for clinician review. Use when the patient reports new pain, "
                "a tweak, swelling, or any in-session warning sign. Quote the "
                "patient's words verbatim in symptom_text. The draft is saved as "
                "`pending_review` and surfaced on the /clinician dashboard; it "
                "does NOT auto-apply."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "symptom_text": {"type": "string"},
                },
                "required": ["symptom_text"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "fire_intake_trigger",
            "description": (
                "Force a full re-intake. ONLY call when the patient explicitly says they want "
                "to restart their intake from scratch (e.g., 'I want to redo my intake', "
                "'reset my plan and start over'). This deletes the existing intake record so "
                "the structured intake modal opens again on the next reload. Do NOT call when "
                "the patient is merely updating a field — for that, give a conversational reply "
                "or call fire_symptom_trigger / fire_checkin_trigger."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "reason": {
                        "type": "string",
                        "description": "One-sentence why the patient asked to restart intake.",
                    },
                },
                "required": ["reason"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "fire_checkin_trigger",
            "description": (
                "Draft a small protocol tweak in response to today's session log "
                "and queue it for clinician review. Use when the patient logs a "
                "session that suggests a load/volume adjustment is warranted. "
                "Saved as `pending_review`; clinician approves on /clinician."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "checkin_text": {"type": "string"},
                },
                "required": ["checkin_text"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "fire_weekly_plan_trigger",
            "description": (
                "Draft next week's progression and queue it for clinician review. "
                "Use only when the patient explicitly asks to progress or it's a "
                "Sunday-style weekly review. Saved as `pending_review`."
            ),
            "parameters": {"type": "object", "properties": {}},
        },
    },
]


# ---------------------------------------------------------------------------
# System prompt
# ---------------------------------------------------------------------------


def build_system_prompt(
    health: dict[str, Any],
    protocol: dict[str, Any],
    display_name: str | None = None,
) -> str:
    """Build Maya's system prompt.

    `display_name` is the patient's name resolved fresh from Supabase
    (intake_records.payload.name -> auth.users metadata -> email local-part).
    Pass None when no name is available - the prompt falls back to "the
    patient" rather than inventing one. The caller MUST NOT pass a name
    pulled from `protocol.patient`; that field drifts between account
    runs and once leaked "Christian" into a chat for Andre.
    """
    ids = ", ".join(exercise_kb.list_ids())
    current_exercises = ", ".join(
        ex.get("name", "") for ex in protocol.get("exercises", [])
    ) or "(none loaded)"
    week = protocol.get("week", "?")
    phase = protocol.get("phase", "rehab")
    patient = (display_name or "").strip() or "the patient"

    recent_line = ""
    recent = protocol.get("_recent_set")
    if recent:
        warns = recent.get("warnings") or []
        warn_summary = (
            f"; flagged {', '.join(w.get('id', '?') for w in warns[:3])}"
            if warns
            else ""
        )
        recent_line = (
            "Live set just finished: patient completed "
            f"{recent.get('rep_count', '?')} reps of "
            f"{recent.get('exercise_name') or recent.get('exercise_id', 'an exercise')} "
            f"({recent.get('worst_status', 'good')}); "
            f"best depth {recent.get('best_depth', '?')}°{warn_summary}. "
            "Acknowledge it conversationally if relevant; do not auto-fire a trigger.\n\n"
        )

    return (
        "You are Coach Maya's chat co-pilot - a concise, evidence-based "
        "physiotherapy assistant. The patient is "
        f"{patient}, week {week} {phase}.\n\n"
        f"Wearables today: HRV {health.get('hrv_ms', 'n/a')}ms, "
        f"sleep score {health.get('sleep_score', 'n/a')}/100, "
        f"recovery {health.get('recovery_score', 'n/a')}/100.\n\n"
        f"Current protocol: {current_exercises}.\n\n"
        f"{recent_line}"
        f"Exercise library (only recommend ids in this list): {ids}.\n\n"
        "Behavior rules:\n"
        "1. Keep replies under 60 words. Speak like a clinician, not a chatbot.\n"
        "2. When the patient describes pain, a tweak, swelling, or a tolerance "
        "issue, IMMEDIATELY call fire_symptom_trigger with their words verbatim, "
        "then call recommend_exercise on a regression from the library.\n"
        "3. When the patient asks how to do an exercise, call recommend_exercise.\n"
        "4. When the patient asks for an overview, call list_phase_exercises.\n"
        "5. When the patient explicitly asks to progress or 'plan next week', "
        "call fire_weekly_plan_trigger.\n"
        "6. NEVER invent an exercise that is not in the library. If asked for "
        "something off-library, say you'll flag it for the human therapist and "
        "offer to fire_symptom_trigger with the patient's text.\n"
        "7. After firing a trigger tool, give the patient a one-sentence summary "
        "noting that a draft has been queued for clinician review (e.g., "
        "'logged - drafted a regression for clinician review on /clinician').\n"
    )


# ---------------------------------------------------------------------------
# Tool dispatch
# ---------------------------------------------------------------------------


# A trigger executor takes a flow + payload dict and returns
# {"pending_protocol_id": str, "summary": str, "phase": str|None, "week": int|None}.
# On failure it raises; the caller surfaces the error to the patient.
TriggerExecutor = Callable[[str, dict[str, Any]], Awaitable[dict[str, Any]]]


async def _dispatch_tool(
    name: str,
    arguments: dict[str, Any],
    trigger_executor: TriggerExecutor,
    user_token: str | None = None,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """
    Returns (tool_result_for_llm, extra_events).
    extra_events are streamed to the frontend (e.g. card events).
    """
    if name == "recommend_exercise":
        ex = exercise_kb.find_by_id(arguments.get("exercise_id", ""))
        if not ex:
            return ({"error": "unknown exercise_id"}, [])
        card = exercise_kb.to_card(ex)
        return (
            {"ok": True, "exercise": card},
            [{"type": "card", "card": card}],
        )

    if name == "list_phase_exercises":
        phase = arguments.get("phase", "")
        injury_type = arguments.get("injury_type")
        matches = exercise_kb.find_by_phase(phase, injury_type=injury_type)
        cards = [exercise_kb.to_card(ex) for ex in matches]
        return (
            {"ok": True, "count": len(cards), "exercises": cards},
            [{"type": "card", "card": c} for c in cards],
        )

    if name == "fire_symptom_trigger":
        try:
            result = await trigger_executor(
                "symptom_adjustment",
                {"symptom_text": arguments.get("symptom_text", "")},
            )
        except Exception as exc:
            logger.exception("fire_symptom_trigger executor failed")
            err = {"ok": False, "error": str(exc), "flow": "symptom_adjustment"}
            return (err, [{"type": "tool_result", "name": name, "result": err}])
        return (
            {"ok": True, **result},
            [{"type": "tool_result", "name": name, "result": result}],
        )

    if name == "fire_intake_trigger":
        # Admin escape hatch: patient explicitly asked to restart intake.
        # Wipe their intake row so the next /patient/me/intake-status returns
        # state="needs_intake" and the frontend re-opens the intake modal.
        if not user_token:
            return (
                {"ok": False, "error": "no authenticated patient on this chat session"},
                [],
            )
        try:
            import user_store as _us
            _us.delete_intake(user_token)
        except Exception as exc:
            logger.exception("delete_intake failed")
            return (
                {"ok": False, "error": str(exc)},
                [],
            )
        reason = arguments.get("reason", "patient requested restart")
        return (
            {"ok": True, "action": "redirect_intake_ui", "reason": reason},
            [{
                "type": "tool_result",
                "name": name,
                "result": {"action": "redirect_intake_ui", "reason": reason},
            }],
        )

    if name == "fire_checkin_trigger":
        try:
            result = await trigger_executor(
                "checkin",
                {"checkin_text": arguments.get("checkin_text", "")},
            )
        except Exception as exc:
            logger.exception("fire_checkin_trigger executor failed")
            err = {"ok": False, "error": str(exc), "flow": "checkin"}
            return (err, [{"type": "tool_result", "name": name, "result": err}])
        return (
            {"ok": True, **result},
            [{"type": "tool_result", "name": name, "result": result}],
        )

    if name == "fire_weekly_plan_trigger":
        try:
            result = await trigger_executor("weekly_plan", {})
        except Exception as exc:
            logger.exception("fire_weekly_plan_trigger executor failed")
            err = {"ok": False, "error": str(exc), "flow": "weekly_plan"}
            return (err, [{"type": "tool_result", "name": name, "result": err}])
        return (
            {"ok": True, **result},
            [{"type": "tool_result", "name": name, "result": result}],
        )

    return ({"error": f"unknown tool {name}"}, [])


# ---------------------------------------------------------------------------
# Streaming chat loop
# ---------------------------------------------------------------------------


async def chat_stream(
    messages: list[dict[str, Any]],
    health: dict[str, Any],
    protocol: dict[str, Any],
    trigger_executor: TriggerExecutor,
    max_iters: int = 3,
    user_token: str | None = None,
    display_name: str | None = None,
) -> AsyncIterator[dict[str, Any]]:
    """
    Drive a tool-using OpenAI chat completion. Yields the event protocol
    documented at the top of this file. Caller is responsible for SSE
    framing.

    `messages` should NOT include the system prompt - this function prepends
    a freshly-built one. `display_name` is sourced fresh from Supabase by
    the caller (see backend/main.py:/chat); when None, Maya addresses the
    patient anonymously rather than inventing or recycling a stale name.
    """
    try:
        client = _client()
    except Exception as exc:
        yield {"type": "error", "message": f"openai client unavailable: {exc}"}
        yield {"type": "done"}
        return

    system_prompt = build_system_prompt(health, protocol, display_name=display_name)
    convo: list[dict[str, Any]] = [
        {"role": "system", "content": system_prompt},
        *messages,
    ]

    for iteration in range(max_iters):
        try:
            stream = client.chat.completions.create(
                model=_model(),
                messages=convo,
                tools=TOOLS,
                tool_choice="auto",
                stream=True,
                max_tokens=350,
                temperature=0.4,
            )
        except Exception as exc:
            logger.exception("openai create failed")
            yield {"type": "error", "message": str(exc)}
            yield {"type": "done"}
            return

        assistant_text = ""
        # tool calls are streamed in pieces; index -> partial dict
        tool_calls_partial: dict[int, dict[str, Any]] = {}

        for chunk in stream:
            choice = chunk.choices[0] if chunk.choices else None
            if choice is None:
                continue
            delta = choice.delta
            if delta.content:
                assistant_text += delta.content
                yield {"type": "token", "delta": delta.content}

            if delta.tool_calls:
                for tc in delta.tool_calls:
                    idx = tc.index
                    slot = tool_calls_partial.setdefault(
                        idx, {"id": "", "name": "", "arguments": ""}
                    )
                    if tc.id:
                        slot["id"] = tc.id
                    if tc.function and tc.function.name:
                        slot["name"] = tc.function.name
                    if tc.function and tc.function.arguments:
                        slot["arguments"] += tc.function.arguments

            if choice.finish_reason:
                break

        # No tool calls - we're done
        if not tool_calls_partial:
            yield {"type": "done"}
            return

        # Append the assistant message (with tool_calls) to history
        assistant_msg: dict[str, Any] = {
            "role": "assistant",
            "content": assistant_text or None,
            "tool_calls": [
                {
                    "id": slot["id"] or f"call_{idx}",
                    "type": "function",
                    "function": {
                        "name": slot["name"],
                        "arguments": slot["arguments"] or "{}",
                    },
                }
                for idx, slot in sorted(tool_calls_partial.items())
            ],
        }
        convo.append(assistant_msg)

        # Execute each tool call, append a tool message, surface events
        for idx, slot in sorted(tool_calls_partial.items()):
            name = slot["name"]
            try:
                arguments = json.loads(slot["arguments"] or "{}")
            except json.JSONDecodeError:
                arguments = {}
            yield {"type": "tool_call", "name": name, "arguments": arguments}

            result, extra_events = await _dispatch_tool(
                name, arguments, trigger_executor, user_token=user_token,
            )
            for ev in extra_events:
                yield ev

            convo.append(
                {
                    "role": "tool",
                    "tool_call_id": slot["id"] or f"call_{idx}",
                    "name": name,
                    "content": json.dumps(result),
                }
            )

        # Loop again so the model can produce the natural-language wrap-up

    yield {"type": "done"}
