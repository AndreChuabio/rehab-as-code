# Exercise coverage: making the pose model trustworthy beyond calf raises

Date: 2026-08-10
Status: approved (design), pending implementation plan
Owner: Andre

## Problem

The 2026-08-10 live validation of Double-Leg Calf Raises (the first ever
possible, after the Center Stage camera blocker fell) found the rep counter
counting the patient's walking and missing their actual raises. The root cause
(rise measured as a fraction of frame height, no locomotion suppression) was
fixed for the rise family in `289d240` — but the fix and its lessons apply
only to calf raises today, and none of the other 17 in-scope exercises have
ever been validated against a real human.

The pose registry (`frontend/pose.js` EXERCISES) splits into four signal
families. The family determines which bug classes apply and what validation
must prove:

| Family | Mode | Count | Examples | Known risk |
|---|---|---|---|---|
| Angle | max / min / max_extension | 10 | mini_squat, TKE, quad_sets, heel_slides | Scale-invariant (angles), but no locomotion gate — walking flexes knees ~60° and can fake shallow reps; in-flight rep state survives idle (phantom-rep class) |
| Rise | rise | 2 | both calf raises | Fixed in `289d240`; human re-validation still owed |
| Hold | hold | 2 | single_leg_balance, lb_bird_dog | `sway` / `hip_drop` are frame-fraction displacement checks — the *same* bug class as calf rise; hold timers don't pause during locomotion |
| Presence | presence | 5 | ankle_alphabet, band work | Nothing to validate beyond framing sanity |

Decisions taken during design (with Andre, 2026-08-10):

1. **Scope order:** Andre's active protocol first (calf raises ×2,
   single_leg_balance, ankle_alphabet), then the knee+ankle library swept by
   family using representative exercises.
2. **Ground truth:** one scripted recording session (~25 min) producing
   permanent replay fixtures — not repeated live testing, not synthetic-only.
   Live testing survives only as a final one-set acceptance pass per exercise.
3. **Thresholds:** pose.js's hardcoded angle targets are validated as-is; a
   report surfaces every disagreement with the protocol library's prescribed
   `ROM_target_deg`; actually wiring prescribed ROM into the pose engine is an
   explicitly separate follow-up. No per-patient "calibrate your range" step —
   range is clinically prescribed, and learning it from the patient would
   ratify compensation patterns (body-size calibration is already automatic
   via span normalization).

## Design

### 1. Shared signal hardening (precedes all validation)

Generalize the three `289d240` lessons from the rise family into machinery
every family uses, so hardened code is validated once rather than the same
bug rediscovered per exercise.

- **`LocomotionGuard`** — a pure module in `pose.js` (mirrored byte-equivalent
  in the node test harness, per the existing `__poseCalfRaiseHelpers`
  convention). Consumes one per-frame body sample `{hipX, hipY, span}` where
  `span` is the shoulder→ankle on-screen distance (torso-scaled fallback when
  ankles are not visible, as in `289d240`). Owns the baseline (median of the
  first 30 stationary frames) and emits `moving` when lateral hip drift
  exceeds 12% of span or apparent size changes by more than 10%. On `moving`
  it drops the baseline; tracking resumes only after the patient re-plants
  and a fresh baseline settles. The rise tracker's private copy of this logic
  migrates into the guard; constants (`CALF_MOVE_*`) are renamed to
  guard-level names.
- **Global suppression channel.** The per-frame loop computes the body sample
  once. While `moving` (or baseline-pending), every tracker receives the
  existing `isIdle` signal: angle `RepTracker`s do not advance, hold-mode
  timers pause, rise already handles it. Presence mode ignores the guard.
- **Abandon in-flight state on idle, all modes.** The angle `RepTracker` gets
  the same rule `calfRaiseStep` got: an idle frame resets any in-progress rep
  phase instead of banking it. (The phantom-rep bug: the movement pattern of
  walking enters a rep phase before the locomotion thresholds trip, then
  "completes" on the first quiet reading after re-planting.)
- **Span-normalize `sway` and `hip_drop`.** Both are displacement checks
  currently measured in frame fractions, which is exactly the calf-rise bug:
  too insensitive at the distance the framing gate requires, oversensitive up
  close. Their thresholds are re-expressed as fractions of the patient's own
  span. This directly affects single_leg_balance in Andre's protocol.

All pure cores get node-test mirrors with synthetic sequences (the inner
validation layer — it caught the phantom-rep bug in the first patch attempt
and remains the fast guard for logic; footage guards truth).

### 2. Recording session (ground truth, one-time per exercise)

Camera-only recordings — QuickTime "New Movie Recording", camera at chest
height ~6 feet, same stance as real use. **Not screen capture.** Three clips
per exercise:

