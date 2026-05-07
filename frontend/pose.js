// Pose-based form check for RehabAsCode (v2 spike).
// Loads MediaPipe Pose Landmarker in-browser. Per frame:
//   * runs landmark detection
//   * evaluates a per-exercise list of "checks" (depth, valgus, trunk lean,
//     hip drop, etc.) → each returns { status, value, unit, label, segments }
//   * colours skeleton segments by worst-touching-check status
//   * draws angle text labels next to tracked joints
//   * fires onPayload({ primary, metrics, warnings }) every frame
//
// Public API on window.PoseFormCheck:
//   await init()
//   start(videoEl, canvasEl, exerciseId, onPayload)
//   stop()

const VISION_CDN =
  "https://cdn.jsdelivr.net/npm/@mediapipe/tasks-vision@0.10.14/vision_bundle.mjs";
const MODEL_URL =
  "https://storage.googleapis.com/mediapipe-models/pose_landmarker/pose_landmarker_lite/float16/1/pose_landmarker_lite.task";
const WASM_BASE =
  "https://cdn.jsdelivr.net/npm/@mediapipe/tasks-vision@0.10.14/wasm";

// MediaPipe Pose landmark indices (BlazePose 33-point).
const L = {
  NOSE: 0,
  LEFT_SHOULDER: 11, RIGHT_SHOULDER: 12,
  LEFT_ELBOW: 13,    RIGHT_ELBOW: 14,
  LEFT_WRIST: 15,    RIGHT_WRIST: 16,
  LEFT_HIP: 23,      RIGHT_HIP: 24,
  LEFT_KNEE: 25,     RIGHT_KNEE: 26,
  LEFT_ANKLE: 27,    RIGHT_ANKLE: 28,
};

// Skeleton edges. Each edge knows which checks may colour it.
//   ck: list of check ids that "own" this segment (worst status wins)
const EDGES = [
  { a: 11, b: 12, ck: ["trunk_lean"] },                       // shoulders
  { a: 11, b: 23, ck: ["trunk_lean"] },                       // left torso
  { a: 12, b: 24, ck: ["trunk_lean"] },                       // right torso
  { a: 23, b: 24, ck: ["hip_drop", "hip_symmetry"] },         // hip line
  // Left arm. shoulder→elbow colored by L shoulder abduction; elbow→wrist by L elbow angle.
  { a: 11, b: 13, ck: ["L_shoulder_abduction"] },
  { a: 13, b: 15, ck: ["L_elbow_angle"] },
  // Right arm. Same pattern, R side.
  { a: 12, b: 14, ck: ["R_shoulder_abduction"] },
  { a: 14, b: 16, ck: ["R_elbow_angle"] },
  { a: 23, b: 25, ck: ["L_knee_valgus", "L_knee_depth"] },    // left thigh
  { a: 25, b: 27, ck: ["L_knee_valgus", "L_knee_depth"] },    // left shin
  { a: 24, b: 26, ck: ["R_knee_valgus", "R_knee_depth"] },    // right thigh
  { a: 26, b: 28, ck: ["R_knee_valgus", "R_knee_depth"] },    // right shin
];

// ---------------------------------------------------------------------------
// Geometry helpers
// ---------------------------------------------------------------------------

function angleAt(b, a, c) {
  // Angle at vertex b formed by rays b->a and b->c, in degrees.
  const bax = a.x - b.x, bay = a.y - b.y;
  const bcx = c.x - b.x, bcy = c.y - b.y;
  const dot = bax * bcx + bay * bcy;
  const magA = Math.hypot(bax, bay);
  const magC = Math.hypot(bcx, bcy);
  if (!magA || !magC) return null;
  const cos = Math.max(-1, Math.min(1, dot / (magA * magC)));
  return (Math.acos(cos) * 180) / Math.PI;
}

// Lowered from 0.4 to 0.3 so far-side joints register on slight body turns.
const VIS_THRESHOLD = 0.3;
function visibleEnough(...pts) {
  return pts.every((p) => p && (p.visibility ?? 1) >= VIS_THRESHOLD);
}

// PR-U9: framing config. Each exercise declares which body region needs to be
// visible; the preflight overlay uses this to give the patient EXERCISE-
// SPECIFIC camera guidance ("sit and point camera at your ankles") instead
// of the generic "step into frame," which is wrong half the time. Required
// landmarks are scored against MediaPipe's per-keypoint visibility.
const FRAMING_CONFIG = {
  full_body: {
    label: "head to feet",
    required: [L.LEFT_SHOULDER, L.RIGHT_SHOULDER, L.LEFT_HIP, L.RIGHT_HIP,
               L.LEFT_KNEE, L.RIGHT_KNEE, L.LEFT_ANKLE, L.RIGHT_ANKLE],
    hint: "Step back — we need to see your whole body, head to feet.",
    cameraTip: "Camera at chest height, about 6 feet away.",
  },
  lower_body: {
    label: "hips to ankles",
    required: [L.LEFT_HIP, L.RIGHT_HIP, L.LEFT_KNEE, L.RIGHT_KNEE,
               L.LEFT_ANKLE, L.RIGHT_ANKLE],
    hint: "We need to see from your hips to your ankles.",
    cameraTip: "Lower the camera or step back so your legs are in frame.",
  },
  feet_seated: {
    label: "knees + ankles",
    required: [L.LEFT_KNEE, L.RIGHT_KNEE, L.LEFT_ANKLE, L.RIGHT_ANKLE],
    hint: "Sit in a chair and point the camera at your feet and ankles.",
    cameraTip: "Set your phone or laptop on the floor, 3-4 feet in front of you.",
  },
  arms_torso: {
    label: "shoulders + arms",
    required: [L.LEFT_SHOULDER, L.RIGHT_SHOULDER, L.LEFT_ELBOW, L.RIGHT_ELBOW,
               L.LEFT_WRIST, L.RIGHT_WRIST],
    hint: "We need to see your shoulders and both arms.",
    cameraTip: "Camera at chest height, about 4 feet away.",
  },
};

// Returns { framing, visible, required, ready, missingLabel, hint, cameraTip }.
// `ready` is true when at least 75% of the required landmarks pass the
// visibility threshold — partial framing still lets pose.js publish per-
// exercise checks; the wrapper UI uses `ready` for the green-light state.
function assessFraming(lms, framing) {
  const cfg = FRAMING_CONFIG[framing];
  if (!cfg || !lms) {
    return { framing, visible: 0, required: 0, ready: false,
             missingLabel: "", hint: "", cameraTip: "" };
  }
  let visible = 0;
  for (const idx of cfg.required) {
    const p = lms[idx];
    if (p && (p.visibility ?? 1) >= VIS_THRESHOLD) visible++;
  }
  const required = cfg.required.length;
  const ready = visible >= Math.ceil(required * 0.75);
  return {
    framing,
    visible,
    required,
    ready,
    missingLabel: cfg.label,
    hint: cfg.hint,
    cameraTip: cfg.cameraTip,
  };
}

