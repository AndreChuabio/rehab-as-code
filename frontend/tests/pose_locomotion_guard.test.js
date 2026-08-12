// Node-runnable unit test for the shared locomotion guard in pose.js.
//
// Usage:
//   node frontend/tests/pose_locomotion_guard.test.js
//
// Same shape as pose_guided.test.js - no Jest, no jsdom. The pure helpers
// under test (locomotionStep, calfRaiseStep with span-stability,
// repTrackerIdleReset) live in pose.js inside the browser IIFE; the copies
// here must stay byte-equivalent.
//
// Why this exists: live acceptance on 2026-08-12 (plan T1) showed the
// cumulative locomotion thresholds leak exactly 2 phantom reps at WALK ONSET
// - the first steps live under the 10 percent scale / 12 percent lateral
// trip while each step's hip-bob clears the 3-percent-of-span rep bar, and
// the phantoms pad the in-set count until sets auto-complete early. Two
// closures, both here: a short-horizon span-drift window (walking trips the
// guard within ~0.5s regardless of cumulative distance), and rep-completion
// span-stability (a real calf raise starts and ends at the same distance
// from the camera; a walking bob never does).

const assert = require("node:assert/strict");

// ── Mirror of pose.js guard constants + locomotionStep (byte-equivalent) ───
const GUARD_BASELINE_FRAMES  = 30;
const GUARD_MOVE_X_FRAC      = 0.12;  // cumulative lateral hip drift = walking
const GUARD_MOVE_SCALE_FRAC  = 0.10;  // cumulative apparent-size change = walking
const GUARD_DRIFT_FRAMES     = 60;    // ~2s at 30fps: the drift-window horizon
const GUARD_DRIFT_FRAC       = 0.03;  // span drift across the window = walking
const GUARD_REP_SPAN_DRIFT   = 0.03;  // rep must start and end at the same distance

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
    st.recentSpans = [];
    return { phase: "moving", riseFrac: null };
  }
  return { phase: "tracking", riseFrac: (st.baselineY - sample.hipY) / st.baselineSpan };
}

function freshGuard() {
  return { baselineY: null, baselineX: null, baselineSpan: null, samples: [], recentSpans: [] };
}

// ── Mirror of pose.js rise thresholds + calfRaiseStep (byte-equivalent) ────
const RISE_ENTER = 60;
const RISE_EXIT  = 25;
const RISE_GOOD  = 70;
const statusRank = (s) => (s === "bad" ? 2 : s === "warn" ? 1 : 0);

function calfRaiseStep(state, pct, isIdle, frameWorst, span) {
  if (isIdle || pct == null) {
    // Baseline-pending or locomotion: abandon any in-flight rise rather than
    // banking it. Without this, the hip-rise phase of a WALK (before the
    // locomotion gate trips) survives the reset and completes as a phantom
    // rep on the first low reading after the patient re-plants.
    state.state = "idle";
    state.peak = 0;
    state.spanAtEnter = null;
    return null;
  }
  if (state.state === "idle") {
    if (pct >= RISE_ENTER) {
      state.state = "rising";
      state.peak  = pct;
      state.worst = "good";
      state.spanAtEnter = span != null ? span : null;
    }
    return null;
  }
  // rising: track the peak + worst alignment until we come back down.
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
    state.spanAtEnter = null;
    return event;
  }
  return null;
}

function freshRep() {
  return { state: "idle", peak: 0, repCount: 0, worst: "good", spanAtEnter: null };
}

// ── Mirror of pose.js repTrackerIdleReset (byte-equivalent) ─────────────────
function repTrackerIdleReset(t) {
  t.state = "idle";
  t.curMin = null;
  t.curWorstStatus = "good";
  t.curWorstMsg = null;
}

// ── Pipeline: guard + rise scaling + rep machine, as the loop chains them ──
const CALF_RISE_STRONG_FRAC = 0.05;
function runPipeline(samples) {
  const g = freshGuard();
  const r = freshRep();
  const events = [];
  const phases = [];
  for (const s of samples) {
    const step = locomotionStep(g, s);
    phases.push(step.phase);
    const pct = step.phase === "tracking"
      ? Math.max(0, Math.min(100, Math.round((step.riseFrac / CALF_RISE_STRONG_FRAC) * 100)))
      : null;
    const ev = calfRaiseStep(r, pct, step.phase !== "tracking", "good", s ? s.span : null);
    if (ev) events.push(ev);
  }
  return { events, phases, r };
}

const still = (n, hipY, span, hipX = 0.5) =>
  Array.from({ length: n }, () => ({ hipY, hipX, span }));

