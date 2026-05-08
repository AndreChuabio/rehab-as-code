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
    {"type": "triage_alert", "severity": str, "symptom_keyword": str | None,
                              "clinic_phone": str | None}
                                                          # PR-H: patient receipt
                                                          # surfaced when symptom
                                                          # classifier returns
                                                          # severity=clinician-attention.
                                                          # Frontend renders a
                                                          # system message in chat.
    {"type": "error",       "message": str}
    {"type": "done"}
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
from typing import Any, AsyncIterator, Awaitable, Callable

import exercise_kb

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Symptom triage (Phase F)
# ---------------------------------------------------------------------------

# Pain / symptom keyword pre-filter. Cheap regex gate so the Haiku call only
# fires on messages that actually mention a symptom. Kept rough on purpose -
# false-positives are fine (Haiku will classify as "minor"); false-negatives
# are the failure mode we care about (a real red-flag missed). Case-insensitive.
SYMPTOM_KEYWORD_RE = re.compile(
    r"\b("
    r"pain|hurt|hurts|sore|ache|achey|achy|tweak|tweaky|sharp|sting|stung|"
    r"swollen|swelling|stiff|stiffness|weak|weakness|giving way|gives way|"
    r"popping|popped|grinding|locked|locking|cant|can'?t|"
    r"numb|numbness|tingling|throbbing|burning"
    r")\b",
    re.IGNORECASE,
)

# In-memory de-dup keyed by (session_id, sha256(message)) so an identical
# message inside the same session doesn't re-classify on the model's
# follow-up loop iteration. Vercel function instance lifetime is fine here -
# this is not a correctness boundary, just a cost-saver. No Redis needed.
_TRIAGE_SEEN: dict[tuple[str, str], bool] = {}


def _triage_seen_key(session_id: str, message: str) -> tuple[str, str]:
    digest = hashlib.sha256(message.encode("utf-8")).hexdigest()
    return (session_id, digest)


def _first_symptom_keyword(message: str) -> str | None:
    """Return the first symptom-keyword match in the patient's message.

    Used to populate the triage_alert event so the frontend can render
    "Your message about a [keyword] was flagged for your PT". Falls back
    to None when nothing matches (the frontend uses a generic phrase).
    """
    if not message:
        return None
    m = SYMPTOM_KEYWORD_RE.search(message)
    return m.group(1).lower() if m else None