function statusFromPercent(percent, mode) {
  if (percent == null) return "idle";
  if (mode === "min") {
    return percent >= 90 ? "good" : percent >= 60 ? "warn" : "bad";
  }
  // for max / max_extension: we treat hitting target as good, below as
  // progressing (still good), miles short as idle. Form deviations come
  // from the alignment checks, not the depth metric.
  return "good";
}

function depthPercent(angleVal, target, mode) {
  if (angleVal == null || target == null) return null;
  if (mode === "max") {
    const span = 180 - target;
    if (span <= 0) return 100;
    return Math.max(0, Math.min(120, Math.round(((180 - angleVal) / span) * 100)));
  }
  if (mode === "min") {
    const err = Math.abs(angleVal - target);
    return Math.max(0, Math.min(100, Math.round(100 - err * 2)));
  }
  if (mode === "max_extension") {
    const span = 90;
    return Math.max(0, Math.min(120, Math.round((Math.max(0, angleVal - 90) / span) * 100)));
  }
  return null;
}

// ---------------------------------------------------------------------------
// Individual checks. Each takes landmarks, returns a metric record:
//   { id, label, value, unit, status, msg?, target?, percent?, jointIdx? }
// or null if not computable this frame.
// ---------------------------------------------------------------------------

function checkKneeAngle(side, lms, target, mode) {
  const isL = side === "L";
  const hip   = lms[isL ? L.LEFT_HIP   : L.RIGHT_HIP];
  const knee  = lms[isL ? L.LEFT_KNEE  : L.RIGHT_KNEE];
  const ankle = lms[isL ? L.LEFT_ANKLE : L.RIGHT_ANKLE];
  if (!visibleEnough(hip, knee, ankle)) return null;
  const a = angleAt(knee, hip, ankle);
  if (a == null) return null;
  const pct = depthPercent(a, target, mode);
  return {
    id: `${side}_knee_depth`,
    label: `${side} knee`,
    value: Math.round(a),
    unit: "°",
    target,
    percent: pct,
    status: statusFromPercent(pct, mode),
    jointIdx: isL ? L.LEFT_KNEE : L.RIGHT_KNEE,
  };
}

function checkHipAngle(side, lms, target, mode) {
  const isL = side === "L";
  const sh   = lms[isL ? L.LEFT_SHOULDER : L.RIGHT_SHOULDER];
  const hip  = lms[isL ? L.LEFT_HIP      : L.RIGHT_HIP];
  const knee = lms[isL ? L.LEFT_KNEE     : L.RIGHT_KNEE];
  if (!visibleEnough(sh, hip, knee)) return null;
  const a = angleAt(hip, sh, knee);
  if (a == null) return null;
  const pct = depthPercent(a, target, mode);
  return {
    id: `${side}_hip_angle`,
    label: `${side} hip`,
    value: Math.round(a),
    unit: "°",
    target,
    percent: pct,
    status: statusFromPercent(pct, mode),
    jointIdx: isL ? L.LEFT_HIP : L.RIGHT_HIP,
  };
}

function checkKneeValgus(side, lms) {
  // 2D frontal-plane proxy: knee.x relative to the line connecting hip.x
  // and ankle.x. Inward (medial) deviation past threshold = valgus.
  const isL = side === "L";
  const hip   = lms[isL ? L.LEFT_HIP   : L.RIGHT_HIP];
  const knee  = lms[isL ? L.LEFT_KNEE  : L.RIGHT_KNEE];
  const ankle = lms[isL ? L.LEFT_ANKLE : L.RIGHT_ANKLE];
  if (!visibleEnough(hip, knee, ankle)) return null;

  // Body midline. Prefer avg of both hips; fall back to single hip + opposite
  // shoulder when only one side is visible.
  const lh = lms[L.LEFT_HIP], rh = lms[L.RIGHT_HIP];
  const ls = lms[L.LEFT_SHOULDER], rs = lms[L.RIGHT_SHOULDER];
  let midX;
  if (visibleEnough(lh, rh)) midX = (lh.x + rh.x) / 2;
  else if (visibleEnough(lh, rs)) midX = (lh.x + rs.x) / 2;
  else if (visibleEnough(rh, ls)) midX = (rh.x + ls.x) / 2;
  else return null;

  // Expected knee.x = lerp(hip.x, ankle.x) at the knee's relative height.
  const t = (knee.y - hip.y) / Math.max(1e-3, ankle.y - hip.y);
  const expectedX = hip.x + (ankle.x - hip.x) * Math.max(0, Math.min(1, t));

  // Medial = toward midline. dev > 0 means knee is medial of expected line.
  const dev = isL ? (expectedX - knee.x) : (knee.x - expectedX);
  // sign of (knee - midline) vs (hip - midline) — if knee is on the same
  // side as hip but closer to midline than expected, it's caving inward.
  const inward = isL ? (knee.x > expectedX) : (knee.x < expectedX);
  const absDev = Math.abs(dev);

  let status = "good", msg;
  if (inward && absDev > 0.08)      { status = "bad";  msg = `${side === "L" ? "left" : "right"} knee caving in`; }
  else if (inward && absDev > 0.04) { status = "warn"; msg = `${side === "L" ? "left" : "right"} knee drifting medial`; }

  return {
    id: `${side}_knee_valgus`,
    label: `${side} knee track`,
    value: +(absDev * 100).toFixed(1),
    unit: "%",
    status,
    msg,
    jointIdx: isL ? L.LEFT_KNEE : L.RIGHT_KNEE,
  };
}

function checkTrunkLean(lms) {
  const ls = lms[L.LEFT_SHOULDER], rs = lms[L.RIGHT_SHOULDER];
  const lh = lms[L.LEFT_HIP],      rh = lms[L.RIGHT_HIP];
  const okBoth   = visibleEnough(ls, rs, lh, rh);
  const okLeft   = visibleEnough(ls, lh);
  const okRight  = visibleEnough(rs, rh);
  let shMid, hipMid, partial = false;
  if (okBoth) {
    shMid = { x: (ls.x + rs.x) / 2, y: (ls.y + rs.y) / 2 };
    hipMid = { x: (lh.x + rh.x) / 2, y: (lh.y + rh.y) / 2 };
  } else if (okLeft) {
    shMid = { x: ls.x, y: ls.y }; hipMid = { x: lh.x, y: lh.y }; partial = true;
  } else if (okRight) {
    shMid = { x: rs.x, y: rs.y }; hipMid = { x: rh.x, y: rh.y }; partial = true;
  } else {
    return null;
  }

  const dx = shMid.x - hipMid.x;
  const dy = shMid.y - hipMid.y;
  const deg = Math.abs((Math.atan2(dx, -dy) * 180) / Math.PI);

  let status = "good", msg;
  if (deg > 20)      { status = "bad";  msg = `trunk leaning ${dx > 0 ? "left" : "right"}`; }
  else if (deg > 10) { status = "warn"; msg = `trunk lean ${Math.round(deg)}°`; }

  return {
    id: "trunk_lean",
    label: partial ? "trunk (1-side)" : "trunk",
    value: Math.round(deg),
    unit: "°",
    status,
    msg,
  };
}

