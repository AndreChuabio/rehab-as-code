"""
exercise_kb.py - curated exercise video knowledge base.

Loads knowledge/exercise-library.json once at import time and exposes simple
lookup helpers used by coach_chat.py and the /chat endpoint. No embeddings,
no vector store - 8 entries fit in a dict.

Public surface:
  list_all()                  -> list[Exercise]
  list_ids()                  -> list[str]
  find_by_id(exercise_id)     -> Exercise | None
  find_by_phase(phase)        -> list[Exercise]
  keyword_search(query)       -> list[Exercise]
  to_card(exercise)           -> dict (frontend-ready render payload)
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

KB_PATH = Path(__file__).parent.parent / "knowledge" / "exercise-library.json"


def _load() -> list[dict[str, Any]]:
    if not KB_PATH.exists():
        return []
    with open(KB_PATH) as f:
        data = json.load(f)
    return data.get("exercises", [])


_EXERCISES: list[dict[str, Any]] = _load()
_BY_ID: dict[str, dict[str, Any]] = {ex["id"]: ex for ex in _EXERCISES}


def list_all() -> list[dict[str, Any]]:
    return list(_EXERCISES)


def list_ids() -> list[str]:
    return [ex["id"] for ex in _EXERCISES]


def find_by_id(exercise_id: str) -> dict[str, Any] | None:
    return _BY_ID.get(exercise_id)


def body_region_for(exercise_id: str | None) -> str | None:
    """Return the body_region tag for a given exercise id, or None.

    Looks up the entry in the library by id (or name). When the entry exists
    but has no body_region (legacy entries pre-backfill), falls back to the
    first injury_types value mapped to the canonical region taxonomy. Used
    by the deterministic post-LLM validator in protocol generation.
    """
    if not exercise_id:
        return None
    ex = _BY_ID.get(exercise_id)
    if ex is None:
        # Loose match: legacy callers sometimes pass the human name.
        slug = exercise_id.strip().lower().replace(" ", "_")
        ex = _BY_ID.get(slug)
    if ex is None:
        return None
    region = ex.get("body_region")
    if region:
        return region
    # Fall back to the injury_types list if the explicit field is missing.
    injury_types = ex.get("injury_types") or []
    if injury_types:
        return str(injury_types[0]).lower()
    return None


def find_by_phase(phase: str, injury_type: str | None = None) -> list[dict[str, Any]]:
    """Filter exercises by phase, optionally further filtered by injury_type.

    injury_type is matched against each entry's injury_types list. None (default)
    returns matches across all injury categories — preserves prior behavior.
    """
    phase = phase.lower().strip()
    results = [ex for ex in _EXERCISES if phase in [p.lower() for p in ex.get("phase", [])]]
    if injury_type:
        injury_type = injury_type.lower().strip()
        results = [ex for ex in results if injury_type in [i.lower() for i in ex.get("injury_types", [])]]
    return results


def resolve_to_library(
    exercise_id: str | None,
    name: str | None = None,
    body_region: str | None = None,
) -> dict[str, Any] | None:
    """Resolve a possibly-planner-invented exercise id to a real library entry.

    The planner LLM emits regression IDs like `seated_heel_raise` or
    `active_assisted_ankle_eversion` that aren't in the library. Without a
    resolution path the patient sees black thumbnails + no form-check button
    on My plan items. This helper tries:

      1. exact id match
      2. slug-normalized id match
      3. keyword search ranked over id+name, scoped to body_region when given

    Returns the matched library dict (callable as `to_card`), or None if no
    candidate can be found. Conservative: only returns a match when the
    keyword search produced a non-zero score.
    """
    if not exercise_id and not name:
        return None
    if exercise_id:
        ex = _BY_ID.get(exercise_id)
        if ex:
            return ex
        slug = exercise_id.strip().lower().replace(" ", "_").replace("-", "_")
        ex = _BY_ID.get(slug)
        if ex:
            return ex

    # Keyword fallback. Score over id + name; scope to body_region if known
    # so a "seated heel raise" on an ankle protocol picks an ankle exercise
    # rather than a knee one with overlapping vocabulary.
    candidates = _EXERCISES
    if body_region:
        br = body_region.lower().strip()
        scoped = [e for e in _EXERCISES if (e.get("body_region") or "").lower() == br]
        if scoped:
            candidates = scoped

    query = " ".join(filter(None, [exercise_id, name])).lower()
    query = query.replace("-", " ").replace("_", " ")
    tokens = {t for t in query.split() if len(t) > 2}  # set: dedup
    if not tokens:
        return None

    # Three-tier weighting so the resolver lands on the *semantically*
    # closest entry rather than whichever ankle exercise happens to share
    # connective vocabulary. id matches > name matches > cue matches.
    # Without this gradient, "active_assisted_ankle_eversion" tied between
    # ankle_towel_calf_stretch (cue "towel-assisted" → "assisted" hit) and
    # ankle_eversion_band (id contains "eversion") because both got the
    # same flat score; the eversion exercise should win.
    def _score(ex: dict[str, Any]) -> int:
        ex_id = (ex.get("id") or "").lower()
        ex_name = (ex.get("name") or "").lower()
        cues_text = " ".join(ex.get("cues", [])).lower()
        s = 0
        for t in tokens:
            if t in ex_id:
                s += 3
            elif t in ex_name:
                s += 2
            elif t in cues_text:
                s += 1
        return s

    best_score = 0
    best_ex: dict[str, Any] | None = None
    for ex in candidates:
        score = _score(ex)
        if score > best_score:
            best_score = score
            best_ex = ex
    return best_ex if best_score > 0 else None


def keyword_search(query: str) -> list[dict[str, Any]]:
    """
    Loose token match across id, name, and cues. Returns ranked list (most
    matches first). Cheap-and-cheerful for an 8-entry corpus.
    """
    q = query.lower().strip()
    if not q:
        return []
    tokens = [t for t in q.replace("-", " ").replace("_", " ").split() if t]
    scored: list[tuple[int, dict[str, Any]]] = []
    for ex in _EXERCISES:
        haystack = " ".join(
            [
                ex.get("id", ""),
                ex.get("name", ""),
                " ".join(ex.get("cues", [])),
            ]
        ).lower()
        score = sum(1 for t in tokens if t in haystack)
        if score:
            scored.append((score, ex))
    scored.sort(key=lambda pair: pair[0], reverse=True)
    return [ex for _, ex in scored]


def _generated_video_url(exercise_id: str) -> str | None:
    """
    Look up an on-disk Sora-generated MP4 for this exercise.

    Returns a /static URL when the file exists, else None. Done lazily so
    a video that finishes mid-session is picked up on the next chat call
    without a server restart. Import is local to keep exercise_kb cheap to
    import from anywhere.
    """
    try:
        from video_generator import static_url
    except Exception:
        return None
    return static_url(exercise_id)


def to_card(exercise: dict[str, Any]) -> dict[str, Any]:
    """Frontend-ready render payload. Never includes the full exercise blob."""
    yt = exercise.get("youtube_id", "")
    eid = exercise.get("id", "")

    # Prefer an explicit override in the JSON, then a Sora-generated MP4 on
    # disk, then fall back to no generated video (frontend uses YouTube).
    generated = exercise.get("generated_video_url") or _generated_video_url(eid)

    return {
        "id": eid,
        "name": exercise.get("name"),
        "phase": exercise.get("phase", []),
        "cues": exercise.get("cues", []),
        "default_dose": exercise.get("default_dose"),
        "youtube_id": yt,
        "youtube_embed_url": f"https://www.youtube.com/embed/{yt}" if yt else None,
        "youtube_watch_url": f"https://www.youtube.com/watch?v={yt}" if yt else None,
        "generated_video_url": generated,
        "video_source": "sora-2" if generated else ("youtube" if yt else None),
        "thumbnail_url": exercise.get("thumbnail_url"),
        "regression_of": exercise.get("regression_of"),
        "progression_to": exercise.get("progression_to", []),
    }


if __name__ == "__main__":
    print(f"Loaded {len(_EXERCISES)} exercises from {KB_PATH}")
    for ex in _EXERCISES:
        print(f"  - {ex['id']}: {ex['name']} ({', '.join(ex['phase'])})")
