# Exercise Coverage Validation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the pose model trustworthy for every exercise in Andre's active protocol (then the knee+ankle library) via shared locomotion/normalization hardening plus a permanent human-footage fixture harness.

**Architecture:** Generalize the calf-raise fixes (`289d240`) into a shared `LocomotionGuard` consumed by all four signal families; capture Andre performing each exercise once as Y4M fixtures; replay them through the real MediaPipe pipeline with a manifest-driven runner asserting the user-facing counters; report pose-vs-protocol threshold conflicts without changing behavior.

**Tech Stack:** Vanilla JS (`frontend/pose.js`, IIFE, no build step), node-runnable mirror tests (no Jest/jsdom), Playwright fake-camera lane (`--use-file-for-fake-video-capture`), ffmpeg for clip conversion.

## Global Constraints

- Spec: `docs/superpowers/specs/2026-08-10-exercise-coverage-design.md`. If plan and spec disagree, the spec wins.
- No emojis, no exclamation marks in code, comments, docs, or commit messages.
- Pure logic extracted from `pose.js` MUST be mirrored **byte-equivalent** in a node test file (existing convention: `__poseCalfRaiseHelpers` / `frontend/tests/pose_guided.test.js`).
- Never commit video files. Clips live under `playwright/.assets/fixtures/` (already gitignored via `playwright/.assets/`). The committed manifest lives at `tests/e2e/pose-fixtures.manifest.json`.
- The fixture runner mutates session state (completing sets writes rows). It must REFUSE any non-local, non-sqlite target — fail, never skip.
- Chromium **loops** Y4M clips. Every count assertion is a first-pass-boundary assertion (`duration_s`), never a "final" count.
- Existing tests are behavior gates: `frontend/tests/pose_guided.test.js` must pass **unmodified** after the LocomotionGuard migration (except where a task explicitly says it adds to the file).
- After every frontend change: `for f in frontend/tests/*.test.js; do node "$f"; done` must be green, and `node --check frontend/pose.js` must pass.
- Human-dependent tasks (1, 2, 10) are ordered to use Andre's availability tonight; code tasks 3–9 need no human.

---

### Task 1: Live acceptance of the rise fix (human, ~5 min)

**Files:** none (verification only)

**Interfaces:**
- Consumes: prod deploy of `289d240` (already live).
- Produces: a verified-true baseline for everything downstream; the go/no-go for building fixtures on top of the rise family.

- [ ] **Step 1: Set up the watch**

Andre: hard-refresh https://rehab-as-code-five.vercel.app (Cmd+Shift+R), Browse exercises, Double-Leg Calf Raises, Start exercise. Claude: attach to the tab via claude-in-chrome and read `#posePreflightStatus` / `#poseRepLabel` live (same method as 2026-08-10).

- [ ] **Step 2: Framing check**

Andre steps back until the gate reads ready. Expected: `Camera sees 8/8 ... ready. Tap Start.` at roughly 6 feet, no Center Stage fighting (toggle persisted from last night).

- [ ] **Step 3: True positives**

Andre taps Start, stands still ~1s for baseline, performs 8 deliberate calf raises. Expected: `Rep 8/15` after the 8th, no misses. Claude confirms from the live counter.

- [ ] **Step 4: True negatives**

Andre walks to the computer, stands there ~3s, walks back, re-plants, does 2 more raises. Expected: counter unchanged during the walk, status shows `hold still — reps count once you're planted` while moving, and the 2 post-walk raises count (10 total).

- [ ] **Step 5: Record the outcome**

Claude updates memory `project-guided-formcheck-live-validation.md`: rise family human-re-validated PASS/FAIL with the observed numbers. A FAIL stops the plan here — fix before recording fixtures.

### Task 2: Recording session 1 (human, ~25 min)

**Files:**
- Create (outside repo): `~/Desktop/rehab-fixtures/<exercise_id>/{good,bad,walk}.mov`

**Interfaces:**
- Produces: raw ground-truth clips + Andre's written actual-performed counts per clip, consumed by Tasks 3–4. The counts Andre reports ARE the manifest values — write them down per clip immediately after recording it.

Recording rules (from spec): QuickTime Player, File, New Movie Recording, camera "MacBook Pro Camera", chest height, ~6 feet, full body head-to-feet in frame. NOT screen capture. Every clip starts and ends with ~2 seconds standing still in the same stance (loop-seam rule).

- [ ] **Step 1: Create the folders**

```bash
mkdir -p ~/Desktop/rehab-fixtures/{ankle_calf_raises_double_leg,ankle_calf_raises_single_leg,ankle_single_leg_balance,ankle_alphabet}
```

- [ ] **Step 2: Double-leg calf raises (3 clips)**

- `good.mov`: still 2s, 8 clean raises (pause at top, slow down), still 2s.
- `bad.mov`: still 2s, 4 raises each with an exaggerated forward trunk lean, still 2s.
- `walk.mov`: still 2s, walk toward camera, stand 3s, walk back, still 2s, 2 clean raises, still 2s.
- Andre notes actual counts performed (if he does 7 instead of 8, the manifest says 7).

