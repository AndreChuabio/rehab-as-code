"""Tests for the PlanGenerationAgent orchestrator.

All five sub-agents are stubbed at the module function level. We don't
hit Anthropic or Supabase. The contract under test:

  * happy path: researcher + trend run in parallel, evaluator decides,
    planner composes, safety_reviewer passes, save_pending called with
    status='pending_review'.
  * safety high: save_pending called with status='needs_clinician_review'
    and safety_concerns attached. Planner runs exactly once (no retries).
  * safety med then ok: planner re-runs with concerns; on the 2nd attempt
    safety passes; final save is pending_review without concerns.
  * safety med exhausted: planner runs MAX+1 times, save is pending_review
    WITH safety_concerns (clinician sees the trail).
  * sub-agent error: PlanGenerationError raised; nothing persisted.
"""
from __future__ import annotations

from typing import Any

import asyncio
import pytest


@pytest.fixture
def stub_user_store(monkeypatch):
    """Stand-in for user_store reads + writes the orchestrator uses."""
    import user_store

    state: dict[str, Any] = {
        "user": {
            "intake": {
                "name": "Test Patient",
                "injury_type": "knee",
                "phase": "subacute",
                "week": 3,
            },
            "health": {"hrv": 60, "recovery_score": 72},
        },
        "history": [
            {"kind": "checkin", "pain_level": 3, "recorded_at": "2026-04-25"},
            {"kind": "checkin", "pain_level": 2, "recorded_at": "2026-04-30"},
        ],
        "save_protocol_state_calls": [],
    }
    monkeypatch.setattr(user_store, "load_user", lambda t: state["user"])
    monkeypatch.setattr(user_store, "get_session_history", lambda t, limit=10: state["history"])
    monkeypatch.setattr(user_store, "get_display_name", lambda t: "Test Patient")
    monkeypatch.setattr(
        user_store, "save_protocol_state",
        lambda t, s: state["save_protocol_state_calls"].append(s),
    )
    return state


@pytest.fixture
def stub_protocol_repo(monkeypatch):
    """Stand-in for protocol_repo reads + writes."""
    import protocol_repo

    state: dict[str, Any] = {
        "active": None,
        "save_pending_calls": [],
    }

    def _save(token, payload, created_by_agent, *, status="pending_review",
              safety_concerns=None):
        protocol_id = f"protocol-{len(state['save_pending_calls'])}"
        state["save_pending_calls"].append({
            "token": token,
            "payload": payload,
            "created_by_agent": created_by_agent,
            "status": status,
            "safety_concerns": safety_concerns,
        })
        return protocol_id

    monkeypatch.setattr(protocol_repo, "get_active", lambda t: state["active"])
    monkeypatch.setattr(protocol_repo, "save_pending", _save)
    return state


@pytest.fixture
def stub_subagents(monkeypatch):
    """Stand-in for the five sub-agent module functions."""
    state: dict[str, Any] = {
        "researcher_calls": 0,
        "evaluator_calls": 0,
        "planner_calls": 0,
        "safety_calls": 0,
        "trend_calls": 0,
        # Configurable behavior:
        "planner_drafts": None,  # callable(attempt_idx) -> draft dict
        "safety_verdicts": None,  # list[dict] consumed in order
        "raise_in": None,  # set to "researcher" / "evaluator" / etc to simulate failure
    }

    def _researcher(injury_type, phase, week, intake=None, *, token=None):
        state["researcher_calls"] += 1
        if state["raise_in"] == "researcher":
            from agents.researcher import ResearcherError
            raise ResearcherError("simulated researcher failure")
        return [
            {"exercise_id": "mini_squats", "rationale": "closed-chain"},
            {"exercise_id": "single_leg_balance", "rationale": "neuromuscular"},
        ]

    def _trend_analyze(*, token, checkins=None, sessions=None, intake=None, weeks=4):
        state["trend_calls"] += 1
        if state["raise_in"] == "trend":
            from agents.trend_analyst import TrendAnalystError
            raise TrendAnalystError("simulated trend failure")
        return {
            "pattern": "steady",
            "evidence": ["pain stable"],
            "implication_for_next_week": "Hold dose.",
        }

    def _evaluator(intake, health, history, trend_summary=None, *, token=None):
        state["evaluator_calls"] += 1
        if state["raise_in"] == "evaluator":
            from agents.evaluator import EvaluatorError
            raise EvaluatorError("simulated evaluator failure")
        return {"decision": "hold", "reasons": ["stable"], "confidence": 0.7}

    def _planner(candidates, signal, intake, *, phase, week, concerns=None, token=None):
        attempt = state["planner_calls"]
        state["planner_calls"] += 1
        if state["raise_in"] == "planner":
            from agents.planner import PlannerError
            raise PlannerError("simulated planner failure")
        if state["planner_drafts"]:
            return state["planner_drafts"](attempt)
        return {
            "patient": "Test Patient",
            "phase": phase,
            "week": week,
            "exercises": [
                {
                    "name": "mini_squats",
                    "sets": 3,
                    "reps": 12,
                    "load": "bodyweight",
                    "references": ["protocols/protocol-library/knee/post-acl-week-4.yaml"],
                },
            ],
            "session_targets": {"frequency_per_week": 4, "duration_min": 35},
        }

    def _safety(draft, intake, trend_summary=None, *, token=None):
        i = state["safety_calls"]
        state["safety_calls"] += 1
        if state["raise_in"] == "safety":
            from agents.safety_reviewer import SafetyReviewError
            raise SafetyReviewError("simulated safety failure")
        if state["safety_verdicts"]:
            return state["safety_verdicts"][i]
        return {"ok": True, "concerns": [], "overall_severity": "low"}

    monkeypatch.setattr("agents.plan_generation_agent.researcher_candidates", _researcher)
    monkeypatch.setattr("agents.plan_generation_agent.trend_analyze", _trend_analyze)
    monkeypatch.setattr("agents.plan_generation_agent.evaluator_signal", _evaluator)
    monkeypatch.setattr("agents.plan_generation_agent.planner_compose", _planner)
    monkeypatch.setattr("agents.plan_generation_agent.safety_review", _safety)

    return state


