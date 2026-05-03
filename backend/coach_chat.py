"""
coach_chat.py - OpenAI-powered Coach Maya chat co-pilot.

Stays out of the orchestrator. Recommends grounded exercises from
exercise_kb and fires existing trigger endpoints via tool calls. The
"trigger" tools are dispatched through a callable injected by main.py to
avoid a circular import.

Event protocol yielded by chat_stream():

    {"type": "token",       "delta": str}
    {"type": "card",        "card": dict}                # exercise card
    {"type": "tool_call",   "name": str, "arguments": dict}
    {"type": "tool_result", "name": str, "result": dict} # includes invocation_id
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
                "Fire the orchestrator's symptom_adjustment flow. Use when the patient "
                "reports new pain, a tweak, swelling, or any in-session warning sign. "
                "Quote the patient's words verbatim in symptom_text."
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
                "Fire the orchestrator's intake flow to (re)initialize the protocol. "
                "Use only when the patient is providing fresh intake data (age, surgery date, "
                "current pain level)."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "intake_text": {"type": "string"},
                },
                "required": ["intake_text"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "fire_checkin_trigger",
            "description": (
                "Fire the orchestrator's checkin flow to log today's session outcome and "
                "let the evaluator flag any trends."
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
                "Fire the orchestrator's weekly_plan flow to generate next week's progression. "
                "Use only when the patient explicitly asks to progress or it's a Sunday-style "
                "weekly review."
            ),
            "parameters": {"type": "object", "properties": {}},
        },
    },
]


# ---------------------------------------------------------------------------
# System prompt
# ---------------------------------------------------------------------------


def build_system_prompt(health: dict[str, Any], protocol: dict[str, Any]) -> str:
    ids = ", ".join(exercise_kb.list_ids())
    current_exercises = ", ".join(
        ex.get("name", "") for ex in protocol.get("exercises", [])
    ) or "(none loaded)"
    week = protocol.get("week", "?")
    phase = protocol.get("phase", "rehab")
    patient = protocol.get("patient", "the patient")

    return (
        "You are Coach Maya's chat co-pilot - a concise, evidence-based "
        "physiotherapy assistant. The patient is "
        f"{patient}, week {week} {phase}.\n\n"
        f"Wearables today: HRV {health.get('hrv_ms', 'n/a')}ms, "
        f"sleep score {health.get('sleep_score', 'n/a')}/100, "
        f"recovery {health.get('recovery_score', 'n/a')}/100.\n\n"
        f"Current protocol: {current_exercises}.\n\n"
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
        "of what was just shipped to the orchestrator (e.g., 'symptom logged - the "
        "team is opening a PR with a regression.').\n"
    )


# ---------------------------------------------------------------------------
# Tool dispatch
# ---------------------------------------------------------------------------


# A trigger executor takes a flow + payload dict and returns
# {"invocation_id", "pr_url", "branch", "provider"}.
TriggerExecutor = Callable[[str, dict[str, Any]], Awaitable[dict[str, Any]]]


async def _dispatch_tool(
    name: str,
    arguments: dict[str, Any],
    trigger_executor: TriggerExecutor,
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
        result = await trigger_executor(
            "symptom_adjustment",
            {"symptom_text": arguments.get("symptom_text", "")},
        )
        return (
            {"ok": True, **result},
            [{"type": "tool_result", "name": name, "result": result}],
        )

    if name == "fire_intake_trigger":
        result = await trigger_executor(
            "intake",
            {"intake_text": arguments.get("intake_text", "")},
        )
        return (
            {"ok": True, **result},
            [{"type": "tool_result", "name": name, "result": result}],
        )

    if name == "fire_checkin_trigger":
        result = await trigger_executor(
            "checkin",
            {"checkin_text": arguments.get("checkin_text", "")},
        )
        return (
            {"ok": True, **result},
            [{"type": "tool_result", "name": name, "result": result}],
        )

    if name == "fire_weekly_plan_trigger":
        result = await trigger_executor("weekly_plan", {})
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
) -> AsyncIterator[dict[str, Any]]:
    """
    Drive a tool-using OpenAI chat completion. Yields the event protocol
    documented at the top of this file. Caller is responsible for SSE
    framing.

    `messages` should NOT include the system prompt - this function prepends
    a freshly-built one.
    """
    try:
        client = _client()
    except Exception as exc:
        yield {"type": "error", "message": f"openai client unavailable: {exc}"}
        yield {"type": "done"}
        return

    system_prompt = build_system_prompt(health, protocol)
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
                name, arguments, trigger_executor
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
