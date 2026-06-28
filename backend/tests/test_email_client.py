"""Tests for Part B: notification email delivery.

Covers, under the sqlite backend forced by conftest:
  * email_client fail-open: no key / no from -> is_email_configured False,
    send_email returns False without raising.
  * email_client httpx error path -> send_email returns False (never raises).
  * email_client happy path posts to the Resend endpoint with a single
    recipient and a Bearer key.
  * notifications pref-gating: email_opt_in master OFF -> never sends; per-type
    OFF -> no send even with opt_in ON; both ON -> send fires once.
  * get_email resolves from the intake payload (sqlite) and returns None on a
    miss.
  * plan_updated approve hook + symptom receipt hook respect prefs and never
    break the caller; the symptom receipt carries no verbatim message text.
"""
from __future__ import annotations

import sys
import uuid
from pathlib import Path

import pytest

_BACKEND_DIR = Path(__file__).resolve().parent.parent
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

import email_client  # noqa: E402
import notifications  # noqa: E402
import user_store  # noqa: E402


# ---------------------------------------------------------------------------
# email_client fail-open
# ---------------------------------------------------------------------------


def test_is_email_configured_false_without_key(monkeypatch):
    monkeypatch.delenv("RESEND_API_KEY", raising=False)
    monkeypatch.delenv("RESEND_FROM", raising=False)
    assert email_client.is_email_configured() is False
    assert email_client.build_config() is None


def test_is_email_configured_false_with_key_but_no_from(monkeypatch):
    monkeypatch.setenv("RESEND_API_KEY", "re_test")
    monkeypatch.delenv("RESEND_FROM", raising=False)
    assert email_client.is_email_configured() is False


def test_is_email_configured_false_with_from_but_no_key(monkeypatch):
    monkeypatch.delenv("RESEND_API_KEY", raising=False)
    monkeypatch.setenv("RESEND_FROM", "care@example.test")
    assert email_client.is_email_configured() is False


def test_send_email_noop_when_unconfigured(monkeypatch):
    monkeypatch.delenv("RESEND_API_KEY", raising=False)
    monkeypatch.delenv("RESEND_FROM", raising=False)
    # Must return False and NOT raise.
    assert email_client.send_email("p@example.test", "s", "<p>h</p>") is False


def test_send_email_returns_false_on_httpx_error(monkeypatch):
    monkeypatch.setenv("RESEND_API_KEY", "re_test")
    monkeypatch.setenv("RESEND_FROM", "care@example.test")

    import httpx

    class _BoomClient:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def post(self, *a, **k):
            raise httpx.ConnectError("boom")

    monkeypatch.setattr(email_client.httpx, "Client", _BoomClient)
    # Fail-open: never raises, returns False.
    assert email_client.send_email("p@example.test", "s", "<p>h</p>") is False


def test_send_email_happy_path_posts_single_recipient(monkeypatch):
    monkeypatch.setenv("RESEND_API_KEY", "re_secret")
    monkeypatch.setenv("RESEND_FROM", "care@example.test")

    captured = {}

    class _OkResp:
        def raise_for_status(self):
            return None

    class _FakeClient:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def post(self, url, headers=None, json=None):
            captured["url"] = url
            captured["headers"] = headers
            captured["json"] = json
            return _OkResp()

    monkeypatch.setattr(email_client.httpx, "Client", _FakeClient)
    ok = email_client.send_email("p@example.test", "Subject", "<p>body</p>")
    assert ok is True
    assert captured["url"] == "https://api.resend.com/emails"
    assert captured["headers"]["Authorization"] == "Bearer re_secret"
    # Single recipient wrapped into a list at the boundary; never a fan-out.
    assert captured["json"]["to"] == ["p@example.test"]
    assert captured["json"]["from"] == "care@example.test"


def test_send_email_empty_recipient_noop(monkeypatch):
    monkeypatch.setenv("RESEND_API_KEY", "re_secret")
    monkeypatch.setenv("RESEND_FROM", "care@example.test")
    assert email_client.send_email("   ", "s", "<p>h</p>") is False


