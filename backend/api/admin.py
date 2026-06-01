"""/admin/* — observability dashboard endpoints.

All routes gated by `require_admin_id` (role='admin' in staff_users).
Plain clinicians get 403; non-staff get 401/403; URL-pasting safe.

Surface (per the synthesized plan):
  GET  /admin/me                         role check
  GET  /admin/pipeline_runs              cursor-paginated, ARRAY_AGG by request_id
  GET  /admin/pipeline_runs/{id}         full agent-by-agent breakdown
  GET  /admin/metrics/agents             p50/p95 latency + error_rate, cached 60s
  GET  /admin/patients                   typeahead for the patient filter
  POST /admin/phi-reveal                 audit-log writer (A5 surface; harmless to land here)

Read-only against pipeline_runs + protocols + auth.users. No writes
that affect patient state ever go through this router.
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from auth import require_admin_id

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/admin", tags=["admin"])


# ── Lightweight in-memory cache for /admin/metrics/agents ────────────────────
_metrics_cache: dict[str, tuple[float, Any]] = {}
_METRICS_TTL_S = 60.0


def _get_conn():
    from db import DbConfigError, get_conn
    try:
        return get_conn(autocommit=True)
    except DbConfigError as exc:
        raise HTTPException(503, detail=f"database not configured: {exc}")


# ── /admin/me ─────────────────────────────────────────────────────────────────

@router.get("/me")
def admin_me(user_id: str = Depends(require_admin_id)) -> dict[str, Any]:
    """Smoke check + lets the frontend know which mode to render.

    Returns role + display name (resolved via user_store). Frontend uses
    this to decide whether to render the segmented control + admin chip.
    """
    import user_store
    return {
        "user_id": user_id,
        "role": "admin",
        "display_name": user_store.get_display_name(user_id),
    }


# ── /admin/pipeline_runs ──────────────────────────────────────────────────────

class _AgentSummary(BaseModel):
    agent: str
    status: str
    duration_ms: int
    decision: str | None
    error_class: str | None
    step_index: int


class _RunSummary(BaseModel):
    request_id: str
    patient_uid: str
    started_at: str
    duration_total_ms: int
    n_agents: int
    n_errors: int
    terminal_decision: str | None
    agents: list[_AgentSummary]


@router.get("/pipeline_runs")
def list_runs(
    user_id: str = Depends(require_admin_id),  # noqa: ARG001
    patient: str | None = Query(None),
    agent: str | None = Query(None),
    errored: bool | None = Query(None),
    limit: int = Query(50, ge=1, le=200),
    cursor: str | None = Query(None, description="ISO timestamp from previous page"),
) -> dict[str, Any]:
    """Cross-patient run feed. Grouped by request_id, latest first.

    The list endpoint returns ENOUGH to render the sidebar feed (per-
    agent status pills + decisions). Full output_summary stays lazy —
    fetched only when an admin opens a specific run via /admin/pipeline_runs/{id}.

    cursor is the started_at timestamp of the last item in the previous
    page; pass it back as `?cursor=...` for the next page. No OFFSET.
    """
    where: list[str] = []
    params: list[Any] = []
    if patient:
        where.append("patient_uid = %s")
        params.append(patient)
    if agent:
        where.append("agent = %s")
        params.append(agent)
    if errored is True:
        where.append("status <> 'ok'")
    elif errored is False:
        where.append("status = 'ok'")
    if cursor:
        where.append("started_at < %s")
        params.append(cursor)
    where_sql = "WHERE " + " AND ".join(where) if where else ""
    params.append(limit)

    sql = f"""
        WITH grouped AS (
            SELECT
                request_id,
                MAX(patient_uid) AS patient_uid,
                MIN(started_at) AS started_at,
                SUM(duration_ms) AS duration_total_ms,
                COUNT(*) AS n_agents,
                SUM(CASE WHEN status <> 'ok' THEN 1 ELSE 0 END) AS n_errors,
                JSONB_AGG(
                    JSONB_BUILD_OBJECT(
                        'agent', agent,
                        'status', status,
                        'duration_ms', duration_ms,
                        'decision', decision,
                        'error_class', error_class,
                        'step_index', step_index
                    ) ORDER BY step_index
                ) AS agents
            FROM pipeline_runs
            {where_sql}
            GROUP BY request_id
        )
        SELECT * FROM grouped
        ORDER BY started_at DESC
        LIMIT %s
    """
    runs: list[dict[str, Any]] = []
    next_cursor = None
    with _get_conn() as conn, conn.cursor() as cur:
        cur.execute(sql, params)
        rows = cur.fetchall()
        # The pool uses dict_row (backend/db.py), so each row is already a dict
        # keyed by column name. dict(row) takes a mutable copy.
        for row in rows:
            r = dict(row)
            # Pick a "terminal_decision" from the last decision found
            agents = r.get("agents") or []
            if isinstance(agents, str):  # defensive: jsonb usually auto-decodes
                agents = json.loads(agents)
            decisions = [a.get("decision") for a in agents if a.get("decision")]
            r["terminal_decision"] = decisions[-1] if decisions else None
            r["started_at"] = r["started_at"].isoformat() if r["started_at"] else None
            runs.append(r)
        if rows and len(rows) == limit:
            next_cursor = runs[-1]["started_at"]
    return {"runs": runs, "next_cursor": next_cursor}


# ── /admin/pipeline_runs/{request_id} ─────────────────────────────────────────

@router.get("/pipeline_runs/{request_id}")
def get_run(
    request_id: str,
    user_id: str = Depends(require_admin_id),  # noqa: ARG001
) -> dict[str, Any]:
    """Full agent-by-agent breakdown for a single request. Loads
    output_summary, error details, token counts, and joins the linked
    protocol row when present."""
    import user_store
    sql = """
        SELECT id, request_id, patient_uid, agent, step_index, status,
               started_at, duration_ms, model, tokens_in, tokens_out,
               decision, output_summary, error_class, error_message,
               protocol_id, created_at
        FROM pipeline_runs
        WHERE request_id = %s
        ORDER BY step_index ASC
    """
    with _get_conn() as conn, conn.cursor() as cur:
        cur.execute(sql, (request_id,))
        rows = cur.fetchall()
        if not rows:
            raise HTTPException(404, detail="request_id not found")
        agents = []
        patient_uid = None
        protocol_id = None
        for row in rows:
            d = dict(row)
            patient_uid = d["patient_uid"]
            protocol_id = d.get("protocol_id") or protocol_id
            for k in ("started_at", "created_at"):
                if d.get(k):
                    d[k] = d[k].isoformat()
            agents.append(d)
    patient_block = {
        "uid": patient_uid,
        "display_name": user_store.get_display_name(patient_uid) if patient_uid else None,
    }
    protocol_block = None
    if protocol_id:
        with _get_conn() as conn, conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, status, narrator_status, safety_concerns
                FROM protocols WHERE id = %s
                """,
                (str(protocol_id),),
            )
            row = cur.fetchone()
            if row:
                protocol_block = dict(row)
                protocol_block["id"] = str(protocol_block["id"])
    return {
        "request_id": request_id,
        "patient": patient_block,
        "agents": agents,
        "protocol": protocol_block,
    }


