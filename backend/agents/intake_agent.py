"""
IntakeAgent — collects structured patient intake data.

7 data points collected through natural conversation:
  name, age, injury_type, surgery_date, pain_level, symptoms, goals

Driven by the structured intake modal (frontend/index.html #intakeModal). The
agent runs server-side and is invoked turn-by-turn from /patient/interact;
the modal renders the conversation. After save_intake_record + trigger_plan_generation
the modal closes and the plan-gen modal opens to stream the AG2 PR run.

Vision support and Tavus session creation tools remain registered for future use
but are no longer surfaced in the system prompt; the modal does not collect a photo.

PR-K adds `capture_intake_from_chat`, an alternate path that lets coach_chat's
`start_intake_tool` persist intake fields conversationally (patient says
"I tweaked my ankle this morning" and Maya gathers the rest in dialog).
The structured modal still works; this is additive.
"""
from __future__ import annotations

import json
import logging
import os
from typing import Any, Literal

import anthropic

from . import register_patient_agent
from .base import PatientAgent, PatientRequest, PatientResponse

import sys
sys.path.insert(0, str(__import__("pathlib").Path(__file__).parent.parent))
import user_store

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Conversational intake (PR-K)
# ---------------------------------------------------------------------------

# Keys that constitute a "complete" intake record. Mirrors the
# save_intake_record tool's required list, minus `name`/`age` (those are
# collected up-front by Supabase Auth + the structured modal; conversational
# intake is additive on top of an authenticated patient).
_INTAKE_REQUIRED_KEYS = (
    "injury_type",
    "surgery_date",
    "pain_level",
    "symptoms",
    "goals",
)

# Keys we accept from the chat tool. Any key outside this allowlist is
# dropped on entry — guards against the model hallucinating a column.
_INTAKE_ALLOWED_KEYS = frozenset(
    _INTAKE_REQUIRED_KEYS
    + ("name", "age", "photo_url", "photo_findings", "payer_model")
)

# Payer model is clinician-owned (set via the clinician toggle, defaults to
# cash). It is accepted here only so a conversational capture that happens to
# include it is validated rather than dropped; it is NOT a required intake key
# and Maya does not ask for it.
_PAYER_MODELS = ("insurance", "medicare", "cash")


class IntakeCaptureError(RuntimeError):
    """Raised when capture_intake_from_chat fails for a recoverable reason
    the chat layer should surface to the patient (DB outage, validation
    failure, etc.). Distinct from ValueError so the dispatcher can map it
    to a tool_result error rather than a hard 500."""


def _validate_intake_fields(fields: dict[str, Any]) -> dict[str, Any]:
    """Drop unknown keys and run light type coercion.

    Pain level: clamp to 0..10. Symptoms / goals: coerce to list[str].
    Free-text fields (injury_type, surgery_date) are left as-is. We
    intentionally do NOT log values here — see PHI hygiene note in the
    PR-K scope.
    """
    cleaned: dict[str, Any] = {}
    for key, value in (fields or {}).items():
        if key not in _INTAKE_ALLOWED_KEYS:
            continue
        if value is None:
            continue
        if key == "pain_level":
            try:
                pain = int(value)
            except (TypeError, ValueError):
                raise IntakeCaptureError(
                    "pain_level must be an integer between 0 and 10"
                )
            if pain < 0 or pain > 10:
                raise IntakeCaptureError(
                    "pain_level must be between 0 and 10"
                )
            cleaned[key] = pain
            continue
        if key in ("symptoms", "goals"):
            if isinstance(value, str):
                cleaned[key] = [value]
            elif isinstance(value, list):
                cleaned[key] = [str(v) for v in value if str(v).strip()]
            else:
                raise IntakeCaptureError(f"{key} must be a string or array of strings")
            continue
        if key == "payer_model":
            pm = str(value).strip().lower()
            if pm not in _PAYER_MODELS:
                raise IntakeCaptureError(
                    "payer_model must be one of insurance, medicare, cash"
                )
            cleaned[key] = pm
            continue
        if key == "age":
            try:
                cleaned[key] = int(value)
            except (TypeError, ValueError):
                raise IntakeCaptureError("age must be an integer")
            continue
        cleaned[key] = value
    return cleaned


def _missing_required(payload: dict[str, Any]) -> list[str]:
    """Return the subset of `_INTAKE_REQUIRED_KEYS` not satisfied by payload."""
    missing: list[str] = []
    for k in _INTAKE_REQUIRED_KEYS:
        v = payload.get(k)
        if v is None:
            missing.append(k)
            continue
        if isinstance(v, (list, str)) and len(v) == 0:
            missing.append(k)
    return missing


