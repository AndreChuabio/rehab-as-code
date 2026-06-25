"""Tests for the Profile / Settings backend (net-new behavior).

Covers, under the sqlite backend forced by conftest:
  * user_store.set_display_name roundtrip + empty-name ValueError -> API 400
  * user_store.delete_account cascade (children gone) + self-scoping (B intact)
  * GET /patient/me/export self-scoping (A's export never contains B's data)
  * DELETE /patient/me confirmation-required + self-scoping
  * payer mode is read-only for the patient (no patient setter route)
  * consent display path (not_recorded -> recorded roundtrip)
  * junction_repo.disconnect + junction_client.delete_user (pg / httpx fakes)
"""
from __future__ import annotations

import sys
import uuid
from pathlib import Path
from typing import Any

import pytest

_BACKEND_DIR = Path(__file__).resolve().parent.parent
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

import user_store  # noqa: E402


# ---------------------------------------------------------------------------
# set_display_name
# ---------------------------------------------------------------------------


def test_set_display_name_roundtrip_and_mirror():
    token = str(uuid.uuid4())
    user_store.ensure_user(token)
    user_store.save_intake(token, {"injury_type": "knee"})
    try:
        stored = user_store.set_display_name(token, "  Andre  ")
        assert stored == "Andre"
        # Resolver echoes the new name from the canonical intake payload.
        assert user_store.get_display_name(token) == "Andre"
        # users.patient_name mirror updated via save_intake.
        user = user_store.load_user(token)
        assert user["patient_name"] == "Andre"
        # Did not clobber the rest of the intake payload.
        intake = user_store.get_intake(token)
        assert intake["injury_type"] == "knee"
        assert intake["name"] == "Andre"
    finally:
        user_store.delete_account(token)


def test_set_display_name_rejects_blank():
    token = str(uuid.uuid4())
    user_store.ensure_user(token)
    try:
        with pytest.raises(ValueError):
            user_store.set_display_name(token, "   ")
        with pytest.raises(ValueError):
            user_store.set_display_name(token, "")
    finally:
        user_store.delete_account(token)


def test_set_patient_profile_endpoint_400_on_blank(authed_client, fake_user_id):
    user_store.ensure_user(fake_user_id)
    try:
        res = authed_client.post("/patient/me/profile", json={"name": "  "})
        assert res.status_code == 400
        res_ok = authed_client.post("/patient/me/profile", json={"name": "Senor"})
        assert res_ok.status_code == 200
        assert res_ok.json()["name"] == "Senor"
    finally:
        user_store.delete_account(fake_user_id)


def test_set_display_name_persists_without_prior_users_row():
    """Regression: a patient may reach Settings before any endpoint ran
    ensure_user. save_intake silently no-ops when the parent users row is
    absent, so set_display_name must register the row itself or the write is
    lost while the endpoint returns a false 200."""
    token = str(uuid.uuid4())  # never ensure_user'd / no prior interaction
    try:
        stored = user_store.set_display_name(token, "Maya")
        assert stored == "Maya"
        # The write actually landed in the DB, not just echoed in memory.
        assert user_store.get_display_name(token) == "Maya"
        assert user_store.get_intake(token)["name"] == "Maya"
    finally:
        user_store.delete_account(token)


def test_set_consent_persists_without_prior_users_row():
    """Regression: same silent-write-loss window for consent."""
    token = str(uuid.uuid4())  # no prior interaction / no users row
    try:
        recorded = user_store.set_consent(token)
        assert recorded["status"] == "recorded"
        # Re-read from the DB confirms the consent actually persisted.
        assert user_store.get_consent(token)["status"] == "recorded"
    finally:
        user_store.delete_account(token)


# ---------------------------------------------------------------------------
# delete_account cascade + self-scoping (sqlite)
# ---------------------------------------------------------------------------


def _child_counts(token: str) -> dict[str, int]:
    """Count this token's rows across the sqlite child tables."""
    with user_store._sql_conn() as c:
        out = {}
        for table in ("intake_records", "health_records", "protocol_state", "checkins"):
            row = c.execute(
                f"SELECT COUNT(*) AS n FROM {table} WHERE token = ?", (token,)
            ).fetchone()
            out[table] = row["n"]
    return out