- [ ] **Step 3: Single-leg calf raise (3 clips)**

Same structure: `good.mov` 6 clean single-leg raises (either leg, consistent); `bad.mov` 4 raises grabbing the wall and leaning into it (the sway/trunk compensation the checks claim to catch); `walk.mov` same walk pattern plus 2 clean raises.

- [ ] **Step 4: Single-leg balance (3 clips, hold mode)**

- `good.mov`: still 2s, lift one foot and hold ~20s steady, foot down, still 2s.
- `bad.mov`: still 2s, hold ~15s with deliberate wobble and 2 touchdowns, still 2s.
- `walk.mov`: still 2s, walk pattern, return, still 2s, hold ~10s steady, still 2s.
- Andre notes the approximate steady-hold seconds per clip.

- [ ] **Step 5: Ankle alphabet (1 clip, presence mode)**

`good.mov` only: seated in a chair, camera pointed at feet and ankles per the preflight's feet_seated guidance, still 2s, trace A-J with the foot (~30s), still 2s.

- [ ] **Step 6: Verify the takes**

Scrub each clip in QuickTime: full body visible throughout (feet included), no Center Stage drift, stillness bookends present. Re-record any take that fails. Andre sends Claude the per-clip performed counts.

### Task 3: Clip conversion script

**Files:**
- Create: `scripts/qa-fixture-convert.mjs`
- Create: `tests/e2e/pose-fixtures.manifest.json` (skeleton)

**Interfaces:**
- Produces: `ensureFixture(movPath, y4mPath)` behavior via CLI: `node scripts/qa-fixture-convert.mjs <in.mov> <out.y4m>` converts, validates, prints `duration_s`. Task 4 uses the CLI; Task 8 reads the manifest schema defined here.

Manifest schema (the contract; per-mode assertion types reflect what the product actually claims — `RepTracker` only counts `mode: "max"`; rise counts via `CalfRaiseRepTracker`; hold and presence do not count reps):

```json
{
  "ankle_calf_raises_double_leg": {
    "mode": "rise",
    "clips": {
      "good": { "file": "good.y4m", "duration_s": 0, "expected_reps": 8, "expected_bad_max": 0 },
      "bad":  { "file": "bad.y4m",  "duration_s": 0, "expected_reps": 4, "expected_bad_min": 3 },
      "walk": { "file": "walk.y4m", "duration_s": 0, "expected_reps": 2 }
    }
  },
  "ankle_single_leg_balance": {
    "mode": "hold",
    "clips": {
      "good": { "file": "good.y4m", "duration_s": 0, "expected_hold_s": { "min": 15, "max": 25 } }
    }
  },
  "ankle_alphabet": {
    "mode": "presence",
    "clips": {
      "good": { "file": "good.y4m", "duration_s": 0, "expected_presence": true }
    }
  }
}
```

(`duration_s: 0` is a skeleton placeholder the convert script overwrites; the runner in Task 8 rejects any entry still at 0.)

- [ ] **Step 1: Write the script**

```js
// scripts/qa-fixture-convert.mjs
//
// Convert a recorded .mov into the 640x480 Y4M Chromium serves as a fake
// webcam, validate the result, and print its duration for the manifest.
//
//   node scripts/qa-fixture-convert.mjs <input.mov> <output.y4m>
//
// ffmpeg is required (present on this machine at /opt/homebrew/bin/ffmpeg).
// Scale-and-pad keeps aspect (letterbox) so the body is never distorted;
// yuv420p is the only pixel format Chromium accepts for Y4M.

import { execFileSync } from "node:child_process";
import { existsSync, statSync } from "node:fs";

const [inPath, outPath] = process.argv.slice(2);
if (!inPath || !outPath) {
  console.error("usage: node scripts/qa-fixture-convert.mjs <in.mov> <out.y4m>");
  process.exit(2);
}
if (!existsSync(inPath)) {
  console.error(`input not found: ${inPath}`);
  process.exit(2);
}

execFileSync("ffmpeg", [
  "-y", "-i", inPath,
  "-vf", "scale=640:480:force_original_aspect_ratio=decrease,pad=640:480:(ow-iw)/2:(oh-ih)/2",
  "-pix_fmt", "yuv420p",
  "-an",
  outPath,
], { stdio: ["ignore", "inherit", "inherit"] });

// Validate: non-empty, and ffprobe agrees on geometry + duration.
if (!existsSync(outPath) || statSync(outPath).size < 100_000) {
  console.error(`conversion produced an empty or tiny file: ${outPath}`);
  process.exit(1);
}
const probe = JSON.parse(execFileSync("ffprobe", [
  "-v", "error", "-select_streams", "v:0",
  "-show_entries", "stream=width,height,duration",
  "-of", "json", outPath,
]).toString());
const s = probe.streams?.[0] ?? {};
if (s.width !== 640 || s.height !== 480) {
  console.error(`wrong geometry ${s.width}x${s.height}, expected 640x480`);
  process.exit(1);
}
const duration = Math.round(Number(s.duration) * 10) / 10;
if (!(duration > 3)) {
  console.error(`suspicious duration ${duration}s - clip too short to be a fixture`);
  process.exit(1);
}
console.log(JSON.stringify({ out: outPath, duration_s: duration }));
```

