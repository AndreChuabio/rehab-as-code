"""
notifications.py - preference-gated transactional email senders.

The single seam between an app event (a plan flips to active, a symptom is
flagged, a scheduled reminder fires) and the pure-transport email_client.
Every public sender here:

  1. reads the patient's notification prefs (user_store.get_notification_prefs,
     which never raises and merges over conservative defaults),
  2. requires BOTH the master switch (email_opt_in) AND the per-type toggle,
  3. resolves the recipient (user_store.get_email) — None no-ops the send,
  4. fires email_client.send_email (itself fail-open), and
  5. swallows every error so a send failure can NEVER break the caller.

Andre's rule: the patient can turn notifications off entirely or pick types —
never annoy them. email_opt_in defaults FALSE, so nothing is sent until the
patient explicitly opts in. Per-type toggles default ON, but the master switch
gates them all.

Copy discipline: calm clinical tone, no emoji, no exclamation marks. Every
email carries a one-line "manage notifications in Settings" pointer and an
opt-out note.

PHI hygiene: an email body carries the patient's name + plan status (PHI to
Resend). We NEVER log the recipient, name, subject, or body. We log only the
event + token + a sent/skipped marker. Real patient emails need a Resend BAA;
keep the test-data-only posture (see CLAUDE.md).
"""

from __future__ import annotations

import logging
from html import escape

import email_client
import user_store

logger = logging.getLogger(__name__)


_SETTINGS_FOOTER_HTML = (
    '<p style="color:#6b6b6b;font-size:13px;margin-top:24px">'
    "You are receiving this because email notifications are turned on in your "
    "rehab-as-code account. You can change what we send you, or turn email off "
    "entirely, under Settings, Notifications and reminders."
    "</p>"
)
_SETTINGS_FOOTER_TEXT = (
    "\n\nYou are receiving this because email notifications are turned on in "
    "your rehab-as-code account. You can change what we send you, or turn "
    "email off entirely, under Settings, Notifications and reminders."
)


def _greeting(name: str | None) -> str:
    """A warm-but-neutral salutation that degrades when the name is unknown."""
    safe = (name or "").strip()
    return f"Hi {safe}," if safe else "Hi,"


def _wrap_html(name: str | None, body_html: str) -> str:
    """Compose the full HTML body with greeting + the Settings footer."""
    return (
        f"<p>{escape(_greeting(name))}</p>"
        f"{body_html}"
        f"{_SETTINGS_FOOTER_HTML}"
    )


def _enabled(token: str, pref_key: str) -> bool:
    """True when email_opt_in (master) AND the per-type pref are both on.

    Never raises — get_notification_prefs returns conservative defaults on any
    failure, so a degraded read fails closed (no send).
    """
    try:
        prefs = user_store.get_notification_prefs(token)
    except Exception:  # noqa: BLE001 - defensive; fail closed, never send on error
        return False
    return bool(prefs.get("email_opt_in")) and bool(prefs.get(pref_key))


def _resolve_recipient(token: str) -> tuple[str | None, str | None]:
    """Return (email, display_name); either may be None. Never raises."""
    try:
        email = user_store.get_email(token)
    except Exception:  # noqa: BLE001
        email = None
    try:
        name = user_store.get_display_name(token)
    except Exception:  # noqa: BLE001
        name = None
    return email, name


def send_plan_updated(token: str) -> bool:
    """Email the patient that their plan was approved + is now active.

    Gated on email_opt_in AND plan_updated. Fires after the approve write; a
    failure here must never affect the approve response. Returns True only when
    an email was actually dispatched.
    """
    if not token or not _enabled(token, "plan_updated"):
        return False
    email, name = _resolve_recipient(token)
    if not email:
        logger.info("plan_updated email skipped (no recipient) token=%s", token)
        return False

    body_html = (
        "<p>Your physical therapist has reviewed and approved an update to your "
        "rehab plan. The new plan is now active in your rehab-as-code app.</p>"
        "<p>Open the app to see what changed and start your next session.</p>"
    )
    body_text = (
        "Your physical therapist has reviewed and approved an update to your "
        "rehab plan. The new plan is now active in your rehab-as-code app. "
        "Open the app to see what changed and start your next session."
    )
    sent = _safe_send(
        email,
        subject="Your rehab plan was updated",
        html=_wrap_html(name, body_html),
        text=_greeting(name) + " " + body_text + _SETTINGS_FOOTER_TEXT,
    )
    logger.info("plan_updated email token=%s sent=%s", token, sent)
    return sent


