// Node-runnable unit tests for the PR-J guided-mode pure helpers.
//
// Usage:
//   node frontend/tests/pose_guided.test.js
//
// We don't have Jest in this repo — the tests below use Node's built-in
// `assert` module so they run with no install. The helpers under test
// are deliberately DOM-free (no document, no SpeechSynthesis) and live
// in app.js as window.__poseGuidedHelpers.
//
// To get those helpers into a node context without jsdom we eval the
// helper definitions directly. They're isolated enough to pull out as
// strings and re-define, but that drifts on every refactor; instead we
// reproduce only the public surface here and keep this file in lock-step
// with app.js — when the throttle rules change, both move together.
//
// What this covers:
//   * parseSetsReps: dose string parsing
//   * decideCorrectionCue: per-frame throttle decision (gap + dedupe)
//   * rolloverRepThrottle: per-rep dedupe-set rollover
//
// What this does NOT cover (smoke check it manually instead):
//   * speakNow / SpeechSynthesis behavior (no headless browser)
//   * preflight landmark gate (depends on real BlazePose output)
//   * rest-countdown ring math (visual only)

const assert = require("node:assert/strict");

// Mirror the helpers from app.js. Keep these byte-equivalent to the
// reference — when a rule changes in app.js, change it here too.
function parseSetsReps(doseStr) {
  if (!doseStr) return { sets: 1, reps: null };
  const s = String(doseStr);
  const tight = s.match(/(\d+)\s*[x×]\s*(\d+)/i);
  if (tight) return { sets: parseInt(tight[1], 10), reps: parseInt(tight[2], 10) };
  const verbose = s.match(/(\d+)\s*sets?\s*[x×]?\s*(\d+)\s*rep/i);
  if (verbose) return { sets: parseInt(verbose[1], 10), reps: parseInt(verbose[2], 10) };
  const repsOnly = s.match(/(\d+)\s*rep/i);
  if (repsOnly) return { sets: 1, reps: parseInt(repsOnly[1], 10) };
  return { sets: 1, reps: null };
}

function decideCorrectionCue(state, transitions, corrections, nowTs, gapMs) {
  if (!transitions || !transitions.length) return null;
  if (state.lastCueTs != null && nowTs - state.lastCueTs < gapMs) return null;
  for (const t of transitions) {
    const key = t.correctionKey;
    if (!key) continue;
    if (state.spokenKeys.has(key)) continue;
    const cue = corrections?.[key];
    if (!cue) continue;
    state.spokenKeys.add(key);
    state.lastCueTs = nowTs;
    return { key, cue, status: t.to };
  }
  return null;
}

function rolloverRepThrottle(state, prevInRep, nextInRep) {
  if (prevInRep && !nextInRep) state.spokenKeys.clear();
}

// ── Calf-raise rise-rep state machine (mirror of pose.js calfRaiseStep) ─────
// Keep byte-equivalent to pose.js. This is the COUNT GATE for calf raises:
// the count advances only on a real heel-rise down/up cycle, never on a still
// patient. statusRank + thresholds mirrored from pose.js.
function statusRank(s) { return s === "bad" ? 3 : s === "warn" ? 2 : s === "good" ? 1 : 0; }
const RISE_ENTER = 60;
const RISE_EXIT  = 25;
const RISE_GOOD  = 70;
function calfRaiseStep(state, pct, isIdle, frameWorst) {
  if (isIdle || pct == null) {
    // Baseline-pending or locomotion: abandon any in-flight rise rather than
    // banking it. Without this, the hip-rise phase of a WALK (before the
    // locomotion gate trips) survives the reset and completes as a phantom
    // rep on the first low reading after the patient re-plants.
    state.state = "idle";
    state.peak = 0;
    return null;
  }
  if (state.state === "idle") {
    if (pct >= RISE_ENTER) {
      state.state = "rising";
      state.peak  = pct;
      state.worst = "good";
    }
    return null;
  }
  if (pct > state.peak) state.peak = pct;
  if (statusRank(frameWorst) > statusRank(state.worst)) state.worst = frameWorst;
  if (pct <= RISE_EXIT) {
    state.repCount += 1;
    const peak = state.peak;
    let status = state.worst;
    let msg = null;
    if (status === "good" && peak < RISE_GOOD) {
      status = "warn";
      msg = "rise higher onto your toes";
    } else if (status !== "good") {
      msg = "form check";
    } else {
      msg = `rise ${Math.round(peak)}%`;
    }
    const event = {
      repNumber: state.repCount,
      metricId: "calf_rise",
      label: "calf rise",
      depthMin: Math.round(peak),
      target: null,
      status,
      msg,
    };
    state.state = "idle";
    state.peak  = 0;
    return event;
  }
  return null;
}
function newRiseState() {
  return { state: "idle", peak: 0, repCount: 0, worst: "good" };
}