def capture_intake_from_chat(
    token: str,
    fields: dict[str, Any],
    mode: Literal["new", "update"],
) -> dict[str, Any]:
    """Persist an intake_records row from a conversational capture (PR-K).

    Parameters
    ----------
    token : str
        Supabase auth.uid() of the patient (NOT a client-supplied token).
    fields : dict
        Subset of intake fields captured from chat. Only keys in
        `_INTAKE_ALLOWED_KEYS` are accepted; everything else is dropped.
    mode : Literal["new", "update"]
        - "new" : start a fresh intake record. Today this REPLACES the
          existing row's payload (intake_records.token is PRIMARY KEY).
          PR-L will lift this to a multi-row schema; until then,
          mode="new" should only fire when the patient is genuinely
          starting over (e.g., new injury) — Maya's prompt is gated on that.
        - "update" : deep-merge `fields` into the latest row's payload
          while preserving keys the patient hasn't re-provided.

    Returns
    -------
    {
        "intake_id": str,            # the patient's token (per current schema)
        "fields_captured": [str],    # keys actually persisted this call
        "fields_missing": [str],     # required keys still absent from the row
        "mode": str,
    }

    Raises
    ------
    IntakeCaptureError
        On validation failure or DB write failure. Caller (coach_chat
        dispatcher) surfaces the message via a tool_result error event;
        no silent fallback to a fake-success.
    """
    if not token:
        raise IntakeCaptureError("auth token required")
    if mode not in ("new", "update"):
        raise IntakeCaptureError(f"unsupported mode: {mode!r}")

    cleaned = _validate_intake_fields(fields)
    if not cleaned:
        raise IntakeCaptureError("no recognised intake fields supplied")

    if mode == "new":
        # Replace the row. Until PR-L's multi-row schema lands, "new" is an
        # upsert that overwrites the existing payload. We log the keys
        # captured (NOT values) so PHI doesn't leak.
        payload = dict(cleaned)
        try:
            user_store.save_intake(token, payload)
        except Exception as exc:
            logger.exception("save_intake (new) failed for token=%s", token)
            raise IntakeCaptureError(f"failed to persist intake: {exc}") from exc
    else:
        # update: deep-merge into the latest row's payload. If no row
        # exists, treat as new so a half-update doesn't silently no-op.
        prior = user_store.get_intake(token) or {}
        merged = dict(prior)
        # For list-typed keys (symptoms, goals) we union the values rather
        # than overwriting — patients refining "and also stiffness" should
        # accumulate, not clobber.
        for k, v in cleaned.items():
            if k in ("symptoms", "goals") and isinstance(v, list):
                existing = merged.get(k) or []
                if not isinstance(existing, list):
                    existing = [str(existing)]
                merged_set = list(dict.fromkeys([*existing, *v]))
                merged[k] = merged_set
            else:
                merged[k] = v
        try:
            user_store.save_intake(token, merged)
        except Exception as exc:
            logger.exception("save_intake (update) failed for token=%s", token)
            raise IntakeCaptureError(f"failed to persist intake: {exc}") from exc

    persisted = user_store.get_intake(token) or {}
    missing = _missing_required(persisted)

    logger.info(
        "conversational_intake captured token=%s mode=%s keys=%s missing=%s",
        token, mode, sorted(cleaned.keys()), missing,
    )

    return {
        "intake_id": token,
        "fields_captured": sorted(cleaned.keys()),
        "fields_missing": missing,
        "mode": mode,
    }

SYSTEM_PROMPT = """You are a rehabilitation intake specialist.
Collect the following fields through warm, natural conversation — ask one question at a time.
Keep replies short (1-2 sentences) so they fit the chat-style intake modal.

Required:
  1. name: patient's first name
  2. age: age in years (integer)
  3. injury_type: e.g., "ACL reconstruction", "rotator cuff repair", "lateral ankle sprain"
  4. surgery_date: approximate surgery or injury date (ask for "how long ago" if exact date unknown)
  5. pain_level: current pain level 0-10
  6. symptoms: list of current symptoms (pain location, stiffness, limited ROM, etc.)
  7. goals: what they want to achieve with rehab (return to sport, daily function, etc.)

When all required fields are collected, confirm with a one-sentence summary, then call
save_intake_record with the structured fields. Immediately after, call trigger_plan_generation.
"""

INTAKE_TOOLS = [
    {
        "name": "analyze_image",
        "description": "Analyze a patient-submitted injury or post-op photo using vision.",
        "input_schema": {
            "type": "object",
            "properties": {"image_url": {"type": "string", "description": "URL or base64 data URI of the image"}},
            "required": ["image_url"],
        },
    },
    {
        "name": "create_video_session",
        "description": "Create a Tavus video coaching session for video-guided intake.",
        "input_schema": {
            "type": "object",
            "properties": {"patient_name": {"type": "string"}},
            "required": ["patient_name"],
        },
    },
    {
        "name": "save_intake_record",
        "description": "Save the completed intake. Call only when all required fields are collected.",
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "age": {"type": "integer"},
                "injury_type": {"type": "string"},
                "surgery_date": {"type": "string"},
                "pain_level": {"type": "integer"},
                "symptoms": {"type": "array", "items": {"type": "string"}},
                "goals": {"type": "array", "items": {"type": "string"}},
                "photo_url": {"type": "string"},
                "photo_findings": {"type": "string"},
            },
            "required": ["name", "age", "injury_type", "surgery_date", "pain_level", "symptoms", "goals"],
        },
    },
    {
        "name": "trigger_plan_generation",
        "description": "Signal that intake is complete and plan generation should start.",
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
]