def test_delete_account_cascade_removes_all_children():
    token = str(uuid.uuid4())
    user_store.ensure_user(token)
    user_store.save_intake(token, {"name": "P", "injury_type": "knee"})
    user_store.save_health(token, {"hrv_ms": 50})
    user_store.save_protocol_state(token, {"current_phase": "p1"})
    user_store.save_checkin(token, {"pain_level": 2})

    before = _child_counts(token)
    assert before["intake_records"] == 1
    assert before["checkins"] == 1
    assert user_store.token_exists(token)

    user_store.delete_account(token)

    assert not user_store.token_exists(token)
    after = _child_counts(token)
    assert after == {
        "intake_records": 0,
        "health_records": 0,
        "protocol_state": 0,
        "checkins": 0,
    }


def test_delete_account_is_self_scoped_leaves_other_patient_intact():
    a = str(uuid.uuid4())
    b = str(uuid.uuid4())
    user_store.ensure_user(a)
    user_store.ensure_user(b)
    user_store.save_intake(a, {"name": "A"})
    user_store.save_intake(b, {"name": "B"})
    try:
        user_store.delete_account(a)
        # A is gone; B is fully intact.
        assert not user_store.token_exists(a)
        assert user_store.token_exists(b)
        assert user_store.get_display_name(b) == "B"
        assert _child_counts(b)["intake_records"] == 1
    finally:
        user_store.delete_account(b)


# ---------------------------------------------------------------------------
# Export self-scoping (endpoint, sqlite)
# ---------------------------------------------------------------------------


def test_export_contains_only_caller_data(authed_client, fake_user_id):
    a = fake_user_id  # the authed_client identity
    b = str(uuid.uuid4())
    user_store.ensure_user(a)
    user_store.ensure_user(b)
    user_store.save_intake(a, {"name": "Alice", "secret": "alice-only"})
    user_store.save_intake(b, {"name": "Bob", "secret": "bob-only"})
    try:
        res = authed_client.get("/patient/me/export")
        assert res.status_code == 200
        # JSON download.
        assert "attachment" in res.headers.get("content-disposition", "")
        body = res.json()
        assert body["token"] == a
        # A's data present.
        assert body["account"]["intake"]["name"] == "Alice"
        # B's data must NOT leak anywhere in the serialized export.
        raw = res.text
        assert "Bob" not in raw
        assert "bob-only" not in raw
    finally:
        user_store.delete_account(a)
        user_store.delete_account(b)


# ---------------------------------------------------------------------------
# DELETE /patient/me — confirmation-required + self-scoping (endpoint)
# ---------------------------------------------------------------------------


def test_delete_endpoint_requires_confirmation(authed_client, fake_user_id):
    user_store.ensure_user(fake_user_id)
    user_store.save_intake(fake_user_id, {"name": "Keep"})
    try:
        # Missing / wrong confirmation -> 400, no delete.
        bad = authed_client.request(
            "DELETE", "/patient/me", json={"confirm": "delete"}
        )
        assert bad.status_code == 400
        assert user_store.token_exists(fake_user_id)

        none = authed_client.request("DELETE", "/patient/me", json={"confirm": ""})
        assert none.status_code == 400
        assert user_store.token_exists(fake_user_id)
    finally:
        user_store.delete_account(fake_user_id)


def test_delete_endpoint_self_scoped(authed_client, fake_user_id):
    a = fake_user_id
    b = str(uuid.uuid4())
    user_store.ensure_user(a)
    user_store.ensure_user(b)
    user_store.save_intake(a, {"name": "A"})
    user_store.save_intake(b, {"name": "B"})
    try:
        res = authed_client.request("DELETE", "/patient/me", json={"confirm": "DELETE"})
        assert res.status_code == 200
        assert res.json() == {"deleted": True}
        # Only A deleted; B untouched. The endpoint takes no token in body/path,
        # so an adversarial cross-patient delete is structurally impossible.
        assert not user_store.token_exists(a)
        assert user_store.token_exists(b)
    finally:
        user_store.delete_account(b)