function checkHipDrop(lms) {
  const lh = lms[L.LEFT_HIP], rh = lms[L.RIGHT_HIP];
  if (!visibleEnough(lh, rh)) return null;
  const dy = rh.y - lh.y;            // +ve = right hip lower
  const abs = Math.abs(dy);

  let status = "good", msg;
  if (abs > 0.06)      { status = "bad";  msg = `${dy > 0 ? "right" : "left"} hip dropping (Trendelenburg)`; }
  else if (abs > 0.03) { status = "warn"; msg = `${dy > 0 ? "right" : "left"} hip slightly low`; }

  return {
    id: "hip_drop",
    label: "hip drop",
    value: +(abs * 100).toFixed(1),
    unit: "%h",
    status,
    msg,
  };
}

function checkHipSymmetry(lms) {
  // Same metric as hip_drop but bilateral context (used for glute_bridge).
  const out = checkHipDrop(lms);
  if (!out) return null;
  return { ...out, id: "hip_symmetry", label: "hip sym" };
}

function checkShoulderAbduction(side, lms, target, mode) {
  // Angle at the shoulder formed by hip-shoulder-elbow. Standing with arms
  // at sides ≈ 0° abduction (hip-shoulder-elbow ≈ 0°, but our angleAt gives
  // the interior angle so resting position ≈ 180°). Arms overhead ≈ 0°.
  // We treat "max" mode like a squat depth metric: smaller angle = closer
  // to overhead = closer to target.
  const isL = side === "L";
  const sh    = lms[isL ? L.LEFT_SHOULDER : L.RIGHT_SHOULDER];
  const hip   = lms[isL ? L.LEFT_HIP      : L.RIGHT_HIP];
  const elbow = lms[isL ? L.LEFT_ELBOW    : L.RIGHT_ELBOW];
  if (!visibleEnough(sh, hip, elbow)) return null;
  const a = angleAt(sh, hip, elbow);
  if (a == null) return null;
  // Reuse depthPercent: "max" mode treats hitting target (small angle) as good.
  const pct = depthPercent(a, target, mode);
  return {
    id: `${side}_shoulder_abduction`,
    label: `${side} shoulder`,
    value: Math.round(a),
    unit: "°",
    target,
    percent: pct,
    status: statusFromPercent(pct, mode),
    jointIdx: isL ? L.LEFT_SHOULDER : L.RIGHT_SHOULDER,
  };
}

function checkElbowAngle(side, lms) {
  // Angle at the elbow (shoulder-elbow-wrist). For wall slides we want this
  // to stay in 60-120° range — too straight (>150°) means the patient lost
  // wall contact; too bent (<45°) means they collapsed. Status reflects the
  // window, not a target percentage.
  const isL = side === "L";
  const sh    = lms[isL ? L.LEFT_SHOULDER : L.RIGHT_SHOULDER];
  const elbow = lms[isL ? L.LEFT_ELBOW    : L.RIGHT_ELBOW];
  const wrist = lms[isL ? L.LEFT_WRIST    : L.RIGHT_WRIST];
  if (!visibleEnough(sh, elbow, wrist)) return null;
  const a = angleAt(elbow, sh, wrist);
  if (a == null) return null;
  let status = "good", msg;
  if (a > 160)      { status = "warn"; msg = `${side === "L" ? "left" : "right"} elbow too straight`; }
  else if (a < 45)  { status = "warn"; msg = `${side === "L" ? "left" : "right"} elbow over-bent`; }
  return {
    id: `${side}_elbow_angle`,
    label: `${side} elbow`,
    value: Math.round(a),
    unit: "°",
    status,
    msg,
    jointIdx: isL ? L.LEFT_ELBOW : L.RIGHT_ELBOW,
  };
}

// Calf-raise body-rise tracker. Stateful across frames: holds a baseline
// hip.y from the first ~1.5s and emits a "value" representing the current
// rise as a percentage of expected travel. Reset on start() via
// resetCalfRaiseTracker(). Rep counting for this lives in
// CalfRaiseRepTracker (separate from the angle-based RepTracker because
// the kinematic signal is hip-displacement, not joint-angle).
const calfRaiseState = { baselineY: null, samples: [], peakRise: 0 };
function resetCalfRaiseTracker() {
  calfRaiseState.baselineY = null;
  calfRaiseState.samples = [];
  calfRaiseState.peakRise = 0;
}
function checkCalfRaiseRise(lms) {
  const lh = lms[L.LEFT_HIP], rh = lms[L.RIGHT_HIP];
  if (!visibleEnough(lh, rh)) return null;
  const hipY = (lh.y + rh.y) / 2;
  if (calfRaiseState.baselineY == null) {
    calfRaiseState.samples.push(hipY);
    if (calfRaiseState.samples.length >= 30) {
      // Use median of samples so a momentary tip-toe at start doesn't
      // pollute the baseline.
      const sorted = [...calfRaiseState.samples].sort((a, b) => a - b);
      calfRaiseState.baselineY = sorted[Math.floor(sorted.length / 2)];
    }
    return {
      id: "calf_rise",
      label: "rise",
      value: 0,
      unit: "%h",
      status: "idle",
      msg: "stand still to set baseline",
    };
  }
  // Lower y = higher in frame. Rise = baseline - current (positive when up).
  const riseFrac = calfRaiseState.baselineY - hipY;
  // ~5% of frame height is a strong calf raise; ~2% is mild.
  const pct = Math.max(0, Math.min(100, Math.round((riseFrac / 0.05) * 100)));
  if (pct > calfRaiseState.peakRise) calfRaiseState.peakRise = pct;
  let status = "idle";
  if (pct >= 70) status = "good";
  else if (pct >= 30) status = "warn";
  return {
    id: "calf_rise",
    label: "calf rise",
    value: pct,
    unit: "%",
    status,
  };
}

function checkSway(lms) {
  // Simple instantaneous deviation from baseline (no windowing for spike).
  const lh = lms[L.LEFT_HIP], rh = lms[L.RIGHT_HIP];
  if (!visibleEnough(lh, rh)) return null;
  const x = ((lh.x + rh.x) / 2 - 0.5) * 100;
  const abs = Math.abs(x);
  let status = "good";
  if (abs > 12) status = "bad";
  else if (abs > 6) status = "warn";
  return {
    id: "sway",
    label: "sway",
    value: +x.toFixed(1),
    unit: "%w",
    status,
  };
}