- [ ] **Step 2: Verify it fails loudly on garbage**

Run: `node scripts/qa-fixture-convert.mjs /tmp/does-not-exist.mov /tmp/out.y4m`
Expected: exit 2, `input not found`.

- [ ] **Step 3: Verify a real conversion**

Run against any one of Andre's Task-2 clips:
`node scripts/qa-fixture-convert.mjs ~/Desktop/rehab-fixtures/ankle_calf_raises_double_leg/good.mov playwright/.assets/fixtures/ankle_calf_raises_double_leg/good.y4m`
(after `mkdir -p playwright/.assets/fixtures/ankle_calf_raises_double_leg`)
Expected: JSON line with a plausible `duration_s`.

- [ ] **Step 4: Write the manifest skeleton**

Create `tests/e2e/pose-fixtures.manifest.json` with the schema above, all four session-1 exercises, `duration_s: 0` everywhere, expected values from Andre's Task-2 notes.

- [ ] **Step 5: Commit**

```bash
git add scripts/qa-fixture-convert.mjs tests/e2e/pose-fixtures.manifest.json
git commit -m "test(pose): fixture conversion script and manifest schema"
```

### Task 4: Convert session-1 clips, finalize the manifest

**Files:**
- Modify: `tests/e2e/pose-fixtures.manifest.json` (fill `duration_s` from convert output)
- Create (gitignored): `playwright/.assets/fixtures/<exercise_id>/*.y4m`

**Interfaces:**
- Consumes: Task 3 CLI. Produces: the exact manifest Task 8 runs against.

- [ ] **Step 1: Convert everything**

```bash
for ex in ankle_calf_raises_double_leg ankle_calf_raises_single_leg ankle_single_leg_balance ankle_alphabet; do
  mkdir -p "playwright/.assets/fixtures/$ex"
  for clip in ~/Desktop/rehab-fixtures/$ex/*.mov; do
    base=$(basename "$clip" .mov)
    node scripts/qa-fixture-convert.mjs "$clip" "playwright/.assets/fixtures/$ex/$base.y4m"
  done
done
```

- [ ] **Step 2: Fill in duration_s**

Copy each printed `duration_s` into the matching manifest entry. No entry may remain at 0.

- [ ] **Step 3: Sanity-check one clip visually**

Run: `QA_POSE_CLIP=playwright/.assets/fixtures/ankle_calf_raises_double_leg/good.y4m npm run qa:pose -- --headed --grep "gates on framing"`
Expected: the preflight now SEES Andre (framing count above 0/8) since the clip contains a real body. This spec asserts 0/8 on the grey clip, so with a body it will FAIL its gate assertion — that failure is the expected sanity signal here, not a defect. Do not commit anything from this step.

- [ ] **Step 4: Commit the manifest**

```bash
git add tests/e2e/pose-fixtures.manifest.json
git commit -m "test(pose): session-1 fixture manifest with measured durations"
```

### Task 5: LocomotionGuard extraction

**Files:**
- Modify: `frontend/pose.js` (the `calfRiseSampleStep` region, the frame loop where checks run, the payload publisher)
- Create: `frontend/tests/pose_locomotion_guard.test.js`

**Interfaces:**
- Produces (in pose.js IIFE, exported on `window.__poseLocomotionHelpers`):
  - `locomotionStep(st, sample)` where `st = {baselineY, baselineX, baselineSpan, samples}` and `sample = {hipY, hipX, span}`, returns `{phase: "baselining"|"moving"|"tracking", riseFrac: number|null}` — the generalized core of `calfRiseSampleStep`, with `riseFrac` raw (span-normalized, unscaled) so different consumers apply their own scaling.
  - `bodySample(lms)` returns `{hipY, hipX, span}` or `null` — the landmark extraction currently inlined in `checkCalfRaiseRise` (hips required, shoulder-to-ankle span, torso-scaled fallback).
  - Constants: `GUARD_BASELINE_FRAMES = 30`, `GUARD_MOVE_X_FRAC = 0.12`, `GUARD_MOVE_SCALE_FRAC = 0.10` (renamed from `CALF_*`; `CALF_RISE_STRONG_FRAC = 0.05` stays rise-specific).
  - The per-frame payload gains `moving: boolean` (true when the guard is in "moving" or "baselining" phase), consumed by Task 7's hold-timer pause in app.js.
- Behavior gate: `node frontend/tests/pose_guided.test.js` passes UNMODIFIED. The rise family's observable behavior does not change.

