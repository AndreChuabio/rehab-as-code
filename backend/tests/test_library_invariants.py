"""CI safety net for the exercise-library audit.

Runs the same audits scripts/audit_exercise_library.py runs, but as
pytest assertions so a regression in knowledge/exercise-library.json or
frontend/pose.js fails CI before merge instead of breaking a patient's
session in production.

The CLI script in scripts/ is the developer-friendly surface; this test
is the gate.
"""
from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPTS_DIR = REPO_ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import audit_exercise_library as audit  # noqa: E402


def _exercises():
    return audit.load_exercises()


def test_required_fields_present_on_every_exercise():
    failures = audit.audit_required_fields(_exercises())
    assert not failures, "required-field failures:\n  " + "\n  ".join(failures)


def test_form_check_supported_matches_pose_js():
    failures = audit.audit_form_check_pose_parity(_exercises())
    assert not failures, "form-check parity failures:\n  " + "\n  ".join(failures)


def test_every_exercise_has_a_video_file():
    failures = audit.audit_video_files(_exercises())
    assert not failures, "video-file failures:\n  " + "\n  ".join(failures)


def test_every_body_region_is_in_canonical_taxonomy():
    failures = audit.audit_body_regions(_exercises())
    assert not failures, "body-region failures:\n  " + "\n  ".join(failures)


def test_full_audit_summary_counts_match_expectations():
    """Sanity check: 50 total, 19 form-check supported. Knee has 10 (post-ACL
    week-4 references single_leg_balance + heel_raises, synced from the
    protocol library); every other region has 8."""
    _, summary = audit.run_audit()
    assert summary["total_exercises"] == 50
    assert summary["form_check_supported"] == 19
    # Update these alongside library growth — the failure mode here is "you
    # added a new exercise; bump the expected count" not a real regression.
    expected = {"knee": 10, "ankle": 8, "hamstring": 8,
                "shoulder": 8, "elbow": 8, "low_back": 8}
    for region, n in expected.items():
        assert summary[f"region_{region}"] == n, (
            f"expected {n} exercises in region={region}, got "
            f"{summary[f'region_{region}']}"
        )