@register_patient_agent
class IntakeAgent(PatientAgent):
    """Collects structured intake, handles image analysis and Tavus, routes to plan_generation."""

    name = "intake"

    def can_handle(self, request: PatientRequest) -> bool:
        return user_store.get_intake(request.user_token) is None

    async def handle(self, request: PatientRequest) -> PatientResponse:
        api_key = os.getenv("ANTHROPIC_API_KEY")
        if not api_key:
            return PatientResponse(
                agent_name=self.name,
                message="Intake unavailable (ANTHROPIC_API_KEY not set).",
                next_agent=None,
            )

        client = anthropic.Anthropic(api_key=api_key)
        history: list[dict] = request.metadata.get("conversation_history", [])

        # inject image_url into first message if provided
        first_msg = request.message
        image_url = request.metadata.get("image_url")
        if image_url:
            first_msg += f"\n\n[Patient has submitted an image for analysis: {image_url}]"

        history.append({"role": "user", "content": first_msg})

        next_agent: str | None = None
        final_message = ""
        artifacts: list[dict] = []
        intake_saved: bool = False

        for _ in range(12):
            resp = client.messages.create(
                model="claude-sonnet-4-6",
                max_tokens=500,
                system=SYSTEM_PROMPT,
                tools=INTAKE_TOOLS,
                messages=history,
            )

            assistant_content = resp.content
            history.append({"role": "assistant", "content": assistant_content})

            tool_calls = [b for b in assistant_content if b.type == "tool_use"]
            text_blocks = [b for b in assistant_content if b.type == "text"]
            if text_blocks:
                final_message = text_blocks[-1].text

            if not tool_calls:
                break

            tool_results = []
            for tc in tool_calls:
                result, extra_artifacts = self._dispatch_tool(
                    tc.name, tc.input, request.user_token, client
                )
                artifacts.extend(extra_artifacts)
                if tc.name == "save_intake_record":
                    intake_saved = True
                if tc.name == "trigger_plan_generation" and intake_saved:
                    next_agent = "plan_generation"
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": tc.id,
                    "content": json.dumps(result),
                })

            history.append({"role": "user", "content": tool_results})

            if next_agent:
                break

        return PatientResponse(
            agent_name=self.name,
            message=final_message or "Let me ask you a few questions to build your rehab plan.",
            next_agent=next_agent,
            data={"intake": user_store.get_intake(request.user_token)},
            artifacts=artifacts,
        )

    def _dispatch_tool(
        self,
        name: str,
        inputs: dict,
        token: str,
        client: anthropic.Anthropic,
    ) -> tuple[dict, list[dict]]:
        if name == "analyze_image":
            image_url = inputs.get("image_url", "")
            try:
                if image_url.startswith("data:"):
                    # base64 data URI
                    header, b64data = image_url.split(",", 1)
                    media_type = header.split(":")[1].split(";")[0]
                    img_block = {"type": "image", "source": {"type": "base64", "media_type": media_type, "data": b64data}}
                else:
                    img_block = {"type": "image", "source": {"type": "url", "url": image_url}}

                vision_resp = client.messages.create(
                    model="claude-sonnet-4-6",
                    max_tokens=300,
                    messages=[{
                        "role": "user",
                        "content": [
                            img_block,
                            {"type": "text", "text": (
                                "You are a physical therapist reviewing a patient's post-op or injury photo. "
                                "Briefly describe visible findings: swelling, bruising, scar condition, range-of-motion cues, or anything clinically relevant. "
                                "Keep it to 2-3 sentences. Do not diagnose."
                            )},
                        ],
                    }],
                )
                findings = vision_resp.content[0].text.strip()
                return {"findings": findings}, [{"type": "image_analysis", "findings": findings}]
            except Exception as exc:
                logger.warning("Image analysis failed: %s", exc)
                return {"error": str(exc)}, []

        if name == "create_video_session":
            try:
                import asyncio, tavus_client
                loop = asyncio.get_event_loop()
                result = loop.run_in_executor(
                    None,
                    tavus_client.create_conversation,
                    "You are Coach Maya conducting a rehab intake conversation.",
                    f"Hi {inputs.get('patient_name', 'there')}, I'm Coach Maya. Let's get your rehab started.",
                    inputs.get("patient_name", "there"),
                )
                return {"ok": True}, [{"type": "tavus_session_pending"}]
            except Exception as exc:
                return {"error": str(exc)}, []

        if name == "save_intake_record":
            user_store.save_intake(token, dict(inputs))
            return {"ok": True}, []

        if name == "trigger_plan_generation":
            return {"ok": True}, []

        return {"error": f"unknown tool {name}"}, []
