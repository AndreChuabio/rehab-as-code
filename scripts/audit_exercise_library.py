"""Audit knowledge/exercise-library.json for schema completeness + parity.

Runs four invariants:

  1. Every exercise has the required fields (id, name, body_region, phase,
     injury_types, cues, default_dose, form_check_supported).
  2. Every exercise with form_check_supported=true has a matching key in
     frontend/pose.js's EXERCISES literal.
  3. Every exercise has a matching frontend/videos/<id>.mp4 on disk.
  4. Every exercise's body_region is one of the canonical regions.

Designed to be runnable two ways:

    python3 scripts/audit_exercise_library.py        # CLI: prints summary, exits 1 on any failure
    pytest backend/tests/test_library_invariants.py  # CI: same logic, asserts inside tests

The CLI is the friendly developer surface; the pytest is the safety net
that runs in CI without manual invocation.
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
LIBRARY_JSON = REPO_ROOT / "knowledge" / "exercise-library.json"
POSE_JS = REPO_ROOT / "frontend" / "pose.js"
VIDEOS_DIR = REPO_ROOT / "frontend" / "videos"

REQUIRED_FIELDS: tuple[str, ...] = (
    "id",
    "name",
    "body_region",
    "phase",
    "injury_types",
    "cues",
    "default_dose",
    "form_check_supported",
)

# Canonical region tokens. Matches clinical_taxonomy.BODY_REGIONS — the
# library uses `low_back` (underscore), not `lowback`.
VALID_BODY_REGIONS: frozenset[str] = frozenset(
    {"knee", "ankle", "hamstring", "shoulder", "elbow", "low_back", "hip"}
)


def load_exercises() -> list[dict]:
    """Read the canonical library JSON. Raises on file missing / parse error."""
    text = LIBRARY_JSON.read_text()
    data = json.loads(text)
    return data.get("exercises", [])


def parse_pose_js_exercise_ids() -> set[str]:
    """Extract the keys from pose.js's `const EXERCISES = { ... };` literal.

    Same parser as backend/tests/test_pose_coverage.py — kept duplicated
    rather than importing across the test boundary so this script stays
    runnable without pytest on path.
    """
    text = POSE_JS.read_text()
    start = text.index("const EXERCISES = {")
    tail = text[start:]
    end_rel = tail.index("\n};")
    block = tail[: end_rel + 3]
    ids = set(re.findall(r"^\s{2}([a-z_]+):\s*\{", block, re.MULTILINE))
    ids.discard("primary")
    return ids


def audit_required_fields(exercises: list[dict]) -> list[str]:
    """Return a list of human-readable failures, empty when all entries pass."""
    failures: list[str] = []
    for ex in exercises:
        ex_id = ex.get("id") or "<missing id>"
        for field in REQUIRED_FIELDS:
            if field not in ex:
                failures.append(f"{ex_id}: missing required field '{field}'")
    return failures


def audit_form_check_pose_parity(exercises: list[dict]) -> list[str]:
    """Form-check-supported entries MUST be in pose.js EXERCISES."""
    pose_ids = parse_pose_js_exercise_ids()
    flagged_ids = {
        ex["id"]
        for ex in exercises
        if ex.get("id") and ex.get("form_check_supported") is True
    }
    failures: list[str] = []
    for missing in sorted(flagged_ids - pose_ids):
        failures.append(
            f"{missing}: form_check_supported=true but not in pose.js EXERCISES"
        )
    for stranded in sorted(pose_ids - flagged_ids):
        failures.append(
            f"{stranded}: in pose.js EXERCISES but library "
            "form_check_supported is not true"
        )
    return failures


def audit_video_files(exercises: list[dict]) -> list[str]:
    """Each exercise needs a frontend/videos/<id>.mp4 on disk."""
    failures: list[str] = []
    for ex in exercises:
        ex_id = ex.get("id")
        if not ex_id:
            continue
        path = VIDEOS_DIR / f"{ex_id}.mp4"
        if not path.is_file():
            failures.append(f"{ex_id}: missing video at {path.relative_to(REPO_ROOT)}")
    return failures


def audit_body_regions(exercises: list[dict]) -> list[str]:
    """Body region must be in the canonical taxonomy."""
    failures: list[str] = []
    for ex in exercises:
        ex_id = ex.get("id") or "<missing id>"
        region = ex.get("body_region")
        if region not in VALID_BODY_REGIONS:
            failures.append(
                f"{ex_id}: body_region={region!r} not in "
                f"{sorted(VALID_BODY_REGIONS)}"
            )
    return failures


def run_audit() -> tuple[list[str], dict[str, int]]:
    """Run every audit. Returns (failures, summary_counts)."""
    exercises = load_exercises()
    failures: list[str] = []
    failures.extend(audit_required_fields(exercises))
    failures.extend(audit_form_check_pose_parity(exercises))
    failures.extend(audit_video_files(exercises))
    failures.extend(audit_body_regions(exercises))

    region_counts: dict[str, int] = {}
    for ex in exercises:
        r = ex.get("body_region") or "<unknown>"
        region_counts[r] = region_counts.get(r, 0) + 1
    summary = {
        "total_exercises": len(exercises),
        "form_check_supported": sum(
            1 for ex in exercises if ex.get("form_check_supported") is True
        ),
        **{f"region_{k}": v for k, v in sorted(region_counts.items())},
    }
    return failures, summary


def _print_summary(summary: dict[str, int]) -> None:
    print("Exercise library audit")
    print("=" * 60)
    for k, v in summary.items():
        print(f"  {k:<32} {v}")


def main() -> int:
    failures, summary = run_audit()
    _print_summary(summary)
    print()
    if failures:
        print(f"FAILURES ({len(failures)}):")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("All audits passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
