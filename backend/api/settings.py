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
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
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