// ─── locomotionStep basics ──────────────────────────────────────────────────
{
  const st = freshGuard();
  let last;
  for (const f of still(60, 0.45, 0.5)) last = locomotionStep(st, f);
  assert.equal(last.phase, "tracking");
  assert.equal(last.riseFrac, 0);
}
{
  const st = freshGuard();
  for (const f of still(60, 0.45, 0.5)) locomotionStep(st, f);
  const r = locomotionStep(st, { hipY: 0.45, hipX: 0.57, span: 0.5 });
  assert.equal(r.phase, "moving");
  assert.equal(st.baselineY, null, "baseline dropped on lateral locomotion");
}
{
  const st = freshGuard();
  for (const f of still(60, 0.45, 0.5)) locomotionStep(st, f);
  assert.equal(locomotionStep(st, { hipY: 0.45, hipX: 0.5, span: 0.56 }).phase, "moving");
}
{
  const st = freshGuard();
  for (const f of still(60, 0.45, 0.5)) locomotionStep(st, f);
  const r = locomotionStep(st, { hipY: 0.428, hipX: 0.5, span: 0.5 });
  assert.equal(r.phase, "tracking");
  assert.ok(Math.abs(r.riseFrac - 0.044) < 0.001);
}

// ─── KNOWN ACCEPTED LEAK: walk-onset step-bobs can count 1-2 phantoms ──────
// The 2026-08-12 gate experiments (span drift window, per-rep span
// stability) all failed in the field: span noise at gate granularity voided
// GENUINE reps far more than it caught phantoms (worst build: 1 counted of
// 10+ performed). Reverted to cumulative-only gates by Andre's call -
// counting truth for real reps outranks walk purity. The walk-onset gate is
// being redesigned offline against replayed fixture clips; until then this
// scenario documents the accepted behavior instead of asserting zero.
{
  const samples = [...still(60, 0.45, 0.5)];
  let span = 0.5;
  for (let i = 0; i < 90; i++) {
    span *= 0.9983;
    const bob = 0.018 * Math.abs(Math.sin(i / 12));
    samples.push({ hipY: 0.45 - bob - i * 0.0008, hipX: 0.5, span });
  }
  const { events } = runPipeline(samples);
  assert.ok(events.length <= 2,
    `walk-onset leak exceeded the documented bound: ${events.length} phantoms`);
}

// ─── Real raises still count: distant, full-body stance ────────────────────
{
  const samples = [...still(60, 0.45, 0.5)];
  for (let rep = 0; rep < 3; rep++) {
    samples.push(...still(6, 0.428, 0.5));  // up: 4.4 percent of span
    samples.push(...still(6, 0.449, 0.5));  // down
  }
  const { events } = runPipeline(samples);
  assert.equal(events.length, 3, "three genuine raises must count exactly three");
  assert.ok(events.every((e) => e.status === "good"));
}

// ─── Replant after a walk: counting resumes at the new stance ──────────────
{
  const samples = [...still(60, 0.45, 0.5)];
  let span = 0.5;
  for (let i = 0; i < 60; i++) {           // walk away
    span *= 0.998;
    samples.push({ hipY: 0.45 - i * 0.001, hipX: 0.5 + i * 0.001, span });
  }
  samples.push(...still(90, 0.39, 0.44));  // replant: drift window + baseline refill
  samples.push(...still(6, 0.37, 0.44));   // raise: 4.5 percent of new span
  samples.push(...still(6, 0.391, 0.44));  // lower
  const { events, phases } = runPipeline(samples);
  assert.equal(events.length, 1, "exactly the one genuine post-replant rep");
  assert.ok(phases.includes("moving"), "the walk must register as locomotion");
}

// ─── RepTracker idle contract (angle family) ───────────────────────────────
{
  const t = { state: "descending", curMin: 95, repCount: 0, curWorstStatus: "good", curWorstMsg: null };
  repTrackerIdleReset(t);
  assert.equal(t.state, "idle");
  assert.equal(t.curMin, null);
  assert.equal(t.repCount, 0, "abandon, never bank");
}

// ── Mirror of pose.js bodySample + deps (byte-equivalent) ───────────────────
const L = {
  LEFT_SHOULDER: 11, RIGHT_SHOULDER: 12,
  LEFT_HIP: 23, RIGHT_HIP: 24,
  LEFT_ANKLE: 27, RIGHT_ANKLE: 28,
};
const VIS_THRESHOLD = 0.3;
function visibleEnough(...pts) {
  return pts.every((p) => p && (p.visibility ?? 1) >= VIS_THRESHOLD);
}