# ---------------------------------------------------------------------------
# user_store.get_email (sqlite)
# ---------------------------------------------------------------------------


def test_get_email_from_intake_payload():
    token = str(uuid.uuid4())
    user_store.ensure_user(token)
    user_store.save_intake(token, {"name": "Test", "email": "  p@example.test "})
    try:
        assert user_store.get_email(token) == "p@example.test"
    finally:
        user_store.delete_account(token)


def test_get_email_none_when_absent():
    token = str(uuid.uuid4())
    user_store.ensure_user(token)
    user_store.save_intake(token, {"name": "Test"})
    try:
        assert user_store.get_email(token) is None
    finally:
        user_store.delete_account(token)


# ---------------------------------------------------------------------------
# notifications pref-gating
# ---------------------------------------------------------------------------


def _seed_patient(email: str = "p@example.test", name: str = "Test") -> str:
    token = str(uuid.uuid4())
    user_store.ensure_user(token)
    user_store.save_intake(token, {"name": name, "email": email})
    return token


def _set_prefs(token: str, **prefs):
    user_store.set_notification_prefs(token, prefs)


def test_plan_updated_skipped_when_opt_in_off(monkeypatch):
    token = _seed_patient()
    try:
        _set_prefs(token, email_opt_in=False, plan_updated=True)
        calls = []
        monkeypatch.setattr(
            notifications.email_client, "send_email",
            lambda *a, **k: calls.append(a) or True,
        )
        assert notifications.send_plan_updated(token) is False
        assert calls == []
    finally:
        user_store.delete_account(token)


def test_plan_updated_skipped_when_per_type_off(monkeypatch):
    token = _seed_patient()
    try:
        _set_prefs(token, email_opt_in=True, plan_updated=False)
        calls = []
        monkeypatch.setattr(
            notifications.email_client, "send_email",
            lambda *a, **k: calls.append(a) or True,
        )
        assert notifications.send_plan_updated(token) is False
        assert calls == []
    finally:
        user_store.delete_account(token)


def test_plan_updated_sends_when_both_on(monkeypatch):
    token = _seed_patient()
    try:
        _set_prefs(token, email_opt_in=True, plan_updated=True)
        calls = []

        def _fake_send(to, subject, html, text=None, reply_to=None):
            calls.append({"to": to, "subject": subject, "html": html})
            return True

        monkeypatch.setattr(notifications.email_client, "send_email", _fake_send)
        assert notifications.send_plan_updated(token) is True
        assert len(calls) == 1
        assert calls[0]["to"] == "p@example.test"
        # Manage-in-Settings pointer + opt-out note present.
        assert "Settings" in calls[0]["html"]
    finally:
        user_store.delete_account(token)


def test_plan_updated_noop_without_recipient(monkeypatch):
    token = str(uuid.uuid4())
    user_store.ensure_user(token)
    # No email captured on intake -> get_email None -> no send even when enabled.
    user_store.save_intake(token, {"name": "Test"})
    try:
        _set_prefs(token, email_opt_in=True, plan_updated=True)
        calls = []
        monkeypatch.setattr(
            notifications.email_client, "send_email",
            lambda *a, **k: calls.append(a) or True,
        )
        assert notifications.send_plan_updated(token) is False
        assert calls == []
    finally:
        user_store.delete_account(token)


def test_symptom_receipt_respects_prefs_and_omits_message(monkeypatch):
    token = _seed_patient()
    try:
        # off -> no send
        _set_prefs(token, email_opt_in=True, symptom_flag_receipts=False)
        calls = []

        def _fake_send(to, subject, html, text=None, reply_to=None):
            calls.append({"html": html, "text": text})
            return True

        monkeypatch.setattr(notifications.email_client, "send_email", _fake_send)
        assert notifications.send_symptom_receipt(token) is False
        assert calls == []

        # on -> sends, and carries NO verbatim symptom text.
        _set_prefs(token, email_opt_in=True, symptom_flag_receipts=True)
        assert notifications.send_symptom_receipt(token) is True
        assert len(calls) == 1
        body = (calls[0]["html"] or "") + (calls[0]["text"] or "")
        assert "knee buckled" not in body  # sanity: no verbatim complaint text
        assert "flagged" in body.lower()
    finally:
        user_store.delete_account(token)


