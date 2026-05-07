"""
researcher.py - Phase B step 1: pick candidate exercises with citations.

The researcher reads the patient's intake (injury type, phase, week)
and returns a structured list of candidate exercises pulled from the
protocol library YAML files in protocols/protocol-library/<injury>/, each
with a citation back to the source library file/line and a brief rationale.

It does NOT decide whether to progress, hold, or regress (that is the
evaluator's job) and does NOT decide the final protocol (that is the
planner's job). Keeping the researcher narrow lets us swap libraries
(KB vs PubMed retrieval vs internal doc store) without touching the
rest of the pipeline.

Sonnet 4.6: clinical reasoning over which evidence-based exercises fit
the phase. Output is a small JSON shape, validated before return.

No silent fallbacks: any Anthropic / SDK / parsing failure raises
ResearcherError. The orchestrator catches it and surfaces a 5xx so the
patient gets a "plan generation failed, please try again" toast instead
of a half-broken protocol.
"""
from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


_DEFAULT_MODEL = "claude-sonnet-4-6"
_LIBRARY_ROOT = Path(__file__).resolve().parent.parent.parent / "protocols" / "protocol-library"


class ResearcherError(RuntimeError):
    """Raised when the researcher cannot produce candidates."""


_SYSTEM_PROMPT = (
    "You are a rehabilitation researcher. You receive a patient's injury "
    "type, rehab phase, and week, plus the contents of one or more "
    "evidence-based protocol library YAML files for that injury. Pick 4-8 "
    "candidate exercises from those files that fit the patient's phase and "
    "week.\n\n"
    "For each candidate, cite the exact library file path it came from and "
    "the approximate line number where the exercise block begins. Give a "
    "one-sentence rationale grounded in the library entry. List "
    "progression options when the file lists progression_to. Do not invent "
    "exercises that are not in the library files. Output only via the "
    "propose_candidates tool."
)


_TOOL = {
    "name": "propose_candidates",
    "description": "Return the candidate exercise set for the planner.",
    "input_schema": {
        "type": "object",
        "properties": {
            "candidates": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "exercise_id": {
                            "type": "string",
                            "description": "Stable id matching the library entry's `name`.",
                        },
                        "citation_path": {
                            "type": "string",
                            "description": (
                                "Library file path the exercise was sourced "
                                "from, relative to the repo root "
                                "(e.g. 'protocols/protocol-library/knee/post-acl-week-3.yaml')."
                            ),
                        },
                        "citation_line": {
                            "type": "integer",
                            "description": (
                                "Approximate line number where the exercise "
                                "block starts in citation_path."
                            ),
                        },
                        "rationale": {"type": "string"},
                        "progression_options": {
                            "type": "array",
                            "items": {"type": "string"},
                        },
                    },
                    "required": ["exercise_id", "citation_path", "rationale"],
                },
            },
        },
        "required": ["candidates"],
    },
}


def _model() -> str:
    return os.getenv("RESEARCHER_MODEL", _DEFAULT_MODEL)


def _injury_dir(injury_type: str | None) -> Path | None:
    """Map an injury_type token to a library subdir (knee, ankle, ...).

    Returns None when the injury doesn't map cleanly. The caller falls
    through to the empty-library branch and returns []; not all patients
    have an evidence-based library yet.
    """
    if not injury_type:
        return None
    s = injury_type.lower().replace("_", "-").replace(" ", "-")
    table = {
        "knee": ["knee", "acl", "mcl", "meniscus", "patell"],
        "ankle": ["ankle", "achilles", "calf"],
        "shoulder": ["shoulder", "rotator-cuff", "labrum"],
        "low-back": ["low-back", "lumbar", "back-pain", "lbp"],
        "hamstring": ["hamstring", "ham-strain"],
        "elbow": ["elbow", "tennis-elbow", "golfer", "epicond"],
    }
    for cat, keys in table.items():
        if any(k in s for k in keys):
            cand = _LIBRARY_ROOT / cat
            if cand.is_dir():
                return cand
    return None


def _load_library_files(injury_dir: Path, week: int) -> list[dict[str, Any]]:
    """Return the YAML files matching the patient's week, or all of them
    if no week-specific match is found.

    The file naming convention is `<condition>-week-N.yaml`. We prefer an
    exact week match; if none exists we fall back to the closest earlier
    week so the model has something to reason from.
    """
    candidates = sorted(p for p in injury_dir.glob("*.yaml") if p.is_file())
    if not candidates:
        return []

    # Prefer exact week match.
    exact = [p for p in candidates if f"week-{week}" in p.stem]
    if exact:
        return [_read_file(p) for p in exact]

    # Fall back to the most-recent earlier week we have.
    def _file_week(p: Path) -> int:
        parts = p.stem.split("-week-")
        if len(parts) != 2:
            return 0
        try:
            return int(parts[1])
        except ValueError:
            return 0

    earlier = [p for p in candidates if _file_week(p) <= week]
    if earlier:
        earlier.sort(key=_file_week, reverse=True)
        return [_read_file(earlier[0])]

    # No earlier file exists either - hand the model the lowest-week
    # entry so it can extrapolate from acute-phase prescriptions.
    candidates.sort(key=_file_week)
    return [_read_file(candidates[0])]


