"""Claude-vision audit: does each exercise video match its prescription?

For every entry in `knowledge/exercise-library.json` (or a filtered subset),
this script:

  1. Samples 6 evenly-spaced frames from `frontend/videos/<id>.mp4` via
     ffmpeg, scaled to <=512px on the longest edge.
  2. Sends the frames + the exercise's name / body_region / phase /
     injury_types / cues / default_dose to Claude Sonnet 4.6 (vision).
  3. Asks the model to grade the match: match | partial | mismatch | unclear,
     with a confidence rating and a patient_safety_risk grade.
  4. Writes a human-readable report to `docs/videos_audit_report.md` and a
     machine-readable JSON to `docs/videos_audit_report.json`.

This script is operator-only. It is NOT run in CI. Andre runs it manually
when he wants to triage video correctness; --dry-run validates inputs
and prints an estimated cost without calling the API.

Surfaces the "Seated Heel Raise -> prone hip extension video" class of
bug at scale: previously a patient had to notice and complain. Now the
audit finds them all in ~5 minutes for ~$2.

Usage:
    python3 scripts/validate_videos.py --dry-run                # validate, no API calls
    python3 scripts/validate_videos.py --filter knee --limit 3  # smoke test
    python3 scripts/validate_videos.py                          # full run (~5 min, ~$2)
    python3 scripts/validate_videos.py --resume                 # skip already-graded

Resume semantics: the JSON report is written incrementally. On --resume,
exercises whose id already has a non-error grade in the JSON are skipped.
"""
from __future__ import annotations

import argparse
import base64
import json
import logging
import os
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger("validate_videos")

REPO_ROOT = Path(__file__).resolve().parent.parent
LIBRARY_JSON = REPO_ROOT / "knowledge" / "exercise-library.json"
VIDEOS_DIR = REPO_ROOT / "frontend" / "videos"

# Match the rest of the codebase. Model ID stays pinned; override via --model
# only for staging / eval.
DEFAULT_MODEL = "claude-sonnet-4-6"

# Frames per video. 6 gives enough temporal coverage to spot wrong exercises
# without exploding cost. Each frame ~1500 input tokens at 512px.
FRAMES_PER_VIDEO = 6

# Longest edge for sampled frames. 512px keeps payloads small and cost low
# while remaining legible to the model.
FRAME_LONGEST_EDGE = 512

# Sleep between API calls. Even 48 exercises is only 48s of sleep — not
# worth aggressive parallelism, and gentle on rate limits.
INTER_CALL_SLEEP_S = 1.0

# Cost estimate constants (Sonnet 4.6 pricing, ~$3/M input + $15/M output).
# Each ~512px image is ~1500 tokens. Prompt + response ~500 each.
COST_PER_IMAGE_USD = 1500 * 3 / 1_000_000          # ~$0.0045
COST_PER_OUTPUT_USD = 500 * 15 / 1_000_000          # ~$0.0075

VALID_BODY_REGIONS: frozenset[str] = frozenset(
    {"knee", "ankle", "hamstring", "shoulder", "elbow", "low_back", "hip"}
)

VALID_GRADES: frozenset[str] = frozenset({"match", "partial", "mismatch", "unclear"})
VALID_CONFIDENCE: frozenset[str] = frozenset({"high", "medium", "low"})
VALID_RISK: frozenset[str] = frozenset({"none", "low", "medium", "high"})

