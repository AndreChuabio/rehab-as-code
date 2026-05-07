"""
test_kb_integrity - regression coverage for the agent knowledge base.

The agents reach into three deterministic sources at inference time:

  1. knowledge/exercise-library.json  - exercise metadata + body_region tags
  2. backend/clinical_taxonomy.py     - injury_type -> body_region map
  3. protocols/protocol-library/      - YAML files the researcher cites

Until now, none of these had a regression suite. The post-LLM region
validator (backend/agents/planner.py:_validate_region) silently skips an
exercise whose body_region cannot be resolved (planner.py line ~354). A
single missing `body_region` field in the JSON would let a cross-region
exercise through without a PlannerError - exactly the safety failure
this file exists to prevent.

These tests do NOT call Anthropic. They exercise the parts of the
pipeline that should be deterministic.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

_BACKEND_DIR = Path(__file__).resolve().parent.parent
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

import clinical_taxonomy  # noqa: E402
import exercise_kb  # noqa: E402

_REPO_ROOT = _BACKEND_DIR.parent
_KB_PATH = _REPO_ROOT / "knowledge" / "exercise-library.json"
_LIBRARY_ROOT = _REPO_ROOT / "protocols" / "protocol-library"


def _load_kb() -> list[dict]:
    with open(_KB_PATH) as f:
        data = json.load(f)
    return data.get("exercises", [])


# ---------------------------------------------------------------------------
# 1. Exercise library integrity
# ---------------------------------------------------------------------------

def test_every_exercise_has_body_region():
    """Every entry must declare a body_region the validator can resolve.

    A missing field bypasses the planner's cross-region safety check
    (backend/agents/planner.py:_validate_region), so this is a
    safety-critical assertion, not a hygiene one.
    """
    missing: list[str] = []
    for ex in _load_kb():
        eid = ex.get("id") or "<no-id>"
        if exercise_kb.body_region_for(eid) is None:
            missing.append(eid)
    assert not missing, (
        "Exercises with no resolvable body_region (validator would "
        f"silently pass them): {missing}"
    )


def test_every_body_region_is_allowed():
    """Every body_region tag must be one clinical_taxonomy recognises."""
    seen: set[str] = set()
    for ex in _load_kb():
        region = ex.get("body_region")
        if region:
            seen.add(region)
    unknown = seen - clinical_taxonomy.ALLOWED_REGIONS
    assert not unknown, (
        f"Exercise library uses body_region values not in "
        f"clinical_taxonomy.ALLOWED_REGIONS: {sorted(unknown)}. "
        "Add them to ALLOWED_REGIONS or fix the entry."
    )


def test_in_scope_regions_have_exercises():
    """Knee + ankle (the in-scope regions per 2026-05-07 scoping decision)
    must each have at least one exercise. Failing this means the agents
    would have nothing to draft from for an in-scope injury."""
    seen: set[str] = {ex.get("body_region") for ex in _load_kb() if ex.get("body_region")}
    for region in ("knee", "ankle"):
        assert region in seen, f"in-scope region {region!r} has no exercises in the KB"


# ---------------------------------------------------------------------------
# 2. clinical_taxonomy round-trip
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "injury_type, expected",
    [
        ("post-acl reconstruction", "knee"),
        ("ACL Reconstruction", "knee"),  # case-insensitive
        ("meniscus tear", "knee"),
        ("knee pain", "knee"),
        ("lateral ankle sprain", "ankle"),
        ("achilles tendinopathy", "ankle"),
        ("ankle sprain", "ankle"),
        # Out-of-scope: deterministic map still resolves them so we can refuse
        # at the planner gate instead of guessing.
        ("rotator cuff repair", "shoulder"),
        ("low back pain", "low_back"),
        ("hamstring strain", "hamstring"),
        ("tennis elbow", "elbow"),
    ],
)
def test_body_region_resolves_known_injuries(injury_type, expected):
    assert clinical_taxonomy.body_region(injury_type) == expected


def test_body_region_returns_none_for_unknown():
    assert clinical_taxonomy.body_region("hangnail") is None
    assert clinical_taxonomy.body_region("") is None
    assert clinical_taxonomy.body_region(None) is None


# ---------------------------------------------------------------------------
# 3. Protocol library coverage (knee + ankle in scope)
# ---------------------------------------------------------------------------

def test_knee_library_has_expected_files():
    """Knee directory must contain post-acl-week-3 and post-acl-week-4.

    These are the two files the researcher cites for ACL patients today.
    A rename or deletion silently regresses every knee draft to the
    closest-earlier fallback path - we want a loud failure instead.
    """
    knee = _LIBRARY_ROOT / "knee"
    assert knee.is_dir(), "protocols/protocol-library/knee/ missing"
    files = {p.name for p in knee.glob("*.yaml")}
    for required in ("post-acl-week-3.yaml", "post-acl-week-4.yaml"):
        assert required in files, (
            f"protocols/protocol-library/knee/{required} missing. "
            f"Found: {sorted(files)}"
        )


def test_ankle_library_has_expected_files():
    """Ankle directory must contain lateral-sprain-grade1-week-1 and -week-3."""
    ankle = _LIBRARY_ROOT / "ankle"
    assert ankle.is_dir(), "protocols/protocol-library/ankle/ missing"
    files = {p.name for p in ankle.glob("*.yaml")}
    required = (
        "lateral-sprain-grade1-week-1.yaml",
        "lateral-sprain-grade1-week-3.yaml",
    )
    for r in required:
        assert r in files, (
            f"protocols/protocol-library/ankle/{r} missing. "
            f"Found: {sorted(files)}"
        )


def test_researcher_loads_exact_week_for_knee():
    """Knee week 4 should resolve to post-acl-week-4 (exact match path)."""
    from agents.researcher import _load_library_files

    files = _load_library_files(_LIBRARY_ROOT / "knee", week=4)
    paths = [f["path"] for f in files]
    assert any("post-acl-week-4" in p for p in paths), paths
    assert not any("post-acl-week-3" in p for p in paths), paths


def test_researcher_falls_back_for_ankle_gap_week():
    """Ankle week 2 has no exact file; researcher returns *something*
    rather than empty. PR-B will surface which file via library_match,
    but the deterministic file resolution shouldn't regress."""
    from agents.researcher import _load_library_files

    files = _load_library_files(_LIBRARY_ROOT / "ankle", week=2)
    assert files, "researcher returned no files for ankle week 2"