// "Presence" mode: the patient is doing an exercise where 2D BlazePose
// can't reliably resolve the joint of interest (ankle alphabet, band
// isolations, lateral hops). Rather than fabricate a degree threshold,
// we just confirm that landmarks are visible — shoulders + hips at
// minimum — and surface a single "tracking" pill. The clinician still
// gets the session row; we just don't claim rep accuracy we can't
// deliver. Returns null when nothing is visible so the SUT can show
// "step into the outline" instead of a green check on an empty frame.
function checkPresence(lms) {
  const ls = lms[L.LEFT_SHOULDER], rs = lms[L.RIGHT_SHOULDER];
  const lh = lms[L.LEFT_HIP],      rh = lms[L.RIGHT_HIP];
  if (!visibleEnough(ls, rs) && !visibleEnough(lh, rh)) return null;
  return {
    id: "presence",
    label: "tracking",
    value: null,
    unit: "",
    status: "good",
  };
}

// ---------------------------------------------------------------------------
// Per-exercise check rosters. `primary` is the headline metric; `checks` is
// the full pill list (alignment + depth, both knees, etc.).
//
// Coverage policy: each id here MUST also exist in knowledge/exercise-library.json
// — otherwise the form-check button silently fails to render on the patient's
// chat card. backend/tests/test_pose_coverage.py enforces this invariant on
// every backend run.
// ---------------------------------------------------------------------------

// Map a metric id (possibly "L_knee_valgus") to its correction key
// ("knee_valgus"). Strips a leading "L_" or "R_" so the per-exercise
// corrections map can use a single key for both sides.
function correctionKey(metricId) {
  return String(metricId).replace(/^[LR]_/, "");
}

const EXERCISES = {
  // ── Knee / quad-dominant (rep-tracked depth metrics) ────────────────
  // Mini squat: shallow, 0-45° flexion → 180-135° knee angle. Target 135°.
  mini_squat: {
    primary: "L_knee_depth", target: 135, mode: "max",
    framing: "full_body",
    checks: ["L_knee_depth", "R_knee_depth", "L_knee_valgus", "R_knee_valgus", "trunk_lean"],
    corrections: {
      knee_valgus: "Knees out, track over toes",
      knee_depth:  "Sit back a little deeper",
      trunk_lean:  "Chest up, stand tall",
    },
  },
  single_leg_squat: {
    primary: "L_knee_depth", target: 75, mode: "max",
    framing: "full_body",
    checks: ["L_knee_depth", "R_knee_depth", "L_knee_valgus", "R_knee_valgus", "trunk_lean"],
    corrections: {
      knee_valgus: "Knee out, track over toes",
      knee_depth:  "Lower under control",
      trunk_lean:  "Hips square, chest up",
    },
  },
  wall_sit: {
    primary: "L_knee_depth", target: 90, mode: "max",
    framing: "full_body",
    checks: ["L_knee_depth", "R_knee_depth", "L_knee_valgus", "R_knee_valgus", "trunk_lean"],
    corrections: {
      knee_valgus: "Knees out, press into the wall",
      knee_depth:  "Slide down to ninety degrees",
      trunk_lean:  "Back flat against the wall",
    },
  },
  heel_slides: {
    primary: "L_knee_depth", target: 100, mode: "max",
    framing: "lower_body",
    checks: ["L_knee_depth", "R_knee_depth"],
    corrections: { knee_depth: "Pull your heel a little closer" },
  },
  stationary_bike: {
    primary: "L_knee_depth", target: 90, mode: "max",
    framing: "lower_body",
    checks: ["L_knee_depth", "R_knee_depth"],
    corrections: { knee_depth: "Full pedal stroke, knee through ninety" },
  },
  terminal_knee_extension: {
    primary: "L_knee_depth", target: 0, mode: "min",
    framing: "lower_body",
    checks: ["L_knee_depth", "R_knee_depth"],
    corrections: { knee_depth: "Lock the knee straight, squeeze the quad" },
  },
  quad_sets: {
    primary: "L_knee_depth", target: 0, mode: "min",
    framing: "lower_body",
    checks: ["L_knee_depth", "R_knee_depth"],
    corrections: { knee_depth: "Tighten the quad, push the knee down" },
  },

  // ── Hip extension (glute / hamstring) ───────────────────────────────
  glute_bridge: {
    primary: "L_hip_angle", target: 170, mode: "max_extension",
    framing: "full_body",
    checks: ["L_hip_angle", "R_hip_angle", "hip_symmetry"],
    corrections: {
      hip_angle:    "Drive hips higher, squeeze the glutes",
      hip_symmetry: "Keep both hips level",
    },
  },

  // ── Hamstring / posterior chain. Walking lunge: front-leg knee ~90°,
  //    trunk upright. Same primitives as a squat but with the rep cycle
  //    driven by the front knee. Both sides tracked because the patient
  //    alternates legs across reps. ─────────────────────────────────────
  ham_walking_lunge: {
    primary: "L_knee_depth", target: 90, mode: "max",
    framing: "full_body",
    checks: ["L_knee_depth", "R_knee_depth", "trunk_lean"],
    corrections: {
      knee_depth: "Drop the back knee, ninety up front",
      trunk_lean: "Trunk upright through the lunge",
    },
  },

  // ── Lower-back stability. Bird dog is a hold, not a rep. We don't try
  //    to count reps — we just surface real-time alignment so the patient
  //    sees if their hips drop / spine sags during the hold. ─────────────
  lb_bird_dog: {
    primary: "trunk_lean", target: null, mode: "hold",
    framing: "full_body",
    checks: ["trunk_lean", "hip_symmetry", "hip_drop"],
    corrections: {
      trunk_lean:   "Keep your back flat",
      hip_symmetry: "Square your hips to the floor",
      hip_drop:     "Don't let the hip drop",
    },
  },

  // ── Calf raises. BlazePose 2D ankle tracking is too noisy for direct
  //    heel-rise angle, so the rep signal here is the body-vertical-rise
  //    (hip.y delta) state machine in checkCalfRaiseRise. Trunk-lean
  //    catches the patient cheating with a forward sway. ─────────────────
  ankle_calf_raises_double_leg: {
    primary: "calf_rise", target: null, mode: "rise",
    framing: "full_body",
    checks: ["calf_rise", "trunk_lean"],
    corrections: {
      calf_rise:  "Up onto your toes",
      trunk_lean: "Don't lean forward, stay tall",
    },
  },
  // Single-leg variant uses the same calf_rise rep tracker. Sway /
  // trunk_lean catch the most common compensations: the patient grabs
  // a wall and leans into it instead of holding their balance.
  ankle_calf_raises_single_leg: {
    primary: "calf_rise", target: null, mode: "rise",
    framing: "full_body",
    checks: ["calf_rise", "trunk_lean", "sway"],
    corrections: {
      calf_rise:  "Up onto your toe",
      trunk_lean: "Stay tall, no forward lean",
      sway:       "Center your weight",
    },
  },

  // ── Ankle balance + ROM. The remaining ankle exercises don't have a
  //    clean rep signal in 2D BlazePose: ankle_alphabet draws letters
  //    with the foot (joint angles too noisy), the band exercises are
  //    seated isolations (camera can't reliably resolve foot ROM at
  //    typical webcam framing), and lateral hops are too fast for a
  //    single-pose detector. Rather than fabricate a degree threshold,
  //    we run them in "presence" mode: confirm the patient is in frame
  //    + tracking, surface trunk_lean / sway when meaningful, and let
  //    the patient self-pace. The clinician can still see the session
  //    row in /sessions/today; we just don't claim rep accuracy we can't
  //    deliver. See checkPresence in runChecks. ──────────────────────────
  ankle_single_leg_balance: {
    primary: "sway", target: null, mode: "hold",
    framing: "full_body",
    checks: ["sway", "trunk_lean", "hip_drop"],
    corrections: {
      sway:       "Find your center",
      trunk_lean: "Chest up",
      hip_drop:   "Level the hips",
    },
  },
  ankle_alphabet: {
    primary: "presence", target: null, mode: "presence",
    framing: "feet_seated",
    checks: ["presence"],
    corrections: { presence: "Sit and point the camera at your feet" },
  },
  ankle_towel_calf_stretch: {
    primary: "presence", target: null, mode: "presence",
    framing: "feet_seated",
    checks: ["presence"],
    corrections: { presence: "Sit and point the camera at your feet" },
  },
  ankle_dorsiflexion_band: {
    primary: "presence", target: null, mode: "presence",
    framing: "feet_seated",
    checks: ["presence"],
    corrections: { presence: "Sit and point the camera at your feet" },
  },
  ankle_eversion_band: {
    primary: "presence", target: null, mode: "presence",
    framing: "feet_seated",
    checks: ["presence"],
    corrections: { presence: "Sit and point the camera at your feet" },
  },
  ankle_lateral_hops: {
    primary: "presence", target: null, mode: "presence",
    framing: "full_body",
    checks: ["presence"],
    corrections: { presence: "Stay in frame, head to feet" },
  },

  // ── Shoulder. Wall slides — track shoulder abduction (shoulder-hip-elbow
  //    angle, "max" mode toward overhead) and elbow flex/ext. Bilateral so
  //    the patient sees if one side is leading. No rep counting; the slow
  //    tempo + symmetric motion make the depth-cycle state machine unstable. ─
  shoulder_wall_slides: {
    primary: "L_shoulder_abduction", target: 160, mode: "max",
    framing: "arms_torso",
    checks: ["L_shoulder_abduction", "R_shoulder_abduction", "L_elbow_angle", "R_elbow_angle"],
    corrections: {
      shoulder_abduction: "Reach overhead along the wall",
      elbow_angle:        "Keep your elbows bent against the wall",
    },
  },
};

