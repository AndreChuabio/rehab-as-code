"""
junction_client.py - lightweight Junction (the rebrand of Vital) API wrapper.

Mirrors open_wearables_client.py: a small httpx client with a call-time config
dataclass, a single typed error (JunctionError), and raise_for_status wrapped so
every caller can degrade-to-mock. NO heavy SDK — httpx is already a dependency of
the Open Wearables path, so no requirements.txt change is needed.

Scope:
  - create_or_get_user(client_user_id)  POST /v2/user            (idempotent)
  - create_link_token(user_id, ...)     POST /v2/link/token      (hosted Link)
  - fetch_daily(user_id, start, end)    GET  /v2/summary/{sleep,activity}/{uid}
  - connect_demo(user_id, provider)     POST /v2/link/connect/demo (sandbox only)

fetch_daily returns RAW per-day rows in the same shape the Open Wearables path
hands to health_mock._build_full_record: {date, sleep_hours, hrv_ms, resting_hr,
steps_yesterday, calories_burned, source:'junction'}. Score derivation +
trend math stay in health_mock so every source is identical downstream.

Auth: the Team API key is sent server-side as the 'x-vital-api-key' header. It is
read at CALL TIME from VITAL_API_KEY (alias JUNCTION_API_KEY), never at import,
never logged, never returned to the browser.

UNITS GOTCHA: Junction sleep total/duration is in SECONDS — divide by 3600 for
hours. The Open Wearables path divides minutes by 60; copying that constant
silently corrupts sleep_hours and the sleep<70 gate.

PHI hygiene: never log the API key, vital_user_id, or raw metric values at INFO.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from datetime import date
from typing import Any

import httpx

logger = logging.getLogger(__name__)


class JunctionError(Exception):
    """Raised when a Junction (Vital) integration call fails."""


@dataclass
class JunctionConfig:
    api_key: str
    base_url: str
    # Kept deliberately low: fetch_daily issues TWO sequential GETs under this
    # budget, so worst-case wall time is ~2 * timeout_seconds. The read path no
    # longer refreshes inline (the resolver serves the stale cache and lets the
    # frontend trigger /api/junction/refresh out-of-band), but /refresh itself
    # and the demo connect still run here, so a tight deadline bounds them well
    # under the Vercel function timeout.
    timeout_seconds: float = 3.5


def _httpx_timeout(seconds: float) -> "httpx.Timeout":
    """Explicit connect + read budget so a hung TCP connect can't stall a leg.

    A single float on httpx.Client applies the same value to every phase; we
    pin a shorter connect so a dead host fails fast instead of burning the whole
    read budget waiting for the handshake.
    """
    connect = min(2.0, seconds)
    return httpx.Timeout(seconds, connect=connect)


def _to_number(value: Any) -> float:
    """Null-safe numeric coercion — a null average_hrv/score never crashes the map."""
    try:
        if value is None:
            return 0.0
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def build_config() -> JunctionConfig | None:
    """Assemble a JunctionConfig from env at call time.

    Returns None (caller degrades to mock) when no API key is configured.
    Base URL precedence:
      1. JUNCTION_API_BASE explicit override
      2. composed from JUNCTION_ENV (sandbox|production) + JUNCTION_REGION (us|eu)
         -> https://api.{sandbox.}{region}.junction.com
    """
    api_key = (os.getenv("VITAL_API_KEY") or os.getenv("JUNCTION_API_KEY") or "").strip()
    if not api_key:
        return None

    override = (os.getenv("JUNCTION_API_BASE") or "").strip()
    if override:
        base_url = override
    else:
        env = (os.getenv("JUNCTION_ENV") or "sandbox").strip().lower()
        region = (os.getenv("JUNCTION_REGION") or "us").strip().lower()
        sandbox_seg = "sandbox." if env != "production" else ""
        base_url = f"https://api.{sandbox_seg}{region}.junction.com"

    return JunctionConfig(api_key=api_key, base_url=base_url)


class JunctionClient:
    def __init__(self, config: JunctionConfig):
        self.config = config
        self.headers = {"x-vital-api-key": config.api_key}
        self.base_url = config.base_url.rstrip("/")

    # ── User + Link ──────────────────────────────────────────────────────────

    def create_or_get_user(self, client_user_id: str) -> str:
        """POST /v2/user {client_user_id} -> Junction user_id (UUID).

        Idempotent: Junction returns a 409/400 conflict when the client_user_id
        already maps to a user; we parse the existing user_id out of the error
        body and reuse it rather than failing. client_user_id is our auth.uid
        token — never PII.
        """
        if not client_user_id:
            raise JunctionError("client_user_id is required")

        url = f"{self.base_url}/v2/user"
        with httpx.Client(timeout=_httpx_timeout(self.config.timeout_seconds)) as client:
            try:
                resp = client.post(url, headers=self.headers, json={"client_user_id": client_user_id})
            except httpx.HTTPError as exc:
                raise JunctionError(f"Junction create-user request failed: {exc}") from exc

            if resp.status_code in (400, 409):
                existing = self._extract_conflict_user_id(resp)
                if existing:
                    return existing
            try:
                resp.raise_for_status()
                body = resp.json()
            except httpx.HTTPError as exc:
                raise JunctionError(f"Junction create-user failed: {exc}") from exc
            except ValueError as exc:
                raise JunctionError("Junction create-user returned invalid JSON") from exc

        user_id = body.get("user_id") or body.get("user_key")
        if not user_id:
            raise JunctionError("Junction create-user response had no user_id")
        return str(user_id)

    def _extract_conflict_user_id(self, resp: httpx.Response) -> str | None:
        """Best-effort parse of an existing user_id from a conflict error body."""
        try:
            body = resp.json()
        except ValueError:
            return None
        if not isinstance(body, dict):
            return None
        # Junction surfaces the existing id either at top level or nested in detail.
        candidate = body.get("user_id") or body.get("user_key")
        if candidate:
            return str(candidate)
        detail = body.get("detail")
        if isinstance(detail, dict):
            nested = detail.get("user_id") or detail.get("user_key")
            if nested:
                return str(nested)
        return None

    def delete_user(self, vital_user_id: str) -> bool:
        """DELETE /v2/user/{vital_user_id} -> {success: bool}.

        Deregisters ALL of the user's provider connections immediately and
        permanently erases their data after Junction's 7-day grace window.
        Idempotent enough for our use: a 404 (already deleted) is treated as
        success since the end state is the same. Used by the wearable
        disconnect flow; the caller clears our local row regardless so a
        Junction-side failure never strands the patient.

        vital_user_id is a Junction-side pointer — never logged at INFO.
        """
        if not vital_user_id:
            raise JunctionError("vital_user_id is required")

        headers = {**self.headers, "Accept": "application/json"}
        url = f"{self.base_url}/v2/user/{vital_user_id}"
        with httpx.Client(timeout=_httpx_timeout(self.config.timeout_seconds)) as client:
            try:
                resp = client.delete(url, headers=headers)
            except httpx.HTTPError as exc:
                raise JunctionError(f"Junction delete-user request failed: {exc}") from exc

            if resp.status_code == 404:
                return True
            try:
                resp.raise_for_status()
            except httpx.HTTPError as exc:
                raise JunctionError(f"Junction delete-user failed: {exc}") from exc
            try:
                body = resp.json()
            except ValueError:
                # A 2xx with a non-JSON body still means the delete landed.
                return True
        if isinstance(body, dict) and "success" in body:
            return bool(body.get("success"))
        return True

    def create_link_token(self, user_id: str, redirect_url: str | None = None) -> dict[str, Any]:
        """POST /v2/link/token -> {link_token, link_web_url}.

        link_web_url is the hosted-Link page the vanilla-JS frontend redirects
        to. The token is single-use, ~10-min expiry — mint fresh per /link call.
        """
        if not user_id:
            raise JunctionError("user_id is required")

        payload: dict[str, Any] = {
            "user_id": user_id,
            "on_close": "redirect",
            "on_error": "redirect",
        }
        if redirect_url:
            payload["redirect_url"] = redirect_url

        url = f"{self.base_url}/v2/link/token"
        with httpx.Client(timeout=_httpx_timeout(self.config.timeout_seconds)) as client:
            try:
                resp = client.post(url, headers=self.headers, json=payload)
                resp.raise_for_status()
                body = resp.json()
            except httpx.HTTPError as exc:
                raise JunctionError(f"Junction link-token request failed: {exc}") from exc
            except ValueError as exc:
                raise JunctionError("Junction link-token returned invalid JSON") from exc

        link_web_url = body.get("link_web_url")
        link_token = body.get("link_token")
        if not link_web_url:
            raise JunctionError("Junction link-token response had no link_web_url")
        return {"link_token": link_token, "link_web_url": link_web_url}

    def connect_demo(self, user_id: str, provider: str) -> dict[str, Any]:
        """POST /v2/link/connect/demo -> synthetic connection (SANDBOX only).

        Bypasses the Link widget: Junction backfills ~30d of synthetic data for
        the given provider so a demo works without a real device. provider is one
        of apple_healthkit | fitbit | oura | freestyle_libre.
        """
        if not user_id or not provider:
            raise JunctionError("user_id and provider are required")

        url = f"{self.base_url}/v2/link/connect/demo"
        with httpx.Client(timeout=_httpx_timeout(self.config.timeout_seconds)) as client:
            try:
                resp = client.post(
                    url, headers=self.headers, json={"user_id": user_id, "provider": provider}
                )
                resp.raise_for_status()
                return resp.json()
            except httpx.HTTPError as exc:
                raise JunctionError(f"Junction demo-connect request failed: {exc}") from exc
            except ValueError as exc:
                raise JunctionError("Junction demo-connect returned invalid JSON") from exc

    # ── Data ─────────────────────────────────────────────────────────────────

    def fetch_daily(self, user_id: str, start_date: date, end_date: date) -> list[dict[str, Any]]:
        """Pull the sleep + activity windows and merge into raw per-day rows.

        GET /v2/summary/sleep/{uid}?start_date&end_date    -> {"sleep": [...]}
        GET /v2/summary/activity/{uid}?start_date&end_date -> {"activity": [...]}

        Returns one raw dict per day present in either response, in the shape
        health_mock._build_full_record consumes. Days missing from both come out
        zeroed by the resolver's window fill (same as the Open Wearables path).
        """
        if not user_id:
            raise JunctionError("user_id is required")

        params = {"start_date": start_date.isoformat(), "end_date": end_date.isoformat()}
        with httpx.Client(timeout=_httpx_timeout(self.config.timeout_seconds)) as client:
            sleep_rows = self._get_list(client, f"/v2/summary/sleep/{user_id}", params, "sleep")
            activity_rows = self._get_list(client, f"/v2/summary/activity/{user_id}", params, "activity")

        return self._merge_daily(sleep_rows, activity_rows)

    def _get_list(
        self, client: httpx.Client, path: str, params: dict[str, Any], envelope_key: str
    ) -> list[dict[str, Any]]:
        url = f"{self.base_url}{path}"
        try:
            resp = client.get(url, headers=self.headers, params=params)
            resp.raise_for_status()
            body = resp.json()
        except httpx.HTTPError as exc:
            raise JunctionError(f"Junction {envelope_key} request failed: {exc}") from exc
        except ValueError as exc:
            raise JunctionError(f"Junction {envelope_key} returned invalid JSON at {path}") from exc

        if isinstance(body, dict):
            items = body.get(envelope_key) or body.get("data") or []
        elif isinstance(body, list):
            items = body
        else:
            items = []
        return [i for i in items if isinstance(i, dict)]

    @staticmethod
    def _day_key(row: dict[str, Any]) -> str | None:
        return row.get("calendar_date") or row.get("date")

    def _merge_daily(
        self, sleep_rows: list[dict[str, Any]], activity_rows: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        """Fold sleep + activity per calendar day into the raw health-row shape."""
        activity_by_day: dict[str, dict[str, Any]] = {}
        for row in activity_rows:
            day = self._day_key(row)
            if day:
                activity_by_day[day] = row

        merged: dict[str, dict[str, Any]] = {}
        for sleep in sleep_rows:
            day = self._day_key(sleep)
            if not day:
                continue
            merged[day] = self._build_raw(day, sleep, activity_by_day.get(day, {}))

        # Activity-only days (rare, but a step-tracker without sleep data).
        for day, activity in activity_by_day.items():
            if day not in merged:
                merged[day] = self._build_raw(day, {}, activity)

        return [merged[d] for d in sorted(merged)]

    def _build_raw(self, day: str, sleep: dict[str, Any], activity: dict[str, Any]) -> dict[str, Any]:
        # Sleep total is SECONDS (rem+light+deep); fall back to duration (seconds).
        total_seconds = _to_number(sleep.get("total"))
        if total_seconds <= 0:
            total_seconds = _to_number(sleep.get("duration"))
        sleep_hours = round(total_seconds / 3600.0, 2) if total_seconds > 0 else 0.0

        hrv_ms = int(round(_to_number(sleep.get("average_hrv"))))  # already rmssd ms

        activity_hr = activity.get("heart_rate") or {}
        resting_hr = _to_number(sleep.get("hr_lowest"))
        if resting_hr <= 0:
            resting_hr = _to_number(activity_hr.get("resting_bpm"))
        if resting_hr <= 0:
            resting_hr = _to_number(sleep.get("hr_average"))

        steps = int(round(_to_number(activity.get("steps"))))
        calories = int(round(_to_number(activity.get("calories_total"))))

        return {
            "date": day,
            "sleep_hours": sleep_hours,
            "hrv_ms": hrv_ms,
            "resting_hr": int(round(resting_hr)),
            "steps_yesterday": steps,
            "calories_burned": calories,
            "source": "junction",
        }
