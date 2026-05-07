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

console.log("PR-J guided-mode pure helpers: all assertions passed.");