# ---------------------------------------------------------------------------
# Payer mode is read-only for the patient
# ---------------------------------------------------------------------------


def test_no_patient_facing_payer_setter_route():
    """There must be no patient-facing setter for payer_model.

    The only payer-model writer is the clinician route
    POST /clinician/patient/{token}/payer-model. A patient-scoped path
    (/patient/me/payer-model or similar) must not exist.
    """
    import main

    paths = {r.path for r in main.app.routes if hasattr(r, "path")}
    assert "/clinician/patient/{token}/payer-model" in paths
    assert "/patient/me/payer-model" not in paths
    for p in paths:
        assert not (p.startswith("/patient/me") and "payer" in p)


def test_clinician_payer_setter_still_400s_on_bad_value(authed_clinician_client):
    """Regression guard: the existing clinician setter validates the enum."""
    token = str(uuid.uuid4())
    res = authed_clinician_client.post(
        f"/clinician/patient/{token}/payer-model",
        json={"payer_model": "self-pay"},
    )
    assert res.status_code == 400


# ---------------------------------------------------------------------------
# Consent display path
# ---------------------------------------------------------------------------


def test_consent_default_not_recorded_then_recorded():
    token = str(uuid.uuid4())
    user_store.ensure_user(token)
    try:
        assert user_store.get_consent(token)["status"] == "not_recorded"
        recorded = user_store.set_consent(token)
        assert recorded["status"] == "recorded"
        assert recorded["recorded_at"]
        again = user_store.get_consent(token)
        assert again["status"] == "recorded"
        assert again["recorded_at"] == recorded["recorded_at"]
        # Consent merge preserves the rest of the payload.
        user_store.save_intake(token, {**(user_store.get_intake(token) or {}), "name": "C"})
        assert user_store.get_consent(token)["status"] == "recorded"
    finally:
        user_store.delete_account(token)


# ---------------------------------------------------------------------------
# Junction disconnect (pg scripted-cursor fake) + delete_user (httpx fake)
# ---------------------------------------------------------------------------


class _FakeCursor:
    def __init__(self, sink: list[tuple[str, tuple]]):
        self._sink = sink

    def execute(self, sql: str, params: tuple = ()) -> None:
        self._sink.append((sql, params))

    def fetchone(self):
        return None

    def __enter__(self) -> "_FakeCursor":
        return self

    def __exit__(self, *exc: object) -> None:
        return None


class _FakeConn:
    def __init__(self, sink: list[tuple[str, tuple]]):
        self._sink = sink
        self.commits = 0

    def cursor(self) -> _FakeCursor:
        return _FakeCursor(self._sink)

    def commit(self) -> None:
        self.commits += 1

    def __enter__(self) -> "_FakeConn":
        return self

    def __exit__(self, *exc: object) -> None:
        return None


def test_junction_disconnect_issues_scoped_delete(monkeypatch):
    import junction_repo

    sink: list[tuple[str, tuple]] = []
    conn = _FakeConn(sink)
    monkeypatch.setattr(junction_repo, "_conn", lambda: conn)

    junction_repo.disconnect("tok-123")

    assert len(sink) == 1
    sql, params = sink[0]
    assert "DELETE FROM junction_connections" in sql
    assert "WHERE token = %s" in sql
    assert params == ("tok-123",)
    assert conn.commits == 1


def test_junction_disconnect_requires_token():
    import junction_repo

    with pytest.raises(junction_repo.JunctionRepoError):
        junction_repo.disconnect("")


def test_junction_client_delete_user_calls_v2_user(monkeypatch):
    import junction_client

    captured: dict[str, Any] = {}

    class _Resp:
        status_code = 200

        def raise_for_status(self):
            return None

        def json(self):
            return {"success": True}

    class _Client:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return None

        def delete(self, url, headers=None):
            captured["url"] = url
            captured["headers"] = headers
            return _Resp()

    monkeypatch.setattr(junction_client.httpx, "Client", _Client)

    cfg = junction_client.JunctionConfig(api_key="key-xyz", base_url="https://api.sandbox.us.junction.com")
    client = junction_client.JunctionClient(cfg)
    assert client.delete_user("vital-abc") is True
    assert captured["url"].endswith("/v2/user/vital-abc")
    assert captured["headers"]["x-vital-api-key"] == "key-xyz"
    assert captured["headers"]["Accept"] == "application/json"


