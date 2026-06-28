"""
patient_context.py - per-patient closures shared by the chat paths.

Extracted from main.py so both the text-chat endpoint (/chat) and the
BYO-LLM Tavus proxy (api/tavus_proxy.py) can build the same coach_chat
collaborators without importing main.py (which would be a circular import:
the proxy router is included by main.py).

Each factory closes over the JWT-derived patient token so drafter rows are
attributed to the authenticated patient, never a client-provided id.

PHI hygiene: these helpers log only the token + a result id, never the
patient's message, name, or protocol payload.
"""
from __future__ import annotations

import asyncio
import logging

import chat_protocol_drafter
from protocol_loader import fetch_protocol_for_user

logger = logging.getLogger(__name__)


def _last_pose_metrics(user_id: str) -> dict | None:
    """Return the most-recent completed session's pose_metrics, or None.

    Used as Phase F context for the symptom classifier so it can correlate
    a complaint ("knee buckled on lunges") with the most recent observed
    form quality. Best-effort: any DB error returns None; we don't want a
    failed pose-metrics lookup to block the chat path.
    """
    try:
        import session_repo as _sr
        rows = _sr.list_recent(token=user_id, days=2)
    except Exception as exc:
        logger.info("last_pose_metrics lookup failed user=%s: %s", user_id, exc)
        return None
    completed = [
        r for r in rows
        if r.get("status") == "completed" and r.get("pose_metrics")
    ]
    if not completed:
        return None
    return completed[-1].get("pose_metrics")


def _clinician_attention_writer_factory(user_id: str):
    """Build a coach_chat.ClinicianAttentionWriter bound to this patient.

    On a clinician-attention symptom verdict, clone the patient's current
    active protocol payload (if any) and persist a needs_clinician_review
    row with safety_concerns set to the classifier output. The clinician
    dashboard already shows these rows at the top of the queue with a red
    banner (see PR-C). Returns the new pending row id.

    Cloning rather than synthesizing a fresh payload means the diff view
    on /clinician renders "no exercise change, but this needs your eyes" -
    which is the right framing: the agent isn't proposing a regression,
    it's escalating a red flag.
    """
    async def _writer(triage: dict, message_text: str) -> str:
        active = fetch_protocol_for_user(user_id) or {}
        # Drop the in-memory _recent_set bag so it doesn't leak into the
        # persisted payload (it's a runtime overlay, not protocol state).
        payload = {k: v for k, v in active.items() if not k.startswith("_")}
        if not payload:
            payload = {
                "patient": "unknown",
                "phase": "rehab",
                "week": 0,
                "exercises": [],
                "_synthetic": True,
            }
        concerns = [{
            "check": "symptom-classifier",
            "severity": "high",
            "detail": (
                f"Patient message: {message_text}\n\n"
                f"Classifier reasoning: {triage.get('reasoning', '')}"
            ),
        }]
        loop = asyncio.get_running_loop()
        from protocol_repo import save_pending
        pending_id = await loop.run_in_executor(
            None,
            lambda: save_pending(
                user_id,
                payload,
                created_by_agent="symptom_classifier",
                status="needs_clinician_review",
                safety_concerns=concerns,
            ),
        )
        # Log only the id, severity, and that we wrote — never the message.
        logger.info(
            "clinician_attention row written user=%s pending_id=%s",
            user_id, pending_id,
        )

        # Pref-gated symptom receipt, fired AFTER the write. Inline + error-
        # swallowed (NOT an unanchored asyncio.create_task — Fluid Compute can
        # cancel a detached task when the SSE request returns). The receipt
        # carries NO verbatim message_text (that is PHI we don't re-egress).
        # notifications.send_symptom_receipt honors email_opt_in +
        # symptom_flag_receipts internally and swallows its own errors.
        try:
            import notifications

            await loop.run_in_executor(
                None, notifications.send_symptom_receipt, user_id,
            )
        except Exception:  # noqa: BLE001 - never break the chat / SSE stream
            logger.warning("symptom_receipt email hook failed (non-fatal)")

        return pending_id

    return _writer


def _chat_trigger_executor_factory(user_id: str):
    """Bind a chat-tool trigger executor to the authenticated patient.

    The executor signature `(flow, payload) -> dict` matches what
    coach_chat.chat_stream expects. We close over `user_id` here so the
    drafter row is attributed to the JWT-derived patient (never client-
    provided), mirroring the auth boundary used by /protocols/*/approve.

    Each fire_*_trigger ultimately runs chat_protocol_drafter.draft_and_save_pending,
    which writes a `pending_review` row to the `protocols` table. Returns
    {pending_protocol_id, summary, phase, week, flow} on success; raises on
    failure so coach_chat._dispatch_tool can render an error tool_result.
    """
    async def _executor(flow: str, payload: dict) -> dict:
        prior_protocol = fetch_protocol_for_user(user_id) or None
        # draft_and_save_pending is sync (blocks on Anthropic + psycopg). Run
        # in the default executor so the SSE stream stays responsive.
        loop = asyncio.get_running_loop()
        result = await loop.run_in_executor(
            None,
            chat_protocol_drafter.draft_and_save_pending,
            user_id,
            flow,
            payload,
            prior_protocol,
        )
        return {
            "pending_protocol_id": result["pending_protocol_id"],
            "summary": result["summary"],
            "phase": result.get("phase"),
            "week": result.get("week"),
            "flow": flow,
        }

    return _executor