- [ ] **Step 1: Write the failing mirror test**

Create `frontend/tests/pose_locomotion_guard.test.js` mirroring `locomotionStep` byte-equivalent (same header comment convention as pose_camera_preference.test.js), with assertions:

```js
const assert = require("node:assert/strict");

// [byte-equivalent mirror of locomotionStep + constants goes here]

const still = (n, hipY, span, hipX = 0.5) =>
  Array.from({ length: n }, () => ({ hipY, hipX, span }));

// Baseline established after GUARD_BASELINE_FRAMES stationary frames.
{
  const st = { baselineY: null, baselineX: null, baselineSpan: null, samples: [] };
  const frames = still(30, 0.45, 0.5);
  let last;
  for (const f of frames) last = locomotionStep(st, f);
  assert.equal(last.phase, "tracking");
  assert.equal(last.riseFrac, 0);
}

// Lateral drift beyond 12 percent of span flips to moving and drops baseline.
{
  const st = { baselineY: null, baselineX: null, baselineSpan: null, samples: [] };
  for (const f of still(30, 0.45, 0.5)) locomotionStep(st, f);
  const r = locomotionStep(st, { hipY: 0.45, hipX: 0.57, span: 0.5 });
  assert.equal(r.phase, "moving");
  assert.equal(st.baselineY, null, "baseline dropped on locomotion");
}

// Scale change beyond 10 percent flips to moving.
{
  const st = { baselineY: null, baselineX: null, baselineSpan: null, samples: [] };
  for (const f of still(30, 0.45, 0.5)) locomotionStep(st, f);
  assert.equal(locomotionStep(st, { hipY: 0.45, hipX: 0.5, span: 0.56 }).phase, "moving");
}

// riseFrac is span-normalized and unscaled: 0.022 lift on 0.5 span = 0.044.
{
  const st = { baselineY: null, baselineX: null, baselineSpan: null, samples: [] };
  for (const f of still(30, 0.45, 0.5)) locomotionStep(st, f);
  const r = locomotionStep(st, { hipY: 0.428, hipX: 0.5, span: 0.5 });
  assert.equal(r.phase, "tracking");
  assert.ok(Math.abs(r.riseFrac - 0.044) < 0.001);
}

console.log("OK: locomotion guard - all assertions passed");
```

- [ ] **Step 2: Run it, verify it fails**

Run: `node frontend/tests/pose_locomotion_guard.test.js`
Expected: FAIL - `locomotionStep is not defined` (the mirror body is what you write in step 3; the test file carries the mirror, so at this step write the assertions importing nothing and the run fails on the missing mirror function).

- [ ] **Step 3: Implement in pose.js, then mirror byte-equivalent**

In `pose.js`, refactor `calfRiseSampleStep` into:

```js
function locomotionStep(st, sample) {
  if (!sample || !(sample.span > 0)) return { phase: "baselining", riseFrac: null };
  if (st.baselineY == null) {
    st.samples.push(sample);
    if (st.samples.length >= GUARD_BASELINE_FRAMES) {
      const med = (key) => {
        const a = st.samples.map((s) => s[key]).sort((x, y) => x - y);
        return a[Math.floor(a.length / 2)];
      };
      st.baselineY = med("hipY");
      st.baselineX = med("hipX");
      st.baselineSpan = med("span");
    }
    return { phase: "baselining", riseFrac: null };
  }
  const lateral = Math.abs(sample.hipX - st.baselineX) / st.baselineSpan;
  const scale = Math.abs(sample.span / st.baselineSpan - 1);
  if (lateral > GUARD_MOVE_X_FRAC || scale > GUARD_MOVE_SCALE_FRAC) {
    st.baselineY = null;
    st.baselineX = null;
    st.baselineSpan = null;
    st.samples = [];
    return { phase: "moving", riseFrac: null };
  }
  return { phase: "tracking", riseFrac: (st.baselineY - sample.hipY) / st.baselineSpan };
}
```

`calfRiseSampleStep` becomes a thin wrapper: calls `locomotionStep`, maps `riseFrac` to the existing pct scale (`riseFrac / CALF_RISE_STRONG_FRAC * 100`, clamped 0..100), preserving its exact `{phase, pct}` return shape. Extract `bodySample(lms)` from `checkCalfRaiseRise`'s landmark code; `checkCalfRaiseRise` calls it. In the frame loop, compute `bodySample` + guard phase ONCE per frame and include `moving` in the published payload. Copy `locomotionStep` + constants byte-equivalent into the new test file.

- [ ] **Step 4: Run all gates**

Run: `node --check frontend/pose.js && node frontend/tests/pose_locomotion_guard.test.js && node frontend/tests/pose_guided.test.js`
Expected: all pass, pose_guided.test.js UNMODIFIED.

- [ ] **Step 5: Commit**

```bash
git add frontend/pose.js frontend/tests/pose_locomotion_guard.test.js
git commit -m "refactor(pose): extract LocomotionGuard from the rise tracker"
```