def test_junction_client_delete_user_wraps_transport_error(monkeypatch):
    import junction_client

    class _Client:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return None

        def delete(self, url, headers=None):
            raise junction_client.httpx.ConnectError("boom")

    monkeypatch.setattr(junction_client.httpx, "Client", _Client)
    cfg = junction_client.JunctionConfig(api_key="k", base_url="https://x")
    client = junction_client.JunctionClient(cfg)
    with pytest.raises(junction_client.JunctionError):
        client.delete_user("vital-abc")


# ---------------------------------------------------------------------------
# Clinician profile setter targets staff_users (pg fake)
# ---------------------------------------------------------------------------


def test_set_clinician_display_name_updates_staff_users(monkeypatch):
    captured: dict[str, Any] = {}

    class _Cur:
        rowcount = 1

        def execute(self, sql, params=()):
            captured["sql"] = sql
            captured["params"] = params

        def fetchone(self):
            return None

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return None

    class _Conn:
        def cursor(self):
            return _Cur()

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return None

    import db

    monkeypatch.setattr(db, "get_conn", lambda **k: _Conn())

    stored = user_store.set_clinician_display_name("clin-1", "  Dr Nikki ")
    assert stored == "Dr Nikki"
    assert "UPDATE staff_users SET display_name" in captured["sql"]
    assert captured["params"] == ("Dr Nikki", "clin-1")


def test_set_clinician_display_name_rejects_blank():
    with pytest.raises(ValueError):
        user_store.set_clinician_display_name("clin-1", "  ")


# ---------------------------------------------------------------------------
# Settings v2 — patient prefs (intake-payload backed; sqlite path)
# ---------------------------------------------------------------------------


def test_notification_prefs_roundtrip_and_non_clobber():
    token = str(uuid.uuid4())
    user_store.ensure_user(token)
    user_store.save_intake(token, {"injury_type": "knee"})
    try:
        # Default benign shape when unset.
        defaults = user_store.get_notification_prefs(token)
        assert defaults["session_reminders"] is True
        assert defaults["email_opt_in"] is False
        # Persisted shape after set.
        stored = user_store.set_notification_prefs(
            token, {"session_reminders": False, "email_opt_in": True},
        )
        assert stored["session_reminders"] is False
        assert stored["email_opt_in"] is True
        assert user_store.get_notification_prefs(token)["email_opt_in"] is True
        # Did not clobber other intake keys.
        assert user_store.get_intake(token)["injury_type"] == "knee"
    finally:
        user_store.delete_account(token)


def test_display_prefs_roundtrip_and_enum_clamp():
    token = str(uuid.uuid4())
    user_store.ensure_user(token)
    try:
        defaults = user_store.get_display_prefs(token)
        assert defaults["theme"] == "light"
        assert defaults["text_size"] == "normal"
        assert defaults["reduced_motion"] is False
        stored = user_store.set_display_prefs(
            token, {"theme": "dark", "text_size": "large", "reduced_motion": True},
        )
        assert stored["theme"] == "dark"
        assert stored["text_size"] == "large"
        assert stored["reduced_motion"] is True
        # Unknown enum values clamp back to the default on set.
        clamped = user_store.set_display_prefs(token, {"theme": "neon", "text_size": "huge"})
        assert clamped["theme"] == "light"
        assert clamped["text_size"] == "normal"
    finally:
        user_store.delete_account(token)


