"""api/settings.py — Profile / Settings endpoints (patient + clinician).

Lives out of the main.py god-file: one router, mounted with a single
include_router line. Covers the four Settings areas:

  Patient:
    POST   /patient/me/profile     set display name (intake-payload canonical)
    GET    /patient/me/consent     consent status (recorded | not_recorded)
    POST   /patient/me/consent     record consent
    GET    /patient/me/export      self-scoped JSON of the caller's OWN data
    DELETE /patient/me             destructive self-delete (confirm-gated)

  Clinician:
    GET    /clinician/me/profile   clinician display name + email-less profile
    POST   /clinician/me/profile   set clinician display name

Self-scope is load-bearing. db.get_conn uses a service-role DSN that bypasses
RLS and sets no per-request auth.uid, so the `WHERE token = %s` predicate fed
ONLY from current_user_id is the sole self-scope guard. Every route here
derives the token from Depends(current_user_id) / Depends(require_clinician_id)
and NEVER accepts a token from the request body or path. A patient can never
read or delete another patient's rows.

PHI hygiene: never log names, metric values, or export contents at INFO. Export
and delete log only the action + a token-present marker + per-section counts.
No emojis, no exclamation marks.
"""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel

import user_store
from auth import current_user_id, require_clinician_id

logger = logging.getLogger(__name__)

router = APIRouter(tags=["settings"])


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ── Patient: display name ─────────────────────────────────────────────────────


class ProfileUpdate(BaseModel):
    """Body for setting a display name (patient or clinician)."""
    name: str


@router.post("/patient/me/profile")
def set_patient_profile(
    body: ProfileUpdate,
    user_id: str = Depends(current_user_id),
) -> dict[str, str]:
    """Set the patient's own display name on the canonical intake payload.

    Self-scoped: token = current_user_id only. A blank name 400s rather than
    storing a value the resolver would skip. Never logs the name (PHI).
    """
    try:
        stored = user_store.set_display_name(user_id, body.name)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    logger.info("display_name set token=%s", user_id)
    return {"name": stored}


# ── Patient: consent status ───────────────────────────────────────────────────


@router.get("/patient/me/consent")
def get_patient_consent(user_id: str = Depends(current_user_id)) -> dict[str, Any]:
    """Return the patient's consent status (recorded | not_recorded)."""
    return user_store.get_consent(user_id)


