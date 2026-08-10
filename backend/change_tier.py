"""Deterministic auto-vs-gate classifier for Coach Maya protocol changes.

Auto-apply only the lowest-risk, in-plan changes; everything that changes
the medical direction of care stays clinician-gated. No LLM. Fail-safe
default is "gate" on any ambiguity or exception.
"""
from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

IN_SCOPE_REGIONS = {"knee", "ankle"}
_AUTO_SEVERITIES = {None, "", "low", "med", "medium"}  # high blocks auto


def _ex_map(protocol: dict[str, Any]) -> dict[str, float]:
    """exercise_id -> numeric load. Non-list payloads raise (caught upstream)."""
    out: dict[str, float] = {}
    for ex in protocol["exercises"]:
        eid = ex.get("exercise_id") or ex.get("id")
        if not eid:
            continue
        try:
            out[eid] = float(ex.get("load") or 0)
        except (TypeError, ValueError):
            out[eid] = 0.0
    return out


def diff_exercises(prior: dict, draft: dict) -> dict:
    pmap, dmap = _ex_map(prior), _ex_map(draft)
    added = [e for e in dmap if e not in pmap]
    removed = [e for e in pmap if e not in dmap]
    shared = [e for e in dmap if e in pmap]
    load_increase = any(dmap[e] > pmap[e] for e in shared)
    load_decrease = any(dmap[e] < pmap[e] for e in shared)
    return {"added": added, "removed": removed,
            "load_increase": load_increase, "load_decrease": load_decrease}


def classify(prior: dict | None,
             draft: dict,
             safety_concerns: list[dict] | None) -> str:
    """Return "auto" (apply live) or "gate" (clinician review)."""
    try:
        if not prior or not prior.get("exercises"):
            return "gate"  # first plan of care is clinician-owned

        region = (draft.get("body_region")
                  or prior.get("body_region") or "").strip().lower()
        if region not in IN_SCOPE_REGIONS:
            return "gate"

        for c in (safety_concerns or []):
            sev = str(c.get("severity", "")).strip().lower()
            if sev not in _AUTO_SEVERITIES:
                return "gate"

        d = diff_exercises(prior, draft)
        # Brand-new exercises are clinician-owned. Only a single 1-for-1
        # regression swap (exactly one added, paired with a removal) stays
        # auto-eligible. More than one new exercise, or a new exercise with no
        # paired removal (net addition), changes the medical direction of care
        # -> clinician.
        if len(d["added"]) > 1 or (len(d["added"]) == 1 and not d["removed"]):
            return "gate"
        if d["load_increase"]:  # progression on any shared exercise -> clinician
            return "gate"
        # Remaining shape: only removals / swaps / load decreases, region in
        # scope, no high-severity flag -> low-risk, apply live.
        return "auto"
    except Exception:
        logger.warning("change_tier.classify defaulted to gate", exc_info=True)
        return "gate"
