"""
test_library_match - PR-B regression coverage for the library_match marker.

Two layers:
  1. compute_library_match unit tests - deterministic against the real
     protocols/protocol-library/ on disk. No mocks; the marker is a pure
     function and the library is small enough to assert against.
  2. Orchestrator save-status branching - confirms an out-of-scope injury
     (e.g. shoulder) gates the draft to needs_clinician_review even when
     the safety reviewer is clean, and that within-scope gap weeks attach
     a low-severity informational concern without gating.
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import Any

import pytest

_BACKEND_DIR = Path(__file__).resolve().parent.parent
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

from agents.researcher import IN_SCOPE_REGIONS, compute_library_match  # noqa: E402
from agents.plan_generation_agent import (  # noqa: E402
    _coverage_concern,
    _resolve_save_status,
)


# ---------------------------------------------------------------------------
# 1. compute_library_match
# ---------------------------------------------------------------------------

def test_in_scope_regions_are_knee_and_ankle_only():
    """Pin the scoping decision so it can't drift silently."""
    assert IN_SCOPE_REGIONS == frozenset({"knee", "ankle"})


def test_compute_library_match_knee_exact_week_4():
    m = compute_library_match("post-acl reconstruction", week=4)
    assert m["status"] == "exact"
    assert m["region"] == "knee"
    assert m["in_scope"] is True
    assert m["matched_week"] == 4
    assert m["requested_week"] == 4
    assert "knee" in m["injury_dir"]


def test_compute_library_match_knee_closest_earlier_week_5():
    """Week 5 has no exact file; the latest earlier file is week 4."""
    m = compute_library_match("post-acl reconstruction", week=5)
    assert m["status"] == "closest_earlier"
    assert m["region"] == "knee"
    assert m["in_scope"] is True
    assert m["matched_week"] == 4
    assert m["requested_week"] == 5


def test_compute_library_match_knee_lowest_available_week_2():
    """Week 2 < earliest available file (week 3); fall back to lowest."""
    m = compute_library_match("post-acl reconstruction", week=2)
    assert m["status"] == "lowest_available"
    assert m["region"] == "knee"
    assert m["in_scope"] is True
    assert m["matched_week"] == 3


def test_compute_library_match_ankle_exact_week_1():
    m = compute_library_match("lateral ankle sprain", week=1)
    assert m["status"] == "exact"
    assert m["region"] == "ankle"
    assert m["in_scope"] is True
    assert m["matched_week"] == 1


def test_compute_library_match_ankle_closest_earlier_week_2():
    """Ankle week 2 has no exact match; closest earlier is week 1."""
    m = compute_library_match("lateral ankle sprain", week=2)
    assert m["status"] == "closest_earlier"
    assert m["region"] == "ankle"
    assert m["in_scope"] is True
    assert m["matched_week"] == 1


def test_compute_library_match_shoulder_is_out_of_scope():
    """Shoulder has files (week-1, week-3) but is outside the autodraft scope."""
    m = compute_library_match("rotator cuff repair", week=1)
    assert m["region"] == "shoulder"
    assert m["in_scope"] is False
    # The status field is still computed (we want the clinician to see
    # which file would have been cited if we'd let the agent draft).
    assert m["status"] == "exact"
    assert m["matched_week"] == 1


def test_compute_library_match_unknown_injury_no_dir():
    """Unmappable injury -> no_dir; in_scope reflects body_region resolution."""
    m = compute_library_match("hangnail", week=1)
    assert m["status"] == "no_dir"
    assert m["region"] is None
    assert m["in_scope"] is False
    assert m["matched_week"] is None
    assert m["injury_dir"] is None


def test_compute_library_match_explicit_body_region_overrides_taxonomy():
    """When body_region is supplied, skip the taxonomy lookup. Lets callers
    that already resolved the region (chat drafter, etc.) avoid the work
    twice and lets tests exercise the in_scope branch deterministically."""
    m = compute_library_match("anything", week=1, body_region="knee")
    # No knee-specific injury_dir resolves from "anything" but the region
    # is honored for the in_scope flag regardless.
    assert m["region"] == "knee"
    assert m["in_scope"] is True


# ---------------------------------------------------------------------------
# 2. Coverage concern translation
# ---------------------------------------------------------------------------

def test_coverage_concern_none_for_in_scope_exact():
    m = {
        "status": "exact",
        "in_scope": True,
        "region": "knee",
        "requested_week": 4,
        "matched_week": 4,
    }
    assert _coverage_concern(m) is None


def test_coverage_concern_med_for_out_of_scope():
    m = {
        "status": "exact",
        "in_scope": False,
        "region": "shoulder",
        "requested_week": 1,
        "matched_week": 1,
    }
    concern = _coverage_concern(m)
    assert concern is not None
    assert concern["severity"] == "med"
    assert concern["category"] == "library_coverage"
    assert "shoulder" in concern["summary"]