def test_every_region_has_minimum_week_coverage():
    """Every body region (knee, ankle, shoulder, hamstring, elbow, low-back)
    must have at least week-1 and week-3 library files.

    Per the 2026-05-07 scoping decision the agent flow is focused on knee
    + ankle, but the other regions still need their existing files
    intact - PR-B's library_match marker reads from them when surfacing
    coverage status to the clinician.
    """
    expected_dirs = {
        "knee": ("post-acl-week-3.yaml", "post-acl-week-4.yaml"),
        "ankle": (
            "lateral-sprain-grade1-week-1.yaml",
            "lateral-sprain-grade1-week-3.yaml",
        ),
        "shoulder": (
            "rotator-cuff-strain-week-1.yaml",
            "rotator-cuff-strain-week-3.yaml",
        ),
        "hamstring": (
            "grade1-strain-week-1.yaml",
            "grade1-strain-week-3.yaml",
        ),
        "elbow": (
            "lateral-tendinopathy-week-1.yaml",
            "lateral-tendinopathy-week-3.yaml",
        ),
        "low-back": (
            "non-specific-lbp-week-1.yaml",
            "non-specific-lbp-week-3.yaml",
        ),
    }
    missing: list[str] = []
    for region, required_files in expected_dirs.items():
        rdir = _LIBRARY_ROOT / region
        if not rdir.is_dir():
            missing.append(f"{region}/ (directory missing)")
            continue
        present = {p.name for p in rdir.glob("*.yaml")}
        for required in required_files:
            if required not in present:
                missing.append(f"{region}/{required}")
    assert not missing, f"Protocol library files missing: {missing}"


# ---------------------------------------------------------------------------
# 4. Planner cross-region safety net
# ---------------------------------------------------------------------------

def test_planner_validator_raises_on_cross_region_payload():
    """A payload with wall_sit (knee) under an ankle plan must raise.

    This is the load-bearing safety net: even if the LLM hallucinates an
    out-of-region exercise, the deterministic post-LLM validator catches
    it before the draft reaches the clinician queue.
    """
    from agents.planner import PlannerError, _validate_region

    payload = {
        "exercises": [
            {"id": "wall_sit", "name": "Wall Sit"},
        ],
    }
    with pytest.raises(PlannerError):
        _validate_region(payload, expected_region="ankle", token="test")


def test_planner_validator_passes_on_in_region_payload():
    """Sanity check: a knee exercise under a knee plan must pass."""
    from agents.planner import _validate_region

    payload = {
        "exercises": [
            {"id": "wall_sit", "name": "Wall Sit"},
            {"id": "quad_sets", "name": "Quad Sets"},
        ],
    }
    _validate_region(payload, expected_region="knee", token="test")


def test_planner_validator_skips_when_region_unknown():
    """Currently the validator skips when expected_region is None. That
    behaviour is documented in planner.py:_validate_region; pinning it in
    a test means PR-B's out-of-scope refusal needs to NOT rely on this
    skip - the refusal must happen upstream of the planner gate."""
    from agents.planner import _validate_region

    payload = {"exercises": [{"id": "wall_sit"}]}
    _validate_region(payload, expected_region=None, token="test")