def test_coach_prefs_roundtrip():
    token = str(uuid.uuid4())
    user_store.ensure_user(token)
    try:
        defaults = user_store.get_coach_prefs(token)
        assert defaults["voice"] is True
        assert defaults["greeting_cadence"] == "every_visit"
        assert defaults["language"] == "en"
        stored = user_store.set_coach_prefs(
            token, {"voice": False, "greeting_cadence": "off", "language": "en"},
        )
        assert stored["voice"] is False
        assert stored["greeting_cadence"] == "off"
        assert user_store.get_coach_prefs(token)["voice"] is False
        # Unknown cadence clamps to default.
        clamped = user_store.set_coach_prefs(token, {"greeting_cadence": "hourly"})
        assert clamped["greeting_cadence"] == "every_visit"
    finally:
        user_store.delete_account(token)


def test_patient_pref_setters_persist_without_prior_users_row():
    """ensure_user must precede save_intake or the write silently no-ops."""
    token = str(uuid.uuid4())  # no prior interaction / no users row
    try:
        user_store.set_notification_prefs(token, {"email_opt_in": True})
        assert user_store.get_notification_prefs(token)["email_opt_in"] is True
    finally:
        user_store.delete_account(token)


# ---------------------------------------------------------------------------
# Settings v2 — patient pref endpoints (authed, self-scoped)
# ---------------------------------------------------------------------------


def test_patient_pref_endpoints_roundtrip(authed_client, fake_user_id):
    user_store.ensure_user(fake_user_id)
    try:
        # notifications
        res = authed_client.post(
            "/patient/me/notifications", json={"session_reminders": False},
        )
        assert res.status_code == 200
        assert res.json()["session_reminders"] is False
        assert authed_client.get("/patient/me/notifications").json()["session_reminders"] is False
        # display
        res = authed_client.post("/patient/me/display", json={"theme": "dark"})
        assert res.status_code == 200
        assert res.json()["theme"] == "dark"
        assert authed_client.get("/patient/me/display").json()["theme"] == "dark"
        # coach-prefs
        res = authed_client.post("/patient/me/coach-prefs", json={"voice": False})
        assert res.status_code == 200
        assert res.json()["voice"] is False
        assert authed_client.get("/patient/me/coach-prefs").json()["voice"] is False
        # An empty body is 400-safe (no required pref values).
        assert authed_client.post("/patient/me/notifications", json={}).status_code == 200
    finally:
        user_store.delete_account(fake_user_id)


def test_care_team_endpoint_self_scoped_and_degrades(authed_client, fake_user_id, monkeypatch):
    """Care-team never 5xx: clinic_name None + reviewer None in the degraded
    sqlite env, clinic_phone resolves via the CLINIC_PHONE env fallback."""
    monkeypatch.setenv("CLINIC_PHONE", "555-CARE")
    user_store.ensure_user(fake_user_id)
    try:
        res = authed_client.get("/patient/me/care-team")
        assert res.status_code == 200
        data = res.json()
        assert data["clinic_phone"] == "555-CARE"
        assert data["clinic_name"] is None
        assert data["reviewing_clinician_name"] is None
    finally:
        user_store.delete_account(fake_user_id)


# ---------------------------------------------------------------------------
# Settings v2 — clinician staff_users helpers (scripted cursor + degrade)
# ---------------------------------------------------------------------------


def _scripted_conn(monkeypatch, *, fetchone_val=None, rowcount=1):
    """Patch db.get_conn with a scripted cursor capturing SQL + params."""
    captured: dict[str, Any] = {}

    class _Cur:
        def __init__(self):
            self.rowcount = rowcount

        def execute(self, sql, params=()):
            captured["sql"] = sql
            captured["params"] = params

        def fetchone(self):
            return fetchone_val

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return None

    class _Conn:
        def cursor(self):
            return _Cur()

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return None

    import db

    monkeypatch.setattr(db, "get_conn", lambda **k: _Conn())
    return captured


def test_clinic_profile_set_get_roundtrip(monkeypatch):
    captured = _scripted_conn(
        monkeypatch,
        fetchone_val={
            "clinic_name": "Plum PT",
            "clinic_phone": "555-1212",
            "license_number": "PT-99",
            "signature": "Nikki, PT, DPT",
        },
    )
    stored = user_store.set_clinic_profile(
        "clin-1",
        {"clinic_name": "Plum PT", "clinic_phone": "555-1212",
         "license_number": "PT-99", "signature": "Nikki, PT, DPT"},
    )
    # set_clinic_profile re-reads via get_clinic_profile, so the final captured
    # SQL is the SELECT (proving the read-back ran); the returned values come
    # from the scripted row.
    assert stored["clinic_name"] == "Plum PT"
    assert stored["signature"] == "Nikki, PT, DPT"
    assert "staff_users" in captured["sql"]
    # get reads the same scripted row.
    got = user_store.get_clinic_profile("clin-1")
    assert got["license_number"] == "PT-99"


