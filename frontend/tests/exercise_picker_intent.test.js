// Node-runnable unit tests for the chat "show me the exercise picker" intent.
//
// Usage:
//   node frontend/tests/exercise_picker_intent.test.js
//
// Same convention as flow_stitching.test.js: _wantsExercisePicker is a
// DOM-free pure helper in app.js; this file mirrors it byte-for-byte. When the
// rule changes in app.js, change it here too.
//
// The gate must fire on a generic "log/do/start an exercise" (-> clickable
// protocol picker) but NOT on a check-in, a named-exercise video request
// (handled elsewhere), or ordinary conversation.

const assert = require("node:assert/strict");

// ── Mirror of _wantsExercisePicker from app.js ──────────────────────────────

function _wantsExercisePicker(text) {
  const t = (text || "").toLowerCase();
  if (/check.?in/.test(t)) return false;
  return /\b(log|do|start|begin|record|track)\b/.test(t)
      && /\b(exercises?|workout|session|my sets?)\b/.test(t);
}

let passed = 0;
function check(label, got, want) {
  assert.equal(got, want, label);
  passed++;
}

// ── Should show the picker ──────────────────────────────────────────────────
check("lets log a exercise", _wantsExercisePicker("lets log a exercise"), true);
check("log an exercise", _wantsExercisePicker("log an exercise"), true);
check("start my session", _wantsExercisePicker("start my session"), true);
check("do my workout", _wantsExercisePicker("do my workout"), true);
check("record an exercise", _wantsExercisePicker("record an exercise"), true);
check("log my exercises", _wantsExercisePicker("log my exercises"), true);
check("begin a session", _wantsExercisePicker("begin a session"), true);

// ── Should NOT show the picker ──────────────────────────────────────────────
check("log a check-in", _wantsExercisePicker("log a check-in"), false);
check("log a checkin", _wantsExercisePicker("log a checkin"), false);
check("pull up my calf raises video", _wantsExercisePicker("pull up my calf raises video"), false);
check("how do i do calf raises", _wantsExercisePicker("how do i do calf raises"), false);
check("lets chat about my progress", _wantsExercisePicker("lets chat about my progress"), false);
check("my knee feels tweaky", _wantsExercisePicker("my knee feels tweaky"), false);
check("empty", _wantsExercisePicker(""), false);
check("null", _wantsExercisePicker(null), false);

console.log(`exercise_picker_intent.test.js: ${passed}/${passed} checks passed`);
