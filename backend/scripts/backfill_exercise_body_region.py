"""
backfill_exercise_body_region.py - one-off: write body_region into every entry
of knowledge/exercise-library.json.

Maps the existing `injury_types` list (already curated per entry) to the
canonical body_region taxonomy. Idempotent: re-running won't change a file
that already has body_region set on every entry.

Run from repo root:
    python backend/scripts/backfill_exercise_body_region.py

Commits should pair the JSON change with this script (the script is the
record of how the values were derived).
"""
from __future__ import annotations

import json
from pathlib import Path

# Allowed regions match clinical_taxonomy.ALLOWED_REGIONS exactly.
_INJURY_TYPE_TO_REGION: dict[str, str] = {
    "knee": "knee",
    "ankle": "ankle",
    "shoulder": "shoulder",
    "low_back": "low_back",
    "hamstring": "hamstring",
    "elbow": "elbow",
}


def _resolve(injury_types: list[str]) -> str | None:
    """Return the single body_region this exercise targets, or None.

    If injury_types covers multiple regions we return "multi" so the
    deterministic validator can't auto-block (clinician decides).
    """
    if not injury_types:
        return None
    mapped = {
        _INJURY_TYPE_TO_REGION[i.lower()]
        for i in injury_types
        if i.lower() in _INJURY_TYPE_TO_REGION
    }
    if not mapped:
        return None
    if len(mapped) == 1:
        return next(iter(mapped))
    return "multi"


def main() -> None:
    repo_root = Path(__file__).resolve().parents[2]
    kb_path = repo_root / "knowledge" / "exercise-library.json"
    data = json.loads(kb_path.read_text(encoding="utf-8"))

    updated = 0
    skipped: list[str] = []
    for ex in data.get("exercises", []):
        region = _resolve(ex.get("injury_types", []))
        if region is None:
            skipped.append(ex.get("id", "<no-id>"))
            continue
        if ex.get("body_region") == region:
            continue
        ex["body_region"] = region
        updated += 1

    kb_path.write_text(
        json.dumps(data, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    print(f"updated {updated} exercise entries with body_region")
    if skipped:
        print(f"skipped (no resolvable injury_type): {skipped}")


if __name__ == "__main__":
    main()