def test_clinic_profile_degrades_without_db(monkeypatch):
    import db

    class _Boom(db.DbConfigError):
        pass

    def _raise(**k):
        raise db.DbConfigError("no DATABASE_URL")

    monkeypatch.setattr(db, "get_conn", _raise)
    # get degrades to all-None, never raises.
    got = user_store.get_clinic_profile("clin-1")
    assert got == {
        "clinic_name": None, "clinic_phone": None,
        "license_number": None, "signature": None,
    }
    # set raises ValueError (-> API 400).
    with pytest.raises(ValueError):
        user_store.set_clinic_profile("clin-1", {"clinic_name": "X"})


def test_clinician_notif_prefs_roundtrip(monkeypatch):
    captured = _scripted_conn(
        monkeypatch, fetchone_val={"notif_prefs": {"new_review_drafts": False}},
    )
    stored = user_store.set_clinician_notif_prefs(
        "clin-1", {"new_review_drafts": False, "high_severity_flags": True},
    )
    assert stored["new_review_drafts"] is False
    assert stored["high_severity_flags"] is True
    assert "notif_prefs" in captured["sql"]
    got = user_store.get_clinician_notif_prefs("clin-1")
    assert got["new_review_drafts"] is False
    # defaults fill the missing key.
    assert got["high_severity_flags"] is True


def test_clinician_goal_templates_roundtrip(monkeypatch):
    captured = _scripted_conn(
        monkeypatch, fetchone_val={"goal_templates": {"insurance": "ADL-focused"}},
    )
    stored = user_store.set_clinician_goal_templates(
        "clin-1", {"insurance": "ADL-focused", "cash": "load mgmt"},
    )
    assert stored["insurance"] == "ADL-focused"
    assert stored["cash"] == "load mgmt"
    assert stored["medicare"] == ""
    assert "goal_templates" in captured["sql"]
    got = user_store.get_clinician_goal_templates("clin-1")
    assert got["insurance"] == "ADL-focused"
    assert got["medicare"] == ""


def test_clinician_jsonb_degrades_without_db(monkeypatch):
    import db

    def _raise(**k):
        raise db.DbConfigError("no DATABASE_URL")

    monkeypatch.setattr(db, "get_conn", _raise)
    assert user_store.get_clinician_notif_prefs("clin-1")["new_review_drafts"] is True
    assert user_store.get_clinician_goal_templates("clin-1")["insurance"] == ""


# ---------------------------------------------------------------------------
# Settings v2 — resolve_clinic_phone precedence
# ---------------------------------------------------------------------------


def test_resolve_clinic_phone_env_fallback_when_no_db(monkeypatch):
    import db

    def _raise(**k):
        raise db.DbConfigError("no DATABASE_URL")

    monkeypatch.setattr(db, "get_conn", _raise)
    monkeypatch.setenv("CLINIC_PHONE", "555-ENV")
    assert user_store.resolve_clinic_phone() == "555-ENV"


def test_resolve_clinic_phone_none_when_unset(monkeypatch):
    import db

    def _raise(**k):
        raise db.DbConfigError("no DATABASE_URL")

    monkeypatch.setattr(db, "get_conn", _raise)
    monkeypatch.delenv("CLINIC_PHONE", raising=False)
    assert user_store.resolve_clinic_phone() is None


def test_resolve_clinic_phone_clinic_takes_precedence(monkeypatch):
    _scripted_conn(monkeypatch, fetchone_val={"clinic_phone": "555-CLINIC"})
    monkeypatch.setenv("CLINIC_PHONE", "555-ENV")
    assert user_store.resolve_clinic_phone() == "555-CLINIC"
