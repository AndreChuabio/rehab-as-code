"""Tests for /admin/* observability endpoints.

Goals:
  * Auth gate: only role='admin' can access; non-staff and clinicians
    get 403 from require_admin_id (verified by running without the
    admin override).
  * Pagination + filtering shape: list endpoint groups by request_id and
    honors patient/agent/errored/cursor filters.
  * Run detail joins: agents are returned in step_index order; missing
    request_id 404s.
  * Metrics cache: second call within TTL doesn't re-query DB.

DB calls are stubbed via monkeypatching backend.api.admin._get_conn so
sqlite test setups don't need a real pipeline_runs table.
"""

from __future__ import annotations

import contextlib
from datetime import datetime, timezone
from typing import Any
from unittest.mock import MagicMock


def _conn_with_rows(rows: list[tuple], cols: list[str]):
    """Return a context-manager-yielding fake connection that exposes one
    cursor whose fetchall() / fetchone() return the supplied rows."""
    cur = MagicMock()
    cur.description = [(c,) for c in cols]
    cur.fetchall.return_value = rows
    cur.fetchone.return_value = rows[0] if rows else None

    @contextlib.contextmanager
    def _ctx_cur():
        yield cur

    conn = MagicMock()
    conn.cursor.return_value = _ctx_cur()

    @contextlib.contextmanager
    def _ctx_conn():
        yield conn

    return _ctx_conn(), cur


# ── Auth ───────────────────────────────────────────────────────────────────────

def test_admin_me_requires_admin(unauthed_client):
    """No JWT, no override → require_admin_id rejects."""
    res = unauthed_client.get("/admin/me")
    assert res.status_code in (401, 403)


def test_admin_me_returns_role(authed_admin_client, fake_admin_id, monkeypatch):
    monkeypatch.setattr("user_store.get_display_name", lambda uid: "Andre Test")
    res = authed_admin_client.get("/admin/me")
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["role"] == "admin"
    assert body["user_id"] == fake_admin_id
    assert body["display_name"] == "Andre Test"


def test_clinician_cannot_hit_admin_endpoints(authed_clinician_client):
    """A clinician (without admin role) hits /admin/me and gets 403 from
    require_admin_id (which authed_clinician_client did NOT override)."""
    res = authed_clinician_client.get("/admin/me")
    # 403 if is_admin returns False; 401 if the underlying header parser
    # complains. Either way, NOT 200.
    assert res.status_code in (401, 403)


# ── /admin/pipeline_runs ──────────────────────────────────────────────────────

def test_pipeline_runs_list_groups_and_paginates(authed_admin_client, monkeypatch):
    rows = [(
        "req-1",                          # request_id
        "patient-a",                      # patient_uid
        datetime(2026, 5, 7, 12, 0, tzinfo=timezone.utc),  # started_at
        4500,                             # duration_total_ms
        4,                                # n_agents
        0,                                # n_errors
        [
            {"agent": "researcher", "status": "ok", "duration_ms": 1500,
             "decision": None, "error_class": None, "step_index": 1},
            {"agent": "evaluator", "status": "ok", "duration_ms": 800,
             "decision": "progress", "error_class": None, "step_index": 2},
            {"agent": "planner", "status": "ok", "duration_ms": 2000,
             "decision": None, "error_class": None, "step_index": 3},
            {"agent": "safety_reviewer", "status": "ok", "duration_ms": 200,
             "decision": "ok", "error_class": None, "step_index": 4},
        ],
    )]
    cols = ["request_id", "patient_uid", "started_at", "duration_total_ms",
            "n_agents", "n_errors", "agents"]
    ctx, cur = _conn_with_rows(rows, cols)
    monkeypatch.setattr("api.admin._get_conn", lambda: ctx)

    res = authed_admin_client.get("/admin/pipeline_runs?limit=50")
    assert res.status_code == 200, res.text
    body = res.json()
    assert len(body["runs"]) == 1
    run = body["runs"][0]
    assert run["request_id"] == "req-1"
    # terminal_decision pulled from last decision in the agents array
    assert run["terminal_decision"] == "ok"
    # next_cursor is None when fewer rows than limit returned
    assert body["next_cursor"] is None


