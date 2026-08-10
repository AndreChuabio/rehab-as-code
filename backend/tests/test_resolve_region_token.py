"""resolve_to_library must not match on the shared body-region word.

Regression: with a body_region scope, every exercise in that region shares
the region word (all ankle ids contain "ankle"), so an off-library request
like "seated ankle pumps" used to resolve to an ARBITRARY ankle exercise
(it grabbed Band-Resisted Dorsiflexion). The fix drops the region word from
scoring, so off-library requests resolve to None (Maya flags them for the
therapist) while distinctive words still match the correct exercise.
"""
from __future__ import annotations

import sys
from pathlib import Path

_BACKEND = Path(__file__).resolve().parent.parent
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

import exercise_kb as kb  # noqa: E402


def test_off_library_request_returns_none_not_arbitrary_match():
    # "pumps" is in no library exercise; "ankle" is the shared region word.
    # Must NOT silently resolve to a wrong ankle exercise.
    assert kb.resolve_to_library("seated_ankle_pumps", body_region="ankle") is None
    assert kb.resolve_to_library("seated ankle pumps", body_region="ankle") is None


def test_distinctive_word_still_resolves_correctly():
    dorsi = kb.resolve_to_library("band-resisted dorsiflexion", body_region="ankle")
    assert dorsi and dorsi["id"] == "ankle_dorsiflexion_band"

    eversion = kb.resolve_to_library("seated ankle isometric eversion", body_region="ankle")
    assert eversion and "eversion" in eversion["id"]

    calf = kb.resolve_to_library("double leg calf raises", body_region="ankle")
    assert calf and calf["id"] == "ankle_calf_raises_double_leg"


def test_exact_id_match_unaffected():
    ex = kb.resolve_to_library("ankle_dorsiflexion_band", body_region="ankle")
    assert ex and ex["id"] == "ankle_dorsiflexion_band"