SYSTEM_PROMPT = (
    "You are a physical-therapy expert auditing exercise demonstration videos. "
    "The user's app prescribes specific exercises with specific body positioning. "
    "Your job: rate whether the video content matches the exercise description.\n\n"
    "For each grading task, output strict JSON with this exact shape:\n"
    "{\n"
    '  "match_grade": "match" | "partial" | "mismatch" | "unclear",\n'
    '  "confidence": "high" | "medium" | "low",\n'
    '  "summary": "<one sentence describing what the video actually shows>",\n'
    '  "issues": ["<each notable mismatch or concern, terse>"],\n'
    '  "patient_safety_risk": "none" | "low" | "medium" | "high"\n'
    "}\n\n"
    "Grading rubric:\n"
    "- match: video unambiguously demonstrates the prescribed exercise correctly\n"
    "- partial: right exercise family, wrong variant or wrong body position "
    "(e.g., supine when seated is prescribed)\n"
    "- mismatch: completely different exercise (e.g., prone hip extension instead "
    "of seated heel raise)\n"
    "- unclear: frames don't show enough to grade (body out of frame, wrong angle)\n\n"
    "patient_safety_risk:\n"
    "- none: wrong content but not dangerous\n"
    "- low: wrong content, low chance a patient could hurt themselves\n"
    "- medium: a patient mimicking this for the prescribed indication could cause "
    "discomfort or minor reinjury\n"
    "- high: a patient mimicking this could cause real harm (e.g., prescribed for "
    "ACL recovery but shows full-load squat)\n\n"
    "Output ONLY the JSON object. No prose before or after."
)


# ---------------------------------------------------------------------------
# Input validation
# ---------------------------------------------------------------------------

def _check_ffmpeg() -> str:
    """Return the ffmpeg path or exit with an actionable error."""
    path = shutil.which("ffmpeg")
    if not path:
        print(
            "ERROR: ffmpeg not found on PATH. Install it via "
            "`brew install ffmpeg` (macOS) or `apt-get install ffmpeg` (Linux).",
            file=sys.stderr,
        )
        sys.exit(2)
    return path


def _check_api_key(dry_run: bool) -> str | None:
    """Return the key, or None on dry-run if absent. Hard-fail on real run."""
    api_key = os.getenv("ANTHROPIC_API_KEY", "").strip()
    if not api_key:
        if dry_run:
            print("WARN: ANTHROPIC_API_KEY is not set (dry-run continues)")
            return None
        print(
            "ERROR: ANTHROPIC_API_KEY environment variable is not set. "
            "Set it before running the audit.",
            file=sys.stderr,
        )
        sys.exit(2)
    return api_key


def _load_exercises() -> list[dict[str, Any]]:
    text = LIBRARY_JSON.read_text()
    data = json.loads(text)
    return data.get("exercises", [])


def _filter_exercises(
    exercises: list[dict[str, Any]],
    region: str | None,
    limit: int | None,
) -> list[dict[str, Any]]:
    """Apply --filter / --limit selectors."""
    out = exercises
    if region:
        if region not in VALID_BODY_REGIONS:
            print(
                f"ERROR: --filter must be one of "
                f"{sorted(VALID_BODY_REGIONS)} (got {region!r})",
                file=sys.stderr,
            )
            sys.exit(2)
        out = [ex for ex in out if ex.get("body_region") == region]
    if limit is not None and limit >= 0:
        out = out[:limit]
    return out


def _verify_videos_exist(exercises: list[dict[str, Any]]) -> list[str]:
    """Return ids whose video file is missing."""
    missing: list[str] = []
    for ex in exercises:
        ex_id = ex.get("id")
        if not ex_id:
            missing.append("<missing-id>")
            continue
        if not (VIDEOS_DIR / f"{ex_id}.mp4").is_file():
            missing.append(ex_id)
    return missing


# ---------------------------------------------------------------------------
# Frame sampling
# ---------------------------------------------------------------------------

def _video_duration_seconds(video_path: Path, ffmpeg_bin: str) -> float | None:
    """Return duration in seconds via ffprobe. None if unavailable."""
    ffprobe = shutil.which("ffprobe")
    if not ffprobe:
        # Fall back to ffmpeg parsing if ffprobe absent. Most installs have both.
        try:
            result = subprocess.run(
                [ffmpeg_bin, "-i", str(video_path)],
                capture_output=True,
                text=True,
                timeout=15,
            )
            stderr = result.stderr
        except Exception:
            return None
        # Look for "Duration: HH:MM:SS.SS"
        import re
        match = re.search(r"Duration:\s+(\d+):(\d+):(\d+(?:\.\d+)?)", stderr)
        if not match:
            return None
        hh, mm, ss = match.groups()
        return int(hh) * 3600 + int(mm) * 60 + float(ss)
    try:
        result = subprocess.run(
            [
                ffprobe,
                "-v", "error",
                "-show_entries", "format=duration",
                "-of", "default=noprint_wrappers=1:nokey=1",
                str(video_path),
            ],
            capture_output=True,
            text=True,
            timeout=15,
            check=True,
        )
        return float(result.stdout.strip())
    except Exception:
        return None


