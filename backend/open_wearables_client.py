"""
open_wearables_client.py - lightweight Open Wearables API wrapper.

Phase-1 scope:
  - Read-only server-to-server integration
  - Existing Open Wearables user ID
  - Summaries needed by health_mock.py
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Any

import httpx


class OpenWearablesError(Exception):
    """Raised when Open Wearables integration fails."""


@dataclass
class OpenWearablesConfig:
    base_url: str
    api_key: str
    user_id: str
    timeout_seconds: float = 8.0


class OpenWearablesClient:
    def __init__(self, config: OpenWearablesConfig):
        self.config = config
        self.headers = {"X-Open-Wearables-API-Key": config.api_key}
        self.base_url = config.base_url.rstrip("/")

    def get_daily_summaries(
        self, start_date: date, end_date: date
    ) -> dict[str, dict[str, dict[str, Any] | None]]:
        """
        Return per-day summary bundles keyed by ISO date:
          {
            "2026-05-02": {
              "activity": {...} | None,
              "sleep": {...} | None,
              "recovery": {...} | None,
            }
          }
        """
        activity = self._paginated_summary("activity", start_date, end_date)
        sleep = self._paginated_summary("sleep", start_date, end_date)
        recovery = self._paginated_summary("recovery", start_date, end_date)

        merged: dict[str, dict[str, dict[str, Any] | None]] = {}
        self._merge_summary(merged, activity, "activity")
        self._merge_summary(merged, sleep, "sleep")
        self._merge_summary(merged, recovery, "recovery")
        return merged

    def _merge_summary(
        self,
        target: dict[str, dict[str, dict[str, Any] | None]],
        items: list[dict[str, Any]],
        key: str,
    ) -> None:
        for item in items:
            day = item.get("date")
            if not day:
                continue
            if day not in target:
                target[day] = {"activity": None, "sleep": None, "recovery": None}
            target[day][key] = item

    def _paginated_summary(
        self,
        summary_type: str,
        start_date: date,
        end_date: date,
    ) -> list[dict[str, Any]]:
        path = f"/api/v1/users/{self.config.user_id}/summaries/{summary_type}"
        params: dict[str, Any] = {
            "start_date": start_date.isoformat(),
            "end_date": end_date.isoformat(),
        }
        return self._collect_paginated(path, params)

    def _collect_paginated(self, path: str, params: dict[str, Any]) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []

        with httpx.Client(timeout=self.config.timeout_seconds) as client:
            while True:
                payload = self._get_json(client, path, params)
                items.extend(payload.get("data", []))

                pagination = payload.get("pagination", {})
                next_cursor = pagination.get("next_cursor")
                has_more = bool(pagination.get("has_more"))
                if not has_more or not next_cursor:
                    break
                params = {**params, "cursor": next_cursor}

        return items

    def _get_json(self, client: httpx.Client, path: str, params: dict[str, Any]) -> dict[str, Any]:
        url = f"{self.base_url}{path}"
        try:
            resp = client.get(url, headers=self.headers, params=params)
            resp.raise_for_status()
            return resp.json()
        except httpx.HTTPError as exc:
            raise OpenWearablesError(f"Open Wearables request failed: {exc}") from exc
        except ValueError as exc:
            raise OpenWearablesError(f"Open Wearables returned invalid JSON at {path}") from exc
