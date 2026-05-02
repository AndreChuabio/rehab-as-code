"""
video_generator.py - Sora-2 powered exercise-video agent.

Given an exercise_id from exercise_kb, this module produces a short MP4
clip tailored to the patient's rehab phase (post-ACL, week 3-4) with the
exact verbal cues baked into the prompt. The ouputs land in
frontend/videos/{exercise_id}.mp4 so they're served by uvicorn's existing
/static mount as /static/videos/{exercise_id}.mp4.

Usage as a module:
    from video_generator import ensure_video_for
    path = await ensure_video_for("mini_squat")

Usage as CLI (the demo prep path):
    python3 -m video_generator --all      # generate all 8
    python3 -m video_generator mini_squat  # one exercise

Failures are non-fatal: the chat card renderer falls back to youtube_id
when generated_video_url is missing, so a half-completed library still
demos cleanly.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import exercise_kb

logger = logging.getLogger(__name__)

VIDEO_DIR = Path(__file__).parent.parent / "frontend" / "videos"
VIDEO_DIR.mkdir(parents=True, exist_ok=True)


# Per-exercise prompts. Each is grounded in the exercise's clinical cues so
# Sora generates a clip that matches the protocol guidance, not a generic
# gym-influencer demo.
EXERCISE_PROMPTS: dict[str, str] = {
    "quad_sets": (
        "A clean, well-lit physical-therapy clinic. A patient lies supine on a "
        "padded table, leg fully extended. Camera holds steady at a 45-degree "
        "side angle. The patient performs a quad set: knee locked straight, "
        "kneecap pulling toward the hip, heel pressing gently down into the "
        "table. Five-second isometric hold, then release. No jerky motion. "
        "Calm clinical lighting, soft natural daylight, no music. 8 seconds."
    ),
    "heel_slides": (
        "Bright physical-therapy clinic. Patient lies supine on a padded table, "
        "wearing athletic shorts. Camera at side angle, knees in frame. Patient "
        "slowly slides their heel along the table toward the glute, bending the "
        "knee actively. They pause at end-range, then slide back to fully extended. "
        "Movement is slow, deliberate, controlled. No bouncing. "
        "Calm clinical lighting, no music. 8 seconds."
    ),
    "terminal_knee_extension": (
        "Physical-therapy clinic. Patient stands facing a sturdy anchor with a "
        "yellow resistance band looped behind one knee, anchored at hip height "
        "in front. Camera at side angle, full leg in frame. Patient extends the "
        "knee through the last 30 degrees of motion against the band, locking "
        "out fully, then slowly returning. Slow, controlled, no quad lag. "
        "Bright clinical lighting, no music. 8 seconds."
    ),
    "stationary_bike": (
        "Physical-therapy gym with a stationary bike. Patient seated upright, "
        "knees tracking forward smoothly through full pedal revolutions. Camera "
        "at a 3/4 side angle, both legs visible. Pace is calm and conversational, "
        "around 60 RPM. Soft natural lighting, no music. 8 seconds."
    ),
    "glute_bridge": (
        "Physical-therapy clinic, patient supine on a padded mat with knees bent, "
        "feet flat on the floor. Camera at a low side angle. Patient squeezes "
        "their glutes and lifts their hips into a bridge, holds for two seconds "
        "at the top, then lowers slowly with control. Hips fully extended at the "
        "top, ribcage stays down. Calm clinical lighting, no music. 8 seconds."
    ),
    "wall_sit": (
        "Physical-therapy clinic. Patient leans against a smooth wall, slides "
        "down into a wall-sit at roughly 60 degrees of knee flexion, thighs "
        "parallel-ish to the floor. Camera at a 3/4 angle, full body in frame. "
        "Patient holds the position with steady breathing, knees tracking over "
        "toes. Calm lighting, no music. 8 seconds."
    ),
    "mini_squat": (
        "Physical-therapy clinic, smooth floor. Patient stands with feet "
        "shoulder-width apart, hands relaxed by the sides. Camera at 3/4 angle, "
        "full body. Patient performs a slow controlled mini squat to roughly 45 "
        "degrees of knee flexion, holds for one second, then rises to standing. "
        "Knees track over the second toe. No deeper than 45 degrees. "
        "Calm clinical lighting, no music. 8 seconds."
    ),
    "single_leg_squat": (
        "Physical-therapy clinic. Patient stands on one leg, opposite leg "
        "extended forward an inch off the floor for counterbalance, arms "
        "reaching forward. Camera at 3/4 front angle. Patient performs a slow "
        "single-leg squat to about 60 degrees, holds briefly, then returns to "
        "standing. Hip, knee, and second toe stay aligned. Three-second descent. "
        "Calm clinical lighting, no music. 8 seconds."
    ),
}


def _client():
    from openai import OpenAI

    api_key = os.getenv("OPENAI_API_KEY", "").strip()
    if not api_key or api_key == "your_openai_key_here":
        raise RuntimeError("OPENAI_API_KEY is not set")
    return OpenAI(api_key=api_key)


def _output_path(exercise_id: str) -> Path:
    return VIDEO_DIR / f"{exercise_id}.mp4"


def static_url(exercise_id: str) -> str | None:
    """Return the /static URL if the MP4 is already on disk, else None."""
    path = _output_path(exercise_id)
    return f"/static/videos/{exercise_id}.mp4" if path.exists() else None


def generate_one(
    exercise_id: str,
    *,
    overwrite: bool = False,
    seconds: str = "8",
    size: str = "1280x720",
    model: str = "sora-2",
    poll_interval: float = 6.0,
    timeout_s: float = 600.0,
) -> Path:
    """
    Generate a single exercise video and write it to frontend/videos/{id}.mp4.

    Returns the local path on success. Raises on failure - callers should
    treat any exception as "fall back to youtube".
    """
    out = _output_path(exercise_id)
    if out.exists() and not overwrite:
        logger.info("[video] %s: already on disk, skipping", exercise_id)
        return out

    prompt = EXERCISE_PROMPTS.get(exercise_id)
    if not prompt:
        raise ValueError(f"no Sora prompt configured for {exercise_id!r}")

    client = _client()
    logger.info("[video] %s: starting Sora-2 job", exercise_id)
    job = client.videos.create(
        model=model,
        prompt=prompt,
        seconds=seconds,
        size=size,
    )
    job_id = job.id
    logger.info("[video] %s: job %s queued", exercise_id, job_id)

    started = time.time()
    while True:
        elapsed = time.time() - started
        if elapsed > timeout_s:
            raise TimeoutError(
                f"Sora job {job_id} for {exercise_id} did not finish in {timeout_s}s"
            )
        time.sleep(poll_interval)
        job = client.videos.retrieve(job_id)
        status = getattr(job, "status", "unknown")
        progress = getattr(job, "progress", 0) or 0
        logger.info(
            "[video] %s: %s %.0f%% (%.0fs elapsed)",
            exercise_id, status, progress, elapsed,
        )
        if status == "completed":
            break
        if status == "failed":
            err = getattr(getattr(job, "error", None), "message", "unknown error")
            raise RuntimeError(f"Sora job {job_id} failed: {err}")

    content = client.videos.download_content(job_id, variant="video")
    content.write_to_file(str(out))
    logger.info("[video] %s: wrote %s (%d bytes)", exercise_id, out, out.stat().st_size)
    return out


def generate_all(
    exercise_ids: list[str] | None = None,
    *,
    overwrite: bool = False,
    max_workers: int = 4,
) -> dict[str, str]:
    """
    Pre-generate videos for the given exercise ids (or all of them).

    Returns {exercise_id: status_string}. Status is "ok" or "error: ...".
    Failures are recorded but do not abort the batch.
    """
    ids = exercise_ids or [
        ex["id"] for ex in exercise_kb.list_all() if ex["id"] in EXERCISE_PROMPTS
    ]
    results: dict[str, str] = {}

    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = {
            pool.submit(generate_one, eid, overwrite=overwrite): eid
            for eid in ids
        }
        for fut in as_completed(futures):
            eid = futures[fut]
            try:
                fut.result()
                results[eid] = "ok"
            except Exception as exc:
                logger.exception("[video] %s: failed", eid)
                results[eid] = f"error: {exc}"

    return results


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "exercises",
        nargs="*",
        help="exercise_ids to generate; empty + --all generates everything",
    )
    parser.add_argument("--all", action="store_true", help="generate every exercise")
    parser.add_argument("--overwrite", action="store_true", help="re-render even if mp4 exists")
    parser.add_argument("--workers", type=int, default=4, help="parallel Sora jobs")
    args = parser.parse_args()

    if args.all:
        ids = list(EXERCISE_PROMPTS.keys())
    elif args.exercises:
        ids = args.exercises
    else:
        parser.print_help()
        return 1

    results = generate_all(ids, overwrite=args.overwrite, max_workers=args.workers)
    print()
    print("Generation summary:")
    for eid, status in results.items():
        print(f"  {eid:30s} {status}")
    n_ok = sum(1 for s in results.values() if s == "ok")
    print(f"\n{n_ok}/{len(results)} succeeded")
    return 0 if n_ok == len(results) else 2


if __name__ == "__main__":
    sys.exit(main())