def _extract_frames(
    video_path: Path,
    out_dir: Path,
    ffmpeg_bin: str,
    n_frames: int = FRAMES_PER_VIDEO,
) -> list[Path]:
    """Extract n evenly-spaced JPEG frames into out_dir.

    Returns the list of frame paths in temporal order. Empty on failure.
    """
    duration = _video_duration_seconds(video_path, ffmpeg_bin)
    if duration is None or duration <= 0:
        logger.warning("could not determine duration for %s", video_path)
        return []

    # Place samples at duration * (i + 0.5) / n for i in [0..n) — avoids
    # the leading black frame and the tail freeze that cap many demo clips.
    timestamps = [duration * (i + 0.5) / n_frames for i in range(n_frames)]
    frames: list[Path] = []
    for i, ts in enumerate(timestamps):
        out_path = out_dir / f"frame_{i:02d}.jpg"
        cmd = [
            ffmpeg_bin,
            "-y",
            "-ss", f"{ts:.3f}",
            "-i", str(video_path),
            "-frames:v", "1",
            "-vf", f"scale='min({FRAME_LONGEST_EDGE},iw)':-2",
            "-q:v", "5",
            str(out_path),
        ]
        try:
            subprocess.run(
                cmd,
                capture_output=True,
                timeout=20,
                check=True,
            )
        except subprocess.CalledProcessError as exc:
            logger.warning(
                "ffmpeg failed for %s @ %.2fs: %s",
                video_path.name, ts, exc.stderr[:200] if exc.stderr else "",
            )
            continue
        except Exception as exc:
            logger.warning("ffmpeg crashed for %s: %s", video_path.name, exc)
            continue
        if out_path.is_file() and out_path.stat().st_size > 0:
            frames.append(out_path)
    return frames


def _encode_image_b64(path: Path) -> str:
    return base64.standard_b64encode(path.read_bytes()).decode("ascii")


# ---------------------------------------------------------------------------
# Anthropic call
# ---------------------------------------------------------------------------

def _build_user_text(exercise: dict[str, Any]) -> str:
    cues = exercise.get("cues", []) or []
    cues_block = "\n".join(f"- {c}" for c in cues) if cues else "(no cues)"
    indications = ", ".join(exercise.get("injury_types", []) or []) or "(unspecified)"
    phase = exercise.get("phase", "(unspecified)")
    if isinstance(phase, list):
        phase = ", ".join(phase) or "(unspecified)"
    return (
        f"Exercise: {exercise.get('name', '?')} (id: {exercise.get('id', '?')})\n"
        f"Body region: {exercise.get('body_region', '?')}\n"
        f"Phase: {phase}\n"
        f"Indications: {indications}\n"
        f"Cues:\n{cues_block}\n"
        f"Prescribed dose: {exercise.get('default_dose', '?')}\n\n"
        "The video should show this exercise being demonstrated correctly. "
        f"Below are {FRAMES_PER_VIDEO} frames sampled from the video. Grade the match."
    )