def send_symptom_receipt(token: str) -> bool:
    """Email the patient a calm receipt that their symptom was flagged for review.

    Gated on email_opt_in AND symptom_flag_receipts. Deliberately carries NO
    verbatim symptom text — only the fact that a clinician will review it (the
    message text is PHI we do not re-egress into an email body). Fires after the
    needs_clinician_review row is written; a failure must never break the chat /
    SSE stream.
    """
    if not token or not _enabled(token, "symptom_flag_receipts"):
        return False
    email, name = _resolve_recipient(token)
    if not email:
        logger.info("symptom_receipt email skipped (no recipient) token=%s", token)
        return False

    body_html = (
        "<p>Thank you for sharing how you are feeling. We have flagged what you "
        "told Coach Maya for your physical therapist to review.</p>"
        "<p>You do not need to do anything right now. If your symptoms feel "
        "severe or are getting worse, please contact your clinic directly.</p>"
    )
    body_text = (
        "Thank you for sharing how you are feeling. We have flagged what you "
        "told Coach Maya for your physical therapist to review. You do not need "
        "to do anything right now. If your symptoms feel severe or are getting "
        "worse, please contact your clinic directly."
    )
    sent = _safe_send(
        email,
        subject="We have flagged your message for your therapist",
        html=_wrap_html(name, body_html),
        text=_greeting(name) + " " + body_text + _SETTINGS_FOOTER_TEXT,
    )
    logger.info("symptom_receipt email token=%s sent=%s", token, sent)
    return sent


def send_session_reminder(token: str) -> bool:
    """Email the patient a calm reminder to do today's session.

    Gated on email_opt_in AND session_reminders. Used by the scheduled reminder
    cron, not an inline event. Returns True only when dispatched.
    """
    if not token or not _enabled(token, "session_reminders"):
        return False
    email, name = _resolve_recipient(token)
    if not email:
        return False

    body_html = (
        "<p>This is a gentle reminder to do today's rehab session when you have "
        "a few minutes. Consistency is what moves your recovery forward.</p>"
        "<p>Open the app to start today's session.</p>"
    )
    body_text = (
        "This is a gentle reminder to do today's rehab session when you have a "
        "few minutes. Consistency is what moves your recovery forward. Open the "
        "app to start today's session."
    )
    sent = _safe_send(
        email,
        subject="A reminder for today's rehab session",
        html=_wrap_html(name, body_html),
        text=_greeting(name) + " " + body_text + _SETTINGS_FOOTER_TEXT,
    )
    logger.info("session_reminder email token=%s sent=%s", token, sent)
    return sent


def send_checkin_reminder(token: str) -> bool:
    """Email the patient a calm reminder to log their daily check-in.

    Gated on email_opt_in AND checkin_reminders. Used by the scheduled reminder
    cron. Returns True only when dispatched.
    """
    if not token or not _enabled(token, "checkin_reminders"):
        return False
    email, name = _resolve_recipient(token)
    if not email:
        return False

    body_html = (
        "<p>This is a gentle reminder to log your daily check-in. A quick note "
        "on how you are feeling helps your therapist keep your plan on track.</p>"
        "<p>Open the app to check in.</p>"
    )
    body_text = (
        "This is a gentle reminder to log your daily check-in. A quick note on "
        "how you are feeling helps your therapist keep your plan on track. Open "
        "the app to check in."
    )
    sent = _safe_send(
        email,
        subject="A reminder for your daily check-in",
        html=_wrap_html(name, body_html),
        text=_greeting(name) + " " + body_text + _SETTINGS_FOOTER_TEXT,
    )
    logger.info("checkin_reminder email token=%s sent=%s", token, sent)
    return sent


def _safe_send(to: str, subject: str, html: str, text: str | None = None) -> bool:
    """email_client.send_email is already fail-open; this is a belt-and-braces
    guard so an unexpected error (e.g. a bug in compose) can never propagate to
    the caller's request path. Logs the error TYPE only — never PHI."""
    try:
        return email_client.send_email(to, subject, html, text=text)
    except Exception as exc:  # noqa: BLE001 - never break the caller on a send
        logger.warning("email send raised unexpectedly: %s", type(exc).__name__)
        return False