def test_coverage_concern_low_for_in_scope_gap():
    m = {
        "status": "closest_earlier",
        "in_scope": True,
        "region": "knee",
        "requested_week": 5,
        "matched_week": 4,
    }
    concern = _coverage_concern(m)
    assert concern is not None
    assert concern["severity"] == "low"
    assert "5" in concern["summary"]


# ---------------------------------------------------------------------------
# 3. Save-status branching
# ---------------------------------------------------------------------------

def _verdict(severity: str, ok: bool, concerns: list[dict] | None = None) -> dict:
    return {
        "ok": ok,
        "concerns": concerns or [],
        "overall_severity": severity,
    }


def _match_in_scope_exact() -> dict:
    return {
        "status": "exact",
        "in_scope": True,
        "region": "knee",
        "requested_week": 4,
        "matched_week": 4,
        "injury_dir": "protocols/protocol-library/knee",
    }


def _match_out_of_scope() -> dict:
    return {
        "status": "exact",
        "in_scope": False,
        "region": "shoulder",
        "requested_week": 1,
        "matched_week": 1,
        "injury_dir": "protocols/protocol-library/shoulder",
    }


def _match_in_scope_gap() -> dict:
    return {
        "status": "closest_earlier",
        "in_scope": True,
        "region": "knee",
        "requested_week": 5,
        "matched_week": 4,
        "injury_dir": "protocols/protocol-library/knee",
    }


def test_resolve_save_status_clean_and_in_scope():
    status, concerns = _resolve_save_status(
        _verdict("low", ok=True), _match_in_scope_exact(),
    )
    assert status == "pending_review"
    assert concerns is None


def test_resolve_save_status_safety_high_overrides_in_scope():
    safety_concerns = [{"category": "pain", "severity": "high"}]
    status, concerns = _resolve_save_status(
        _verdict("high", ok=False, concerns=safety_concerns),
        _match_in_scope_exact(),
    )
    assert status == "needs_clinician_review"
    assert concerns == safety_concerns


def test_resolve_save_status_out_of_scope_gates_clean_verdict():
    """The headline behavior: a clean safety verdict still routes
    out-of-scope injuries to needs_clinician_review."""
    status, concerns = _resolve_save_status(
        _verdict("low", ok=True), _match_out_of_scope(),
    )
    assert status == "needs_clinician_review"
    assert len(concerns) == 1
    assert concerns[0]["category"] == "library_coverage"
    assert concerns[0]["severity"] == "med"


def test_resolve_save_status_in_scope_gap_attaches_low_concern():
    """Within-scope gap-week drafts stay pending_review but surface the
    library_match marker as a low-severity informational concern."""
    status, concerns = _resolve_save_status(
        _verdict("low", ok=True), _match_in_scope_gap(),
    )
    assert status == "pending_review"
    assert concerns is not None
    assert len(concerns) == 1
    assert concerns[0]["severity"] == "low"
    assert concerns[0]["category"] == "library_coverage"


def test_resolve_save_status_med_exhausted_stays_pending_review():
    safety_concerns = [{"category": "dose", "severity": "med"}]
    status, concerns = _resolve_save_status(
        _verdict("med", ok=False, concerns=safety_concerns),
        _match_in_scope_exact(),
    )
    assert status == "pending_review"
    assert concerns == safety_concerns


def test_resolve_save_status_med_exhausted_plus_out_of_scope():
    """Both signals fire: out_of_scope wins on routing, both concerns
    survive on the persisted row."""
    safety_concerns = [{"category": "dose", "severity": "med"}]
    status, concerns = _resolve_save_status(
        _verdict("med", ok=False, concerns=safety_concerns),
        _match_out_of_scope(),
    )
    assert status == "needs_clinician_review"
    assert len(concerns) == 2
    categories = {c["category"] for c in concerns}
    assert categories == {"dose", "library_coverage"}


# ---------------------------------------------------------------------------
# 4. End-to-end orchestrator routing
# ---------------------------------------------------------------------------