def _format_triage_block(triage: dict[str, Any]) -> str:
    """Render the classifier output for injection into Maya's system prompt."""
    return (
        "\n\n[SYMPTOM_TRIAGE]\n"
        f"severity: {triage.get('severity', '')}\n"
        f"reasoning: {triage.get('reasoning', '')}\n"
        f"recommendation: {triage.get('suggested_response', '')}\n"
        "[/SYMPTOM_TRIAGE]\n"
        "Acknowledge the patient's symptom, follow the recommendation, and "
        "use the recommendation as a starting point - don't quote it verbatim. "
        "When severity is clinician-attention, do NOT prescribe anything; "
        "tell the patient their clinician has been flagged and will reach "
        "out shortly. When severity is hold-load, suggest the regression. "
        "When severity is minor, reassure and continue normal coaching."
    )


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
    {
        "type": "function",
        "function": {
            "name": "propose_calendar_event",
            "description": (
                "Propose adding an event to the patient's Google Calendar. "
                "Use when the patient agrees to schedule a session, mobility "
                "block, or follow-up — e.g., 'block 5pm Tuesday for my knee "
                "rehab', 'remind me to do mobility tomorrow morning'. This "
                "DOES NOT create the event; it surfaces a confirm card in the "
                "chat UI and the patient taps 'Add to calendar' to actually "
                "write. Times must be RFC3339 (e.g. 2026-05-09T17:00:00-07:00). "
                "Pick reasonable defaults: a rehab session is 30-45 min unless "
                "the patient says otherwise. Only call when the patient has "
                "connected Google Calendar AND has expressed an intent to "
                "schedule something."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "title": {
                        "type": "string",
                        "description": "Short event title, e.g. 'Knee rehab session'.",
                    },
                    "start_iso": {
                        "type": "string",
                        "description": "RFC3339 start time with timezone offset.",
                    },
                    "end_iso": {
                        "type": "string",
                        "description": "RFC3339 end time with timezone offset.",
                    },
                    "description": {
                        "type": "string",
                        "description": "Optional notes shown on the calendar event body.",
                    },
                },
                "required": ["title", "start_iso", "end_iso"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "start_intake_tool",
            "description": (
                "Begin or update a patient intake conversationally. Use when the "
                "patient mentions a new or existing injury and you have enough info "
                "to capture intake fields (injury_type, surgery_date if applicable, "
                "pain_level, symptoms, goals). Capture incrementally - if a field "
                "is missing, ask the patient on the next turn. Do NOT call until "
                "you have at least an injury_type and a pain_level. Never call "
                "this tool twice in the same conversation."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "injury_type": {
                        "type": "string",
                        "description": "Free-text injury description, e.g. 'lateral ankle sprain'.",
                    },
                    "pain_level": {
                        "type": "integer",
                        "minimum": 0,
                        "maximum": 10,
                    },
                    "symptoms": {
                        "type": "array",
                        "items": {"type": "string"},
                    },
                    "goals": {
                        "type": "array",
                        "items": {"type": "string"},
                    },
                    "surgery_date": {
                        "type": "string",
                        "description": "ISO date or relative phrase like '3 weeks ago'.",
                    },
                    "mode": {
                        "type": "string",
                        "enum": ["new", "update"],
                        "description": (
                            "new = start a fresh intake (use when the patient "
                            "describes a new injury). update = patch the latest "
                            "row's payload with the fields provided "
                            "(use when the patient is refining or adding to an "
                            "existing intake)."
                        ),
                    },
                },
                "required": ["injury_type", "mode"],
            },
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
    triage_block: str | None = None,
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

    base = (
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
        "then call recommend_exercise on a regression from the library. "
        "EXCEPTION: when a triage block below says severity is "
        "'clinician-attention', do NOT call any fire_*_trigger tool - the "
        "orchestrator has already flagged the clinician; just respond to the "
        "patient.\n"
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
        "8. If the patient mentions a NEW injury (in addition to or instead of an "
        "existing one), you can capture intake conversationally with "
        "start_intake_tool. Capture as much as the patient gives in one turn; "
        "ask one targeted question for any required-but-missing field on the "
        "next turn. Do NOT call the tool until you have at least an injury_type "
        "AND a pain_level. Never call it twice in the same conversation. After "
        "a successful capture, acknowledge with one sentence (e.g., 'got it - "
        "captured intake for [injury]') and, if the patient seems ready, offer "
        "a chip 'Draft me a plan based on this' rather than auto-firing "
        "fire_weekly_plan_trigger.\n"
        "9. Always respond in English, regardless of the language the patient "
        "writes in. The clinical content, exercise library, and clinician "
        "review pipeline are English-only; mirroring the patient's language "
        "would put non-English replies in front of clinicians who can't audit "
        "them. If the patient writes in another language, reply in English and "
        "continue normally - do not translate, do not apologize, do not switch "
        "languages mid-conversation.\n"
    )
    if triage_block:
        base = base + triage_block
    return base


# ---------------------------------------------------------------------------
# Tool dispatch
# ---------------------------------------------------------------------------


# A trigger executor takes a flow + payload dict and returns
# {"pending_protocol_id": str, "summary": str, "phase": str|None, "week": int|None}.
# On failure it raises; the caller surfaces the error to the patient.
TriggerExecutor = Callable[[str, dict[str, Any]], Awaitable[dict[str, Any]]]


# Phase F: when the symptom classifier returns severity='clinician-attention',
# coach_chat asks the orchestrator (main.py) to clone the patient's current
# protocol payload and persist a needs_clinician_review row with the
# classifier output attached as safety_concerns. The writer takes the
# triage dict + the patient's verbatim message and returns the
# pending_protocol_id (str) on success. Raises on failure - we don't want a
# silent miss on a high-severity flag, so coach_chat surfaces the error.
ClinicianAttentionWriter = Callable[
    [dict[str, Any], str], Awaitable[str]
]


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

    if name == "propose_calendar_event":
        # Propose-then-confirm: this tool NEVER writes to the calendar.
        # It streams a `calendar_proposal` event to the frontend, which
        # renders a confirm card; on click the frontend POSTs to
        # /calendar/events with the user's JWT. That keeps event-creation
        # gated by an explicit user action even if Maya hallucinates.
        title = (arguments.get("title") or "").strip()
        start_iso = (arguments.get("start_iso") or "").strip()
        end_iso = (arguments.get("end_iso") or "").strip()
        if not (title and start_iso and end_iso):
            err = {"ok": False, "error": "title, start_iso, end_iso are required"}
            return (err, [{"type": "tool_result", "name": name, "result": err}])
        proposal = {
            "title": title,
            "start_iso": start_iso,
            "end_iso": end_iso,
            "description": arguments.get("description"),
        }
        return (
            {"ok": True, "proposed": True, **proposal},
            [{"type": "calendar_proposal", "proposal": proposal}],
        )

    if name == "start_intake_tool":
        # PR-K conversational intake. The structured intake modal still works;
        # this is the alternate path Maya uses when the patient mentions a
        # new injury mid-chat. We forward the args verbatim to
        # agents.intake_agent.capture_intake_from_chat, which validates,
        # persists, and returns fields_captured + fields_missing.
        if not user_token:
            err = {
                "ok": False,
                "error": "no authenticated patient on this chat session",
            }
            return (err, [{"type": "tool_result", "name": name, "result": err}])

        mode = arguments.get("mode")
        if mode not in ("new", "update"):
            err = {"ok": False, "error": f"invalid mode: {mode!r}"}
            return (err, [{"type": "tool_result", "name": name, "result": err}])

        # Strip the routing key from the persisted fields.
        fields = {k: v for k, v in arguments.items() if k != "mode"}

        try:
            from agents.intake_agent import (
                IntakeCaptureError,
                capture_intake_from_chat,
            )
            result = capture_intake_from_chat(user_token, fields, mode)
        except Exception as exc:
            # Surface the error back to the model + frontend rather than
            # pretending the capture succeeded. Both IntakeCaptureError
            # (validation / DB failure we expect) and unexpected exceptions
            # land here. PHI hygiene: log the token + mode + KEYS only,
            # never the values themselves.
            try:
                from agents.intake_agent import IntakeCaptureError as _IC
                is_known = isinstance(exc, _IC)
            except Exception:
                is_known = False
            if is_known:
                logger.warning(
                    "start_intake_tool capture failed token=%s mode=%s keys=%s: %s",
                    user_token, mode, sorted(fields.keys()), exc,
                )
            else:
                logger.exception(
                    "start_intake_tool unexpected failure token=%s mode=%s keys=%s",
                    user_token, mode, sorted(fields.keys()),
                )
            err = {"ok": False, "error": str(exc)}
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
    session_id: str = "default",
    last_pose_metrics: dict[str, Any] | None = None,
    clinician_attention_writer: ClinicianAttentionWriter | None = None,
) -> AsyncIterator[dict[str, Any]]:
    """
    Drive a tool-using OpenAI chat completion. Yields the event protocol
    documented at the top of this file. Caller is responsible for SSE
    framing.

    `messages` should NOT include the system prompt - this function prepends
    a freshly-built one. `display_name` is sourced fresh from Supabase by
    the caller (see backend/main.py:/chat); when None, Maya addresses the
    patient anonymously rather than inventing or recycling a stale name.

    Phase F: when the latest user message contains a pain / symptom keyword
    and we haven't classified it in this session yet, call the symptom
    classifier (Haiku 4.5). The classifier output is injected into Maya's
    system prompt as a [SYMPTOM_TRIAGE] block. Side effects on
    severity == 'clinician-attention' are routed through
    `clinician_attention_writer` (provided by main.py); on hold-load /
    minor we just steer Maya's reply via the prompt.
    """
    try:
        client = _client()
    except Exception as exc:
        yield {"type": "error", "message": f"openai client unavailable: {exc}"}
        yield {"type": "done"}
        return

    # ── Phase F: pre-flight symptom triage ────────────────────────────────
    triage_block: str | None = None
    triage_result: dict[str, Any] | None = None
    latest_user_msg = next(
        (m.get("content", "") for m in reversed(messages) if m.get("role") == "user"),
        "",
    )
    if latest_user_msg and SYMPTOM_KEYWORD_RE.search(latest_user_msg):
        seen_key = _triage_seen_key(session_id, latest_user_msg)
        if not _TRIAGE_SEEN.get(seen_key):
            _TRIAGE_SEEN[seen_key] = True
            try:
                # Local import so the OpenAI-only chat path doesn't pay
                # the anthropic import cost when no symptom is mentioned.
                from agents.symptom_classifier import (
                    SymptomClassifierError,
                    classify,
                )
                triage_result = classify(
                    message=latest_user_msg,
                    wearables=health,
                    protocol=protocol,
                    last_pose_metrics=last_pose_metrics,
                    token=user_token,
                )
                triage_block = _format_triage_block(triage_result)
            except SymptomClassifierError as exc:
                # No silent fallback to a fake "minor". Log + skip the
                # triage block; Maya responds with her normal prompt.
                logger.warning(
                    "symptom triage skipped (classifier error) token=%s: %s",
                    user_token, exc,
                )
                triage_result = None
                triage_block = None
            except Exception as exc:
                logger.exception(
                    "symptom triage skipped (unexpected error) token=%s: %s",
                    user_token, exc,
                )
                triage_result = None
                triage_block = None

        # Side effect: clinician-attention writes a needs_clinician_review row.
        # This is deterministic in the orchestrator, NOT LLM-routed.
        if (
            triage_result
            and triage_result.get("severity") == "clinician-attention"
            and clinician_attention_writer is not None
        ):
            try:
                pending_id = await clinician_attention_writer(
                    triage_result, latest_user_msg,
                )
                yield {
                    "type": "tool_result",
                    "name": "symptom_triage",
                    "result": {
                        "ok": True,
                        "severity": "clinician-attention",
                        "pending_protocol_id": pending_id,
                    },
                }
            except Exception as exc:
                logger.exception(
                    "clinician_attention_writer failed token=%s: %s",
                    user_token, exc,
                )
                yield {
                    "type": "tool_result",
                    "name": "symptom_triage",
                    "result": {
                        "ok": False,
                        "severity": "clinician-attention",
                        "error": str(exc),
                    },
                }

        # PR-H: patient-side receipt. ALWAYS emit when severity is
        # clinician-attention so the patient sees a system message that
        # their PT was flagged - even if the writer is absent (test path)
        # or failed (we still want them to know to call urgent care if
        # severe). Phone number is sourced from CLINIC_PHONE env, falling
        # back to None so the frontend can render "call your clinic" copy
        # without an actual link until ops configures the real number.
        if (
            triage_result
            and triage_result.get("severity") == "clinician-attention"
        ):
            phone = (os.getenv("CLINIC_PHONE", "") or "").strip() or None
            symptom_keyword = _first_symptom_keyword(latest_user_msg)
            yield {
                "type": "triage_alert",
                "severity": "clinician-attention",
                "symptom_keyword": symptom_keyword,
                "clinic_phone": phone,
            }

    system_prompt = build_system_prompt(
        health, protocol, display_name=display_name, triage_block=triage_block,
    )
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
