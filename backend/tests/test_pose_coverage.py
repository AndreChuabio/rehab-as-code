"""Pose form-check coverage invariant.

The form-check button in the chat-rendered exercise card only attaches when
the exercise's id is a key in window.PoseFormCheck.EXERCISES (defined in
frontend/pose.js). If the library renames an exercise (e.g., "mini_squats" →
"mini_squat"), the pose engine still works in isolation but the button
silently never renders on that card.

This test parses both files and enforces that every id registered in
PoseFormCheck.EXERCISES exists in knowledge/exercise-library.json. We do
NOT enforce the reverse direction — the library is intentionally larger
than the form-check coverage (only a subset of exercises have skeleton
checks tuned).

Failure mode this catches: a future PR renames an exercise id in the
library and forgets to update pose.js, so the chat card silently loses
its form-check CTA. The test fails at CI time instead of in production.
"""

from __future__ import annotations

import json
import re
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
POSE_JS = REPO_ROOT / "frontend" / "pose.js"
LIBRARY_JSON = REPO_ROOT / "knowledge" / "exercise-library.json"


def _registered_pose_ids() -> set[str]:
    """Parse the EXERCISES = { ... } block out of frontend/pose.js.

    We use a regex rather than a JS parser because pose.js is plain ES
    modules and the EXERCISES literal uses simple `id: { ... }` keys at
    the top level of the object. This matches `^  some_id_snake_case:` at
    the start of an indented line within the EXERCISES block.
    """
    text = POSE_JS.read_text()
    start = text.index("const EXERCISES = {")
    # Match the closing `};` that ends the literal — the next top-level one
    # after the opening brace.
    tail = text[start:]
    end_rel = tail.index("\n};")
    block = tail[: end_rel + 3]
    # Each entry looks like `  some_id: { ... },` on its own line.
    ids = set(re.findall(r"^\s{2}([a-z_]+):\s*\{", block, re.MULTILINE))
    # The token "primary" appears inside each entry's value — strip it
    # alongside any other false positives that aren't real exercise keys.
    ids.discard("primary")
    return ids


def _library_ids() -> set[str]:
    data = json.loads(LIBRARY_JSON.read_text())
    return {ex["id"] for ex in data.get("exercises", []) if ex.get("id")}


def test_every_pose_form_check_id_exists_in_library():
    """Every id registered with form-check checks must exist in the library.

    If this fails, either:
      * the library renamed/removed the exercise — update pose.js to match, or
      * pose.js added a new id that isn't in the library — add the exercise
        to knowledge/exercise-library.json so chat cards can render it.
    """
    pose_ids = _registered_pose_ids()
    library_ids = _library_ids()
    assert pose_ids, "Could not parse any ids out of frontend/pose.js EXERCISES block"
    missing = sorted(pose_ids - library_ids)
    assert not missing, (
        f"Pose form-check ids not found in exercise library: {missing}. "
        "The form-check button silently fails to render for these ids. "
        "Either add them to knowledge/exercise-library.json or remove "
        "them from frontend/pose.js EXERCISES."
    )


def test_pose_coverage_includes_a_few_high_value_exercises():
    """Smoke check: the curated 'a few exercises' coverage set is intact.

    These ids are the ones the demo and clinician dashboard depend on for
    visible form-check + agent observability. If a refactor accidentally
    drops one, this test surfaces it before merge.
    """
    pose_ids = _registered_pose_ids()
    expected_minimum = {
        "mini_squat",            # keystone knee rehab exercise
        "single_leg_squat",      # progression target
        "wall_sit",              # isometric quad
        "glute_bridge",          # hip extension
        "ham_walking_lunge",     # hamstring + knee combined
        "lb_bird_dog",           # lower-back stability
        "ankle_calf_raises_double_leg",  # ankle / calf
        "shoulder_wall_slides",  # shoulder mobility
    }
    missing = sorted(expected_minimum - pose_ids)
    assert not missing, (
        f"Expected baseline form-check coverage missing for: {missing}. "
        "These exercises are referenced in product/demo flows."
    )