@pytest.fixture
def stub_orchestrator(monkeypatch):
    """Minimal stand-ins for the orchestrator's external deps so we can run
    .handle() and inspect what landed on save_pending."""
    import agents.plan_generation_agent as pga
    import protocol_repo
    import user_store

    state: dict[str, Any] = {
        "save_pending_calls": [],
        "intake": None,
    }

    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")

    def _load_inputs(token):
        return {
            "intake": state["intake"],
            "health": {},
            "history": [],
            "active_payload": None,
            "recent_sessions": [],
        }

    monkeypatch.setattr(pga.PlanGenerationAgent, "_load_inputs", lambda self, t: _load_inputs(t))

    def _save(token, payload, created_by_agent, *, status="pending_review",
              safety_concerns=None):
        protocol_id = f"protocol-{len(state['save_pending_calls'])}"
        state["save_pending_calls"].append({
            "token": token,
            "payload": payload,
            "status": status,
            "safety_concerns": safety_concerns,
        })
        return protocol_id

    monkeypatch.setattr(protocol_repo, "save_pending", _save)
    monkeypatch.setattr(user_store, "save_protocol_state", lambda t, s: None)
    monkeypatch.setattr(user_store, "get_display_name", lambda t: "Test Patient")

    monkeypatch.setattr(
        pga, "researcher_candidates",
        lambda *a, **kw: [{"exercise_id": "wall_sit", "rationale": "ok"}],
    )
    monkeypatch.setattr(
        pga, "trend_analyze",
        lambda **kw: {"pattern": "steady", "implication_for_next_week": "Hold"},
    )
    monkeypatch.setattr(
        pga, "evaluator_signal",
        lambda *a, **kw: {"decision": "hold", "reasons": [], "confidence": 0.7},
    )
    monkeypatch.setattr(
        pga, "planner_compose",
        lambda **kw: {
            "patient": "Test Patient",
            "phase": kw["phase"],
            "week": kw["week"],
            "exercises": [{"name": "wall_sit", "sets": 3, "reps": 12}],
            "session_targets": {"frequency_per_week": 4, "duration_min": 30},
        },
    )
    monkeypatch.setattr(
        pga, "safety_review",
        lambda **kw: {"ok": True, "concerns": [], "overall_severity": "low"},
    )

    return state


def test_orchestrator_out_of_scope_injury_gates_to_clinician_review(stub_orchestrator):
    """A shoulder patient with a clean safety verdict still hits
    needs_clinician_review because the library_coverage gate fires."""
    from agents.base import PatientRequest
    from agents.plan_generation_agent import PlanGenerationAgent

    stub_orchestrator["intake"] = {
        "name": "Test Patient",
        "injury_type": "rotator cuff repair",
        "phase": "acute",
        "week": 1,
    }

    asyncio.run(PlanGenerationAgent().handle(
        PatientRequest(user_token="t", message="generate plan",
                       slack_user_id=None, metadata={}),
    ))

    calls = stub_orchestrator["save_pending_calls"]
    assert len(calls) == 1
    call = calls[0]
    assert call["status"] == "needs_clinician_review"
    assert call["safety_concerns"] is not None
    assert any(c["category"] == "library_coverage" for c in call["safety_concerns"])

    # Marker also persisted on the draft for clinician dashboard traceability.
    meta = call["payload"].get("_meta") or {}
    library_match = meta.get("library_match") or {}
    assert library_match.get("region") == "shoulder"
    assert library_match.get("in_scope") is False


def test_orchestrator_in_scope_clean_stays_pending_review(stub_orchestrator):
    from agents.base import PatientRequest
    from agents.plan_generation_agent import PlanGenerationAgent

    stub_orchestrator["intake"] = {
        "name": "Test Patient",
        "injury_type": "post-acl reconstruction",
        "phase": "subacute",
        "week": 4,
    }

    asyncio.run(PlanGenerationAgent().handle(
        PatientRequest(user_token="t", message="generate plan",
                       slack_user_id=None, metadata={}),
    ))

    call = stub_orchestrator["save_pending_calls"][0]
    assert call["status"] == "pending_review"
    assert call["safety_concerns"] is None
    library_match = call["payload"]["_meta"]["library_match"]
    assert library_match["region"] == "knee"
    assert library_match["in_scope"] is True
    assert library_match["status"] == "exact"


def test_orchestrator_in_scope_gap_attaches_low_concern_only(stub_orchestrator):
    """Knee week 5 has no exact file. Stays pending_review but the clinician
    sees the closest_earlier marker as a low-severity informational concern."""
    from agents.base import PatientRequest
    from agents.plan_generation_agent import PlanGenerationAgent

    stub_orchestrator["intake"] = {
        "name": "Test Patient",
        "injury_type": "post-acl reconstruction",
        "phase": "subacute",
        "week": 5,
    }

    asyncio.run(PlanGenerationAgent().handle(
        PatientRequest(user_token="t", message="generate plan",
                       slack_user_id=None, metadata={}),
    ))

    call = stub_orchestrator["save_pending_calls"][0]
    assert call["status"] == "pending_review"
    assert call["safety_concerns"] is not None
    assert len(call["safety_concerns"]) == 1
    assert call["safety_concerns"][0]["severity"] == "low"
    assert call["safety_concerns"][0]["category"] == "library_coverage"