# ── /admin/metrics/agents ─────────────────────────────────────────────────────

@router.get("/metrics/agents")
def agent_metrics(
    user_id: str = Depends(require_admin_id),  # noqa: ARG001
    window: str = Query("24h", pattern="^[0-9]+[mhd]$"),
) -> dict[str, Any]:
    """p50/p95 latency + error rate + run count per agent for the window.

    Cached 60s in-process; admin dashboard polls this maybe every 30s
    when open, so the cache covers the bursty case. Cache key includes
    the window so changing the dropdown invalidates correctly.
    """
    cache_key = f"metrics:{window}"
    now = time.monotonic()
    if cache_key in _metrics_cache:
        ts, payload = _metrics_cache[cache_key]
        if now - ts < _METRICS_TTL_S:
            return payload

    interval_sql = _interval_for(window)
    sql = f"""
        SELECT
            agent,
            COUNT(*) AS n_runs,
            COUNT(*) FILTER (WHERE status <> 'ok') AS n_errors,
            PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY duration_ms) AS p50_ms,
            PERCENTILE_CONT(0.95) WITHIN GROUP (ORDER BY duration_ms) AS p95_ms,
            SUM(tokens_in) AS tokens_in,
            SUM(tokens_out) AS tokens_out
        FROM pipeline_runs
        WHERE created_at >= NOW() - INTERVAL '{interval_sql}'
        GROUP BY agent
        ORDER BY agent
    """
    rows: list[dict[str, Any]] = []
    with _get_conn() as conn, conn.cursor() as cur:
        cur.execute(sql)
        for row in cur.fetchall():
            d = dict(row)
            for k in ("p50_ms", "p95_ms"):
                if d.get(k) is not None:
                    d[k] = round(float(d[k]))
            rows.append(d)
    payload = {"window": window, "agents": rows}
    _metrics_cache[cache_key] = (now, payload)
    return payload