def test_reminders_respect_prefs(monkeypatch):
    token = _seed_patient()
    try:
        calls = []
        monkeypatch.setattr(
            notifications.email_client, "send_email",
            lambda *a, **k: calls.append(a) or True,
        )
        _set_prefs(token, email_opt_in=True, session_reminders=False, checkin_reminders=True)
        assert notifications.send_session_reminder(token) is False
        assert notifications.send_checkin_reminder(token) is True
        assert len(calls) == 1
    finally:
        user_store.delete_account(token)


def test_send_returns_false_when_transport_unconfigured(monkeypatch):
    """End-to-end fail-open: prefs ON + recipient present, but no Resend key
    -> the real email_client no-ops and the sender returns False, no raise."""
    monkeypatch.delenv("RESEND_API_KEY", raising=False)
    monkeypatch.delenv("RESEND_FROM", raising=False)
    token = _seed_patient()
    try:
        _set_prefs(token, email_opt_in=True, plan_updated=True)
        assert notifications.send_plan_updated(token) is False
    finally:
        user_store.delete_account(token)


# ---------------------------------------------------------------------------
# approve hook (plan_updated) never breaks the 200
# ---------------------------------------------------------------------------


def test_approve_hook_survives_send_exception(authed_client, monkeypatch):
    """A raising email hook must NOT break the approve response.

    We drive the approve endpoint with protocol_repo.approve faked to return a
    row + token, and force notifications.send_plan_updated to raise. The
    endpoint wraps the hook in try/except, so the 200 still lands.
    """
    import datetime as _dt

    import protocol_repo

    fake_row = {
        "id": "abc-123",
        "token": "11111111-1111-1111-1111-111111111111",
        "status": "active",
        "reviewed_at": _dt.datetime(2026, 1, 1, tzinfo=_dt.timezone.utc),
    }
    monkeypatch.setattr(protocol_repo, "approve", lambda **k: dict(fake_row))

    def _boom(_token):
        raise RuntimeError("smtp on fire")

    monkeypatch.setattr(notifications, "send_plan_updated", _boom)

    res = authed_client.post(
        "/protocols/abc-123/approve", json={"notes": "ok"},
    )
    assert res.status_code == 200
    assert res.json()["approved"] is True


# ---------------------------------------------------------------------------
# cron endpoint guard
# ---------------------------------------------------------------------------


def test_cron_requires_configured_secret(authed_client, monkeypatch):
    monkeypatch.delenv("INTERNAL_CRON_SECRET", raising=False)
    res = authed_client.post("/internal/cron/reminders")
    assert res.status_code == 503


def test_cron_rejects_bad_secret(authed_client, monkeypatch):
    monkeypatch.setenv("INTERNAL_CRON_SECRET", "right")
    res = authed_client.post(
        "/internal/cron/reminders", headers={"X-Cron-Secret": "wrong"},
    )
    assert res.status_code == 403


def test_cron_runs_with_good_secret(authed_client, monkeypatch):
    monkeypatch.setenv("INTERNAL_CRON_SECRET", "right")
    import protocol_repo

    monkeypatch.setattr(protocol_repo, "list_active_tokens", lambda *a, **k: [])
    res = authed_client.post(
        "/internal/cron/reminders", headers={"X-Cron-Secret": "right"},
    )
    assert res.status_code == 200
    body = res.json()
    assert body["candidates"] == 0
    assert body["session_sent"] == 0
    assert body["checkin_sent"] == 0