| Clip | Content | Proves |
|---|---|---|
| `good` | ~8 clean reps (or a ~20s hold for hold-mode) | true positives count, and count exactly |
| `bad` | ~4 deliberate mistakes (trunk lean, shallow depth, wobble — whichever the exercise's checks claim to catch) | form flags fire on real mistakes |
| `walk` | stand still → walk toward camera → return → settle → 2 clean reps | locomotion contributes zero; re-baseline works; the 2 reps count |

Session 1 (Andre's protocol): `ankle_calf_raises_double_leg`,
`ankle_calf_raises_single_leg`, `ankle_single_leg_balance`,
`ankle_alphabet` (a single presence clip suffices for alphabet — seated,
feet visible, no rep semantics).

Session 2 (library sweep, later): angle-family representatives
`mini_squat`, `terminal_knee_extension`, `heel_slides` (standing max, floor
min, and lower-body-framed max variants — the three angle sub-shapes).

Storage and conversion:

- Raw `.mov` and converted `.y4m` live in
  `playwright/.assets/fixtures/<exercise_id>/` — **gitignored** (binary size
  plus Andre's likeness; test-data person, but not repo material).
- `scripts/qa-fixture-convert.mjs` wraps ffmpeg (present on the machine):
  scales/letterboxes to 640×480 (`-pix_fmt yuv420p`), validates the output's
  resolution and frame rate, refuses silently-empty inputs.
- `playwright/.assets/fixtures/manifest.json` is **committed** (no PHI —
  exercise ids, clip filenames, expected counts). It is the contract:

```json
{
  "ankle_calf_raises_double_leg": {
    "good": { "clip": "good.y4m", "expected_reps": 8, "expected_bad_max": 0 },
    "bad":  { "clip": "bad.y4m",  "expected_reps": 4, "expected_bad_min": 3 },
    "walk": { "clip": "walk.y4m", "expected_reps": 2 }
  }
}
```

Every expectation is written down from what Andre actually performed — no
invented numbers. A tolerance of ±0 on `expected_reps` is the default;
hold-mode entries carry `expected_hold_s: {min, max}` instead, and
presence-mode entries carry `expected_presence: true` (assert the presence
check reports the patient present for ≥80% of the clip and the set
completes — that is all presence mode claims).

Two recording rules exist because Chromium **loops** Y4M clips:

- Every clip begins and ends with ~2s of standing still in the same stance,
  so the loop seam is not itself a movement artifact.
- Every manifest entry carries `duration_s` (stamped by the conversion
  script). The runner asserts at the single-pass boundary and then tears the
  engine down — it never lets a second loop inflate the count. "Final count
  exactly N" is meaningless under looping; "count at first-pass end" is the
  real contract.

### 3. Fixture runner

`npm run qa:fixtures` → a runner in the existing `pose-chromium` lane
(`tests/e2e/pose-fixtures.spec.ts` plus a small launcher helper):

- For each manifest entry (reading `duration_s` for the single-pass
  boundary), launch a fresh Chromium with
  `--use-file-for-fake-video-capture=<clip>` (the `QA_POSE_CLIP` machinery
  proved on 2026-08-10), reuse the captured storage state, drive the real UI:
  Browse exercises → the exercise → Start exercise → through the preflight →
  run the set to completion.
- Assert the **user-facing** counters, because those are the product:
  final rep count exactly `expected_reps`; at least `expected_bad_min` reps
  flagged not-good on `bad` clips and at most `expected_bad_max` on `good`
  clips; `walk` clips end at exactly their post-walk rep count; hold duration
  within `expected_hold_s`.
- Backend: **local uvicorn + sqlite** (same env as the Maya conversational
  runs). Completing a set writes session state, so prod is never a valid
  target here; the existing prod-write guard already refuses it.
- Runtime budget: MediaPipe cold-starts ~60–90s per browser launch, so the
  full protocol manifest is ~10 minutes. This is an on-demand lane (and later,
  if wanted, a nightly), never per-PR CI.

### 4. Threshold-consistency report

`scripts/pose-threshold-report.mjs`:

- Parses the `EXERCISES` registry's hardcoded targets out of
  `frontend/pose.js` and every `ROM_target_deg` per exercise across
  `protocols/protocol-library/**.yaml`.
- Prints a conflict table: exercise, pose target, protocol range(s), verdict
  (`consistent` / `conflict` / `no-protocol-entry`).
- Informational and manually run in this effort. Its output is the opening
  brief for the separate wire-prescribed-ROM follow-up. It changes no
  behavior.

### 5. Order of work, success criteria, error handling

Order:

1. Shared hardening + node mirrors green (includes migrating the rise
   tracker onto `LocomotionGuard` without changing its behavior — the
   existing rise mirror tests must pass unmodified).
2. Andre's recording session 1; convert; write manifest from observed truth.
3. Fixture runner green on the full protocol manifest.
4. Live acceptance: one set per protocol exercise on prod, Andre in frame,
   counters watched live (the only surviving manual step).
5. Library sweep: recording session 2 (three angle representatives),
   fixtures green, threshold report produced; presence exercises get a
   framing-sanity fixture only if one of them ever misbehaves (YAGNI).

Done means:

- Every exercise in Andre's active protocol: its manifest fixtures green
  (three clips for rep/hold modes, one presence clip for ankle_alphabet) +
  one live acceptance pass.
- Angle family: representative fixtures green.
- Threshold report exists; every conflict is either explained in the report
  or ticketed for the ROM-wiring follow-up.

Error handling:

- Runner failures are loud and per-clip: the rep log, the final counter
  state, and a screenshot land in the report; other clips continue.
- The conversion script rejects wrong-resolution/empty output rather than
  producing a fixture that silently tests nothing.
- A manifest entry whose clip file is missing is an **error, not a skip**.
  Silent skips are how false greens happen; a fixture lane that quietly runs
  zero fixtures must go red. (Session lesson, twice over: the acknowledge
  spec's trivially-true assertion, and the "sequential log" fallacy.)

## Out of scope (explicit)

- Wiring prescribed `ROM_target_deg` into the pose engine (follow-up, seeded
  by the threshold report).
- Any per-patient range-calibration step (clinically rejected in design).
- Shoulder / low-back / hamstring exercises (out of product scope —
  `IN_SCOPE_REGIONS` is knee+ankle).
- The three counter-display defects from the live session (engine-total vs
  in-set mismatch, `%` rendered as `°`, opaque ✗ "form check") — already
  queued as their own fix; the fixture runner will simply make their absence
  visible.
- Committing any video of Andre to the repo.