// Mirror of pose.js checkCalfRaiseRise status -> isIdle derivation, the way
// CalfRaiseRepTracker.observe computes it in production: isIdle is true ONLY
// while the hip baseline is still pending. Once the baseline is set, a low
// reading (pct 0-29) is status "good" (standing flat), NOT "idle" — so it
// flows into calfRaiseStep and can complete the descent. This is the exact
// coupling the blocking bug fix corrected; modeling isIdle off pct (the old,
// broken wiring) would make the count gate unreachable.
function statusForPct(pct, baselinePending) {
  if (baselinePending) return "idle";
  if (pct >= 70) return "good";
  if (pct >= 30) return "warn";
  return "good";
}
// Feed a sequence of pct frames; collect emitted rep events.
//   opts.baselineFrames: number of leading frames that are baseline-pending
//     (status "idle" -> isIdle true), mirroring the ~30-sample baseline window.
//   opts.isIdle: explicit override (i, pct) -> bool, for direct gate tests.
//   opts.worst:  per-frame worst alignment status.
function runRise(pcts, opts = {}) {
  const st = newRiseState();
  const events = [];
  const baselineFrames = opts.baselineFrames || 0;
  const isIdle = opts.isIdle ||
    ((i, pct) => statusForPct(pct, i < baselineFrames) === "idle");
  const worst  = opts.worst  || (() => "good");
  pcts.forEach((p, i) => {
    const ev = calfRaiseStep(st, p, isIdle(i, p), worst(i, p));
    if (ev) events.push(ev);
  });
  return { st, events };
}

// ─── parseSetsReps ────────────────────────────────────────────────────────
{
  assert.deepEqual(parseSetsReps("3 x 10"), { sets: 3, reps: 10 });
  assert.deepEqual(parseSetsReps("3x10"),   { sets: 3, reps: 10 });
  assert.deepEqual(parseSetsReps("3×12"),   { sets: 3, reps: 12 });
  assert.deepEqual(parseSetsReps("3 sets x 10 reps"), { sets: 3, reps: 10 });
  assert.deepEqual(parseSetsReps("10 reps"), { sets: 1, reps: 10 });
  assert.deepEqual(parseSetsReps("hold 30s"), { sets: 1, reps: null });
  assert.deepEqual(parseSetsReps(""),        { sets: 1, reps: null });
  assert.deepEqual(parseSetsReps(null),      { sets: 1, reps: null });
}

// ─── decideCorrectionCue: dedupe per rep ──────────────────────────────────
{
  const state = { spokenKeys: new Set(), lastCueTs: null};
  const corrections = {
    knee_valgus: "Knees out",
    trunk_lean:  "Chest up",
  };
  const t1 = [{ id: "L_knee_valgus", correctionKey: "knee_valgus", to: "bad" }];

  // First fire: cue returned, state mutated.
  const a = decideCorrectionCue(state, t1, corrections, 1000, 900);
  assert.equal(a.cue, "Knees out");
  assert.ok(state.spokenKeys.has("knee_valgus"));
  assert.equal(state.lastCueTs, 1000);

  // Same correctionKey within the same rep + past the gap: still skipped
  // (dedupe set wins).
  const b = decideCorrectionCue(state, t1, corrections, 5000, 900);
  assert.equal(b, null, "same key within same rep must not re-speak");
}

// ─── decideCorrectionCue: gap window blocks rapid distinct cues ──────────
{
  const state = { spokenKeys: new Set(), lastCueTs: null};
  const corrections = {
    knee_valgus: "Knees out",
    trunk_lean:  "Chest up",
  };
  const tValgus = [{ id: "L_knee_valgus", correctionKey: "knee_valgus", to: "bad" }];
  const tLean   = [{ id: "trunk_lean",    correctionKey: "trunk_lean",  to: "warn" }];

  decideCorrectionCue(state, tValgus, corrections, 0, 900);
  // 100ms later a different cue would normally fire, but gapMs=900 blocks it.
  const blocked = decideCorrectionCue(state, tLean, corrections, 100, 900);
  assert.equal(blocked, null, "gapMs must block distinct cues fired too close together");

  // After the gap elapses, the distinct cue lands.
  const lands = decideCorrectionCue(state, tLean, corrections, 1500, 900);
  assert.equal(lands.cue, "Chest up");
}

