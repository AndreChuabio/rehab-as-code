"""Tier-routing tail of chat_protocol_drafter.draft_and_save_pending.

The drafter now classifies every drafted revision through change_tier.classify
(deterministic, no LLM) and routes the persist call accordingly:
  - "auto" -> protocol_repo.save_active_auto (applied live, revertible)
  - "gate" -> protocol_repo.save_pending (clinician review queue)

These tests monkeypatch the two module-level seams (_draft_payload, _run_safety)
so the routing decision is exercised in isolation from the Anthropic call and
the cross-region validator. change_tier.classify itself runs for real.

_draft_payload returns (summary, payload, phase, week, in_scope, coverage_concerns);
in_scope is the library-coverage verdict and vetoes auto-apply on its own.
"""
from __future__ import annotations

import chat_protocol_drafter as d


def test_auto_classified_draft_writes_active(monkeypatch):
    prior = {"body_region": "knee", "exercises": [{"exercise_id": "a", "load": 20}]}
    draft_payload = {"body_region": "knee", "exercises": [{"exercise_id": "a", "load": 10}]}
    monkeypatch.setattr(d, "_draft_payload", lambda *a, **k: ("summary", draft_payload, "rehab", 3, True, []))
    monkeypatch.setattr(d, "_run_safety", lambda payload, region: [])
    seen: dict[str, bool] = {}
    monkeypatch.setattr(d.protocol_repo, "save_active_auto",
                        lambda *a, **k: (seen.update(auto=True) or "active-id"))
    monkeypatch.setattr(d.protocol_repo, "save_pending",
                        lambda *a, **k: (seen.update(gate=True) or "pend-id"))
    out = d.draft_and_save_pending("tok", "checkin", {"checkin_text": "easier"}, prior)
    assert out["auto_applied"] is True
    assert out["protocol_id"] == "active-id"
    assert out["pending_protocol_id"] == "active-id"
    assert seen.get("auto") and not seen.get("gate")


def test_gate_classified_draft_writes_pending(monkeypatch):
    prior = {"body_region": "knee", "exercises": [{"exercise_id": "a", "load": 20}]}
    draft_payload = {"body_region": "knee", "exercises": [{"exercise_id": "a", "load": 30}]}  # load up
    monkeypatch.setattr(d, "_draft_payload", lambda *a, **k: ("summary", draft_payload, "rehab", 3, True, []))
    monkeypatch.setattr(d, "_run_safety", lambda payload, region: [])
    seen: dict[str, bool] = {}
    monkeypatch.setattr(d.protocol_repo, "save_pending",
                        lambda *a, **k: (seen.update(gate=True) or "pend-id"))
    monkeypatch.setattr(d.protocol_repo, "save_active_auto",
                        lambda *a, **k: (seen.update(auto=True) or "active-id"))
    out = d.draft_and_save_pending("tok", "weekly_plan", {}, prior)
    assert out["auto_applied"] is False
    assert out["protocol_id"] == "pend-id"
    assert seen.get("gate") and not seen.get("auto")


def test_first_plan_of_care_gates_even_with_safe_diff(monkeypatch):
    """No prior protocol -> change_tier.classify returns gate (clinician-owned)."""
    draft_payload = {"body_region": "knee", "exercises": [{"exercise_id": "a", "load": 10}]}
    monkeypatch.setattr(d, "_draft_payload", lambda *a, **k: ("summary", draft_payload, "acute", 1, True, []))
    monkeypatch.setattr(d, "_run_safety", lambda payload, region: [])
    seen: dict[str, bool] = {}
    monkeypatch.setattr(d.protocol_repo, "save_pending",
                        lambda *a, **k: (seen.update(gate=True) or "pend-id"))
    monkeypatch.setattr(d.protocol_repo, "save_active_auto",
                        lambda *a, **k: (seen.update(auto=True) or "active-id"))
    out = d.draft_and_save_pending("tok", "weekly_plan", {}, None)
    assert out["auto_applied"] is False
    assert seen.get("gate") and not seen.get("auto")