def _grade_video(
    client: Any,
    model: str,
    exercise: dict[str, Any],
    frame_paths: list[Path],
) -> dict[str, Any]:
    """Call Claude vision and return parsed JSON. Raises on hard failure."""
    user_text = _build_user_text(exercise)
    content_blocks: list[dict[str, Any]] = [{"type": "text", "text": user_text}]
    for fp in frame_paths:
        content_blocks.append(
            {
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": "image/jpeg",
                    "data": _encode_image_b64(fp),
                },
            }
        )

    resp = client.messages.create(
        model=model,
        max_tokens=600,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": content_blocks}],
    )

    # Extract first text block.
    text = ""
    for block in resp.content or []:
        if getattr(block, "type", None) == "text":
            text = (getattr(block, "text", "") or "").strip()
            if text:
                break
    if not text:
        raise RuntimeError("model returned empty response")

    # Strip ```json fences if model adds them despite instruction.
    if text.startswith("```"):
        lines = text.splitlines()
        # drop opening fence
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        # drop closing fence
        if lines and lines[-1].startswith("```"):
            lines = lines[:-1]
        text = "\n".join(lines).strip()

    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"could not parse JSON: {exc}; raw={text[:300]}") from exc

    grade = parsed.get("match_grade")
    if grade not in VALID_GRADES:
        raise RuntimeError(f"invalid match_grade: {grade!r}")
    confidence = parsed.get("confidence")
    if confidence not in VALID_CONFIDENCE:
        raise RuntimeError(f"invalid confidence: {confidence!r}")
    risk = parsed.get("patient_safety_risk")
    if risk not in VALID_RISK:
        raise RuntimeError(f"invalid patient_safety_risk: {risk!r}")

    usage = getattr(resp, "usage", None)
    in_tokens = getattr(usage, "input_tokens", None) if usage else None
    out_tokens = getattr(usage, "output_tokens", None) if usage else None
    parsed["_usage"] = {"input_tokens": in_tokens, "output_tokens": out_tokens}
    return parsed


# ---------------------------------------------------------------------------
# Report writers
# ---------------------------------------------------------------------------

def _write_json_report(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, default=str))


def _format_md_section(grade_label: str, rows: list[dict[str, Any]]) -> str:
    """Render one severity bucket of the MD report."""
    if not rows:
        return ""
    lines: list[str] = [f"## {grade_label}", ""]
    for row in rows:
        ex = row["exercise"]
        result = row.get("result") or {}
        cues = ex.get("cues", []) or []
        issues = result.get("issues", []) or []
        lines.append(
            f"### {ex.get('id')} — {ex.get('name')} ({ex.get('body_region')})"
        )
        lines.append(
            f"- Grade: {result.get('match_grade')} "
            f"({result.get('confidence')} confidence)"
        )
        lines.append(
            f"- Patient safety risk: {result.get('patient_safety_risk')}"
        )
        lines.append(
            f"- What the video actually shows: {result.get('summary', '(n/a)')}"
        )
        if issues:
            lines.append("- Issues:")
            for iss in issues:
                lines.append(f"  - {iss}")
        if cues:
            lines.append("- Cues prescribed:")
            for cue in cues:
                lines.append(f"  - {cue}")
        if result.get("match_grade") == "mismatch":
            lines.append(
                f"- Suggested action: replace video; current content shows "
                f"{result.get('summary', 'something else')} not "
                f"{ex.get('name')}."
            )
        elif result.get("match_grade") == "partial":
            lines.append(
                "- Suggested action: review with PT; consider re-shoot or "
                "remap to the closer-matching library entry."
            )
        elif result.get("match_grade") == "unclear":
            lines.append(
                "- Suggested action: re-shoot with a clearer angle / better "
                "framing of the working body region."
            )
        lines.append("")
    return "\n".join(lines)