// ─── decideCorrectionCue: no-cue exercises return null ───────────────────
{
  const state = { spokenKeys: new Set(), lastCueTs: null};
  const t = [{ id: "calf_rise", correctionKey: "calf_rise", to: "warn" }];
  // Empty corrections map (e.g., exercise without corrections defined):
  // throttle returns null, no state mutation.
  const out = decideCorrectionCue(state, t, {}, 0, 900);
  assert.equal(out, null);
  assert.equal(state.spokenKeys.size, 0);
  assert.equal(state.lastCueTs, null);
}

// ─── rolloverRepThrottle: clears on rep boundary ─────────────────────────
{
  const state = { spokenKeys: new Set(["knee_valgus", "trunk_lean"]) };
  // No transition: keep dedupe set intact.
  rolloverRepThrottle(state, true, true);
  assert.equal(state.spokenKeys.size, 2);
  rolloverRepThrottle(state, false, false);
  assert.equal(state.spokenKeys.size, 2);
  rolloverRepThrottle(state, false, true);  // rep started; don't clear
  assert.equal(state.spokenKeys.size, 2);

  // True → false (rep finished): dedupe set clears.
  rolloverRepThrottle(state, true, false);
  assert.equal(state.spokenKeys.size, 0, "rep boundary must clear dedupe set");
}

// ─── End-to-end: same cue speaks twice across two reps but not within ────
{
  const state = { spokenKeys: new Set(), lastCueTs: null};
  const corrections = { knee_valgus: "Knees out" };
  const t = [{ id: "L_knee_valgus", correctionKey: "knee_valgus", to: "bad" }];

  // Rep 1: in-rep, fires once.
  const r1a = decideCorrectionCue(state, t, corrections, 0, 900);
  assert.equal(r1a.cue, "Knees out");
  // Rep 1: same cue blocked by dedupe.
  const r1b = decideCorrectionCue(state, t, corrections, 1000, 900);
  assert.equal(r1b, null);

  // Rep boundary: clear.
  rolloverRepThrottle(state, true, false);

  // Rep 2: cue can speak again. Set lastCueTs old enough to clear gap.
  const r2 = decideCorrectionCue(state, t, corrections, 5000, 900);
  assert.equal(r2.cue, "Knees out", "after rep boundary the cue can re-fire");
}

// ─── calfRaiseStep: stand still → ZERO reps (the core gate) ──────────────
{
  // pct hovers near 0 (small sway), never crossing RISE_ENTER.
  const { events } = runRise([0, 1, 0, 2, 0, 3, 1, 0, 2, 0, 1]);
  assert.equal(events.length, 0, "standing still must count zero reps");
}

// ─── calfRaiseStep: idle/baseline frames → ZERO reps ─────────────────────
{
  // Even with high pct, while baseline is not set (isIdle true) nothing counts.
  const { events } = runRise([80, 90, 70, 10, 80], { isIdle: () => true });
  assert.equal(events.length, 0, "frames during baseline (idle) must not count");
}

// ─── calfRaiseStep: one full up→down cycle → exactly one rep ─────────────
{
  // Rise above RISE_ENTER (60), peak, then back below RISE_EXIT (25).
  const { events } = runRise([0, 30, 65, 80, 75, 40, 20, 5]);
  assert.equal(events.length, 1, "one heel-rise cycle is exactly one rep");
  assert.equal(events[0].repNumber, 1);
  assert.equal(events[0].metricId, "calf_rise");
  // Peak 80 >= RISE_GOOD with good alignment → good rep.
  assert.equal(events[0].status, "good");
}

// ─── calfRaiseStep: held tip-toe (up, no return) → ZERO reps ─────────────
{
  // Crosses RISE_ENTER and stays high — never falls below RISE_EXIT.
  const { events, st } = runRise([0, 65, 85, 90, 88, 90, 87, 91]);
  assert.equal(events.length, 0, "held tip-toe without lowering is not a rep");
  assert.equal(st.state, "rising", "tracker stays mid-rep until the patient lowers");
}

// ─── calfRaiseStep: two full cycles → repNumber 1 then 2 ─────────────────
{
  const { events } = runRise([
    0, 65, 80, 30, 10,   // cycle 1
    5, 62, 78, 22, 0,    // cycle 2
  ]);
  assert.equal(events.length, 2, "two cycles → two reps");
  assert.equal(events[0].repNumber, 1);
  assert.equal(events[1].repNumber, 2);
}

