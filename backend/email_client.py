"""
email_client.py - lightweight Resend transactional-email wrapper.

Mirrors junction_client.py discipline: a small httpx client with a call-time
config dataclass, config read from env at CALL time (never at import), and a
fail-open posture so the app works with no email configured.

FAIL-OPEN contract (load-bearing):
  - build_config() returns None when RESEND_API_KEY or RESEND_FROM is unset.
  - send_email() returns False (never raises) on a missing config OR any
    httpx error, logging at WARNING with the error TYPE only.
  A send failure must never break the caller (the approve 200, the SSE stream).

This module is PURE TRANSPORT and pref-agnostic. All notification-preference
gating (email_opt_in master switch + per-type toggles) lives at the call sites
so this client can be reused for any future system email.

PHI hygiene: an email body carries the patient's name + plan status, which is
PHI to Resend. We never log the recipient address, subject, or body at any
level. Real patient emails require a Resend BAA — keep the test-data-only
posture (same gating as Tavus / Junction); see CLAUDE.md.

Single recipient per call by design: send_email takes one `to` string, never a
list, so a coding mistake can't fan one patient's email out to others.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass

import httpx

logger = logging.getLogger(__name__)

_RESEND_ENDPOINT = "https://api.resend.com/emails"


@dataclass
class EmailConfig:
    api_key: str
    from_addr: str
    # Kept low: a single POST per send. A tight deadline keeps a hung email
    # provider from stalling the request the send is fired after (it runs
    # inline-after-write, error-swallowed, but we still bound the wall time).
    timeout_seconds: float = 5.0


def _httpx_timeout(seconds: float) -> "httpx.Timeout":
    """Explicit connect + read budget so a hung TCP connect can't stall a send."""
    connect = min(2.0, seconds)
    return httpx.Timeout(seconds, connect=connect)


def build_config() -> EmailConfig | None:
    """Assemble an EmailConfig from env at call time.

    Returns None (caller no-ops the send) when EITHER RESEND_API_KEY or
    RESEND_FROM is empty. Reads at call time so a test / deploy can flip the
    env without an import-order surprise.
    """
    api_key = (os.getenv("RESEND_API_KEY") or "").strip()
    from_addr = (os.getenv("RESEND_FROM") or "").strip()
    if not api_key or not from_addr:
        return None
    return EmailConfig(api_key=api_key, from_addr=from_addr)


def is_email_configured() -> bool:
    """True when both RESEND_API_KEY and RESEND_FROM are set. Never raises."""
    return build_config() is not None


def send_email(
    to: str,
    subject: str,
    html: str,
    *,
    text: str | None = None,
    reply_to: str | None = None,
) -> bool:
    """POST a single transactional email via Resend. Returns True on a 2xx.

    FAIL-OPEN: returns False (never raises) when email is not configured or any
    httpx error occurs. Logs only the error TYPE on failure — never the
    recipient, subject, or body (PHI).

    `to` is a single recipient string by design; the Resend API takes a list,
    so we wrap it ([to]) at the boundary but never accept a list from callers.
    """
    config = build_config()
    if config is None:
        logger.warning("email not configured (RESEND_API_KEY / RESEND_FROM unset); skipping send")
        return False

    recipient = (to or "").strip()
    if not recipient:
        logger.warning("email send skipped: empty recipient")
        return False

    payload: dict[str, object] = {
        "from": config.from_addr,
        "to": [recipient],
        "subject": subject,
        "html": html,
    }
    if text:
        payload["text"] = text
    if reply_to:
        payload["reply_to"] = reply_to

    headers = {
        "Authorization": f"Bearer {config.api_key}",
        "Content-Type": "application/json",
    }

    try:
        with httpx.Client(timeout=_httpx_timeout(config.timeout_seconds)) as client:
            resp = client.post(_RESEND_ENDPOINT, headers=headers, json=payload)
            resp.raise_for_status()
    except httpx.HTTPError as exc:
        # Type only — the message could echo the recipient/subject back.
        logger.warning("email send failed: %s", type(exc).__name__)
        return False
    return True