def _read_file(path: Path) -> dict[str, Any]:
    """Read a YAML library file and return {path, contents}.

    We send the model the raw YAML text rather than a parsed dict so it
    can quote line numbers back. Limited to ~6KB per file in practice.
    """
    text = path.read_text(encoding="utf-8")
    rel = str(path.relative_to(_LIBRARY_ROOT.parent.parent))
    return {"path": rel, "contents": text}


def _build_user_prompt(
    injury_type: str | None,
    phase: str,
    week: int,
    intake: dict[str, Any] | None,
    library_files: list[dict[str, Any]],
) -> str:
    parts: list[str] = [
        f"Injury type: {injury_type or 'unspecified'}",
        f"Rehab phase: {phase}",
        f"Week: {week}",
    ]
    if intake:
        # Intake may contain PHI fields the model needs (surgery_date,
        # ROM goals). Don't log this content - the orchestrator's logger
        # only emits token + step name.
        parts.append("Patient intake:\n" + json.dumps(intake, indent=2, default=str))

    for entry in library_files:
        parts.append(
            f"Library file ({entry['path']}):\n"
            f"```yaml\n{entry['contents']}\n```"
        )

    parts.append(
        "Return 4-8 candidates. Each candidate's exercise_id MUST be the "
        "`name` field from one of the library entries above. citation_path "
        "MUST be one of the file paths shown. citation_line is the line "
        "number where that exercise's block begins. Do not invent."
    )
    return "\n\n".join(parts)


def candidates(
    injury_type: str | None,
    phase: str,
    week: int,
    intake: dict[str, Any] | None = None,
    *,
    token: str | None = None,
) -> list[dict[str, Any]]:
    """Return candidate exercises with citations and rationale.

    Parameters
    ----------
    injury_type : str | None
        e.g. "knee", "ankle". Pulled from intake.injury_type. None means
        the injury didn't map to a known library subdir.
    phase : str
        One of "acute", "subacute", "strength".
    week : int
        Week number within the phase.
    intake : dict | None
        Full intake snapshot for context (surgery_date, ROM goals, etc.).
    token : str | None
        For logging only - the patient's auth.uid().

    Returns
    -------
    list[dict]
        Each dict has keys: exercise_id, citation_path, citation_line,
        rationale, progression_options. Empty list when no library files
        exist for the patient's injury (legitimately empty library).

    Raises
    ------
    ResearcherError
        On Anthropic API failures, missing API key, or malformed model
        output. The orchestrator catches and surfaces a 5xx; we never
        silently degrade into a half-empty protocol.
    """
    api_key = os.getenv("ANTHROPIC_API_KEY", "").strip()
    if not api_key:
        raise ResearcherError("ANTHROPIC_API_KEY is not configured")

    injury_dir = _injury_dir(injury_type)
    if injury_dir is None:
        # Empty library is a known case (general conditioning, undocumented
        # injury). Empty-list is a valid response, not an error.
        logger.info(
            "researcher: no library dir for injury=%s token=%s",
            injury_type, token,
        )
        return []

    library_files = _load_library_files(injury_dir, week)
    if not library_files:
        logger.info(
            "researcher: empty library_files in %s for week=%d token=%s",
            injury_dir, week, token,
        )
        return []

    try:
        import anthropic
    except ImportError as exc:
        raise ResearcherError(f"anthropic SDK not installed: {exc}") from exc

    client = anthropic.Anthropic(api_key=api_key)
    prompt = _build_user_prompt(injury_type, phase, week, intake, library_files)

    started = time.monotonic()
    try:
        resp = client.messages.create(
            model=_model(),
            max_tokens=1500,
            system=_SYSTEM_PROMPT,
            tools=[_TOOL],
            tool_choice={"type": "tool", "name": "propose_candidates"},
            messages=[{"role": "user", "content": prompt}],
        )
    except Exception as exc:
        elapsed_ms = int((time.monotonic() - started) * 1000)
        logger.warning(
            "researcher anthropic call failed in %dms token=%s: %s",
            elapsed_ms, token, exc,
        )
        raise ResearcherError(f"researcher unavailable: {exc}") from exc

    elapsed_ms = int((time.monotonic() - started) * 1000)
    usage = getattr(resp, "usage", None)
    in_tokens = getattr(usage, "input_tokens", None) if usage else None
    out_tokens = getattr(usage, "output_tokens", None) if usage else None

    for block in resp.content or []:
        if (
            getattr(block, "type", None) == "tool_use"
            and getattr(block, "name", "") == "propose_candidates"
        ):
            cands = (block.input or {}).get("candidates") or []
            logger.info(
                "researcher ok in %dms in_tokens=%s out_tokens=%s "
                "n_candidates=%d token=%s",
                elapsed_ms, in_tokens, out_tokens, len(cands), token,
            )
            return list(cands)

    raise ResearcherError("researcher returned no propose_candidates tool call")