def test_pipeline_runs_list_filters_errored(authed_admin_client, monkeypatch):
    """errored=true should produce status<>ok in the WHERE clause. We
    smoke this by making the SQL execute through the mock and inspecting
    the call args."""
    cur = MagicMock()
    cur.description = [("request_id",), ("patient_uid",), ("started_at",),
                       ("duration_total_ms",), ("n_agents",), ("n_errors",),
                       ("agents",)]
    cur.fetchall.return_value = []

    @contextlib.contextmanager
    def _cur_ctx():
        yield cur

    conn = MagicMock()
    conn.cursor.return_value = _cur_ctx()

    @contextlib.contextmanager
    def _conn_ctx():
        yield conn

    monkeypatch.setattr("api.admin._get_conn", lambda: _conn_ctx())
    res = authed_admin_client.get("/admin/pipeline_runs?errored=true")
    assert res.status_code == 200
    sql_executed = cur.execute.call_args[0][0]
    assert "status <> 'ok'" in sql_executed


# ── /admin/pipeline_runs/{request_id} ─────────────────────────────────────────

def test_pipeline_run_detail_returns_404_for_missing(authed_admin_client, monkeypatch):
    ctx, cur = _conn_with_rows([], ["id"])
    monkeypatch.setattr("api.admin._get_conn", lambda: ctx)
    res = authed_admin_client.get("/admin/pipeline_runs/missing-req-id")
    assert res.status_code == 404


def test_pipeline_run_detail_returns_agents_in_step_order(authed_admin_client, monkeypatch):
    rows = [
        ("id-1", "req-1", "patient-a", "researcher", 1, "ok",
         datetime(2026, 5, 7, 12, 0, tzinfo=timezone.utc), 1500, "claude-sonnet-4-6",
         1000, 200, None, {"n_candidates": 5}, None, None, None,
         datetime(2026, 5, 7, 12, 0, tzinfo=timezone.utc)),
        ("id-2", "req-1", "patient-a", "planner", 2, "ok",
         datetime(2026, 5, 7, 12, 0, 2, tzinfo=timezone.utc), 2000, "claude-sonnet-4-6",
         3000, 800, None, {"n_exercises": 5}, None, None, None,
         datetime(2026, 5, 7, 12, 0, 2, tzinfo=timezone.utc)),
    ]
    cols = ["id", "request_id", "patient_uid", "agent", "step_index", "status",
            "started_at", "duration_ms", "model", "tokens_in", "tokens_out",
            "decision", "output_summary", "error_class", "error_message",
            "protocol_id", "created_at"]
    ctx, cur = _conn_with_rows(rows, cols)
    monkeypatch.setattr("api.admin._get_conn", lambda: ctx)
    monkeypatch.setattr("user_store.get_display_name", lambda uid: "Patient A")

    res = authed_admin_client.get("/admin/pipeline_runs/req-1")
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["request_id"] == "req-1"
    assert body["patient"]["display_name"] == "Patient A"
    assert [a["agent"] for a in body["agents"]] == ["researcher", "planner"]
    assert body["agents"][0]["output_summary"]["n_candidates"] == 5


# ── /admin/metrics/agents (cache) ─────────────────────────────────────────────

def test_metrics_uses_cache_within_ttl(authed_admin_client, monkeypatch):
    rows = [("planner", 10, 0, 1500.0, 2200.0, 30000, 8000)]
    cols = ["agent", "n_runs", "n_errors", "p50_ms", "p95_ms", "tokens_in", "tokens_out"]
    ctx, cur = _conn_with_rows(rows, cols)
    monkeypatch.setattr("api.admin._get_conn", lambda: ctx)
    # Reset the module-level cache between tests
    from api import admin as admin_mod
    admin_mod._metrics_cache.clear()

    r1 = authed_admin_client.get("/admin/metrics/agents?window=24h")
    r2 = authed_admin_client.get("/admin/metrics/agents?window=24h")
    assert r1.status_code == 200 and r2.status_code == 200
    assert r1.json() == r2.json()
    # Cursor's execute should only have been hit once for the second
    # request (cache hit).
    assert cur.execute.call_count == 1


# ── /admin/patients ──────────────────────────────────────────────────────────

def test_patients_typeahead_shapes(authed_admin_client, monkeypatch):
    rows = [
        ("patient-a", datetime(2026, 5, 7, tzinfo=timezone.utc)),
        ("patient-b", datetime(2026, 5, 6, tzinfo=timezone.utc)),
    ]
    cols = ["patient_uid", "last_run"]
    ctx, cur = _conn_with_rows(rows, cols)
    monkeypatch.setattr("api.admin._get_conn", lambda: ctx)
    monkeypatch.setattr("user_store.get_display_name",
                        lambda uid: f"Display {uid}")

    res = authed_admin_client.get("/admin/patients")
    assert res.status_code == 200
    body = res.json()
    assert len(body["patients"]) == 2
    assert body["patients"][0]["uid"] == "patient-a"
    assert body["patients"][0]["display_name"] == "Display patient-a"