function bodySample(lms) {
  if (!lms) return null;
  const lh = lms[L.LEFT_HIP], rh = lms[L.RIGHT_HIP];
  if (!visibleEnough(lh, rh)) return null;
  const ls = lms[L.LEFT_SHOULDER], rs = lms[L.RIGHT_SHOULDER];
  if (!visibleEnough(ls, rs)) return null;
  const hipY = (lh.y + rh.y) / 2;
  const hipX = (lh.x + rh.x) / 2;
  const shoulderY = (ls.y + rs.y) / 2;
  // Span is a RULER, not a tripwire: it scales the rise percentage and the
  // cumulative walk thresholds, where a few percent of keypoint noise is
  // harmless (the 60/25 rise hysteresis dwarfs it). One night of field
  // regressions (2026-08-12) taught the boundary: three span variants all
  // counted fine as rulers, and every attempt to ALSO use span as a
  // fine-grained movement gate (3 percent windows) drowned in noise -
  // ankle-mean bounced with a swinging free foot, planted-ankle flipped
  // when feet overlap in image space, and torso/0.37 amplified jitter 2.7x
  // past the gates. Walk-onset detection is deliberately absent here; it is
  // being redesigned offline against replayed fixture clips.
  const la = lms[L.LEFT_ANKLE], ra = lms[L.RIGHT_ANKLE];
  let span;
  if (visibleEnough(la, ra)) {
    span = (la.y + ra.y) / 2 - shoulderY;
  } else {
    span = (hipY - shoulderY) / 0.37;
  }
  return { hipY, hipX, span };
}

// ─── SINGLE-LEG FIELD REGRESSION: swinging free foot must not void reps ─────
// 2026-08-12, twice: ankle-MEAN span counted 2-3 of ~15 (free-foot swing);
// planted-ankle span counted 1 of 10+ (the max flips mid-rep as the working
// heel lifts). Torso-derived span ignores the feet entirely; this scenario
// swings the free ankle hard through six genuine raises and must count six.
{
  const mkLms = (hipY, freeAnkleY) => {
    const lms = [];
    lms[L.LEFT_SHOULDER] = { x: 0.48, y: hipY - 0.17, visibility: 1 };
    lms[L.RIGHT_SHOULDER] = { x: 0.52, y: hipY - 0.17, visibility: 1 };
    lms[L.LEFT_HIP] = { x: 0.48, y: hipY, visibility: 1 };
    lms[L.RIGHT_HIP] = { x: 0.52, y: hipY, visibility: 1 };
    lms[L.LEFT_ANKLE] = { x: 0.49, y: hipY + 0.28, visibility: 1 };   // planted
    lms[L.RIGHT_ANKLE] = { x: 0.55, y: freeAnkleY, visibility: 1 };   // free, swinging
    return lms;
  };

  const g = freshGuard();
  const r = freshRep();
  const events = [];
  const feed = (hipY, freeAnkleY) => {
    const sample = bodySample(mkLms(hipY, freeAnkleY));
    const step = locomotionStep(g, sample);
    const pct = step.phase === "tracking"
      ? Math.max(0, Math.min(100, Math.round((step.riseFrac / CALF_RISE_STRONG_FRAC) * 100)))
      : null;
    const ev = calfRaiseStep(r, pct, step.phase !== "tracking", "good", sample ? sample.span : null);
    if (ev) events.push(ev);
  };

  // Settle standing on one leg, free foot hovering and jittering.
  for (let i = 0; i < 70; i++) feed(0.45, 0.62 + 0.02 * Math.sin(i / 3));
  // Six genuine raises: hips rise ~4.4 percent of the planted span while the
  // free foot swings hard (its y moving several percent every few frames).
  for (let rep = 0; rep < 6; rep++) {
    for (let i = 0; i < 6; i++) feed(0.428, 0.60 + 0.03 * Math.sin((rep * 12 + i) / 2));
    for (let i = 0; i < 6; i++) feed(0.449, 0.63 + 0.03 * Math.sin((rep * 12 + 6 + i) / 2));
  }
  assert.equal(events.length, 6,
    `swinging free foot voided reps: counted ${events.length} of 6 performed`);
}

// ── Mirror of pose.js swayFrom (byte-equivalent) ────────────────────────────
const SWAY_WARN_FRAC = 0.05;
const SWAY_BAD_FRAC  = 0.10;
function swayFrom(hipX, baselineX, baselineSpan) {
  const frac = Math.abs(hipX - baselineX) / baselineSpan;
  let status = "good";
  if (frac > SWAY_BAD_FRAC) status = "bad";
  else if (frac > SWAY_WARN_FRAC) status = "warn";
  return { frac: +(frac * 100).toFixed(1), status };
}

// ─── Sway: baseline-relative and distance-invariant ────────────────────────
{
  assert.equal(swayFrom(0.60, 0.60, 0.5).status, "good", "standing off-center is not sway");
  const nearWobble = swayFrom(0.545, 0.5, 0.9);
  const farWobble  = swayFrom(0.525, 0.5, 0.5);
  assert.equal(nearWobble.status, farWobble.status, "same body wobble, same verdict at any distance");
  assert.equal(swayFrom(0.60, 0.5, 0.5).status, "bad");
  assert.equal(swayFrom(0.535, 0.5, 0.5).status, "warn");
}

console.log("OK: locomotion guard - all assertions passed");