@router.post("/patient/me/consent")
def record_patient_consent(user_id: str = Depends(current_user_id)) -> dict[str, Any]:
    """Record the patient's consent on the canonical intake payload."""
    try:
        consent = user_store.set_consent(user_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    logger.info("consent recorded token=%s", user_id)
    return consent


# ── Patient: data export ──────────────────────────────────────────────────────


def _gather_export(user_id: str) -> dict[str, Any]:
    """Assemble the caller's OWN data into one JSON object, self-scoped.

    Every repo is called with user_id only. Each section is wrapped so a
    missing DATABASE_URL (sqlite/CI) or one bad section degrades to an empty
    value instead of 500ing the whole export. NEVER logs section contents.
    """
    export: dict[str, Any] = {
        "exported_at": _now(),
        "token": user_id,
    }

    # account: users row + latest health + intake + protocol_state + checkins.
    try:
        export["account"] = user_store.load_user(user_id)
    except Exception as exc:  # noqa: BLE001 - degrade, never 500 the export
        logger.warning("export account section failed token=%s: %s", user_id, exc)
        export["account"] = None

    # consent status (intake-payload backed).
    try:
        export["consent"] = user_store.get_consent(user_id)
    except Exception:  # noqa: BLE001
        export["consent"] = {"status": "not_recorded", "recorded_at": None}

    # protocols: all versions, all statuses (postgres-only).
    try:
        import protocol_repo

        export["protocols"] = protocol_repo.list_by_token(user_id)
    except Exception as exc:  # noqa: BLE001
        logger.warning("export protocols section failed token=%s: %s", user_id, exc)
        export["protocols"] = []

    # sessions: effectively all (generous date window), postgres-only.
    try:
        import session_repo

        export["sessions"] = session_repo.list_recent(user_id, days=3650)
    except Exception as exc:  # noqa: BLE001
        logger.warning("export sessions section failed token=%s: %s", user_id, exc)
        export["sessions"] = []

    # video_sessions: metadata only, postgres-only.
    try:
        import tavus_repo

        export["video_sessions"] = tavus_repo.list_recent(user_id, limit=50)
    except Exception as exc:  # noqa: BLE001
        logger.warning("export video section failed token=%s: %s", user_id, exc)
        export["video_sessions"] = []

    # wearable: connection state + cached_metrics (postgres-only).
    try:
        import junction_repo

        export["wearable"] = junction_repo.get_by_token(user_id)
    except Exception as exc:  # noqa: BLE001
        logger.warning("export wearable section failed token=%s: %s", user_id, exc)
        export["wearable"] = None

    return export


def _json_default(value: Any) -> str:
    """datetime -> ISO; UUID/other -> str. Keeps the export serializable."""
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


@router.get("/patient/me/export")
def export_patient_data(user_id: str = Depends(current_user_id)) -> Response:
    """Self-scoped JSON export of the caller's OWN data, as a file download.

    The token is derived solely from current_user_id; no body/path token is
    accepted, so a patient can only ever export their own rows. Logs section
    counts only — never the contents.
    """
    export = _gather_export(user_id)
    payload = json.dumps(export, default=_json_default, indent=2)
    logger.info(
        "data export token=%s protocols=%d sessions=%d video=%d",
        user_id,
        len(export.get("protocols") or []),
        len(export.get("sessions") or []),
        len(export.get("video_sessions") or []),
    )
    return Response(
        content=payload,
        media_type="application/json",
        headers={
            "Content-Disposition": 'attachment; filename="rehab-data-export.json"',
        },
    )


# ── Patient: destructive account delete ───────────────────────────────────────


class DeleteAccountRequest(BaseModel):
    """Confirmation body for the destructive self-delete."""
    confirm: str


_DELETE_CONFIRM_TOKEN = "DELETE"


@router.delete("/patient/me")
def delete_patient_account(
    body: DeleteAccountRequest,
    user_id: str = Depends(current_user_id),
) -> dict[str, bool]:
    """Permanently delete the caller's OWN data. Confirmation-gated.

    Requires body.confirm == 'DELETE' (server-validated) before any DB write;
    a mismatch 400s. Deletes ONLY the authenticated patient's users row via
    user_store.delete_account(user_id), which CASCADEs to every child table.
    Self-scoped: token = current_user_id only — never a body/path token, so a
    patient can never delete another patient's data.

    Residual (documented, v1): the Supabase auth.users login row is NOT
    deleted (separate schema; pooled DSN lacks perms; needs the Admin API) and
    pipeline_runs rows survive (NOT token-scoped; protocol_id SET NULL). All
    public.* PHI is erased; both residuals flagged for v2.
    """
    if (body.confirm or "").strip() != _DELETE_CONFIRM_TOKEN:
        raise HTTPException(status_code=400, detail="confirmation required")
    try:
        user_store.delete_account(user_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    logger.info("account deleted token=%s", user_id)
    return {"deleted": True}


# ── Clinician: profile (display name) ─────────────────────────────────────────


@router.get("/clinician/me/profile")
def get_clinician_profile(
    user_id: str = Depends(require_clinician_id),
) -> dict[str, Any]:
    """Return the clinician's own profile (display name; email is client-side).

    Self-scoped to the authenticated clinician's user_id.
    """
    return {"display_name": user_store.get_clinician_display_name(user_id)}


@router.post("/clinician/me/profile")
def set_clinician_profile(
    body: ProfileUpdate,
    user_id: str = Depends(require_clinician_id),
) -> dict[str, str]:
    """Set the clinician's own display name on staff_users (self-scoped).

    A blank name 400s. Targets the staff_users base table scoped to the
    authenticated user_id, never the clinicians VIEW.
    """
    try:
        stored = user_store.set_clinician_display_name(user_id, body.name)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    logger.info("clinician display_name set user=%s", user_id)
    return {"display_name": stored}


# ── Patient: notification / reminder preferences ──────────────────────────────
#
# Settings v2. Delivery (email/push) is NOT built — these are stored only and
# surfaced honestly in the UI as "coming soon". Backed by the canonical intake
# payload (no migration), same merge seam as consent. PHI: never log values.


class NotificationPrefs(BaseModel):
    """Patient notification / reminder toggles (all optional; default applied)."""
    session_reminders: bool | None = None
    checkin_reminders: bool | None = None
    plan_updated: bool | None = None
    symptom_flag_receipts: bool | None = None
    email_opt_in: bool | None = None


@router.get("/patient/me/notifications")
def get_patient_notifications(
    user_id: str = Depends(current_user_id),
) -> dict[str, bool]:
    """Return the patient's notification prefs (benign defaults when unset)."""
    return user_store.get_notification_prefs(user_id)


@router.post("/patient/me/notifications")
def set_patient_notifications(
    body: NotificationPrefs,
    user_id: str = Depends(current_user_id),
) -> dict[str, bool]:
    """Persist the patient's notification prefs on the canonical intake payload.

    Self-scoped to current_user_id. Unset fields fall back to the stored /
    default value (set_notification_prefs coerces the known keys only). Never
    logs the pref VALUES — only the action + token.
    """
    current = user_store.get_notification_prefs(user_id)
    merged = {**current, **body.model_dump(exclude_none=True)}
    try:
        stored = user_store.set_notification_prefs(user_id, merged)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    logger.info("notification_prefs set token=%s", user_id)
    return stored


# ── Patient: display preferences (theme / text-size / reduced-motion) ─────────
#
# Applied CLIENT-side from localStorage (instant, source of truth); this is the
# durable cross-device mirror.


class DisplayPrefs(BaseModel):
    """Patient display prefs (all optional; constrained to known values)."""
    theme: str | None = None
    text_size: str | None = None
    reduced_motion: bool | None = None


@router.get("/patient/me/display")
def get_patient_display(
    user_id: str = Depends(current_user_id),
) -> dict[str, Any]:
    """Return the patient's display prefs (theme/text_size/reduced_motion)."""
    return user_store.get_display_prefs(user_id)


@router.post("/patient/me/display")
def set_patient_display(
    body: DisplayPrefs,
    user_id: str = Depends(current_user_id),
) -> dict[str, Any]:
    """Persist the patient's display prefs (durable mirror; self-scoped)."""
    current = user_store.get_display_prefs(user_id)
    merged = {**current, **body.model_dump(exclude_none=True)}
    try:
        stored = user_store.set_display_prefs(user_id, merged)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    logger.info("display_prefs set token=%s", user_id)
    return stored


# ── Patient: Coach Maya preferences ───────────────────────────────────────────
#
# `voice` gates the in-call rep-count echo (frontend mirrors it into
# localStorage 'rac-maya-voice' for a synchronous per-rep read). greeting_cadence
# gates the state-aware greeting. language is stored only (English-only copy).


class CoachPrefs(BaseModel):
    """Patient Coach Maya prefs (all optional; constrained to known values)."""
    voice: bool | None = None
    greeting_cadence: str | None = None
    language: str | None = None


@router.get("/patient/me/coach-prefs")
def get_patient_coach_prefs(
    user_id: str = Depends(current_user_id),
) -> dict[str, Any]:
    """Return the patient's Coach Maya prefs (voice/greeting_cadence/language)."""
    return user_store.get_coach_prefs(user_id)


@router.post("/patient/me/coach-prefs")
def set_patient_coach_prefs(
    body: CoachPrefs,
    user_id: str = Depends(current_user_id),
) -> dict[str, Any]:
    """Persist the patient's Coach Maya prefs (self-scoped)."""
    current = user_store.get_coach_prefs(user_id)
    merged = {**current, **body.model_dump(exclude_none=True)}
    try:
        stored = user_store.set_coach_prefs(user_id, merged)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    logger.info("coach_prefs set token=%s", user_id)
    return stored


# ── Patient: care team & support (read-only) ──────────────────────────────────
#
# Read-only display: the clinic contact + the clinician who reviews the plan +
# the static flare/urgent safety block (rendered frontend-side). The phone
# resolves via the clinic-profile precedence helper. PHI: log nothing here.


@router.get("/patient/me/care-team")
def get_patient_care_team(
    user_id: str = Depends(current_user_id),
) -> dict[str, Any]:
    """Return the patient's care-team display data (read-only, self-scoped).

    Assembles: clinic_phone (resolve_clinic_phone precedence), clinic_name
    (single-clinic best-effort), and the reviewing clinician's display name (the
    reviewed_by on the active / latest-reviewed protocol -> clinician name).
    Every leg is best-effort and degrades to None — never 5xx. No values logged.
    """
    clinic_phone = None
    try:
        clinic_phone = user_store.resolve_clinic_phone()
    except Exception:  # noqa: BLE001 - degrade, never 500 the care-team view
        clinic_phone = None

    reviewing_clinician_name = None
    clinic_name = None
    try:
        import protocol_repo

        active = protocol_repo.get_active(user_id)
        reviewed_by = (active or {}).get("reviewed_by")
        if reviewed_by:
            reviewing_clinician_name = user_store.get_clinician_display_name(
                reviewed_by,
            )
            profile = user_store.get_clinic_profile(reviewed_by)
            clinic_name = profile.get("clinic_name")
    except Exception as exc:  # noqa: BLE001 - best-effort context
        logger.info("care-team context unavailable token=%s: %s", user_id, exc)

    return {
        "clinic_name": clinic_name,
        "clinic_phone": clinic_phone,
        "reviewing_clinician_name": reviewing_clinician_name,
    }


# ── Clinician: clinic profile ─────────────────────────────────────────────────
#
# Settings v2. Postgres-only (new staff_users columns); degrades cleanly when
# DATABASE_URL is absent. clinic_phone feeds the flare escalation; signature +
# clinic_name appear on the generated super-bill. PHI: never log the values.


class ClinicProfileUpdate(BaseModel):
    """Clinic-profile fields (all optional; only provided fields are written)."""
    clinic_name: str | None = None
    clinic_phone: str | None = None
    license_number: str | None = None
    signature: str | None = None


@router.get("/clinician/me/clinic-profile")
def get_clinician_clinic_profile(
    user_id: str = Depends(require_clinician_id),
) -> dict[str, str | None]:
    """Return the clinician's clinic-profile fields (None each when unset / no DB)."""
    return user_store.get_clinic_profile(user_id)


@router.post("/clinician/me/clinic-profile")
def set_clinician_clinic_profile(
    body: ClinicProfileUpdate,
    user_id: str = Depends(require_clinician_id),
) -> dict[str, str | None]:
    """Persist the clinician's clinic-profile fields on staff_users (self-scoped).

    Only the provided fields are written (an empty string clears one to NULL).
    400s when the staff store is unavailable. Never logs the field VALUES.
    """
    provided = body.model_dump(exclude_none=True)
    try:
        stored = user_store.set_clinic_profile(user_id, provided)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    logger.info("clinic_profile set user=%s", user_id)
    return stored


# ── Clinician: review notifications (stored only; no delivery in v1) ───────────


class NotifPrefsUpdate(BaseModel):
    """Clinician review-alert toggles (all optional; default applied)."""
    new_review_drafts: bool | None = None
    high_severity_flags: bool | None = None


@router.get("/clinician/me/notif-prefs")
def get_clinician_notif_prefs(
    user_id: str = Depends(require_clinician_id),
) -> dict[str, bool]:
    """Return the clinician's review-alert prefs (benign defaults when unset)."""
    return user_store.get_clinician_notif_prefs(user_id)


@router.post("/clinician/me/notif-prefs")
def set_clinician_notif_prefs(
    body: NotifPrefsUpdate,
    user_id: str = Depends(require_clinician_id),
) -> dict[str, bool]:
    """Persist the clinician's review-alert prefs (JSONB on staff_users)."""
    current = user_store.get_clinician_notif_prefs(user_id)
    merged = {**current, **body.model_dump(exclude_none=True)}
    try:
        stored = user_store.set_clinician_notif_prefs(user_id, merged)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    logger.info("clinician notif_prefs set user=%s", user_id)
    return stored


# ── Clinician: per-payer goal templates ───────────────────────────────────────
#
# Surfaced + stored now; the cheap planner wire injects the per-payer text as
# style guidance. Deep pipeline integration is PHASED. PHI surface: the text is
# Anthropic-bound, so the UI steers clinicians to GENERIC language — never
# patient-specific. We do not log the template TEXT.


class GoalTemplatesUpdate(BaseModel):
    """Per-payer goal-language templates (all optional; only provided written)."""
    insurance: str | None = None
    medicare: str | None = None
    cash: str | None = None


@router.get("/clinician/me/goal-templates")
def get_clinician_goal_templates(
    user_id: str = Depends(require_clinician_id),
) -> dict[str, str]:
    """Return the clinician's per-payer goal templates (empty strings when unset)."""
    return user_store.get_clinician_goal_templates(user_id)


@router.post("/clinician/me/goal-templates")
def set_clinician_goal_templates(
    body: GoalTemplatesUpdate,
    user_id: str = Depends(require_clinician_id),
) -> dict[str, str]:
    """Persist the clinician's per-payer goal templates (JSONB on staff_users)."""
    current = user_store.get_clinician_goal_templates(user_id)
    merged = {**current, **body.model_dump(exclude_none=True)}
    try:
        stored = user_store.set_clinician_goal_templates(user_id, merged)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    logger.info("clinician goal_templates set user=%s", user_id)
    return stored


# ── Internal: scheduled reminder cron ─────────────────────────────────────────
#
# Scheduled reminders (session / daily check-in) have no inline app event to
# hang off the way plan_updated and symptom_flag_receipts do, so they need a
# scheduler to call them periodically. This endpoint is the sender; the SCHEDULE
# is the remaining deploy step.
#
# DEPLOY STEP (not wired this pass, flagged honestly): add a Vercel cron in
# vercel.json so the platform hits this endpoint daily, e.g.
#
#   { "crons": [ { "path": "/internal/cron/reminders", "schedule": "0 16 * * *" } ] }
#
# Vercel cron requests cannot set a custom header, so the production wiring will
# either move the shared secret into the path/query or rely on Vercel's signed
# cron request — that decision is part of the deploy step. Until the cron is
# live, NO scheduled reminders are delivered; the patient Settings UI must not
# over-promise daily reminders (they remain saved preferences only).
#
# Auth: a shared-secret header (X-Cron-Secret == INTERNAL_CRON_SECRET). When the
# secret is unset the endpoint 503s (closed by default) rather than running
# unauthenticated. It is NOT a user endpoint — no current_user_id dependency.

_CRON_SECRET_HEADER = "X-Cron-Secret"


class CronReminderResult(BaseModel):
    """Summary of one reminder-cron run (no PHI — counts only)."""
    candidates: int
    session_sent: int
    checkin_sent: int


@router.post("/internal/cron/reminders", response_model=CronReminderResult)
def run_reminder_cron(
    x_cron_secret: str | None = Header(default=None, alias=_CRON_SECRET_HEADER),
) -> CronReminderResult:
    """Send pref-gated session + check-in reminders to patients with active plans.

    Guarded by a shared secret. Iterates patients with an active protocol and
    fires the two scheduled-reminder emails; each send is internally gated on
    email_opt_in + its per-type pref and is fully fail-open, so a missing
    recipient or a provider error is silently skipped. Returns counts only —
    never any PHI. A DB outage degrades to a zero-candidate run rather than 5xx.
    """
    configured_secret = (os.getenv("INTERNAL_CRON_SECRET") or "").strip()
    if not configured_secret:
        # Closed by default: refuse to run unauthenticated.
        raise HTTPException(status_code=503, detail="cron not configured")
    if (x_cron_secret or "").strip() != configured_secret:
        raise HTTPException(status_code=403, detail="forbidden")

    import notifications

    try:
        import protocol_repo

        tokens = protocol_repo.list_active_tokens()
    except Exception as exc:  # noqa: BLE001 - degrade to an empty run, never 5xx
        logger.warning("reminder cron roster lookup failed: %s", type(exc).__name__)
        tokens = []

    session_sent = 0
    checkin_sent = 0
    for token in tokens:
        try:
            if notifications.send_session_reminder(token):
                session_sent += 1
            if notifications.send_checkin_reminder(token):
                checkin_sent += 1
        except Exception:  # noqa: BLE001 - one bad patient never stalls the run
            logger.warning("reminder send raised for one patient (non-fatal)")

    logger.info(
        "reminder cron run candidates=%d session_sent=%d checkin_sent=%d",
        len(tokens), session_sent, checkin_sent,
    )
    return CronReminderResult(
        candidates=len(tokens),
        session_sent=session_sent,
        checkin_sent=checkin_sent,
    )