def _write_md_report(
    path: Path,
    rows: list[dict[str, Any]],
    skipped: list[dict[str, Any]],
    started_at: str,
    model: str,
) -> None:
    """Write the human-readable MD report. Mismatches first."""
    counts = {"match": 0, "partial": 0, "mismatch": 0, "unclear": 0, "error": 0}
    for row in rows:
        result = row.get("result")
        if result is None:
            counts["error"] += 1
            continue
        grade = result.get("match_grade", "error")
        counts[grade] = counts.get(grade, 0) + 1

    by_grade: dict[str, list[dict[str, Any]]] = {
        "mismatch": [], "partial": [], "unclear": [], "match": [], "error": [],
    }
    for row in rows:
        result = row.get("result")
        if result is None:
            by_grade["error"].append(row)
            continue
        by_grade.setdefault(result["match_grade"], []).append(row)

    out: list[str] = [
        "# Exercise video audit report",
        "",
        f"Generated: {started_at}",
        f"Model: {model}",
        f"Total: {len(rows)} exercises | "
        f"match: {counts.get('match', 0)} | "
        f"partial: {counts.get('partial', 0)} | "
        f"mismatch: {counts.get('mismatch', 0)} | "
        f"unclear: {counts.get('unclear', 0)} | "
        f"error: {counts.get('error', 0)}",
        "",
    ]

    if by_grade["mismatch"]:
        out.append(_format_md_section("Mismatches (highest priority)", by_grade["mismatch"]))
    if by_grade["partial"]:
        out.append(_format_md_section("Partial matches (review)", by_grade["partial"]))
    if by_grade["unclear"]:
        out.append(_format_md_section("Unclear (need re-shoot or re-frame)", by_grade["unclear"]))

    if by_grade["match"]:
        out.append("## Matches (no action needed)")
        out.append("")
        out.append("| ID | Name | Confidence |")
        out.append("|----|------|------------|")
        for row in by_grade["match"]:
            ex = row["exercise"]
            res = row["result"]
            out.append(
                f"| {ex.get('id')} | {ex.get('name')} | {res.get('confidence')} |"
            )
        out.append("")

    if by_grade["error"]:
        out.append("## Errors (re-run these)")
        out.append("")
        for row in by_grade["error"]:
            ex = row["exercise"]
            out.append(
                f"- {ex.get('id')} — {ex.get('name')}: {row.get('error', 'unknown')}"
            )
        out.append("")

    if skipped:
        out.append("## Skipped")
        out.append("")
        for row in skipped:
            out.append(
                f"- {row.get('id')}: {row.get('reason', 'skipped')}"
            )
        out.append("")

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(out))


# ---------------------------------------------------------------------------
# Resume support
# ---------------------------------------------------------------------------

def _load_existing_grades(json_path: Path) -> dict[str, dict[str, Any]]:
    """Load prior grades for --resume. Returns id -> row mapping."""
    if not json_path.is_file():
        return {}
    try:
        data = json.loads(json_path.read_text())
    except Exception:
        return {}
    out: dict[str, dict[str, Any]] = {}
    for row in data.get("rows", []) or []:
        ex = row.get("exercise") or {}
        ex_id = ex.get("id")
        if ex_id and row.get("result") is not None:
            out[ex_id] = row
    return out


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="validate_videos.py",
        description=(
            "Audit each exercise video against its library description "
            "via Claude Sonnet 4.6 vision."
        ),
    )
    p.add_argument(
        "--filter",
        choices=sorted(VALID_BODY_REGIONS),
        default=None,
        help="Only audit exercises in this body region.",
    )
    p.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Audit only the first N exercises after filtering.",
    )
    p.add_argument(
        "--output-dir",
        default=str(REPO_ROOT / "docs"),
        help="Where to write reports (default: docs/).",
    )
    p.add_argument(
        "--model",
        default=DEFAULT_MODEL,
        help=f"Claude model id (default: {DEFAULT_MODEL}).",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Validate inputs + print plan; do NOT call Claude.",
    )
    p.add_argument(
        "--resume",
        action="store_true",
        help="Skip exercises that already have a non-error grade in the JSON report.",
    )
    return p