const DEFAULT_EX = EXERCISES.mini_squat;

// ---------------------------------------------------------------------------
// EMA smoothing + visibility timeout. Per-metric exponential moving average
// so pill values + status stop flickering. Smoothed state is dropped when a
// metric goes unseen for >0.5s so re-entry doesn't lerp from stale values.
// ---------------------------------------------------------------------------

const SMOOTH_ALPHA      = 0.35;
const SMOOTH_DROP_AFTER = 500; // ms

const smoothedById = new Map();   // id -> { value, status, lastSeenTs }

function smoothMetric(m, nowMs) {
  if (m.value == null) return m;
  const prev = smoothedById.get(m.id);
  let s;
  if (prev && (nowMs - prev.lastSeenTs) < SMOOTH_DROP_AFTER) {
    s = SMOOTH_ALPHA * m.value + (1 - SMOOTH_ALPHA) * prev.value;
  } else {
    s = m.value;
  }
  smoothedById.set(m.id, { value: s, status: m.status, lastSeenTs: nowMs });
  // Round to 1 decimal for non-integer units, 0 for degrees.
  const rounded = m.unit === "°" ? Math.round(s) : +s.toFixed(1);
  return { ...m, value: rounded };
}

function smoothMetrics(metrics, nowMs) {
  const seen = new Set();
  const out = metrics.map((m) => {
    seen.add(m.id);
    return smoothMetric(m, nowMs);
  });
  // Drop stale entries so memory doesn't grow + re-entry resets cleanly.
  for (const id of [...smoothedById.keys()]) {
    if (!seen.has(id)) {
      const e = smoothedById.get(id);
      if (nowMs - e.lastSeenTs > SMOOTH_DROP_AFTER) smoothedById.delete(id);
    }
  }
  return out;
}

function resetSmoothing() { smoothedById.clear(); }

// ---------------------------------------------------------------------------
// Rep tracking. Per depth-metric state machine:
//   idle -> descending (angle below baseline - 20°)
//        -> bottom (local trough; capture min)
//        -> ascending (rising back)
//        -> complete (within 10° of baseline) -> emit rep_complete
//
// Baseline: average angle over first ~1.5s after start(). Until baseline is
// established, no reps are emitted. Worst-status during a rep is tracked so
// the rep card row gets the right color + message.
// ---------------------------------------------------------------------------

const BASELINE_MS    = 1500;
const DESCEND_DELTA  = 20;
const COMPLETE_DELTA = 10;

class RepTracker {
  constructor(metricId, label, target, mode) {
    this.metricId = metricId;
    this.label    = label;
    this.target   = target;
    this.mode     = mode;
    this.startTs  = null;
    this.baseline = null;       // null until established
    this.baseSamples = [];
    this.state    = "idle";
    this.repCount = 0;
    this.bestDepth = null;      // for "max" mode: smallest angle reached
    this.curMin   = null;       // tracks trough during current rep
    this.curWorstStatus = "good";
    this.curWorstMsg    = null;
  }
  observe(angle, frameMetrics, ts) {
    if (angle == null) return null;
    if (this.startTs == null) this.startTs = ts;

    // Establish baseline.
    if (this.baseline == null) {
      this.baseSamples.push(angle);
      if (ts - this.startTs >= BASELINE_MS && this.baseSamples.length >= 8) {
        const avg = this.baseSamples.reduce((a, b) => a + b, 0) / this.baseSamples.length;
        this.baseline = avg;
      } else {
        return null;
      }
    }

    // Track worst alignment status seen during the current rep.
    if (this.state !== "idle") {
      for (const m of frameMetrics) {
        if (m === null || m.status === "good" || m.status === "idle") continue;
        if (statusRank(m.status) > statusRank(this.curWorstStatus)) {
          this.curWorstStatus = m.status;
          this.curWorstMsg    = m.msg || `${m.label} ${m.status}`;
        }
      }
    }

    // State machine. Only handles "max" mode (squat-style: angle drops to bottom).
    // Skip rep counting for "min" / "max_extension" / "sway" — those don't have
    // a clear rep cycle worth counting in this spike.
    if (this.mode !== "max") return null;

    if (this.state === "idle" && angle < this.baseline - DESCEND_DELTA) {
      this.state = "descending";
      this.curMin = angle;
      this.curWorstStatus = "good";
      this.curWorstMsg = null;
    } else if (this.state === "descending") {
      if (angle < this.curMin) this.curMin = angle;
      else if (angle > this.curMin + 5) this.state = "ascending";
    } else if (this.state === "ascending") {
      if (angle >= this.baseline - COMPLETE_DELTA) {
        // Rep complete.
        this.repCount += 1;
        const depthMin = this.curMin;
        if (this.bestDepth == null || depthMin < this.bestDepth) this.bestDepth = depthMin;

        const hitTarget = this.target != null ? depthMin <= this.target + 5 : true;
        let status = this.curWorstStatus;
        let msg = this.curWorstMsg;
        if (status === "good" && !hitTarget) {
          status = "warn";
          msg = `didn't reach depth (${Math.round(depthMin)}°)`;
        } else if (!msg) {
          msg = `depth ${Math.round(depthMin)}°`;
        }
        const event = {
          repNumber: this.repCount,
          metricId: this.metricId,
          label: this.label,
          depthMin: Math.round(depthMin),
          target: this.target,
          status,
          msg,
        };
        this.state = "idle";
        this.curMin = null;
        return event;
      }
    }
    return null;
  }
}

