"""Tests for superbill.generate_draft — the completed-sessions-only DRAFT.

Pins the hard contract: status stays draft_unsigned, every line is
attestation-required + needs-verification, only completed sessions count,
payer-aware justification language is correct, and a missing session store
degrades gracefully instead of raising.
"""
from __future__ import annotations

import pytest


def _session(exercise_id: str, status: str = "completed", date: str = "2026-06-01"):
    return {
        "status": status,
        "exercise_id": exercise_id,
        "completed_at": f"{date}T10:00:00+00:00",
        "created_at": f"{date}T09:00:00+00:00",
    }


def _patch_stores(monkeypatch, *, sessions, payer="cash", region="knee", goals=None):
    import session_repo
    import protocol_repo
    import user_store

    monkeypatch.setattr(session_repo, "list_recent", lambda token, days=7: list(sessions))
    monkeypatch.setattr(
        protocol_repo, "get_active",
        lambda token: {"payload": {"body_region": region, "goals": goals or []}},
    )
    monkeypatch.setattr(user_store, "resolve_payer_model", lambda token: payer)


def test_draft_groups_by_cpt_and_counts_units(monkeypatch):
    sessions = [
        _session("mini_squat"),            # 97110 default
        _session("mini_squat"),            # 97110 default
        _session("single_leg_balance"),    # 97112 (balance/single_leg)
        _session("gait_training"),         # 97116 (gait)
        _session("wall_sit", status="planned"),  # ignored — not completed
    ]
    _patch_stores(monkeypatch, sessions=sessions)
    import superbill
    draft = superbill.generate_draft("tok")

    assert draft["status"] == "draft_unsigned"
    assert draft["requires_clinician_attestation"] is True
    assert draft["source"] == "completed_sessions_only"
    # 4 completed sessions across 3 CPT buckets; planned row ignored.
    assert draft["totals"]["total_sessions"] == 4
    assert draft["totals"]["total_units"] == 4
    codes = {li["cpt"] for li in draft["line_items"]}
    assert codes == {"97110", "97112", "97116"}
    for li in draft["line_items"]:
        assert li["requires_clinician_attestation"] is True
        assert li["needs_verification"] is True
    # 97110 bucket has the two mini_squat sessions.
    ther_ex = next(li for li in draft["line_items"] if li["cpt"] == "97110")
    assert ther_ex["units"] == 2 and ther_ex["session_count"] == 2


def test_justification_is_payer_aware(monkeypatch):
    sessions = [_session("mini_squat")]
    # insurance -> medical-necessity language
    _patch_stores(monkeypatch, sessions=sessions, payer="insurance")
    import superbill
    ins = superbill.generate_draft("tok")
    assert ins["payer_model"] == "insurance"
    assert "medically necessary" in ins["line_items"][0]["justification"].lower()

    # cash -> out-of-network self-submission language, no medical-necessity claim
    _patch_stores(monkeypatch, sessions=sessions, payer="cash")
    cash = superbill.generate_draft("tok")
    assert cash["payer_model"] == "cash"
    j = cash["line_items"][0]["justification"].lower()
    assert "out-of-network" in j
    assert "medically necessary" not in j


def test_empty_when_no_completed_sessions(monkeypatch):
    _patch_stores(monkeypatch, sessions=[_session("mini_squat", status="planned")])
    import superbill
    draft = superbill.generate_draft("tok")
    assert draft["line_items"] == []
    assert draft["totals"]["total_sessions"] == 0
    assert draft["status"] == "draft_unsigned"
    assert draft["disclaimers"]  # always carries the DRAFT disclaimers


def test_degrades_when_session_store_unavailable(monkeypatch):
    import session_repo
    import protocol_repo
    import user_store

    def _boom(token, days=7):
        raise session_repo.SessionRepoError("no DATABASE_URL")

    monkeypatch.setattr(session_repo, "list_recent", _boom)
    monkeypatch.setattr(protocol_repo, "get_active", lambda token: None)
    monkeypatch.setattr(user_store, "resolve_payer_model", lambda token: "cash")

    import superbill
    draft = superbill.generate_draft("tok")  # must not raise
    assert draft["status"] == "draft_unsigned"
    assert draft["line_items"] == []
    assert any("unavailable" in d.lower() for d in draft["disclaimers"])