### Task 6: Angle-family suppression and abandon-on-idle

**Files:**
- Modify: `frontend/pose.js` (`RepTracker.observe`, the frame loop where angle trackers are fed)
- Modify: `frontend/tests/pose_locomotion_guard.test.js` (add the angle scenarios)

**Interfaces:**
- Consumes: Task 5's per-frame guard phase.
- Produces: `RepTracker.observe(null, ...)` resets in-flight rep state; the loop passes `null` as the angle to every angle tracker while the guard is not "tracking". Export a pure `angleRepStep`-style reset check is NOT needed — `RepTracker` is already exercised through pose_guided.test.js patterns; add a minimal mirror of the observe-idle contract instead (below).

- [ ] **Step 1: Write the failing test**

Append to `pose_locomotion_guard.test.js` a minimal mirror of the RepTracker idle contract (mirror only the state-machine skeleton needed, byte-equivalent to the `observe` idle branch you will write):

```js
// RepTracker idle contract: a null angle abandons any in-flight rep.
// Walking flexes the knee ~60 deg; without suppression that reads as a
// shallow squat cycle. With it, the guard's null feed keeps count at 0.
{
  const t = { state: "descending", curMin: 95, repCount: 0, curWorstStatus: "good", curWorstMsg: null };
  repTrackerIdleReset(t);
  assert.equal(t.state, "idle");
  assert.equal(t.curMin, null);
  assert.equal(t.repCount, 0, "abandon, never bank");
}
```

- [ ] **Step 2: Run, verify FAIL** (`repTrackerIdleReset is not defined`)

- [ ] **Step 3: Implement**

In pose.js add the helper and call it from `observe`:

```js
// Abandon any in-flight rep. Idle frames (guard moving, baseline pending,
// or the joint not visible) must never bank progress: the walking motion
// pattern enters a rep phase before locomotion thresholds trip, and a
// banked phase "completes" as a phantom rep on the first quiet frame.
function repTrackerIdleReset(t) {
  t.state = "idle";
  t.curMin = null;
  t.curWorstStatus = "good";
  t.curWorstMsg = null;
}
```

In `RepTracker.observe`, replace the bare `if (angle == null) return null;` with `if (angle == null) { repTrackerIdleReset(this); return null; }`. In the frame loop, where angle trackers are fed their metric value, pass `null` instead of the angle whenever the guard phase is not "tracking". Mirror `repTrackerIdleReset` byte-equivalent into the test file. Export it on `window.__poseLocomotionHelpers`.

- [ ] **Step 4: Run all gates** (same command as Task 5 step 4; plus `node frontend/tests/pose_guided.test.js`)

- [ ] **Step 5: Commit**

```bash
git add frontend/pose.js frontend/tests/pose_locomotion_guard.test.js
git commit -m "fix(pose): suppress angle reps during locomotion, abandon in-flight on idle"
```

### Task 7: Hold and displacement checks join the guard

