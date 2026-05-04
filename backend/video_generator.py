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


# Canonical character + setting injected into every prompt to maximize visual
# uniformity across the library. Sora-2 has no character reference image
# feature; detailed prompt-side description is the only available lever.
# Acknowledged ceiling: same demographic, outfit, setting, lighting; face/build
# still varies modestly between generations.
SORA_CHARACTER_PREAMBLE = (
    "A 32-year-old woman with shoulder-length brown hair tied back in a low "
    "ponytail, wearing a fitted black athletic tank top and gray cropped "
    "leggings, athletic but not muscular build, light olive skin tone. "
    "Clean modern physical-therapy clinic with soft natural daylight from "
    "large windows, light wood floor, neutral gray walls, padded therapy "
    "table visible. Calm clinical lighting, no music."
)


# Per-exercise body. Each describes camera angle, setup, then exactly 2 reps
# (or 2 isometric holds) at the prescribed clinical tempo with a brief release
# between reps. Duration 8-14 seconds tuned to the movement.
_EXERCISE_BODIES: dict[str, str] = {
    # ---------- knee (post-ACL) ----------
    "quad_sets": (
        "Camera at 45-degree side angle. The patient lies supine on the padded "
        "table, leg fully extended. She performs 2 quad sets: knee locked straight, "
        "kneecap pulling toward the hip, heel pressing gently down into the table "
        "for a 5-second isometric hold, then a 2-second release, then the second "
        "5-second hold. No jerky motion. 14 seconds."
    ),
    "heel_slides": (
        "Camera at side angle, knee in frame. The patient lies supine on the "
        "padded table. She performs 2 heel slides: heel slides slowly toward the "
        "glute over 3 seconds, pauses 2 seconds at end-range, slides back over 3 "
        "seconds. Brief reset, then the second rep. Slow, deliberate, no bouncing. "
        "14 seconds."
    ),
    "terminal_knee_extension": (
        "Camera at side angle, full leg in frame. The patient stands facing a "
        "sturdy anchor with a yellow resistance band looped behind her right knee, "
        "anchored at hip height in front. She performs 2 terminal knee extensions: "
        "extends the knee through the last 30 degrees against the band over 2 "
        "seconds, holds 1 second locked out, returns over 2 seconds. Brief reset, "
        "then the second rep. No quad lag. 12 seconds."
    ),
    "stationary_bike": (
        "Camera at 3/4 side angle, both legs visible. The patient sits upright on "
        "a stationary bike, pedaling smoothly through full revolutions at a calm "
        "conversational pace, roughly 60 RPM. She completes about 6 controlled "
        "revolutions. Knees track forward, hips stable. 10 seconds."
    ),
    "glute_bridge": (
        "Camera at low side angle. The patient lies supine on a padded mat with "
        "knees bent, feet flat. She performs 2 glute bridges: squeezes glutes, "
        "lifts hips into a bridge over 2 seconds, holds 2 seconds at the top "
        "with ribs down, lowers over 2 seconds. Brief reset, then the second "
        "rep. 14 seconds."
    ),
    "wall_sit": (
        "Camera at 3/4 angle, full body in frame. The patient leans against a "
        "smooth wall, slides down into a wall-sit at roughly 60 degrees of knee "
        "flexion. She holds the position with steady breathing for 5 seconds, "
        "stands back up briefly, then slides back down for a second 5-second "
        "hold. Knees track over toes. 14 seconds."
    ),
    "mini_squat": (
        "Camera at 3/4 angle, full body. The patient stands with feet "
        "shoulder-width apart, hands relaxed at sides. She performs 2 controlled "
        "mini squats: descent to 45 degrees over 3 seconds, holds 1 second, rises "
        "to standing over 2 seconds. Brief reset, then the second rep. Knees "
        "track over the second toe. No deeper than 45 degrees. 14 seconds."
    ),
    "single_leg_squat": (
        "Camera at 3/4 front angle. The patient stands on her right leg, opposite "
        "leg extended forward an inch off the floor, arms reaching forward for "
        "counterbalance. She performs 2 single-leg squats: descent to about 60 "
        "degrees over 3 seconds, holds 1 second, rises over 2 seconds. Brief "
        "reset between reps. Hip, knee, second toe stay aligned. 14 seconds."
    ),

    # ---------- ankle (lateral sprain) ----------
    "ankle_alphabet": (
        "Camera at side angle, foot in frame. The patient sits at the edge of "
        "the padded therapy table with her right leg extended and foot off the "
        "edge. She slowly traces the letter A in the air with her big toe, "
        "moving only at the ankle joint, drawing the letter deliberately over "
        "about 3 seconds. Brief pause, then traces the letter B for another 3 "
        "seconds. 10 seconds."
    ),
    "ankle_towel_calf_stretch": (
        "Camera at side angle. The patient sits on the floor with her right leg "
        "extended in front, a long towel looped around the ball of her right "
        "foot, ends held in both hands. She gently pulls the foot toward her "
        "shin until a calf stretch is felt, holds steady 5 seconds, releases 2 "
        "seconds. Repeats once. No bouncing. 14 seconds."
    ),
    "ankle_dorsiflexion_band": (
        "Camera at side angle, foot in frame. The patient sits on the floor with "
        "her right leg extended, a yellow resistance band looped around the "
        "forefoot and anchored beyond the toes. She performs 2 dorsiflexions: "
        "pulls the foot up toward her shin against the band over 2 seconds, "
        "holds 1 second, slow controlled return over 3 seconds. Brief reset "
        "between reps. 14 seconds."
    ),
    "ankle_eversion_band": (
        "Camera at side angle. The patient sits on the floor with her right leg "
        "extended slightly inward, a yellow band looped around the outside of "
        "the foot and anchored medially to her left. She performs 2 eversions: "
        "rolls the foot outward against the band over 2 seconds, holds 1 second, "
        "returns over 2 seconds. Movement only at the ankle. Brief reset. 12 "
        "seconds."
    ),
    "ankle_calf_raises_double_leg": (
        "Camera at 3/4 side angle, both feet in frame. The patient stands with "
        "feet hip-width apart. She performs 2 calf raises: rises onto the balls "
        "of both feet over 1 second, pauses 1 second at the top, lowers slowly "
        "over 3 seconds. Brief reset, then the second rep. 12 seconds."
    ),
    "ankle_calf_raises_single_leg": (
        "Camera at 3/4 angle, single foot in frame. The patient stands on her "
        "right foot with the left foot lifted slightly behind, fingertips "
        "lightly touching a wall for balance only. She performs 2 single-leg "
        "calf raises on the right: rises through full range over 1 second, "
        "holds 1 second at the top, slow lower over 3 seconds. Brief reset "
        "between reps. 12 seconds."
    ),
    "ankle_single_leg_balance": (
        "Camera at 3/4 front angle, full body in frame. The patient stands on "
        "her right leg with a soft knee, hands relaxed at her sides. She holds "
        "the balance steady for 5 seconds, sets her left foot down briefly, "
        "lifts back into balance for another 5 seconds. 14 seconds."
    ),
    "ankle_lateral_hops": (
        "Camera at side angle, full body in frame. The patient stands beside a "
        "thin line on the floor. She performs 2 small lateral hops over the "
        "line, about 6 inches each, hopping right then back left. Soft landings "
        "through midfoot. 8 seconds."
    ),

    # ---------- shoulder (rotator cuff) ----------
    "shoulder_pendulum": (
        "Camera at side angle, full body. The patient stands and leans forward "
        "at the hips with her right arm hanging loosely toward the floor. Using "
        "body sway only, she swings the relaxed arm in 2 small circles: one "
        "clockwise circle over 4 seconds, brief pause, then one counterclockwise "
        "circle over 4 seconds. The arm is completely passive throughout. 10 "
        "seconds."
    ),
    "shoulder_isometric_er": (
        "Camera at 3/4 angle, upper body in frame. The patient stands beside a "
        "wall on her right side. Her right elbow is tucked against her ribs, "
        "bent at 90 degrees, the back of her right hand resting against the "
        "wall. She performs 2 isometric external rotation holds: presses the "
        "back of the hand outward into the wall at submaximal effort for 5 "
        "seconds with no movement, releases 2 seconds, second 5-second hold. "
        "14 seconds."
    ),
    "shoulder_isometric_ir": (
        "Camera at 3/4 angle, upper body in frame. The patient stands at the "
        "corner of a wall. Her right elbow is tucked at her ribs, bent 90 "
        "degrees, palm pressed against the wall corner. She performs 2 "
        "isometric internal rotation holds: presses the palm inward into the "
        "wall at submaximal effort for 5 seconds with no motion, releases 2 "
        "seconds, second 5-second hold. 14 seconds."
    ),
    "shoulder_sleeper_stretch": (
        "Camera at side angle. The patient lies on her right side on the padded "
        "table, head supported, right shoulder under her with the right elbow "
        "bent 90 degrees and the forearm pointing toward the ceiling. Her left "
        "hand presses the right forearm gently down toward the table, holds the "
        "stretch 5 seconds, releases 2 seconds. Repeats once. 14 seconds."
    ),
    "shoulder_scapular_retraction": (
        "Camera at front angle, upper body in frame. The patient stands with "
        "arms relaxed at her sides. She performs 2 scapular retractions: "
        "squeezes both shoulder blades down and back, holds the squeeze 3 "
        "seconds, releases 2 seconds, second 3-second squeeze. No shrugging. 12 "
        "seconds."
    ),
    "shoulder_wall_slides": (
        "Camera at side angle. The patient stands with her back flat against a "
        "wall, both forearms in contact with the wall in a goalpost position. "
        "She performs 2 wall slides: slides both arms slowly upward keeping "
        "forearm contact with the wall over 3 seconds, returns to starting "
        "position over 3 seconds. Brief reset, then the second rep. 14 seconds."
    ),
    "shoulder_prone_y": (
        "Camera at side angle, upper body in frame. The patient lies prone on "
        "the padded table with arms extended overhead in a Y position, thumbs "
        "pointed up. She performs 2 Y-raises: lifts the arms about 2 inches off "
        "the surface over 2 seconds squeezing the lower traps, holds 1 second "
        "at the top, lowers slowly over 3 seconds. Brief reset between reps. 14 "
        "seconds."
    ),
    "shoulder_prone_t": (
        "Camera at side angle, upper body in frame. The patient lies prone on "
        "the padded table with arms straight out to the sides forming a T, "
        "thumbs pointed up. She performs 2 T-raises: lifts the arms by squeezing "
        "the shoulder blades together over 2 seconds, holds 1 second, slow lower "
        "over 3 seconds. Brief reset between reps. 14 seconds."
    ),

    # ---------- low back (non-specific LBP) ----------
    "lb_pelvic_tilt": (
        "Camera at side angle, full body in frame. The patient lies supine on a "
        "padded mat with knees bent and feet flat on the floor. She performs 2 "
        "pelvic tilts: flattens the low back into the mat by tilting the pelvis "
        "posteriorly, holds 5 seconds, releases 2 seconds, second 5-second hold. "
        "14 seconds."
    ),
    "lb_cat_cow": (
        "Camera at side angle, full body in frame. The patient is in a quadruped "
        "position with hands under shoulders, knees under hips. She performs 2 "
        "cat-cow cycles: rounds the spine upward toward the ceiling over 2 "
        "seconds, returns to neutral 1 second, arches the spine downward over 2 "
        "seconds, returns to neutral 1 second. Smooth tempo. 12 seconds."
    ),
    "lb_supine_knee_to_chest": (
        "Camera at side angle. The patient lies supine on a padded mat. She "
        "pulls her right knee gently toward her chest with both hands, holds 5 "
        "seconds, lowers the leg back. Then pulls her left knee to her chest, "
        "holds 5 seconds, lowers. The opposite leg stays bent with foot on the "
        "floor throughout. 14 seconds."
    ),
    "lb_child_pose": (
        "Camera at 3/4 side angle. The patient kneels on the padded mat, sits "
        "her hips back to her heels with arms reaching forward and forehead "
        "toward the floor. She holds the pose with steady breathing for 8 "
        "seconds, briefly rises to a kneeling position, then settles back into "
        "the pose for another 4 seconds. 14 seconds."
    ),
    "lb_mckenzie_press_up": (
        "Camera at side angle. The patient lies prone on the padded mat with "
        "hands placed under her shoulders. She performs 2 press-ups: presses "
        "her upper body upward with her hips remaining on the floor over 2 "
        "seconds, holds 2 seconds, lowers over 2 seconds. Brief reset between "
        "reps. 14 seconds."
    ),
    "lb_glute_bridge_lb": (
        "Camera at low side angle. The patient lies supine on the mat with knees "
        "bent and feet flat. She performs 2 glute bridges: squeezes the glutes "
        "and lifts the hips with the ribs staying down over 2 seconds, holds 2 "
        "seconds at the top, lowers over 2 seconds. Brief reset between reps. "
        "14 seconds."
    ),
    "lb_dead_bug": (
        "Camera at high side angle. The patient lies supine on the mat with "
        "both arms reaching straight up toward the ceiling and both knees bent "
        "at 90 degrees over the hips. She performs 2 dead bugs: slowly lowers "
        "the right arm overhead and the left leg toward the floor over 3 "
        "seconds, returns over 2 seconds. Brief reset, then repeats with the "
        "opposite arm and leg. Low back stays pressed into the mat. 14 seconds."
    ),
    "lb_bird_dog": (
        "Camera at side angle, full body in frame. The patient is in a "
        "quadruped position with neutral spine. She performs 2 bird dogs: "
        "extends the right arm forward and the left leg straight back over 2 "
        "seconds, holds the extended position 3 seconds, returns to quadruped "
        "over 2 seconds. Brief reset between reps. Pelvis stays level. 14 "
        "seconds."
    ),

    # ---------- hamstring (grade 1 strain) ----------
    "ham_seated_active_extension": (
        "Camera at side angle. The patient sits on the edge of a sturdy chair "
        "with hands resting on her thighs. She performs 2 active knee "
        "extensions on the right leg: slowly straightens the knee until the "
        "first stretch is felt over 3 seconds, returns the foot to the floor "
        "over 2 seconds. Brief reset between reps. Smooth and controlled. 12 "
        "seconds."
    ),
    "ham_supine_active_stretch": (
        "Camera at side angle. The patient lies supine on the padded mat with "
        "her right leg lifted to roughly 90 degrees of hip flexion, both hands "
        "supporting behind the right thigh. She performs 2 active hamstring "
        "stretches: slowly straightens the knee over 3 seconds pulling the toes "
        "toward her face until a stretch is felt, returns to bent over 2 "
        "seconds. Brief reset between reps. 12 seconds."
    ),
    "ham_supine_curl_ball": (
        "Camera at high side angle. The patient lies supine on the padded mat "
        "with both heels resting on a stability ball, arms at her sides. She "
        "lifts her hips into a bridge, then performs 2 heel curls: bends the "
        "knees pulling the ball toward the glutes over 2 seconds, returns over "
        "3 seconds. Brief reset between reps. 14 seconds."
    ),
    "ham_bridge_heel_slide": (
        "Camera at side angle. The patient lies supine in a bridge position "
        "with hips lifted, heels under the knees. She performs 2 heel slides: "
        "the right heel slides slowly out away from the glutes over 2 seconds, "
        "returns over 2 seconds. Hips stay level throughout. Brief reset "
        "between reps. 12 seconds."
    ),
    "ham_prone_hip_extension": (
        "Camera at side angle. The patient lies prone on the padded mat with "
        "hands stacked under her forehead. She performs 2 hip extensions on the "
        "right leg: glute squeeze first, then lifts the straight leg about 6 "
        "inches off the floor over 2 seconds, holds 1 second, lowers over 2 "
        "seconds. No low-back arching. Brief reset between reps. 12 seconds."
    ),
    "ham_single_leg_rdl": (
        "Camera at 3/4 side angle, full body in frame. The patient stands on "
        "her right leg with a soft knee. She performs 2 single-leg Romanian "
        "deadlifts: hinges at the hip with the left leg extending straight "
        "behind for balance over 3 seconds, returns to standing over 2 seconds. "
        "Brief reset between reps. Stops where her back would round. 12 "
        "seconds."
    ),
    "ham_walking_lunge": (
        "Camera at side angle, full body in frame. The patient stands with feet "
        "together, then performs 2 forward walking lunges: steps the right foot "
        "forward and lowers the left back knee toward the floor over 2 seconds, "
        "drives through the front heel and steps left foot forward to lower the "
        "right back knee over 2 seconds. Front knee tracks over the second toe. "
        "12 seconds."
    ),
    "ham_nordic_eccentric_assisted": (
        "Camera at side angle. The patient kneels on a padded mat with her feet "
        "anchored under a heavy object and arms crossed at her chest. She "
        "performs 2 assisted Nordic curls: slowly lowers her torso forward "
        "keeping hips straight over 4 seconds, catches with both hands when "
        "needed, pushes back to the upright kneeling position over 2 seconds. "
        "Brief reset between reps. 14 seconds."
    ),

    # ---------- elbow (lateral tendinopathy) ----------
    "elbow_wrist_flexor_stretch": (
        "Camera at front angle, upper body in frame. The patient stands with "
        "her right arm extended forward at shoulder height, palm facing up. "
        "Her left hand reaches across and gently pulls the right fingers down "
        "and back, holding the stretch in the forearm flexors for 5 seconds, "
        "releases 2 seconds. Repeats once. 14 seconds."
    ),
    "elbow_wrist_extensor_stretch": (
        "Camera at front angle, upper body in frame. The patient stands with "
        "her right arm extended forward at shoulder height, palm facing down. "
        "Her left hand reaches across and gently pulls the right hand down "
        "toward the floor, holding the stretch over the top of the forearm for "
        "5 seconds, releases 2 seconds. Repeats once. 14 seconds."
    ),
    "elbow_grip_squeeze": (
        "Camera at side angle, hand in frame. The patient sits and holds a "
        "soft stress ball in her right hand. She performs 2 grip squeezes: "
        "squeezes the ball firmly for 5 seconds, full release for 2 seconds, "
        "second 5-second squeeze. 14 seconds."
    ),
    "elbow_isometric_extension": (
        "Camera at side angle, hand in frame. The patient sits at the padded "
        "therapy table with her right forearm flat on the table palm down, hand "
        "extending off the edge. Her left hand presses down on top of the right "
        "hand. She performs 2 isometric wrist extensions: presses the right "
        "hand upward against the left at submaximal effort for 5 seconds with "
        "no motion, releases 2 seconds, second 5-second hold. 14 seconds."
    ),
    "elbow_radial_nerve_glide": (
        "Camera at 3/4 angle, full body. The patient stands with her right arm "
        "extended out to the side at shoulder height, palm facing forward. She "
        "performs 2 nerve glides: drops the right wrist downward then tilts her "
        "head to the left away from the arm over 2 seconds, returns to neutral "
        "over 2 seconds. Brief reset between reps. Stops short of any tingling. "
        "12 seconds."
    ),
    "elbow_pronation_supination_band": (
        "Camera at side angle, forearm in frame. The patient sits with her "
        "right elbow tucked at her side bent 90 degrees, holding one end of a "
        "yellow band in her right hand while her left hand holds the band taut. "
        "She performs 2 pronation-supination cycles: rotates the forearm "
        "palm-down against the band over 2 seconds, then palm-up over 2 "
        "seconds. Brief reset between reps. 14 seconds."
    ),
    "elbow_eccentric_wrist_extension": (
        "Camera at side angle, forearm in frame. The patient sits at the "
        "padded therapy table with her right forearm flat on the table palm "
        "down, hand extending off the edge holding a small dumbbell. She "
        "performs 2 eccentric wrist extensions: her left hand lifts the weight "
        "to the extended position, then the right wrist slowly lowers it over 5 "
        "seconds. Brief reset between reps. 14 seconds."
    ),
    "elbow_eccentric_wrist_flexion": (
        "Camera at side angle, forearm in frame. The patient sits at the "
        "padded therapy table with her right forearm flat on the table palm "
        "up, hand extending off the edge holding a small dumbbell. She "
        "performs 2 eccentric wrist flexions: her left hand lifts the weight "
        "to the flexed position, then the right wrist slowly lowers it over 5 "
        "seconds. Brief reset between reps. 14 seconds."
    ),
}


# Public dict combines preamble + body for every exercise.
# Used by static_url() membership check and by generate_one() prompt lookup.
EXERCISE_PROMPTS: dict[str, str] = {
    eid: f"{SORA_CHARACTER_PREAMBLE} {body}"
    for eid, body in _EXERCISE_BODIES.items()
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
    """Return the /static URL for the generated MP4, or None if not available.

    Local dev: checks file existence so partially-generated libraries don't
    advertise broken URLs. Production (Vercel): the frontend/ tree isn't
    bundled into the Python function, so we fall back to the EXERCISE_PROMPTS
    registry — every exercise with a prompt was generated offline and shipped
    as a static asset.
    """
    path = _output_path(exercise_id)
    if path.exists():
        return f"/static/videos/{exercise_id}.mp4"
    if exercise_id in EXERCISE_PROMPTS:
        return f"/static/videos/{exercise_id}.mp4"
    return None


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
