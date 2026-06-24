"""
api/junction.py - Junction (the rebrand of Vital) connect / refresh / status.

Lives out of the main.py god-file: one router, mounted under /api/junction, all
routes gated by Depends(current_user_id). Drives the hosted-Link flow so a
patient can connect a wearable (Oura / Garmin / Apple Health) and have their REAL
sleep / HRV / recovery flow into the dashboard + the whole clinical pipeline. Any
Junction failure degrades to mock — these endpoints never 500 the dashboard.

Flow:
  POST /api/junction/link    create-or-get the Junction user, mint a fresh
                             single-use link_web_url, upsert a pending row.
                             Returns {link_web_url, status} ONLY — never the key.
  POST /api/junction/refresh pull the 7-day window, map, cache, flip to
                             'connected'. On failure: set_error + status='error'
                             (HTTP 200, non-fatal).
  GET  /api/junction/status  {status, providers, last_synced_at, isLive} for the
                             dashboard. No raw metric values.
  POST /api/junction/demo-connect  SANDBOX-only synthetic connection.

PHI hygiene: never log vital_user_id or raw metric values at INFO; the Team API
key is read server-side only (junction_client.build_config). Never trust a token
from the request body — identity comes from current_user_id (the JWT).
"""
from __future__ import annotations

import logging
import os
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

import junction_repo
from auth import current_user_id
from junction_client import JunctionClient, JunctionError, build_config

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/junction", tags=["junction"])


def _redirect_url() -> str | None:
    url = (os.getenv("JUNCTION_REDIRECT_URL") or "").strip()
    return url or None


@router.post("/link")
def create_link(user_id: str = Depends(current_user_id)) -> dict[str, Any]:
    """Create-or-get the patient's Junction user and mint a hosted-Link URL.

    The frontend redirects the browser to link_web_url; the patient connects a
    device on Junction's hosted page and is returned to JUNCTION_REDIRECT_URL.
    503 when Junction is not configured (no API key) so the UI can surface a
    clear "not available" message rather than silently failing.
    """
    config = build_config()
    if not config:
        raise HTTPException(503, detail="Junction is not configured")

    client = JunctionClient(config)
    try:
        # client_user_id is our auth.uid token — never PII.
        vital_user_id = client.create_or_get_user(client_user_id=user_id)
    except JunctionError:
        logger.warning("junction: create_or_get_user failed")
        raise HTTPException(502, detail="Could not create wearable connection")

    try:
        junction_repo.upsert_pending(user_id, vital_user_id)
    except junction_repo.JunctionRepoError:
        logger.warning("junction: upsert_pending failed (db unavailable)")
        raise HTTPException(503, detail="Connection store unavailable")

    try:
        link = client.create_link_token(vital_user_id, redirect_url=_redirect_url())
    except JunctionError:
        logger.warning("junction: create_link_token failed")
        raise HTTPException(502, detail="Could not start wearable connection")

    return {"link_web_url": link["link_web_url"], "status": "pending"}


@router.post("/refresh")
def refresh(user_id: str = Depends(current_user_id)) -> dict[str, Any]:
    """Pull the latest 7-day window from Junction, map, cache, flip to connected.

    Non-fatal: on any Junction/repo error the row is marked 'error' and we return
    HTTP 200 with status='error' so the dashboard keeps rendering mock defaults
    instead of seeing a 5xx.
    """
    try:
        row = junction_repo.get_by_token(user_id)
    except junction_repo.JunctionRepoError:
        raise HTTPException(503, detail="Connection store unavailable")

    if not row or not row.get("vital_user_id"):
        raise HTTPException(404, detail="No wearable connection to refresh")

    config = build_config()
    if not config:
        raise HTTPException(503, detail="Junction is not configured")

    vital_user_id = row["vital_user_id"]
    # Reuse the single fetch+map seam (health_mock._refresh_junction_metrics) so
    # the network round trip lives in exactly one place. This is the out-of-band
    # refresh path the frontend calls after render — the read path itself never
    # touches Junction over the wire.
    from health_mock import _provider_list, _refresh_junction_metrics

    try:
        mapped = _refresh_junction_metrics(vital_user_id)
        if not mapped:
            raise JunctionError("Junction fetch returned no data")
        saved = junction_repo.set_connected(user_id, _provider_list(mapped), mapped)
    except (JunctionError, junction_repo.JunctionRepoError):
        logger.warning("junction: refresh failed, marking error")
        try:
            junction_repo.set_error(user_id)
        except junction_repo.JunctionRepoError:
            pass
        return {"status": "error", "last_synced_at": None}
    except Exception:  # noqa: BLE001 — mapping bug must not 500 the dashboard
        logger.warning("junction: unexpected refresh error, marking error")
        try:
            junction_repo.set_error(user_id)
        except junction_repo.JunctionRepoError:
            pass
        return {"status": "error", "last_synced_at": None}

    return {"status": "connected", "last_synced_at": saved.get("last_synced_at")}


@router.get("/status")
def status(user_id: str = Depends(current_user_id)) -> dict[str, Any]:
    """Connection state for the dashboard. NO raw metric values.

    Always returns a benign 'not_connected' shape when there is no row or the DB
    is unavailable, so the dashboard never errors on this read.
    """
    try:
        row = junction_repo.get_by_token(user_id)
    except junction_repo.JunctionRepoError:
        return {"status": "not_connected", "providers": [], "last_synced_at": None, "isLive": False}

    if not row:
        return {"status": "not_connected", "providers": [], "last_synced_at": None, "isLive": False}

    is_connected = row.get("status") == "connected"
    return {
        "status": row.get("status") or "not_connected",
        "providers": row.get("providers") or [],
        "last_synced_at": row.get("last_synced_at"),
        "isLive": bool(is_connected),
    }


class DemoConnectRequest(BaseModel):
    provider: str = "oura"


@router.post("/demo-connect")
def demo_connect(
    req: DemoConnectRequest, user_id: str = Depends(current_user_id)
) -> dict[str, Any]:
    """SANDBOX-only: create a synthetic Junction connection without a device.

    Guarded on JUNCTION_ENV=sandbox so it can never run against production data.
    Bypasses the Link widget; Junction backfills synthetic data for the provider.
    """
    env = (os.getenv("JUNCTION_ENV") or "sandbox").strip().lower()
    if env == "production":
        raise HTTPException(403, detail="demo-connect is sandbox-only")

    config = build_config()
    if not config:
        raise HTTPException(503, detail="Junction is not configured")

    try:
        row = junction_repo.get_by_token(user_id)
    except junction_repo.JunctionRepoError:
        raise HTTPException(503, detail="Connection store unavailable")

    client = JunctionClient(config)
    try:
        if not row or not row.get("vital_user_id"):
            vital_user_id = client.create_or_get_user(client_user_id=user_id)
            junction_repo.upsert_pending(user_id, vital_user_id)
        else:
            vital_user_id = row["vital_user_id"]
        client.connect_demo(vital_user_id, req.provider)
    except (JunctionError, junction_repo.JunctionRepoError):
        logger.warning("junction: demo-connect failed")
        raise HTTPException(502, detail="Could not create demo connection")

    return {"status": "pending", "provider": req.provider}
