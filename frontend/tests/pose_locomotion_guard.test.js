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

  // Short-horizon drift: walking changes apparent size continuously, so the
  // span moves a few percent within the window long before the cumulative
  // threshold trips. Checked before everything else so onset is caught even
  // while a stale baseline still looks plausible.
  st.recentSpans.push(sample.span);
  if (st.recentSpans.length > GUARD_DRIFT_FRAMES) st.recentSpans.shift();
  if (st.recentSpans.length === GUARD_DRIFT_FRAMES) {
    const oldest = st.recentSpans[0];
    if (Math.abs(sample.span / oldest - 1) > GUARD_DRIFT_FRAC) {
      st.baselineY = null;
      st.baselineX = null;
      st.baselineSpan = null;
      st.samples = [];
      st.recentSpans = [];
      return { phase: "moving", riseFrac: null };
    }
  }

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
    // Span-stability: a genuine rep begins and ends at the same distance
    // from the camera. A walking bob that sneaked past the guard drifts a
    // few percent across its cycle - abandon it instead of counting it.
    if (
      state.spanAtEnter != null && span != null &&
      Math.abs(span / state.spanAtEnter - 1) > GUARD_REP_SPAN_DRIFT
    ) {
      state.state = "idle";
      state.peak = 0;
      state.spanAtEnter = null;
      return null;
    }
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

// ─── Drift window: slow walking trips the guard inside ~2s ─────────────────
{
  // Span shrinks 0.2 percent per frame - far under the 10 percent cumulative
  // trip for a long time, but 60 frames of it is ~11 percent... use a gentler
  // slope: 0.07 percent per frame = ~4.3 percent over the 60-frame window,
  // above GUARD_DRIFT_FRAC. The guard must flip to moving without the
  // cumulative threshold ever firing.
  const st = freshGuard();
  for (const f of still(60, 0.45, 0.5)) locomotionStep(st, f);
  let sawMoving = false;
  let span = 0.5;
  for (let i = 0; i < 90; i++) {
    span *= 0.9993;
    const r = locomotionStep(st, { hipY: 0.45 - i * 0.0006, hipX: 0.5, span });
    if (r.phase === "moving") { sawMoving = true; break; }
  }
  assert.ok(sawMoving, "slow walk must trip the drift window, not wait for 10 percent");
}

// ─── THE T1 LEAK: walk-onset step-bobs must not count ──────────────────────
{
  // Reproduces 2026-08-12 set 1/2: baseline standing at the desk, then walk
  // away. Each step bobs the hips (rise-shaped cycles ~0.8s) while span
  // shrinks ~5 percent per second. Old behavior: the first 1-2 bobs complete
  // as reps before cumulative thresholds trip. Required: ZERO reps.
  const st = freshGuard();
  const rep = freshRep();
  const samples = [...still(60, 0.45, 0.5)];
  let span = 0.5;
  for (let i = 0; i < 90; i++) {
    span *= 0.9983;                                 // ~5 percent shrink per 30 frames
    const bob = 0.018 * Math.abs(Math.sin(i / 12)); // step bob: ~3.6 percent of span
    samples.push({ hipY: 0.45 - bob - i * 0.0008, hipX: 0.5, span });
  }
  const { events } = runPipeline(samples);
  assert.equal(events.length, 0,
    `walk-onset bobs counted ${events.length} phantom rep(s) - the T1 leak is open`);
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

// ─── Span-stability rejection at rep completion ────────────────────────────
{
  // A bob that clears ENTER and EXIT while span drifts 5 percent across the
  // cycle is a walking artifact, not a rep - even if the guard missed it.
  const st = freshRep();
  assert.equal(calfRaiseStep(st, 80, false, "good", 0.50), null);   // enter at span 0.50
  assert.equal(st.state, "rising");
  const done = calfRaiseStep(st, 10, false, "good", 0.475);         // exit at span 0.475
  assert.equal(done, null, "5 percent span drift across the cycle must abandon the rep");
  assert.equal(st.repCount, 0);
  assert.equal(st.state, "idle");
}
{
  // Same cycle with stable span completes normally.
  const st = freshRep();
  calfRaiseStep(st, 80, false, "good", 0.50);
  const done = calfRaiseStep(st, 10, false, "good", 0.503);
  assert.ok(done, "stable-span rep must complete");
  assert.equal(done.status, "good");
}
{
  // Span unavailable (ankles briefly lost): the check is skipped, not fatal.
  const st = freshRep();
  calfRaiseStep(st, 80, false, "good", null);
  const done = calfRaiseStep(st, 10, false, "good", null);
  assert.ok(done, "missing span must not block a rep");
}

// ─── RepTracker idle contract (angle family) ───────────────────────────────
{
  const t = { state: "descending", curMin: 95, repCount: 0, curWorstStatus: "good", curWorstMsg: null };
  repTrackerIdleReset(t);
  assert.equal(t.state, "idle");
  assert.equal(t.curMin, null);
  assert.equal(t.repCount, 0, "abandon, never bank");
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