let trackers = [];   // active RepTrackers, one per depth metric, rebuilt on start

function getOrCreateTracker(metric, ex) {
  let t = trackers.find((tk) => tk.metricId === metric.id);
  if (!t) {
    t = new RepTracker(metric.id, metric.label, ex.target, ex.mode);
    trackers.push(t);
  }
  return t;
}

function isDepthMetric(m, ex) {
  return (m.id === "L_knee_depth" || m.id === "R_knee_depth" ||
          m.id === "L_hip_angle"  || m.id === "R_hip_angle") &&
         ex.target != null;
}

function trackerSummary() {
  if (!trackers.length) return null;
  // Use the side with the most reps as the headline.
  const headline = trackers.reduce((a, b) => (b.repCount > a.repCount ? b : a), trackers[0]);
  return {
    repCount:  headline.repCount,
    bestDepth: headline.bestDepth,
    label:     headline.label,
  };
}

// ---------------------------------------------------------------------------

function runChecks(lms, exId) {
  const ex = EXERCISES[exId] || DEFAULT_EX;
  const out = [];
  const seen = new Set();

  for (const ckId of ex.checks) {
    if (seen.has(ckId)) continue;
    seen.add(ckId);
    let m = null;
    if      (ckId === "L_knee_depth")  m = checkKneeAngle("L", lms, ex.target, ex.mode);
    else if (ckId === "R_knee_depth")  m = checkKneeAngle("R", lms, ex.target, ex.mode);
    else if (ckId === "L_hip_angle")   m = checkHipAngle("L", lms, ex.target, ex.mode);
    else if (ckId === "R_hip_angle")   m = checkHipAngle("R", lms, ex.target, ex.mode);
    else if (ckId === "L_knee_valgus") m = checkKneeValgus("L", lms);
    else if (ckId === "R_knee_valgus") m = checkKneeValgus("R", lms);
    else if (ckId === "trunk_lean")    m = checkTrunkLean(lms);
    else if (ckId === "hip_drop")      m = checkHipDrop(lms);
    else if (ckId === "hip_symmetry")  m = checkHipSymmetry(lms);
    else if (ckId === "sway")          m = checkSway(lms);
    else if (ckId === "L_shoulder_abduction") m = checkShoulderAbduction("L", lms, ex.target, ex.mode);
    else if (ckId === "R_shoulder_abduction") m = checkShoulderAbduction("R", lms, ex.target, ex.mode);
    else if (ckId === "L_elbow_angle") m = checkElbowAngle("L", lms);
    else if (ckId === "R_elbow_angle") m = checkElbowAngle("R", lms);
    else if (ckId === "calf_rise")     m = checkCalfRaiseRise(lms);
    else if (ckId === "presence")      m = checkPresence(lms);
    if (m) out.push(m);
  }
  return { ex, metrics: out };
}

// ---------------------------------------------------------------------------
// Drawing
// ---------------------------------------------------------------------------

const COLORS = {
  good: "rgba(80, 220, 180, 0.95)",
  warn: "rgba(245, 200, 80, 0.95)",
  bad:  "rgba(255, 100, 100, 0.95)",
  idle: "rgba(180, 180, 180, 0.7)",
};

function statusRank(s) { return s === "bad" ? 3 : s === "warn" ? 2 : s === "good" ? 1 : 0; }

function buildCheckStatusMap(metrics) {
  const m = {};
  for (const r of metrics) m[r.id] = r.status;
  return m;
}

function colorForEdge(edge, statusByCheck) {
  let worst = "idle";
  for (const ckId of edge.ck) {
    const s = statusByCheck[ckId] || "idle";
    if (statusRank(s) > statusRank(worst)) worst = s;
  }
  return COLORS[worst] || COLORS.idle;
}

function drawSkeleton(landmarks, metrics) {
  if (!ctx) return;
  const w = canvasEl.width, h = canvasEl.height;
  ctx.clearRect(0, 0, w, h);

  const statusByCheck = buildCheckStatusMap(metrics);

  // Edges
  ctx.lineWidth = 4;
  for (const edge of EDGES) {
    const pa = landmarks[edge.a], pb = landmarks[edge.b];
    if (!pa || !pb) continue;
    if ((pa.visibility ?? 1) < 0.3 || (pb.visibility ?? 1) < 0.3) continue;
    ctx.strokeStyle = edge.ck.length ? colorForEdge(edge, statusByCheck) : COLORS.good;
    ctx.beginPath();
    ctx.moveTo(pa.x * w, pa.y * h);
    ctx.lineTo(pb.x * w, pb.y * h);
    ctx.stroke();
  }

  // Joint dots
  for (const p of landmarks) {
    if (!p) continue;
    if ((p.visibility ?? 1) < 0.3) continue;
    ctx.fillStyle = COLORS.good;
    ctx.beginPath();
    ctx.arc(p.x * w, p.y * h, 4, 0, Math.PI * 2);
    ctx.fill();
  }

  // Per-joint angle labels. Compensate for the canvas's CSS scaleX(-1) so
  // text reads left-to-right in the mirrored view.
  ctx.font = "bold 16px ui-monospace, SFMono-Regular, Menlo, monospace";
  ctx.textBaseline = "middle";
  ctx.textAlign = "left";
  for (const m of metrics) {
    if (m.jointIdx == null || m.value == null) continue;
    const p = landmarks[m.jointIdx];
    if (!p || (p.visibility ?? 1) < 0.3) continue;
    const text = `${m.value}${m.unit || ""}`;
    const x = p.x * w + 14;
    const y = p.y * h;
    ctx.save();
    ctx.translate(w, 0);
    ctx.scale(-1, 1);
    ctx.fillStyle = "rgba(0,0,0,0.6)";
    const metrics2 = ctx.measureText(text);
    ctx.fillRect(x - 4, y - 11, metrics2.width + 8, 22);
    ctx.fillStyle = COLORS[m.status] || COLORS.good;
    ctx.fillText(text, x, y);
    ctx.restore();
  }
}

