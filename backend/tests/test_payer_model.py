"""Tests for user_store payer-model resolver + clinician setter.

payer_model is clinician-owned, stored on the canonical intake payload, and
defaults to "cash" (the insurance-lapse-bridge GTM is cash-pay first). These
tests pin the default, the enum guard, and the set/resolve roundtrip.
"""
from __future__ import annotations

import uuid

import pytest


def test_resolve_defaults_to_cash_for_empty_token():
    import user_store
    assert user_store.resolve_payer_model("") == "cash"
    assert user_store.resolve_payer_model(None) == "cash"


def test_set_payer_model_rejects_unknown_value():
    import user_store
    with pytest.raises(ValueError):
        user_store.set_payer_model("tok", "self-pay")


def test_set_and_resolve_roundtrip_preserves_intake():
    import user_store
    token = str(uuid.uuid4())
    user_store.ensure_user(token)
    user_store.save_intake(token, {"name": "P", "injury_type": "knee"})
    try:
        # Default before any set.
        assert user_store.resolve_payer_model(token) == "cash"
        # Case-insensitive normalization on set.
        assert user_store.set_payer_model(token, "Insurance") == "insurance"
        assert user_store.resolve_payer_model(token) == "insurance"
        # Set must not clobber the rest of the intake payload.
        intake = user_store.get_intake(token)
        assert intake["name"] == "P"
        assert intake["injury_type"] == "knee"
        assert intake["payer_model"] == "insurance"
    finally:
        user_store.delete_intake(token)


def test_resolve_falls_back_when_payer_model_absent():
    import user_store
    token = str(uuid.uuid4())
    user_store.ensure_user(token)
    user_store.save_intake(token, {"name": "Q"})
    try:
        assert user_store.resolve_payer_model(token) == "cash"
    finally:
        user_store.delete_intake(token)


# ---------------------------------------------------------------------------
# Part C — resolve_goal_template (clinic-wide per-payer goal-language template
# threaded into the planner as STYLE guidance only).
# ---------------------------------------------------------------------------


def _scripted_conn(monkeypatch, *, fetchone_val=None):
    """Patch db.get_conn with a scripted cursor capturing SQL + params."""
    captured: dict = {}

    class _Cur:
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


def test_resolve_goal_template_degrades_to_empty_without_db(monkeypatch):
    """No DATABASE_URL (sqlite test env) -> empty string, the planner no-op."""
    import db
    import user_store

    def _raise(**k):
        raise db.DbConfigError("no DATABASE_URL")

    monkeypatch.setattr(db, "get_conn", _raise)
    # Explicit payer avoids touching the intake store; the DB leg is what degrades.
    assert user_store.resolve_goal_template("tok", payer_model="insurance") == ""


def test_resolve_goal_template_returns_stored_template(monkeypatch):
    """A non-null staff_users.goal_templates[payer] is returned verbatim."""
    import user_store

    captured = _scripted_conn(
        monkeypatch, fetchone_val={"tmpl": "Restore prior level of function for ADLs."},
    )
    got = user_store.resolve_goal_template("tok", payer_model="insurance")
    assert got == "Restore prior level of function for ADLs."
    # The SQL keys on the resolved payer for every ->> placeholder.
    assert captured["params"] == ("insurance", "insurance", "insurance")
    assert "goal_templates" in captured["sql"]


def test_resolve_goal_template_indexes_by_payer(monkeypatch):
    """Per-payer indexing: the resolved payer drives the SQL key, so an
    insurance patient reads the insurance template and a cash patient the cash
    one. Payer is resolved from intake when not passed explicitly."""
    import user_store

    monkeypatch.setattr(user_store, "resolve_payer_model", lambda t: "cash")
    captured = _scripted_conn(monkeypatch, fetchone_val={"tmpl": "Manage load; ramp mileage."})
    got = user_store.resolve_goal_template("cash-patient")
    assert got == "Manage load; ramp mileage."
    assert captured["params"] == ("cash", "cash", "cash")

    monkeypatch.setattr(user_store, "resolve_payer_model", lambda t: "insurance")
    captured2 = _scripted_conn(monkeypatch, fetchone_val={"tmpl": "Independent ambulation."})
    got2 = user_store.resolve_goal_template("ins-patient")
    assert got2 == "Independent ambulation."
    assert captured2["params"] == ("insurance", "insurance", "insurance")