// ─── calfRaiseStep: low peak → warn rep (rose but not high enough) ───────
{
  // Crosses RISE_ENTER (60) but peak 64 < RISE_GOOD (70).
  const { events } = runRise([0, 60, 64, 62, 20, 0]);
  assert.equal(events.length, 1);
  assert.equal(events[0].status, "warn", "shallow rise is a warn rep");
}

// ─── calfRaiseStep: bad alignment during rep → bad rep, still counts ─────
{
  // A real rep cycle but trunk_lean was bad mid-rep → status bad, count holds.
  const { events } = runRise([0, 65, 85, 30, 0], { worst: () => "bad" });
  assert.equal(events.length, 1, "a bad-form rep still counts (it happened)");
  assert.equal(events[0].status, "bad");
}

// ─── REGRESSION: production status->isIdle coupling counts a real cycle ───
// This mirrors the exact CalfRaiseRepTracker.observe wiring: isIdle is derived
// from checkCalfRaiseRise's status, which is "idle" only during baseline. A
// full cycle [0,30,65,80,75,40,20,5] fed through that derivation must yield
// exactly one rep. Before the fix, status was "idle" for every pct<30, the
// descent frames (20,5) returned null at the isIdle guard, and the tracker
// stuck in "rising" forever -> ZERO reps. This test would have caught it.
{
  const { events, st } = runRise([0, 30, 65, 80, 75, 40, 20, 5]);
  assert.equal(events.length, 1,
    "real cycle under production status->isIdle wiring must count exactly one rep");
  assert.equal(events[0].repNumber, 1);
  assert.equal(st.state, "idle", "tracker must return to idle after the descent completes");
}

// ─── REGRESSION: baseline-pending frames do not count, real reps after do ──
// Leading frames are baseline-pending (status "idle"); a held tip-toe during
// baseline must not count, and the first real cycle AFTER baseline counts.
{
  const { events } = runRise(
    [80, 90, 70,           // baseline window: high pct but isIdle -> no count
     0, 65, 85, 30, 5],    // first real cycle post-baseline
    { baselineFrames: 3 },
  );
  assert.equal(events.length, 1,
    "high pct during baseline must not count; the post-baseline cycle counts once");
  assert.equal(events[0].repNumber, 1);
}


// ── Calf-rise sampling core (mirror of pose.js calfRiseSampleStep) ──────────
//
// Live-validation regression, 2026-08-10: rise was frame-normalized, so at
// the head-to-feet distance the framing gate REQUIRES a real 6-8cm heel raise
// was ~2% of frame (never counted), while WALKING swung hip-y by far more
// through perspective (every trip to the computer counted as reps). The core
// below normalizes by the patient's own body span and suppresses counting
// during locomotion. Keep byte-equivalent to pose.js.

const CALF_BASELINE_FRAMES  = 30;
const CALF_RISE_STRONG_FRAC = 0.05;
const CALF_MOVE_X_FRAC      = 0.12;
const CALF_MOVE_SCALE_FRAC  = 0.10;

function calfRiseSampleStep(st, sample) {
  if (!sample || !(sample.span > 0)) return { phase: "baselining", pct: null };
  if (st.baselineY == null) {
    st.samples.push(sample);
    if (st.samples.length >= CALF_BASELINE_FRAMES) {
      const med = (key) => {
        const a = st.samples.map((s) => s[key]).sort((x, y) => x - y);
        return a[Math.floor(a.length / 2)];
      };
      st.baselineY = med("hipY");
      st.baselineX = med("hipX");
      st.baselineSpan = med("span");
    }
    return { phase: "baselining", pct: null };
  }
  const lateral = Math.abs(sample.hipX - st.baselineX) / st.baselineSpan;
  const scale = Math.abs(sample.span / st.baselineSpan - 1);
  if (lateral > CALF_MOVE_X_FRAC || scale > CALF_MOVE_SCALE_FRAC) {
    st.baselineY = null;
    st.baselineX = null;
    st.baselineSpan = null;
    st.samples = [];
    return { phase: "moving", pct: null };
  }
  const riseFrac = (st.baselineY - sample.hipY) / st.baselineSpan;
  const pct = Math.max(0, Math.min(100, Math.round((riseFrac / CALF_RISE_STRONG_FRAC) * 100)));
  return { phase: "tracking", pct };
}