def test_high_severity_concern_gates_to_needs_clinician_review(monkeypatch):
    """A high-severity concern forces gate AND sets needs_clinician_review status."""
    prior = {"body_region": "knee", "exercises": [{"exercise_id": "a", "load": 20}]}
    draft_payload = {"body_region": "knee", "exercises": [{"exercise_id": "a", "load": 10}]}
    monkeypatch.setattr(d, "_draft_payload", lambda *a, **k: ("summary", draft_payload, "rehab", 3, True, []))
    monkeypatch.setattr(d, "_run_safety",
                        lambda payload, region: [{"severity": "high", "rule": "pain_ceiling"}])
    captured: dict[str, object] = {}

    def _save_pending(token, payload, created_by_agent, **kw):
        captured["status"] = kw.get("status")
        captured["safety_concerns"] = kw.get("safety_concerns")
        return "pend-id"

    monkeypatch.setattr(d.protocol_repo, "save_pending", _save_pending)
    out = d.draft_and_save_pending("tok", "checkin", {}, prior)
    assert out["auto_applied"] is False
    assert captured["status"] == "needs_clinician_review"
    assert captured["safety_concerns"] == [{"severity": "high", "rule": "pain_ceiling"}]


# ---------------------------------------------------------------------------
# Library-coverage veto.
#
# change_tier.classify reasons about the SHAPE of the change (regression, swap,
# load-down) and is blind to whether a protocol library exists for the region at
# all. The coverage gate that main added to the chat drafter has to survive the
# tier-routing refactor, or a shoulder revision drafted from chat could go live
# with no clinician ever seeing it, in a region the safety reviewer holds no
# reference material for.


def test_out_of_scope_region_cannot_auto_apply(monkeypatch):
    """in_scope=False vetoes auto-apply even on a diff classify() calls "auto"."""
    prior = {"body_region": "knee", "exercises": [{"exercise_id": "a", "load": 20}]}
    # Load-down on the same exercise: classify() returns "auto" on its own.
    draft_payload = {"body_region": "knee", "exercises": [{"exercise_id": "a", "load": 10}]}
    monkeypatch.setattr(
        d, "_draft_payload",
        lambda *a, **k: ("summary", draft_payload, "rehab", 3, False, []),
    )
    monkeypatch.setattr(d, "_run_safety", lambda payload, region: [])
    seen: dict[str, bool] = {}
    captured: dict[str, object] = {}

    def _save_pending(token, payload, created_by_agent, **kw):
        seen["gate"] = True
        captured["status"] = kw.get("status")
        return "pend-id"

    monkeypatch.setattr(d.protocol_repo, "save_pending", _save_pending)
    monkeypatch.setattr(d.protocol_repo, "save_active_auto",
                        lambda *a, **k: (seen.update(auto=True) or "active-id"))

    out = d.draft_and_save_pending("tok", "checkin", {}, prior)
    assert out["auto_applied"] is False
    assert seen.get("gate") and not seen.get("auto")
    # Escalated the same way a high-severity concern is.
    assert captured["status"] == "needs_clinician_review"


def test_coverage_concerns_ride_along_to_the_clinician(monkeypatch):
    """Coverage concerns merge into safety_concerns so one row carries both."""
    prior = {"body_region": "knee", "exercises": [{"exercise_id": "a", "load": 20}]}
    draft_payload = {"body_region": "knee", "exercises": [{"exercise_id": "a", "load": 30}]}
    cov = [{"severity": "low", "rule": "library_match", "detail": "no exact week file"}]
    monkeypatch.setattr(
        d, "_draft_payload",
        lambda *a, **k: ("summary", draft_payload, "rehab", 3, True, cov),
    )
    monkeypatch.setattr(d, "_run_safety",
                        lambda payload, region: [{"severity": "med", "rule": "rom"}])
    captured: dict[str, object] = {}

    def _save_pending(token, payload, created_by_agent, **kw):
        captured["safety_concerns"] = kw.get("safety_concerns")
        return "pend-id"

    monkeypatch.setattr(d.protocol_repo, "save_pending", _save_pending)
    d.draft_and_save_pending("tok", "weekly_plan", {}, prior)
    assert captured["safety_concerns"] == [
        {"severity": "med", "rule": "rom"},
        {"severity": "low", "rule": "library_match", "detail": "no exact week file"},
    ]