**Files:**
- Modify: `frontend/pose.js` (`checkSway`, `checkHipDrop`)
- Modify: `frontend/app.js` (the guided wrapper's hold/presence timer)
- Modify: `frontend/tests/pose_locomotion_guard.test.js`

**Interfaces:**
- Consumes: guard baseline (`baselineX`, `baselineSpan`) and the payload `moving` flag from Task 5.
- Produces: `swayFrom(hipX, baselineX, baselineSpan)` pure helper (exported + mirrored); hold timers freeze while `payload.moving`.

- [ ] **Step 1: Write the failing sway test**

```js
// Sway is deviation from the GUARD BASELINE as a fraction of body span -
// not deviation from frame center. A patient standing off-center is not
// swaying, and the same wobble must read the same at any distance.
{
  assert.equal(swayFrom(0.60, 0.60, 0.5).status, "good", "standing off-center is not sway");
  const nearWobble = swayFrom(0.545, 0.5, 0.9);   // 0.045/0.9 = 5 percent of span
  const farWobble  = swayFrom(0.525, 0.5, 0.5);   // 0.025/0.5 = 5 percent of span
  assert.equal(nearWobble.status, farWobble.status, "same body wobble, same verdict at any distance");
  assert.equal(swayFrom(0.60, 0.5, 0.5).status, "bad");   // 20 percent of span
  assert.equal(swayFrom(0.535, 0.5, 0.5).status, "warn"); // 7 percent of span
}
```

- [ ] **Step 2: Run, verify FAIL**

- [ ] **Step 3: Implement**

```js
// Pure sway core. Thresholds are fractions of the patient's own span:
// beyond 5 percent is a warn, beyond 10 percent is bad. The old check
// measured distance from FRAME CENTER (0.5) in frame widths, which flagged
// any off-center stance as permanent sway and scaled with distance.
const SWAY_WARN_FRAC = 0.05;
const SWAY_BAD_FRAC  = 0.10;
function swayFrom(hipX, baselineX, baselineSpan) {
  const frac = Math.abs(hipX - baselineX) / baselineSpan;
  let status = "good";
  if (frac > SWAY_BAD_FRAC) status = "bad";
  else if (frac > SWAY_WARN_FRAC) status = "warn";
  return { frac: +(frac * 100).toFixed(1), status };
}
```

`checkSway` uses the guard's live baseline (`baselineX`, `baselineSpan`); when the guard has no baseline (moving or pending) it returns status "idle". Same treatment for `checkHipDrop`: its left-right hip y difference divides by `baselineSpan` instead of raw frame units, thresholds re-derived to match current behavior at the calibration distance the old constants assumed (document the derivation in a comment: old frame-fraction threshold X at span 0.75 becomes X/0.75 of span). In `app.js`, in the guided wrapper's hold/presence interval callback, skip the tick when the latest payload has `moving: true` (find the interval via `guided.presenceTimer`; store the latest payload's moving flag on `guided.lastMoving` where payloads are handled).

- [ ] **Step 4: Run all gates** (node --check on both files, all frontend tests)

- [ ] **Step 5: Commit**

```bash
git add frontend/pose.js frontend/app.js frontend/tests/pose_locomotion_guard.test.js
git commit -m "fix(pose): span-normalized sway and hip drop, hold timer pauses on locomotion"
```

### Task 8: Fixture runner

**Files:**
- Create: `tests/e2e/pose-fixtures.spec.ts`
- Modify: `package.json` (add `qa:fixtures` script)

**Interfaces:**
- Consumes: `tests/e2e/pose-fixtures.manifest.json` (Task 4), converted clips, the fake-camera launch flags proved in `playwright.config.ts`'s pose-chromium project, fixtures helpers (`usePatientView`, `suppressTour`, `hasSavedSession`, `STORAGE_STATE`).
- Produces: `npm run qa:fixtures` — the on-demand truth lane.

- [ ] **Step 1: Add the npm script**

```json
"qa:fixtures": "playwright test tests/e2e/pose-fixtures.spec.ts --project=pose-chromium --workers=1"
```

Callers run it as (documented in the spec file header):
`QA_TARGET=local QA_PORT=8030 STORAGE_BACKEND=sqlite DATABASE_URL=postgresql://127.0.0.1:5432/rac_scratch QA_ALLOW_MUTATION=1 npm run qa:fixtures`

- [ ] **Step 2: Write the runner**

```ts
// tests/e2e/pose-fixtures.spec.ts
//
// Replay Andre's recorded exercise clips through the real MediaPipe pipeline
// and assert the user-facing counters. Ground truth: the manifest numbers
// are what was actually performed on camera - never invented.
//
// MUTATING lane: completing a set writes session state, so this refuses any
// target that is not a local backend on sqlite. Chromium LOOPS Y4M clips, so
// every assertion is bounded by the clip's first pass (duration_s); a count
// that exceeds its expectation at any point is an immediate failure.
import { chromium, expect, test } from "@playwright/test";
import { readFileSync, existsSync } from "node:fs";
import { resolve } from "node:path";
import { STORAGE_STATE, hasSavedSession } from "./fixtures";

const MANIFEST = JSON.parse(
  readFileSync(resolve(__dirname, "pose-fixtures.manifest.json"), "utf8"),
);
const FIXTURE_ROOT = resolve(__dirname, "../../playwright/.assets/fixtures");
const BASE = process.env.QA_BASE_URL ?? `http://127.0.0.1:${process.env.QA_PORT ?? "8000"}`;

test.describe("pose fixtures @pose @mutating", () => {
  test.beforeAll(() => {
    if (!/127\.0\.0\.1|localhost/.test(BASE)) {
      throw new Error(`fixture runner writes session state; target ${BASE} is not local`);
    }
    if (process.env.STORAGE_BACKEND !== "sqlite") {
      throw new Error("fixture runner requires STORAGE_BACKEND=sqlite");
    }
  });

  for (const [exerciseId, entry] of Object.entries(MANIFEST) as [string, any][]) {
    for (const [clipName, clip] of Object.entries(entry.clips) as [string, any][]) {
      test(`${exerciseId} / ${clipName}`, async () => {
        test.skip(!hasSavedSession(), "No authenticated session.");
        const clipPath = resolve(FIXTURE_ROOT, exerciseId, clip.file);
        if (!existsSync(clipPath)) {
          throw new Error(`manifest names a missing clip: ${clipPath} - record or convert it`);
        }
        if (!(clip.duration_s > 0)) {
          throw new Error(`${exerciseId}/${clipName}: duration_s not stamped - rerun conversion`);
        }

        const browser = await chromium.launch({
          args: [
            "--use-fake-ui-for-media-stream",
            "--use-fake-device-for-media-capture",
            `--use-file-for-fake-video-capture=${clipPath}`,
          ],
        });
        try {
          const ctx = await browser.newContext({ storageState: STORAGE_STATE, baseURL: BASE });
          const page = await ctx.newPage();
          await page.addInitScript(() => {
            sessionStorage.setItem("asPatient", "1");
            try {
              localStorage.setItem("rehab_tour_clinician_v1", "1");
              localStorage.setItem("rehab_tour_patient_v1", "1");
            } catch {}
          });
          await page.goto("/");
          await page.locator("#exerciseBtn").click();
          const label = new RegExp(exerciseId.replace(/^ankle_/, "").replace(/_/g, ".?"), "i");
          await page.locator(".gallery-thumb-btn", { hasText: label }).first().click();
          await page.locator(".pose-form-check-btn").click();
          await expect(page.locator("#posePreflight")).toBeVisible({ timeout: 30_000 });

          // The clip contains a real body, so the gate should open; Start
          // anyway is the fallback for marginal takes.
          const go = page.locator("#posePreflightGoBtn");
          const anyway = page.locator("#posePreflightAnywayBtn");
          await Promise.race([
            go.locator(":scope.ready").waitFor({ timeout: 90_000 }).catch(() => {}),
            anyway.waitFor({ state: "visible", timeout: 90_000 }).catch(() => {}),
          ]);
          if (await go.evaluate((el) => el.classList.contains("ready"))) await go.click();
          else await anyway.click();

          const readCount = async () => {
            const t = (await page.locator("#poseRepLabel").textContent()) ?? "";
            const m = t.match(/Rep (\d+)/i);
            return m ? Number(m[1]) : 0;
          };

          const budgetMs = clip.duration_s * 1000 + 10_000;
          if (entry.mode === "rise" || entry.mode === "max") {
            const deadline = Date.now() + budgetMs;
            let count = 0;
            while (Date.now() < deadline) {
              count = await readCount();
              expect(count, "count OVERSHOT ground truth - fake reps").toBeLessThanOrEqual(clip.expected_reps);
              if (count === clip.expected_reps) break;
              await page.waitForTimeout(500);
            }
            expect(count, "count never reached ground truth - missed reps").toBe(clip.expected_reps);
            // Hold at the boundary: no drift for a further 3 seconds.
            await page.waitForTimeout(3_000);
            expect(await readCount()).toBe(clip.expected_reps);
          } else if (entry.mode === "hold") {
            await page.waitForTimeout(budgetMs);
            const holdText = (await page.locator("#poseRepLabel").textContent()) ?? "";
            const secs = Number((holdText.match(/(\d+)s/) ?? [])[1] ?? 0);
            expect(secs).toBeGreaterThanOrEqual(clip.expected_hold_s.min);
            expect(secs).toBeLessThanOrEqual(clip.expected_hold_s.max);
          } else if (entry.mode === "presence") {
            await page.waitForTimeout(Math.min(budgetMs, 20_000));
            await expect(page.locator("#poseStage, #posePreflight")).toBeVisible();
          }
        } finally {
          await browser.close();
        }
      });
    }
  }
});
```

- [ ] **Step 3: Run it, expect informative first contact**

Run: `QA_TARGET=local QA_PORT=8030 STORAGE_BACKEND=sqlite DATABASE_URL=postgresql://127.0.0.1:5432/rac_scratch QA_ALLOW_MUTATION=1 npm run qa:fixtures`
Expected: each clip launches, plays, and either passes or fails with a specific counter mismatch. First contact WILL surface calibration surprises (bad-clip flag counts, hold-second extraction from the label) - fix the runner's selectors/extraction against reality, and treat any counting mismatch as a product bug to diagnose, not a number to fudge in the manifest.

Note: the `bad` clips assert `expected_bad_min` flagged reps; read them from the rep log rows (`.pose-rep-row` glyphs) with `page.locator(".pose-rep-row:has-text('x'), .pose-rep-row.warn, .pose-rep-row.bad").count()` - reconcile the selector with the DOM when running, the rows render with status classes per renderPoseSession.

- [ ] **Step 4: Iterate to green on all session-1 fixtures**

- [ ] **Step 5: Commit**

```bash
git add tests/e2e/pose-fixtures.spec.ts package.json
git commit -m "test(pose): manifest-driven fixture runner over recorded ground truth"
```

### Task 9: Threshold-consistency report

**Files:**
- Create: `scripts/pose-threshold-report.mjs`
- Create: `docs/pose-threshold-report.md` (committed output snapshot)

**Interfaces:**
- Consumes: `frontend/pose.js` EXERCISES targets; `protocols/protocol-library/**/*.yaml` `- name:` + `ROM_target_deg:` pairs.
- Produces: the conflict table; the opening brief for the wire-prescribed-ROM follow-up.

- [ ] **Step 1: Write the script**

```js
// scripts/pose-threshold-report.mjs
//
// Diff pose.js hardcoded angle targets against every prescribed
// ROM_target_deg in the protocol library. Informational only - changes no
// behavior. YAML is line-scanned (entries are "- name: x" blocks with an
// optional ROM_target_deg line) to avoid a YAML dependency.

import { readFileSync, readdirSync, statSync } from "node:fs";
import { join } from "node:path";

const pose = readFileSync("frontend/pose.js", "utf8");
const block = pose.slice(pose.indexOf("const EXERCISES"), pose.indexOf("};", pose.indexOf("const EXERCISES")));
const targets = {};
for (const m of block.matchAll(/(\w+):\s*\{\s*primary:\s*"[^"]+",\s*target:\s*(\d+|null),\s*mode:\s*"(\w+)"/g)) {
  targets[m[1]] = { target: m[2] === "null" ? null : Number(m[2]), mode: m[3] };
}

const protocolROMs = {};
const walk = (dir) => {
  for (const f of readdirSync(dir)) {
    const p = join(dir, f);
    if (statSync(p).isDirectory()) { walk(p); continue; }
    if (!f.endsWith(".yaml")) continue;
    const lines = readFileSync(p, "utf8").split("\n");
    let current = null;
    for (const line of lines) {
      const name = line.match(/^\s*-\s*name:\s*(\S+)/);
      if (name) current = name[1];
      const rom = line.match(/^\s*ROM_target_deg:\s*(\d+)/);
      if (rom && current) (protocolROMs[current] ??= []).push({ file: p, deg: Number(rom[1]) });
    }
  }
};
walk("protocols/protocol-library");

const rows = [];
for (const [ex, { target, mode }] of Object.entries(targets)) {
  const roms = protocolROMs[ex];
  if (!roms) { rows.push([ex, mode, target, "-", "no-protocol-entry"]); continue; }
  const degs = roms.map((r) => r.deg);
  const conflict = target != null && !degs.includes(target);
  rows.push([ex, mode, target, degs.join("/"), conflict ? "CONFLICT" : "consistent"]);
}
const pad = (s, n) => String(s ?? "-").padEnd(n);
console.log(pad("exercise", 32) + pad("mode", 8) + pad("pose", 6) + pad("protocol ROMs", 16) + "verdict");
for (const r of rows) console.log(pad(r[0], 32) + pad(r[1], 8) + pad(r[2], 6) + pad(r[3], 16) + r[4]);
```

- [ ] **Step 2: Run it**

Run: `node scripts/pose-threshold-report.mjs`
Expected: a table; mini_squat alone is a known likely CONFLICT (pose 135 vs week-4 prescribed 60).

- [ ] **Step 3: Snapshot into docs**

Save the output with a dated header + one-line interpretation per CONFLICT row into `docs/pose-threshold-report.md`.

- [ ] **Step 4: Commit**

```bash
git add scripts/pose-threshold-report.mjs docs/pose-threshold-report.md
git commit -m "docs(pose): threshold-consistency report, pose targets vs prescribed ROM"
```

### Task 10: Deploy and live acceptance (human, ~10 min)

**Files:** none (verification; push already deployed tasks 5-9 via merge to main)

**Interfaces:**
- Consumes: everything above, deployed to prod.
- Produces: the spec's done-criterion for Andre's protocol: fixtures green + one live acceptance pass per exercise.

- [ ] **Step 1: Push and verify pipeline**

`git push origin main`; watch `gh run watch` green; confirm `curl -s .../static/pose.js | grep -c locomotionStep` is nonzero.

- [ ] **Step 2: Live acceptance, single-leg balance**

Andre on prod: one ~20s hold. Expected: hold seconds accumulate only while steady, pause during a deliberate walk-away, sway pill does not flag his off-center stance.

- [ ] **Step 3: Live acceptance, ankle alphabet**

Seated, feet framed per preflight guidance; one set. Expected: presence tracks, session completes, no framing fight.

- [ ] **Step 4: Re-run the full local suites**

`for f in frontend/tests/*.test.js; do node "$f"; done` green; `npm run qa` (read-only prod suite) green; fixture lane green from Task 8.

- [ ] **Step 5: Record outcomes**

Update memory (`project-guided-formcheck-live-validation.md`): per-exercise PASS/FAIL, and the library-sweep session 2 (mini_squat, terminal_knee_extension, heel_slides) as the queued next block.

---

## Self-review notes

- Spec coverage: hardening (Tasks 5-7), recording session (Task 2), conversion + manifest (Tasks 3-4), runner (Task 8), threshold report (Task 9), live acceptance (Tasks 1, 10). Session-2 recording is intentionally not a task here - it is queued work recorded in Task 10 step 5, matching the spec's "later" phasing.
- Placeholders: manifest `duration_s: 0` is an explicit skeleton value the runner rejects; Task 8 step 3 names the two spots that must be reconciled against the live DOM (bad-flag selector, hold-seconds extraction) rather than pretending certainty.
- Type consistency: `locomotionStep` returns `{phase, riseFrac}`; `calfRiseSampleStep` keeps `{phase, pct}` for its existing consumers; payload flag is `moving` everywhere (Tasks 5, 7, 8).