def _make_request(token: str = "user-token") -> Any:
    from agents.base import PatientRequest
    return PatientRequest(
        user_token=token,
        message="generate plan",
        slack_user_id=None,
        metadata={},
    )


def test_orchestrator_happy_path(monkeypatch, stub_user_store, stub_protocol_repo, stub_subagents):
    """Researcher + trend in parallel; evaluator -> planner -> safety; save pending."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")

    from agents.plan_generation_agent import PlanGenerationAgent

    agent = PlanGenerationAgent()
    resp = asyncio.run(agent.handle(_make_request()))

    # Each sub-agent called exactly once.
    assert stub_subagents["researcher_calls"] == 1
    assert stub_subagents["trend_calls"] == 1
    assert stub_subagents["evaluator_calls"] == 1
    assert stub_subagents["planner_calls"] == 1
    assert stub_subagents["safety_calls"] == 1

    # save_pending invoked with status=pending_review and no safety_concerns.
    assert len(stub_protocol_repo["save_pending_calls"]) == 1
    save = stub_protocol_repo["save_pending_calls"][0]
    assert save["status"] == "pending_review"
    assert save["safety_concerns"] is None
    assert save["created_by_agent"] == "plan_generation"
    # Patient name anchored to the canonical Supabase value.
    assert save["payload"]["patient"] == "Test Patient"
    # body_region stamped onto the payload (was null on every row before).
    assert save["payload"]["body_region"] == "knee"

    assert resp.agent_name == "plan_generation"
    assert resp.data["save_status"] == "pending_review"
    assert resp.data["safety_severity"] == "low"
    assert resp.data["pending_protocol_id"] == "protocol-0"


def test_orchestrator_safety_high_skips_retries(
    monkeypatch, stub_user_store, stub_protocol_repo, stub_subagents,
):
    """High severity: planner runs exactly once; save is needs_clinician_review."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")

    high_concerns = [
        {"check": "contraindication", "severity": "high", "detail": "unsafe load"},
    ]
    stub_subagents["safety_verdicts"] = [
        {"ok": False, "concerns": high_concerns, "overall_severity": "high"},
    ]

    from agents.plan_generation_agent import PlanGenerationAgent
    agent = PlanGenerationAgent()
    resp = asyncio.run(agent.handle(_make_request()))

    # Exactly one planner call - high severity does NOT retry.
    assert stub_subagents["planner_calls"] == 1
    assert stub_subagents["safety_calls"] == 1

    save = stub_protocol_repo["save_pending_calls"][0]
    assert save["status"] == "needs_clinician_review"
    assert save["safety_concerns"] == high_concerns
    assert resp.data["safety_severity"] == "high"