// ---------------------------------------------------------------------------
// Target ROM ghost guide. For "max" mode (squat-style) knee depth metrics:
// while the patient is descending or near bottom, draw a translucent dashed
// line showing where the shin should be at target ROM. Anchored at the knee
// dot. Math: rotate the thigh vector (hip→knee) by the target angle to get
// the goal shin direction, scaled by current shin length.
// ---------------------------------------------------------------------------

function drawTargetGhost(landmarks, metrics, ex) {
  if (!ctx || ex.mode !== "max") return;
  const w = canvasEl.width, h = canvasEl.height;

  for (const m of metrics) {
    if (m.id !== "L_knee_depth" && m.id !== "R_knee_depth") continue;
    if (ex.target == null || m.value == null) continue;
    if (m.value > ex.target + 60) continue;  // hide when standing tall (clutter-free)

    const isL = m.id === "L_knee_depth";
    const hip   = landmarks[isL ? L.LEFT_HIP   : L.RIGHT_HIP];
    const knee  = landmarks[isL ? L.LEFT_KNEE  : L.RIGHT_KNEE];
    const ankle = landmarks[isL ? L.LEFT_ANKLE : L.RIGHT_ANKLE];
    if (!hip || !knee || !ankle) continue;
    if ((hip.visibility ?? 1) < VIS_THRESHOLD) continue;
    if ((knee.visibility ?? 1) < VIS_THRESHOLD) continue;

    const hipPx   = { x: hip.x   * w, y: hip.y   * h };
    const kneePx  = { x: knee.x  * w, y: knee.y  * h };
    const anklePx = { x: ankle.x * w, y: ankle.y * h };

    // Thigh vector (knee->hip), to be rotated by target angle into goal-shin.
    const tx = hipPx.x - kneePx.x;
    const ty = hipPx.y - kneePx.y;
    const targetRad = (ex.target * Math.PI) / 180;
    // Sign convention: rotate the thigh (knee->hip) by (180° - target) toward
    // the patient's anterior side. For a frontal webcam we don't know
    // anterior — use the same direction the actual shin is pointing relative
    // to the thigh, just at the target magnitude.
    const shinLen = Math.hypot(anklePx.x - kneePx.x, anklePx.y - kneePx.y) || 100;
    // Cross product sign of (thigh × shin) tells which side the shin sits on.
    const sx = anklePx.x - kneePx.x;
    const sy = anklePx.y - kneePx.y;
    const crossSign = Math.sign(tx * sy - ty * sx) || 1;

    const rotateAngle = (Math.PI - targetRad) * crossSign;
    const cos = Math.cos(rotateAngle), sin = Math.sin(rotateAngle);
    const gx = tx * cos - ty * sin;
    const gy = tx * sin + ty * cos;
    const gLen = Math.hypot(gx, gy) || 1;
    const goalAnkleX = kneePx.x + (gx / gLen) * shinLen;
    const goalAnkleY = kneePx.y + (gy / gLen) * shinLen;

    ctx.save();
    ctx.strokeStyle = "rgba(140, 240, 200, 0.55)";
    ctx.lineWidth = 3;
    ctx.setLineDash([10, 6]);
    ctx.beginPath();
    ctx.moveTo(kneePx.x, kneePx.y);
    ctx.lineTo(goalAnkleX, goalAnkleY);
    ctx.stroke();
    ctx.setLineDash([]);

    // Goal-ankle pip
    ctx.fillStyle = "rgba(140, 240, 200, 0.7)";
    ctx.beginPath();
    ctx.arc(goalAnkleX, goalAnkleY, 5, 0, Math.PI * 2);
    ctx.fill();
    ctx.restore();
  }
}

// ---------------------------------------------------------------------------
// Run loop
// ---------------------------------------------------------------------------

let landmarker = null;
let visionMod   = null;
let stream      = null;
let rafHandle   = null;
let running     = false;
let videoEl     = null;
let canvasEl    = null;
let ctx         = null;
let onPayloadCb = null;
let activeExId  = "";

// Voice / rep-target state. Reset on every start().
let voiceCb            = null;
let targetReps         = null;
let halfwayAnnounced   = false;
let setCompleteFired   = false;
let lastVoiceTs        = 0;
// suppressInternalVoice: when the guided-mode wrapper in app.js takes over
// (PR-J), it owns set-complete + correction speech. We suppress the
// internal halfway/set-complete cues, and silence the per-rep count cue
// since the wrapper announces counts with set context. The wrapper's own
// correction-bubble layer is fed by checkTransitions in the payload.
let suppressInternalVoice = false;
// Per-frame check-status memory so we can emit transition events when a
// check flips from "good"/"idle" → "warn"/"bad". Reset on start().
let prevCheckStatus = {};
const VOICE_THROTTLE_MS = 600;
const NUM_WORDS = [
  "zero","one","two","three","four","five","six","seven","eight","nine","ten",
  "eleven","twelve","thirteen","fourteen","fifteen","sixteen","seventeen",
  "eighteen","nineteen","twenty",
];

function maybeSpeak(text, ts) {
  if (!voiceCb || !text) return;
  if (ts - lastVoiceTs < VOICE_THROTTLE_MS) return;
  lastVoiceTs = ts;
  try { voiceCb(text); } catch (e) { /* ignore */ }
}

function parseTargetReps(doseStr) {
  if (!doseStr) return null;
  // matches "3 x 10", "3x10", "3×10", "3 sets x 10 reps", etc.
  const m = String(doseStr).match(/(\d+)\s*[x×]\s*(\d+)/i);
  return m ? parseInt(m[2], 10) : null;
}

async function init() {
  if (landmarker) return;
  visionMod = await import(/* @vite-ignore */ VISION_CDN);
  const { PoseLandmarker, FilesetResolver } = visionMod;
  const fileset = await FilesetResolver.forVisionTasks(WASM_BASE);
  landmarker = await PoseLandmarker.createFromOptions(fileset, {
    baseOptions: { modelAssetPath: MODEL_URL, delegate: "GPU" },
    runningMode: "VIDEO",
    numPoses: 1,
  });
}

// Track partial-visibility duration for the soft "stand square" hint.
let partialSinceTs = null;
const PARTIAL_HINT_MS = 2000;