def _interval_for(window: str) -> str:
    """Convert '15m' / '24h' / '7d' to a Postgres INTERVAL literal piece."""
    n = int(window[:-1])
    unit = window[-1]
    return {
        "m": f"{n} minutes",
        "h": f"{n} hours",
        "d": f"{n} days",
    }[unit]


# ── /admin/patients ──────────────────────────────────────────────────────────

@router.get("/patients")
def patients_typeahead(
    user_id: str = Depends(require_admin_id),  # noqa: ARG001
    has_runs_since: str | None = Query(None, description="ISO timestamp"),
    limit: int = Query(50, ge=1, le=200),
) -> dict[str, Any]:
    """Distinct patients with at least one pipeline run in the window.

    Used by the admin sidebar's patient filter. Display name resolved
    via user_store; UID is the canonical filter value.
    """
    import user_store
    where = ""
    params: list[Any] = []
    if has_runs_since:
        where = "WHERE created_at >= %s"
        params.append(has_runs_since)
    sql = f"""
        SELECT DISTINCT patient_uid, MAX(created_at) AS last_run
        FROM pipeline_runs
        {where}
        GROUP BY patient_uid
        ORDER BY last_run DESC
        LIMIT %s
    """
    params.append(limit)
    out: list[dict[str, Any]] = []
    with _get_conn() as conn, conn.cursor() as cur:
        cur.execute(sql, params)
        for row in cur.fetchall():  # dict_row: rows are dicts, not tuples
            uid = row["patient_uid"]
            last_run = row["last_run"]
            out.append({
                "uid": uid,
                "display_name": user_store.get_display_name(uid),
                "last_run": last_run.isoformat() if last_run else None,
            })
    return {"patients": out}


# ── /admin/phi-reveal (audit log writer; A5 dependency) ───────────────────────

class _PhiReveal(BaseModel):
    target_user_id: str
    field: str
    request_id: str | None = None


@router.post("/phi-reveal")
def log_phi_reveal(
    body: _PhiReveal,
    user_id: str = Depends(require_admin_id),
) -> dict[str, Any]:
    """Append an audit row each time an admin clicks 'Reveal' on a PHI
    field. Frontend calls this BEFORE rendering the revealed value so
    the reveal is durably tracked even if the admin closes the tab.

    The admin_phi_reveals table is created in the A5 migration; until
    that lands, this endpoint silently no-ops on missing-table errors
    so A4 can ship without depending on A5.
    """
    sql = """
        INSERT INTO admin_phi_reveals (admin_user_id, target_user_id, field, request_id)
        VALUES (%s, %s, %s, %s)
    """
    try:
        with _get_conn() as conn, conn.cursor() as cur:
            cur.execute(sql, (user_id, body.target_user_id, body.field, body.request_id))
        return {"ok": True}
    except HTTPException:
        raise
    except Exception as exc:
        # Most likely the table doesn't exist yet (pre-A5). Log + tolerate.
        logger.warning("phi_reveal audit insert skipped: %s", exc)
        return {"ok": False, "reason": "audit_unavailable"}