// Drive samples through the sampling core chained into the rep machine,
// exactly as checkCalfRaiseRise -> CalfRaiseRepTracker does in pose.js:
// baselining/moving phases surface as isIdle, which calfRaiseStep ignores.
function runPipeline(samples) {
  const sampleSt = { baselineY: null, baselineX: null, baselineSpan: null, samples: [] };
  const repSt = { state: "idle", peak: 0, repCount: 0, worst: "good" };
  const events = [];
  const phases = [];
  for (const s of samples) {
    const step = calfRiseSampleStep(sampleSt, s);
    phases.push(step.phase);
    const ev = calfRaiseStep(repSt, step.pct, step.phase !== "tracking", "good");
    if (ev) events.push(ev);
  }
  return { events, phases, repSt };
}

const still = (n, hipY, span, hipX = 0.5) =>
  Array.from({ length: n }, () => ({ hipY, hipX, span }));

// ─── Walking must count ZERO reps (the bug that fired tonight) ─────────────
{
  // Approach the camera (span grows, hips slide down the frame), pause,
  // then walk back (span shrinks, hips slide up past the old baseline).
  // Under frame-normalized math this hip-y swing dwarfs a real calf raise:
  // (0.55 - 0.38) / 0.05 * 100 = 340 "percent" - a guaranteed fake rep cycle.
  const walk = [
    ...still(30, 0.45, 0.50),                    // settle: baseline at far stance
    ...Array.from({ length: 12 }, (_, i) => ({   // approach: size + hip-y change
      hipY: 0.45 + i * 0.012, hipX: 0.5, span: 0.50 + i * 0.02 })),
    ...still(6, 0.55, 0.74),                     // stand at the computer briefly
    ...Array.from({ length: 12 }, (_, i) => ({   // walk back out
      hipY: 0.55 - i * 0.014, hipX: 0.5, span: 0.74 - i * 0.02 })),
  ];
  const { events } = runPipeline(walk);
  assert.equal(events.length, 0,
    "walking toward/away from the camera must never count as calf-raise reps");

  // Document the prior bug: the OLD frame-normalized math over this same
  // path crosses RISE_ENTER and falls back below RISE_EXIT - a counted rep.
  const oldPct = (base, hipY) =>
    Math.max(0, Math.min(100, Math.round(((base - hipY) / 0.05) * 100)));
  assert.ok(oldPct(0.55, 0.38) >= RISE_ENTER,
    "sanity: old frame-normalized math read the walk as a strong rise");
}

// ─── A real raise at full-body distance MUST count (the miss tonight) ──────
{
  // Far enough back that head-to-feet fits: span 0.5 of frame. A genuine
  // heel raise lifts the hips ~4.4% of the patient's own span = 0.022 of
  // frame - under OLD math that is pct 44, below RISE_ENTER, silently lost.
  const raise = [
    ...still(30, 0.45, 0.50),
    ...still(4, 0.428, 0.50),   // up: 0.022/0.5 = 4.4% of span -> pct 88
    ...still(4, 0.448, 0.50),   // down: pct 4 -> completes the rep
  ];
  const { events } = runPipeline(raise);
  assert.equal(events.length, 1, "a real calf raise at full-body distance must count");
  assert.equal(events[0].status, "good", "peak 88 >= RISE_GOOD is a good rep");

  const oldPct = Math.round(((0.45 - 0.428) / 0.05) * 100);
  assert.ok(oldPct < RISE_ENTER,
    "sanity: old frame-normalized math dropped this genuine rep");
}

// ─── Walk away, re-plant, and the counter resumes at the new stance ────────
{
  const seq = [
    ...still(30, 0.45, 0.50),                    // baseline at spot A
    ...Array.from({ length: 10 }, (_, i) => ({   // walk to spot B (size shrinks)
      hipY: 0.45 - i * 0.008, hipX: 0.5 + i * 0.01, span: 0.50 - i * 0.012 })),
    ...still(30, 0.37, 0.38),                    // settle at B: fresh baseline
    ...still(4, 0.353, 0.38),                    // raise: 0.017/0.38 = 4.5% span
    ...still(4, 0.368, 0.38),                    // lower: completes
  ];
  const { events, phases } = runPipeline(seq);
  assert.equal(events.length, 1, "exactly the one genuine rep at the new stance");
  assert.ok(phases.includes("moving"), "the walk itself must register as locomotion");
}

// ─── Degenerate samples never crash or count ───────────────────────────────
{
  const { events } = runPipeline([
    { hipY: 0.5, hipX: 0.5, span: 0 },
    { hipY: 0.5, hipX: 0.5, span: -1 },
    null,
    ...still(3, 0.5, 0.5),
  ]);
  assert.equal(events.length, 0);
}

console.log("PR-J guided-mode pure helpers: all assertions passed.");
console.log("Calf-raise rise-rep gate: all assertions passed.");
console.log("Calf-rise normalization + locomotion gate: all assertions passed.");