def test_resolve_goal_template_empty_when_no_row(monkeypatch):
    """No staff row / no template set for that payer -> empty string (no-op)."""
    import user_store

    _scripted_conn(monkeypatch, fetchone_val=None)
    assert user_store.resolve_goal_template("tok", payer_model="medicare") == ""


def test_resolve_goal_template_never_raises_on_db_error(monkeypatch):
    """Any unexpected DB error degrades to empty rather than 500-ing the
    plan-generation pipeline."""
    import db
    import user_store

    def _boom(**k):
        raise RuntimeError("connection reset")

    monkeypatch.setattr(db, "get_conn", _boom)
    assert user_store.resolve_goal_template("tok", payer_model="cash") == ""


def test_resolve_goal_template_threaded_into_planner(monkeypatch):
    """End-to-end: handle() resolves the template by payer and threads it into
    planner.compose at the live call site. STYLE only — the orchestrator still
    runs safety review and saves a pending row."""
    import asyncio

    import agents.plan_generation_agent as pga
    import protocol_repo
    import user_store
    from agents.base import PatientRequest

    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")

    intake = {"name": "Ins Patient", "injury_type": "knee", "phase": "subacute", "week": 3}
    monkeypatch.setattr(user_store, "load_user", lambda t: {"intake": intake, "health": {}})
    monkeypatch.setattr(user_store, "get_session_history", lambda t, limit=10: [])
    monkeypatch.setattr(user_store, "get_display_name", lambda t: "Ins Patient")
    monkeypatch.setattr(user_store, "save_protocol_state", lambda t, s: None)
    # Resolve an insurance template; the orchestrator must pass it to the planner.
    monkeypatch.setattr(
        user_store, "resolve_goal_template",
        lambda t, payer_model=None: "Tie every goal to a functional ADL.",
    )

    monkeypatch.setattr(protocol_repo, "get_active", lambda t: None)
    monkeypatch.setattr(
        protocol_repo, "save_pending",
        lambda token, payload, created_by_agent, *, status="pending_review",
        safety_concerns=None: "protocol-0",
    )

    monkeypatch.setattr(
        pga, "researcher_candidates",
        lambda *a, **kw: [{"exercise_id": "mini_squat", "rationale": "closed-chain"}],
    )
    monkeypatch.setattr(
        pga, "trend_analyze",
        lambda **kw: {"pattern": "steady", "implication_for_next_week": "Hold"},
    )
    monkeypatch.setattr(
        pga, "evaluator_signal",
        lambda *a, **kw: {"decision": "hold", "reasons": [], "confidence": 0.7},
    )

    seen: dict = {}

    def _planner(candidates, signal, intake, *, phase, week, concerns=None,
                 token=None, goal_template=None):
        seen["goal_template"] = goal_template
        return {
            "patient": "Ins Patient",
            "phase": phase,
            "week": week,
            "exercises": [{"name": "mini_squat", "sets": 3, "reps": 12}],
            "session_targets": {"frequency_per_week": 4, "duration_min": 30},
        }

    monkeypatch.setattr(pga, "planner_compose", _planner)
    monkeypatch.setattr(
        pga, "safety_review",
        lambda **kw: {"ok": True, "concerns": [], "overall_severity": "low"},
    )

    req = PatientRequest(
        user_token="ins-token", message="generate plan", slack_user_id=None, metadata={},
    )
    asyncio.run(pga.PlanGenerationAgent().handle(req))
    assert seen["goal_template"] == "Tie every goal to a functional ADL."
