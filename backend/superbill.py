"""superbill.py — DRAFT super-bill generator (completed-sessions-only).

Generates a clinician-attested DRAFT super-bill from a patient's ACTIVE
protocol + COMPLETED session records. Completed-sessions-only by design: a CPT
unit is only billable if the skilled service actually happened, so the draft is
built from logged sessions, never from the speculative protocol plan.

HARD CONTRACT (do not weaken — see project-kendell-demo2-feature-plan):
  * Every line item carries requires_clinician_attestation = True.
  * The artifact is status = "draft_unsigned" — NEVER auto-final.
  * needs_verification = True on every CPT mapping: no CPT code, unit count, or
    reimbursement outcome is asserted as settled fact. A licensed clinician
    (and Nikki's CPT research) must verify before anything is submitted.
  * Nothing here implies guaranteed reimbursement.

The CPT catalog + exercise->code mapping is a STRUCTURE for clinician review,
not a billing authority. The justification language is payer-aware (insurance/
medicare = medical-necessity; cash = load-management / out-of-network
self-submission framing) but a clinician edits and signs it.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger(__name__)


# CPT codes plausibly in scope for remote PT in this model. NONE are asserted
# as reimbursable — every use is flagged needs_verification. Sources: the
# pt-clinical-reviewer audit; pending Nikki/Kendell sign-off.
_CPT_CATALOG: dict[str, str] = {
    "97110": "Therapeutic exercise (strength / ROM / endurance)",
    "97112": "Neuromuscular re-education (balance / coordination / proprioception)",
    "97530": "Therapeutic activities (functional / ADL)",
    "97116": "Gait training",
}

# Keyword heuristics over exercise_id -> CPT bucket. Deliberately coarse: this
# is a draft a clinician corrects, not an authoritative coder. The final
# exercise->CPT table is a Kendell (PT) artifact — these keywords only seed it.
# Tightened per clinical review: bare `single_leg` dropped from 97112 (it
# captures single-leg STRENGTHENING, which is 97110 not neuromuscular re-ed);
# `lunge`/`step_up` removed from 97116 gait (a lunge is exercise/activity, not
# gait training — mapping it to gait reads as miscoding to a payer auditor).
_CPT_KEYWORDS: list[tuple[str, tuple[str, ...]]] = [
    ("97112", ("balance", "propriocep", "bird_dog", "stability", "wobble")),
    ("97116", ("gait", "walk", "treadmill", "ambulat")),
    ("97530", ("sit_to_stand", "sts", "functional", "reach", "carry", "step_up", "stair")),
    # lunge / single_leg_* strengthening fall through to 97110 (therapeutic exercise)
    # default below -> 97110
]
_DEFAULT_CPT = "97110"

_DISCLAIMERS = (
    "DRAFT — not a bill. Requires licensed-clinician review, attestation, and "
    "signature before submission.",
    "CPT codes, units, and justification language are AI-drafted and "
    "unverified. A clinician must confirm code selection and the time-based "
    "unit count (8-minute rule) per payer rules.",
    "Built from completed/logged sessions only. Does not imply guaranteed "
    "reimbursement.",
)


def _classify_cpt(exercise_id: str) -> str:
    """Map an exercise_id to a CPT bucket via keyword heuristics. Coarse by design."""
    eid = (exercise_id or "").strip().lower()
    for code, keys in _CPT_KEYWORDS:
        if any(k in eid for k in keys):
            return code
    return _DEFAULT_CPT


def _justification(
    code: str,
    descriptor: str,
    n_sessions: int,
    body_region: str | None,
    payer_model: str,
    date_range: dict[str, str | None],
) -> str:
    """Payer-aware draft justification text for one CPT line. Clinician edits this."""
    region = body_region or "the involved region"
    span = ""
    if date_range.get("start") and date_range.get("end"):
        span = f" ({date_range['start']} to {date_range['end']})"
    if payer_model in ("insurance", "medicare"):
        return (
            f"Skilled {descriptor.lower()} delivered over {n_sessions} "
            f"session(s){span} to address {region} functional deficit; "
            "medically necessary to restore prior level of function and reduce "
            "fall/re-injury risk. Clinician attests to skilled tactile/verbal/"
            "visual cueing — VERIFY before submission."
        )
    return (
        f"{descriptor} — {n_sessions} supervised session(s){span} for {region} "
        "load management. Provided for patient self-submission for possible "
        "out-of-network reimbursement — VERIFY code + units before submitting."
    )


def _completed(sessions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [s for s in (sessions or []) if (s or {}).get("status") == "completed"]


def _date_range(rows: list[dict[str, Any]]) -> dict[str, str | None]:
    """Min/max completed_at (date prefix) across a group of session rows."""
    dates = sorted(
        str(r.get("completed_at") or r.get("created_at") or "")[:10]
        for r in rows
        if (r.get("completed_at") or r.get("created_at"))
    )
    if not dates:
        return {"start": None, "end": None}
    return {"start": dates[0], "end": dates[-1]}


def generate_draft(token: str, *, window_days: int = 56) -> dict[str, Any]:
    """Build a DRAFT super-bill for one patient from completed sessions.

    Returns an unsigned draft even when there are no completed sessions (empty
    line_items + a disclaimer) so the caller can render the screen uniformly.
    Never raises on a missing session store — degrades to an empty draft with a
    note, the same posture as the rest of the patient-state endpoints.
    """
    import user_store

    payer_model = user_store.resolve_payer_model(token)

    body_region: str | None = None
    protocol_goals: list[dict[str, Any]] = []
    try:
        import protocol_repo

        active = protocol_repo.get_active(token)
        if active:
            payload = active.get("payload") or {}
            body_region = payload.get("body_region")
            protocol_goals = payload.get("goals") or []
    except Exception as exc:  # noqa: BLE001 — context is best-effort
        logger.info("superbill: active protocol unavailable token=%s: %s", token, exc)

    notes: list[str] = list(_DISCLAIMERS)
    completed: list[dict[str, Any]] = []
    try:
        import session_repo

        completed = _completed(session_repo.list_recent(token, days=window_days))
    except Exception as exc:  # noqa: BLE001 — session store optional in some envs
        logger.info("superbill: session store unavailable token=%s: %s", token, exc)
        notes.append("Session store unavailable — no completed sessions counted.")

    # Group completed sessions by CPT bucket.
    groups: dict[str, list[dict[str, Any]]] = {}
    for s in completed:
        groups.setdefault(_classify_cpt(s.get("exercise_id", "")), []).append(s)

    line_items: list[dict[str, Any]] = []
    total_units = 0
    for code, rows in sorted(groups.items()):
        descriptor = _CPT_CATALOG.get(code, code)
        # One unit per completed session as a STARTING point; the real
        # time-based unit count needs clinician verification (8-minute rule).
        units = len(rows)
        total_units += units
        dr = _date_range(rows)
        line = {
            "cpt": code,
            "descriptor": descriptor,
            "units": units,
            "session_count": len(rows),
            "exercise_ids": sorted({str(r.get("exercise_id") or "") for r in rows}),
            "date_range": dr,
            "justification": _justification(
                code, descriptor, len(rows), body_region, payer_model, dr,
            ),
            "requires_clinician_attestation": True,
            "needs_verification": True,
        }
        # Medicare medical-necessity differs from commercial (skilled-maintenance
        # vs restorative framing + plan-of-care certification). Lumping it with
        # commercial insurance is fine for a DRAFT, but the draft must say so.
        if payer_model == "medicare":
            line["payer_note"] = (
                "Medicare: verify restorative vs skilled-maintenance framing and "
                "plan-of-care certification before submission."
            )
        line_items.append(line)

    return {
        "status": "draft_unsigned",
        "payer_model": payer_model,
        "patient_token": token,
        "body_region": body_region,
        "protocol_goals": protocol_goals,
        "requires_clinician_attestation": True,
        "source": "completed_sessions_only",
        "window_days": window_days,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "line_items": line_items,
        "totals": {
            "total_units": total_units,
            "total_sessions": len(completed),
            "n_line_items": len(line_items),
            # The unverified status travels WITH the number so a clinician (or
            # patient) can't read total_units as a real billable count.
            "needs_verification": True,
            "basis": "1-unit-per-session placeholder; 8-minute rule not applied",
        },
        "disclaimers": notes,
    }