def _print_plan(
    n_exercises: int,
    n_frames: int,
    model: str,
    dry_run: bool,
) -> None:
    n_images = n_exercises * n_frames
    est_input = n_images * COST_PER_IMAGE_USD
    est_output = n_exercises * COST_PER_OUTPUT_USD
    est_total = est_input + est_output
    print(f"Model: {model}")
    print(
        f"Would audit {n_exercises} exercises with {n_frames} frames each "
        f"= {n_images} images sent to Claude."
    )
    print(
        f"Estimated cost: ~${est_total:.2f} "
        f"(input ~${est_input:.2f}, output ~${est_output:.2f})"
    )
    if dry_run:
        print("Dry run; no API calls made.")


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    args = _build_parser().parse_args(argv)

    output_dir = Path(args.output_dir).resolve()
    md_path = output_dir / "videos_audit_report.md"
    json_path = output_dir / "videos_audit_report.json"

    # Validation phase. Both dry-run and real run go through this.
    ffmpeg_bin = _check_ffmpeg()
    api_key = _check_api_key(dry_run=args.dry_run)

    exercises = _load_exercises()
    selected = _filter_exercises(exercises, args.filter, args.limit)

    if not selected:
        print("No exercises match the given filter / limit.", file=sys.stderr)
        return 1

    missing = _verify_videos_exist(selected)
    if missing:
        print(
            f"ERROR: {len(missing)} exercise(s) missing video files: "
            f"{', '.join(sorted(missing))}",
            file=sys.stderr,
        )
        return 2

    # Resume support: skip already-graded ids.
    prior_rows: dict[str, dict[str, Any]] = {}
    if args.resume:
        prior_rows = _load_existing_grades(json_path)
        if prior_rows:
            already = [ex for ex in selected if ex.get("id") in prior_rows]
            print(f"--resume: {len(already)} already graded, skipping those.")
        selected_to_run = [ex for ex in selected if ex.get("id") not in prior_rows]
    else:
        selected_to_run = selected

    _print_plan(
        n_exercises=len(selected_to_run),
        n_frames=FRAMES_PER_VIDEO,
        model=args.model,
        dry_run=args.dry_run,
    )

    if args.dry_run:
        return 0

    # Real run.
    try:
        import anthropic
    except ImportError:
        print(
            "ERROR: anthropic SDK missing. Install with `pip install anthropic`.",
            file=sys.stderr,
        )
        return 2

    assert api_key is not None
    client = anthropic.Anthropic(api_key=api_key)

    started_at = datetime.now(timezone.utc).isoformat(timespec="seconds")

    # Seed rows with prior runs (resume) so the JSON report stays append-merged.
    rows: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for ex_id, row in prior_rows.items():
        rows.append(row)
        seen_ids.add(ex_id)

    skipped: list[dict[str, Any]] = []

    for idx, ex in enumerate(selected_to_run, start=1):
        ex_id = ex.get("id", "<no-id>")
        video_path = VIDEOS_DIR / f"{ex_id}.mp4"
        logger.info("[%d/%d] grading %s", idx, len(selected_to_run), ex_id)

        try:
            with tempfile.TemporaryDirectory(prefix=f"validate_{ex_id}_") as tmp:
                tmp_path = Path(tmp)
                frames = _extract_frames(video_path, tmp_path, ffmpeg_bin)
                if len(frames) < 2:
                    rows.append({
                        "exercise": ex,
                        "result": None,
                        "error": f"only {len(frames)} frame(s) extracted; expected {FRAMES_PER_VIDEO}",
                    })
                    seen_ids.add(ex_id)
                    _write_json_report(json_path, {
                        "started_at": started_at,
                        "model": args.model,
                        "rows": rows,
                        "skipped": skipped,
                    })
                    continue

                result = _grade_video(client, args.model, ex, frames)
                rows.append({"exercise": ex, "result": result, "error": None})
                seen_ids.add(ex_id)
        except Exception as exc:
            logger.warning("grading failed for %s: %s", ex_id, exc)
            rows.append({"exercise": ex, "result": None, "error": str(exc)})
            seen_ids.add(ex_id)

        # Incremental write — crash-safe resume.
        _write_json_report(json_path, {
            "started_at": started_at,
            "model": args.model,
            "rows": rows,
            "skipped": skipped,
        })

        # Gentle pacing.
        if idx < len(selected_to_run):
            time.sleep(INTER_CALL_SLEEP_S)

    # Final MD render. Done last so the JSON is always the source of truth
    # if a crash happens partway through.
    _write_md_report(md_path, rows, skipped, started_at, args.model)

    counts: dict[str, int] = {}
    for row in rows:
        res = row.get("result")
        key = res.get("match_grade") if res else "error"
        counts[key] = counts.get(key, 0) + 1
    print()
    print(f"Audit complete. Reports written to:")
    print(f"  {md_path}")
    print(f"  {json_path}")
    print(f"Summary: {counts}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