function loop() {
  if (!running) return;
  if (videoEl.readyState >= 2 && landmarker) {
    const ts = performance.now();
    const result = landmarker.detectForVideo(videoEl, ts);
    const lms = result?.landmarks?.[0];
    if (lms) {
      const { ex, metrics: rawMetrics } = runChecks(lms, activeExId);
      const metrics = smoothMetrics(rawMetrics, ts);

      // Run rep trackers on each depth metric (smoothed) and collect events.
      const repEvents = [];
      for (const m of metrics) {
        if (!isDepthMetric(m, ex)) continue;
        const tracker = getOrCreateTracker(m, ex);
        const ev = tracker.observe(m.value, metrics, ts);
        if (ev) repEvents.push(ev);
      }

      // Voice cues + set-complete detection. We use the headline tracker
      // (the side with the most reps) so two-leg exercises don't double-fire.
      const summaryNow = trackerSummary();
      let setCompleteThisFrame = false;
      if (repEvents.length && summaryNow) {
        const headlineCount = summaryNow.repCount;
        const last = repEvents[repEvents.length - 1];
        // Per-rep cue. When the guided-mode wrapper is driving (PR-J), the
        // wrapper speaks the count itself with set context, so we skip the
        // internal count + form cues here.
        if (!suppressInternalVoice) {
          if (last.status === "warn" || last.status === "bad") {
            maybeSpeak(last.msg || "form check", ts);
          } else {
            const word = NUM_WORDS[headlineCount] || String(headlineCount);
            maybeSpeak(word, ts);
          }
        }
        // Halfway one-shot.
        if (
          !suppressInternalVoice &&
          targetReps && targetReps >= 4 &&
          !halfwayAnnounced &&
          headlineCount === Math.floor(targetReps / 2)
        ) {
          halfwayAnnounced = true;
          setTimeout(() => { try { voiceCb && voiceCb("halfway"); } catch (_) {} }, 700);
        }
        // Set complete one-shot. Always raises the payload flag so the
        // wrapper drives its rest-countdown UI off it; spoken cue is
        // suppressed when the wrapper is driving voice.
        if (targetReps && !setCompleteFired && headlineCount >= targetReps) {
          setCompleteFired = true;
          setCompleteThisFrame = true;
          if (!suppressInternalVoice) {
            setTimeout(() => { try { voiceCb && voiceCb("set complete"); } catch (_) {} }, 700);
          }
        }
      }

      // Per-frame check-transitions (good/idle → warn/bad). The guided-mode
      // wrapper uses these to fire correction TTS the moment a check flips,
      // throttled per-rep on its side.
      const checkTransitions = [];
      const nextStatus = {};
      for (const m of metrics) {
        nextStatus[m.id] = m.status;
        const prev = prevCheckStatus[m.id] || "idle";
        const wasOk = prev === "good" || prev === "idle";
        const isBad = m.status === "warn" || m.status === "bad";
        if (wasOk && isBad) {
          checkTransitions.push({
            id: m.id,
            from: prev,
            to: m.status,
            label: m.label,
            msg: m.msg,
            correctionKey: correctionKey(m.id),
          });
        }
      }
      prevCheckStatus = nextStatus;

      drawSkeleton(lms, metrics);
      drawTargetGhost(lms, metrics, ex);

      if (onPayloadCb) {
        const primary = metrics.find((m) => m.id === ex.primary) || metrics[0] || null;
        const warnings = metrics
          .filter((m) => (m.status === "warn" || m.status === "bad") && m.msg)
          .map((m) => ({ id: m.id, msg: m.msg, status: m.status }));

        // Partial-side hint: too few of expected checks resolved.
        const expected = ex.checks.length;
        const got      = metrics.length;
        const partial  = got > 0 && got < Math.max(2, Math.ceil(expected / 2));
        if (partial) {
          if (partialSinceTs == null) partialSinceTs = ts;
          if (ts - partialSinceTs >= PARTIAL_HINT_MS) {
            warnings.push({ id: "stand_square", msg: "face the camera for full feedback", status: "warn" });
          }
        } else {
          partialSinceTs = null;
        }

        const summary = trackerSummary();
        // inRep: any tracker is mid-cycle (descending or ascending). The
        // wrapper uses the true → false transition (rep complete) as the
        // signal to reset the spokenCorrections set so the next rep can
        // re-speak a cue if the form error recurs.
        const inRep = trackers.some(
          (t) => t.state === "descending" || t.state === "ascending",
        );
        const exDef = EXERCISES[activeExId] || DEFAULT_EX;
        // PR-U9: framing assessment per frame so the wrapper UI can
        // render exercise-specific guidance ("point camera at your
        // ankles" vs "step into frame, head to feet"). Falls back
        // gracefully when the exercise didn't declare a framing.
        const framingStatus = exDef.framing
          ? assessFraming(lms, exDef.framing)
          : null;
        onPayloadCb({
          primary,
          metrics,
          warnings,
          repEvents,
          repSummary: summary,
          setComplete: setCompleteThisFrame,
          targetReps,
          checkTransitions,
          corrections: exDef.corrections || {},
          inRep,
          framingStatus,
        });
      }
    } else if (ctx) {
      ctx.clearRect(0, 0, canvasEl.width, canvasEl.height);
    }
  }
  rafHandle = requestAnimationFrame(loop);
}

async function start(_videoEl, _canvasEl, exerciseId, onPayload, opts = {}) {
  if (running) return;
  videoEl     = _videoEl;
  canvasEl    = _canvasEl;
  ctx         = canvasEl.getContext("2d");
  onPayloadCb = onPayload;
  activeExId  = exerciseId;

  voiceCb           = typeof opts.voice === "function" ? opts.voice : null;
  targetReps        = parseTargetReps(opts.targetDose);
  halfwayAnnounced  = false;
  setCompleteFired  = false;
  lastVoiceTs       = 0;
  // PR-J wrapper opts in to drive its own count + correction TTS.
  suppressInternalVoice = !!opts.suppressInternalVoice;
  prevCheckStatus       = {};

  resetSmoothing();
  resetCalfRaiseTracker();
  trackers       = [];
  partialSinceTs = null;

  stream = await navigator.mediaDevices.getUserMedia({
    video: { width: 640, height: 480, facingMode: "user" },
    audio: false,
  });
  videoEl.srcObject = stream;
  await videoEl.play();

  canvasEl.width  = videoEl.videoWidth  || 640;
  canvasEl.height = videoEl.videoHeight || 480;

  running = true;
  loop();
}

function stop() {
  running = false;
  if (rafHandle) cancelAnimationFrame(rafHandle);
  rafHandle = null;
  if (stream) {
    for (const track of stream.getTracks()) track.stop();
    stream = null;
  }
  if (videoEl) videoEl.srcObject = null;
  if (ctx && canvasEl) ctx.clearRect(0, 0, canvasEl.width, canvasEl.height);
  onPayloadCb = null;
  voiceCb     = null;
  targetReps  = null;
}

window.PoseFormCheck = { init, start, stop, EXERCISES, FRAMING_CONFIG, assessFraming };
