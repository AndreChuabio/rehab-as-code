// Node-runnable unit tests for the PR-M flow-stitching pure helpers.
//
// Usage:
//   node frontend/tests/flow_stitching.test.js
//
// Same shape as pose_guided.test.js — no Jest, no jsdom. The helpers
// under test (nextExerciseAfter, buildPickerItems) are intentionally
// DOM-free so they can run in plain Node. They live in app.js as
// window.__flowHelpers, but to avoid pulling in a browser context this
// test re-defines byte-equivalent copies inline. When the rules change
// in app.js, change them here too.
//
// What this covers:
//   * buildPickerItems: filters malformed rows, normalizes id/name/dose
//   * nextExerciseAfter: pointer arithmetic + done detection
//   * Round-trip simulation of the happy-path 3-exercise flow

const assert = require("node:assert/strict");

// ── Mirror of helpers from app.js ───────────────────────────────────────────

function nextExerciseAfter(state) {
  const exercises = state.exercises || [];
  const nextIdx = (state.currentIdx ?? -1) + 1;
  if (!exercises.length) return { done: true, exercise: null, nextIdx };
  if (nextIdx >= exercises.length) return { done: true, exercise: null, nextIdx };
  return { done: false, exercise: exercises[nextIdx], nextIdx };
}

function buildPickerItems(exercises) {
  if (!Array.isArray(exercises)) return [];
  return exercises
    .filter((ex) => ex && (ex.id || ex.name))
    .map((ex) => ({
      id: ex.id || ex.name,
      name: ex.name || ex.id || "exercise",
      default_dose: ex.default_dose || ex.spec || "",
      cues: ex.cues || [],
      generated_video_url: ex.generated_video_url || "",
      youtube_id: ex.youtube_id || "",
      youtube_watch_url: ex.youtube_watch_url || "",
      thumbnail_url: ex.thumbnail_url || "",
    }));
}

// ── buildPickerItems ────────────────────────────────────────────────────────
{
  // Empty / malformed input is tolerated, returns []
  assert.deepEqual(buildPickerItems(undefined), []);
  assert.deepEqual(buildPickerItems(null), []);
  assert.deepEqual(buildPickerItems([]), []);
  assert.deepEqual(buildPickerItems("not an array"), []);

  // Rows with neither id nor name are dropped (defensive — would render an
  // unclickable row otherwise).
  const out = buildPickerItems([
    { id: "wall_sit", name: "Wall Sit", default_dose: "3x30s", cues: ["a", "b"] },
    { name: "quad_sets", default_dose: "3x15" },  // no id - falls back to name
    { id: "heel_slides" },                         // no name - falls back to id
    {},                                            // dropped
    null,                                          // dropped
  ]);
  assert.equal(out.length, 3, "rows without id+name are filtered");
  assert.equal(out[0].id, "wall_sit");
  assert.equal(out[0].cues.length, 2);
  assert.equal(out[1].id, "quad_sets", "row with only name uses name as id");
  assert.equal(out[2].name, "heel_slides", "row with only id uses id as name");

  // spec is honored when default_dose is missing (matches /protocol/exercises shape).
  const specRow = buildPickerItems([{ id: "stationary_bike", name: "Bike", spec: "8 min" }]);
  assert.equal(specRow[0].default_dose, "8 min", "spec falls through to default_dose");
}

// ── nextExerciseAfter: empty + done detection ───────────────────────────────
{
  // Empty exercise list -> immediately done.
  const empty = nextExerciseAfter({ exercises: [], currentIdx: -1 });
  assert.equal(empty.done, true);
  assert.equal(empty.exercise, null);

  // Past the end of the list -> done.
  const past = nextExerciseAfter({
    exercises: [{ id: "a" }, { id: "b" }],
    currentIdx: 1,  // last index already played
  });
  assert.equal(past.done, true);
  assert.equal(past.exercise, null);
  assert.equal(past.nextIdx, 2);
}

// ── nextExerciseAfter: forward progression ──────────────────────────────────
{
  const state = {
    exercises: [
      { id: "wall_sit",   name: "Wall Sit" },
      { id: "quad_sets",  name: "Quad Sets" },
      { id: "heel_slides", name: "Heel Slides" },
    ],
    currentIdx: -1,  // not started
  };

  const a = nextExerciseAfter(state);
  assert.equal(a.done, false);
  assert.equal(a.nextIdx, 0);
  assert.equal(a.exercise.id, "wall_sit");

  const b = nextExerciseAfter({ ...state, currentIdx: 0 });
  assert.equal(b.exercise.id, "quad_sets");

  const c = nextExerciseAfter({ ...state, currentIdx: 1 });
  assert.equal(c.exercise.id, "heel_slides");

  const d = nextExerciseAfter({ ...state, currentIdx: 2 });
  assert.equal(d.done, true, "after the last exercise, next is done");
}

// ── End-to-end: simulated 3-exercise happy path ─────────────────────────────
{
  const items = buildPickerItems([
    { id: "wall_sit", name: "Wall Sit", default_dose: "3x30s" },
    { id: "quad_sets", name: "Quad Sets", default_dose: "3x15" },
    { id: "heel_slides", name: "Heel Slides", default_dose: "3x10" },
  ]);
  const state = {
    active: true,
    exercises: items,
    currentIdx: -1,
    completedIds: [],
  };

  // Exercise 1
  let step = nextExerciseAfter(state);
  assert.equal(step.exercise.id, "wall_sit");
  state.currentIdx = step.nextIdx;
  state.completedIds.push(step.exercise.id);

  // Exercise 2
  step = nextExerciseAfter(state);
  assert.equal(step.exercise.id, "quad_sets");
  state.currentIdx = step.nextIdx;
  state.completedIds.push(step.exercise.id);

  // Exercise 3
  step = nextExerciseAfter(state);
  assert.equal(step.exercise.id, "heel_slides");
  state.currentIdx = step.nextIdx;
  state.completedIds.push(step.exercise.id);

  // Done
  step = nextExerciseAfter(state);
  assert.equal(step.done, true);
  assert.deepEqual(state.completedIds, ["wall_sit", "quad_sets", "heel_slides"]);
}

// ── Mid-session bail + re-entry ─────────────────────────────────────────────
{
  // Simulate: patient completes exercise 1, bails, returns later. The
  // completed set persists across the bail; re-entry resumes from idx 1.
  const items = buildPickerItems([
    { id: "wall_sit", name: "Wall Sit" },
    { id: "quad_sets", name: "Quad Sets" },
    { id: "heel_slides", name: "Heel Slides" },
  ]);
  const state = {
    active: true,
    exercises: items,
    currentIdx: 0,
    completedIds: ["wall_sit"],
  };

  // Bail: caller flips active=false. completedIds untouched.
  state.active = false;
  assert.equal(state.completedIds.length, 1);

  // Re-entry: pickerStartFirst handler computes the first not-yet-done idx.
  const completed = new Set(state.completedIds);
  const firstUndoneIdx = items.findIndex((e) => !completed.has(e.id));
  assert.equal(firstUndoneIdx, 1, "re-entry resumes after the last completed exercise");

  // From there the same nextExerciseAfter advance applies.
  state.active = true;
  state.currentIdx = firstUndoneIdx - 1;
  const resumed = nextExerciseAfter(state);
  assert.equal(resumed.exercise.id, "quad_sets");
}

console.log("PR-M flow-stitching pure helpers: all assertions passed.");
