"""
slack_notifier.py — send rehab session reminders to a Slack webhook.
"""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone

import httpx
from dotenv import load_dotenv

load_dotenv()
logger = logging.getLogger(__name__)

WEBAPP_URL = os.getenv("WEBAPP_URL", "https://rehab-as-code-five.vercel.app/")


def send_reminder(patient: str, exercises: list[str], days: list[str]) -> bool:
    """POST a rehab session reminder to the configured Slack webhook.

    Returns True on success, False on failure (never raises).
    """
    webhook_url = os.getenv("SLACK_WEBHOOK_URL", "")
    if not webhook_url:
        logger.warning("SLACK_WEBHOOK_URL not set — skipping Slack notification")
        return False

    today = datetime.now(timezone.utc).strftime("%A, %b %d")
    ex_list = "\n".join(f"• {e}" for e in exercises) if exercises else "• See your protocol"
    days_str = ", ".join(days) if days else "every day"

    payload = {
        "blocks": [
            {
                "type": "header",
                "text": {"type": "plain_text", "text": "🏃 Rehab Session Reminder", "emoji": True},
            },
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": f"Hey *{patient}* — time for your session! 💪\n*{today}*  ·  scheduled days: {days_str}",
                },
            },
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": f"*Today's exercises:*\n{ex_list}"},
            },
            {
                "type": "actions",
                "elements": [
                    {
                        "type": "button",
                        "text": {"type": "plain_text", "text": "Open RehabAsCode →", "emoji": True},
                        "style": "primary",
                        "url": f"{WEBAPP_URL}#exercise",
                    }
                ],
            },
            {"type": "divider"},
            {
                "type": "context",
                "elements": [
                    {"type": "mrkdwn", "text": "Powered by RehabAsCode · cloud agents · Cursor orchestrator"}
                ],
            },
        ]
    }

    try:
        r = httpx.post(webhook_url, json=payload, timeout=10)
        r.raise_for_status()
        logger.info("Slack reminder sent for %s", patient)
        return True
    except Exception as exc:
        logger.error("Slack notification failed: %s", exc)
        return False
