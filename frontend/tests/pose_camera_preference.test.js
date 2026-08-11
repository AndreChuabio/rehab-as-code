// Node-runnable unit test for the auto-framing camera avoidance in pose.js.
//
// Usage:
//   node frontend/tests/pose_camera_preference.test.js
//
// Same shape as auto_applied_feed.test.js - no Jest, no jsdom. The pure
// helpers under test (cameraLooksAutoFraming, pickPreferredCamera) live in
// pose.js inside the browser IIFE; the copies here must stay byte-equivalent.
//
// Why this exists: macOS Center Stage (iPhone Continuity Camera, Studio
// Display) zooms onto the face at the OS level and no getUserMedia constraint
// can disable it, so on those cameras the full_body preflight gate can never
// open. macOS often promotes a nearby iPhone to the DEFAULT camera, so the
// default choice - not the framing gate - was what blocked guided form check.

const assert = require("node:assert/strict");

// ── Mirror of pose.js (byte-equivalent) ────────────────────────────────────
const AUTO_FRAMING_CAMERA_RE = /iphone|continuity|desk view|studio display/i;
const BUILTIN_CAMERA_RE = /facetime|built-in|integrated|internal/i;

function cameraLooksAutoFraming(label) {
  return AUTO_FRAMING_CAMERA_RE.test(label || "");
}

function pickPreferredCamera(activeLabel, cameras) {
  if (!cameraLooksAutoFraming(activeLabel)) return null;
  const list = Array.isArray(cameras) ? cameras : [];
  const builtin = list.find((c) => BUILTIN_CAMERA_RE.test(c.label || ""));
  if (builtin) return builtin;
  const nonAuto = list.find(
    (c) => (c.label || "") !== activeLabel && !cameraLooksAutoFraming(c.label),
  );
  return nonAuto || null;
}

// ── cameraLooksAutoFraming: the labels macOS actually produces ─────────────
assert.ok(cameraLooksAutoFraming("Andre's iPhone Camera"));
assert.ok(cameraLooksAutoFraming("iPhone Camera (Continuity)"));
assert.ok(cameraLooksAutoFraming("Desk View Camera"));
assert.ok(cameraLooksAutoFraming("Studio Display Camera"));
assert.ok(!cameraLooksAutoFraming("FaceTime HD Camera"));
assert.ok(!cameraLooksAutoFraming("FaceTime HD Camera (Built-in)"));
assert.ok(!cameraLooksAutoFraming("Logitech C920"));
assert.ok(!cameraLooksAutoFraming(""));
assert.ok(!cameraLooksAutoFraming(null));

// ── pickPreferredCamera: switches AWAY from auto-framing, prefers built-in ──
const IPHONE = { deviceId: "id-iphone", label: "Andre's iPhone Camera" };
const FACETIME = { deviceId: "id-ft", label: "FaceTime HD Camera" };
const LOGITECH = { deviceId: "id-lg", label: "Logitech C920" };
const DESKVIEW = { deviceId: "id-dv", label: "Desk View Camera" };

// Active camera is fine -> never propose a switch, whatever else exists.
assert.equal(pickPreferredCamera("FaceTime HD Camera", [FACETIME, IPHONE]), null);
assert.equal(pickPreferredCamera("Logitech C920", [LOGITECH, IPHONE]), null);
assert.equal(pickPreferredCamera("", [FACETIME, IPHONE]), null);

// Auto-framing active -> prefer the built-in camera when present.
assert.equal(
  pickPreferredCamera("Andre's iPhone Camera", [IPHONE, FACETIME, LOGITECH]).deviceId,
  "id-ft",
);

// No built-in -> any non-auto-framing camera beats staying on the iPhone.
assert.equal(
  pickPreferredCamera("Andre's iPhone Camera", [IPHONE, LOGITECH]).deviceId,
  "id-lg",
);

// Only auto-framing cameras available -> no switch target; the preflight
// hint (turn Center Stage off in the menu bar) is the remaining fix.
assert.equal(pickPreferredCamera("Andre's iPhone Camera", [IPHONE, DESKVIEW]), null);
assert.equal(pickPreferredCamera("Andre's iPhone Camera", [IPHONE]), null);
assert.equal(pickPreferredCamera("Andre's iPhone Camera", []), null);
assert.equal(pickPreferredCamera("Andre's iPhone Camera", null), null);

console.log("OK: pose camera preference - all assertions passed");