def test_orchestrator_safety_med_then_ok_succeeds_clean(
    monkeypatch, stub_user_store, stub_protocol_repo, stub_subagents,
):
    """First attempt med -> retry once -> ok -> save pending without concerns."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")

    med_concerns = [
        {"check": "pain_ceiling", "severity": "med", "detail": "load too high"},
    ]
    stub_subagents["safety_verdicts"] = [
        {"ok": False, "concerns": med_concerns, "overall_severity": "med"},
        {"ok": True, "concerns": [], "overall_severity": "low"},
    ]

    from agents.plan_generation_agent import PlanGenerationAgent
    agent = PlanGenerationAgent()
    resp = asyncio.run(agent.handle(_make_request()))

    # Two planner attempts, two safety reviews.
    assert stub_subagents["planner_calls"] == 2
    assert stub_subagents["safety_calls"] == 2

    save = stub_protocol_repo["save_pending_calls"][0]
    assert save["status"] == "pending_review"
    assert save["safety_concerns"] is None
    assert resp.data["safety_severity"] == "low"


def test_orchestrator_safety_med_exhausted_attaches_concerns(
    monkeypatch, stub_user_store, stub_protocol_repo, stub_subagents,
):
    """Three attempts (initial + 2 retries) all return med -> save pending
    with concerns attached so clinician sees the trail."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")

    med_concerns = [
        {"check": "pain_ceiling", "severity": "med", "detail": "still high"},
    ]
    stub_subagents["safety_verdicts"] = [
        {"ok": False, "concerns": med_concerns, "overall_severity": "med"},
        {"ok": False, "concerns": med_concerns, "overall_severity": "med"},
        {"ok": False, "concerns": med_concerns, "overall_severity": "med"},
    ]

    from agents.plan_generation_agent import PlanGenerationAgent
    agent = PlanGenerationAgent()
    resp = asyncio.run(agent.handle(_make_request()))

    # 1 initial + 2 retries = 3 planner calls, 3 safety reviews.
    assert stub_subagents["planner_calls"] == 3
    assert stub_subagents["safety_calls"] == 3

    save = stub_protocol_repo["save_pending_calls"][0]
    assert save["status"] == "pending_review"
    assert save["safety_concerns"] == med_concerns
    assert resp.data["safety_severity"] == "med"


def test_orchestrator_researcher_error_propagates(
    monkeypatch, stub_user_store, stub_protocol_repo, stub_subagents,
):
    """Researcher raises -> orchestrator raises PlanGenerationError; no save."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    stub_subagents["raise_in"] = "researcher"

    from agents.plan_generation_agent import PlanGenerationAgent, PlanGenerationError
    agent = PlanGenerationAgent()
    with pytest.raises(PlanGenerationError):
        asyncio.run(agent.handle(_make_request()))

    assert len(stub_protocol_repo["save_pending_calls"]) == 0


def test_orchestrator_evaluator_error_propagates(
    monkeypatch, stub_user_store, stub_protocol_repo, stub_subagents,
):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    stub_subagents["raise_in"] = "evaluator"

    from agents.plan_generation_agent import PlanGenerationAgent, PlanGenerationError
    agent = PlanGenerationAgent()
    with pytest.raises(PlanGenerationError):
        asyncio.run(agent.handle(_make_request()))
    assert len(stub_protocol_repo["save_pending_calls"]) == 0


def test_orchestrator_safety_error_propagates(
    monkeypatch, stub_user_store, stub_protocol_repo, stub_subagents,
):
    """Safety reviewer raises -> orchestrator raises (fails closed). No save."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    stub_subagents["raise_in"] = "safety"

    from agents.plan_generation_agent import PlanGenerationAgent, PlanGenerationError
    agent = PlanGenerationAgent()
    with pytest.raises(PlanGenerationError):
        asyncio.run(agent.handle(_make_request()))
    assert len(stub_protocol_repo["save_pending_calls"]) == 0


def test_orchestrator_no_api_key_falls_back_to_stub(
    monkeypatch, stub_user_store, stub_protocol_repo, stub_subagents,
):
    """Missing ANTHROPIC_API_KEY -> stub-pending fallback (preserved)."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

    from agents.plan_generation_agent import PlanGenerationAgent
    agent = PlanGenerationAgent()
    resp = asyncio.run(agent.handle(_make_request()))

    # No sub-agents called.
    assert stub_subagents["researcher_calls"] == 0
    assert stub_subagents["planner_calls"] == 0
    # But we still saved a stub pending so the clinician sees something.
    assert len(stub_protocol_repo["save_pending_calls"]) == 1
    save = stub_protocol_repo["save_pending_calls"][0]
    assert save["status"] == "pending_review"
    assert save["created_by_agent"] == "plan_generation.fallback"
    assert resp.agent_name == "plan_generation"
