// pose_record.js -- offline reference-clip recorder for the KNN classifier.
//
// Loads MediaPipe Pose Landmarker (same model as frontend/pose.js), runs it
// on the user's webcam, captures every frame's landmark array while
// "recording", and serializes the sequence to a JSON download on Save.
//
// The downloaded JSON is fed to frontend/scripts/build_pose_refs.js which
// computes embeddings and writes pose_refs/index.json. See
// frontend/pose_refs/README.md for the end-to-end workflow.
//
// Internal tool. Not linked from the patient or clinician dashboards.

const VISION_CDN =
  "https://cdn.jsdelivr.net/npm/@mediapipe/tasks-vision@0.10.14/vision_bundle.mjs";
const MODEL_URL =
  "https://storage.googleapis.com/mediapipe-models/pose_landmarker/pose_landmarker_lite/float16/1/pose_landmarker_lite.task";
const WASM_BASE =
  "https://cdn.jsdelivr.net/npm/@mediapipe/tasks-vision@0.10.14/wasm";

const $ = (id) => document.getElementById(id);

const state = {
  landmarker: null,
  videoEl: null,
  canvasEl: null,
  ctx: null,
  stream: null,
  recording: false,
  rafId: null,
  sequence: [],   // captured landmark frames
  startedAt: null,
};

function setStatus(msg) {
  if ($("recStatus")) $("recStatus").textContent = msg;
}

function getQueryDefaults() {
  const sp = new URLSearchParams(window.location.search);
  return {
    exercise: sp.get("exercise") || "",
    label: sp.get("label") || "",
    recorder: sp.get("recorder") || "",
  };
}

async function loadVision() {
  const { FilesetResolver, PoseLandmarker } = await import(VISION_CDN);
  const fileset = await FilesetResolver.forVisionTasks(WASM_BASE);
  return PoseLandmarker.createFromOptions(fileset, {
    baseOptions: { modelAssetPath: MODEL_URL, delegate: "GPU" },
    runningMode: "VIDEO",
    numPoses: 1,
    minPoseDetectionConfidence: 0.5,
    minPosePresenceConfidence: 0.5,
    minTrackingConfidence: 0.5,
    outputSegmentationMasks: false,
  });
}

async function startCamera() {
  const stream = await navigator.mediaDevices.getUserMedia({
    video: { width: 640, height: 480, facingMode: "user" },
    audio: false,
  });
  state.stream = stream;
  state.videoEl.srcObject = stream;
  await new Promise((res) => {
    state.videoEl.onloadedmetadata = () => res();
  });
  await state.videoEl.play();
  state.canvasEl.width = state.videoEl.videoWidth;
  state.canvasEl.height = state.videoEl.videoHeight;
}

function stopCamera() {
  if (state.stream) {
    for (const t of state.stream.getTracks()) t.stop();
    state.stream = null;
  }
}

function drawSkeleton(landmarks) {
  const ctx = state.ctx;
  const w = state.canvasEl.width;
  const h = state.canvasEl.height;
  ctx.clearRect(0, 0, w, h);
  ctx.fillStyle = "rgba(95, 208, 154, 0.7)";
  for (const p of landmarks) {
    if (!p) continue;
    ctx.beginPath();
    ctx.arc(p.x * w, p.y * h, 4, 0, Math.PI * 2);
    ctx.fill();
  }
}

function loop() {
  if (!state.landmarker || !state.videoEl) return;
  const ts = performance.now();
  const result = state.landmarker.detectForVideo(state.videoEl, ts);
  const landmarks = (result.landmarks && result.landmarks[0]) || null;
  if (landmarks) {
    drawSkeleton(landmarks);
    if (state.recording) {
      state.sequence.push(landmarks.map((p) => ({
        x: p.x, y: p.y, z: p.z,
        visibility: p.visibility,
      })));
    }
  }
  if (state.recording) {
    setStatus(
      `Recording...\nframes: ${state.sequence.length}\nelapsed: ${(
        (performance.now() - state.startedAt) /
        1000
      ).toFixed(1)}s`
    );
  }
  state.rafId = requestAnimationFrame(loop);
}

async function onStart() {
  if (state.recording) return;
  state.sequence = [];
  state.startedAt = performance.now();
  state.recording = true;
  $("recStartBtn").disabled = true;
  $("recStartBtn").textContent = "Stop";
  $("recSaveBtn").disabled = true;
  $("recDiscardBtn").hidden = true;
  setStatus("Recording...\nframes: 0");

  // Re-bind the start button to "stop recording" so a single click toggles.
  $("recStartBtn").onclick = onStop;
}

function onStop() {
  if (!state.recording) return;
  state.recording = false;
  $("recStartBtn").disabled = false;
  $("recStartBtn").textContent = "Start";
  $("recStartBtn").onclick = onStart;
  $("recSaveBtn").disabled = state.sequence.length < 4;
  $("recDiscardBtn").hidden = false;
  setStatus(
    `Stopped.\nframes captured: ${state.sequence.length}\n` +
      `Click Save to download, or Discard to retry.`
  );
}

function onSave() {
  if (state.sequence.length < 4) {
    setStatus("Sequence too short to save (<4 frames). Discard and retry.");
    return;
  }
  const exercise = ($("recExerciseId").value || "").trim();
  const label = ($("recLabel").value || "").trim();
  const recorder = ($("recRecorder").value || "").trim();
  if (!exercise || !label) {
    setStatus("exercise_id and label are required.");
    return;
  }
  const payload = {
    exercise_id: exercise,
    label,
    recorder: recorder || "anonymous",
    recorded_at: new Date().toISOString(),
    n_frames: state.sequence.length,
    keypoint_sequence: state.sequence,
  };
  const json = JSON.stringify(payload);
  const blob = new Blob([json], { type: "application/json" });
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  const stamp = new Date().toISOString().slice(0, 10).replace(/-/g, "");
  a.href = url;
  a.download = `${exercise}__${label}__${recorder || "anon"}__${stamp}.json`;
  document.body.appendChild(a);
  a.click();
  document.body.removeChild(a);
  URL.revokeObjectURL(url);
  setStatus(`Saved ${a.download}.\nDrop it in frontend/pose_refs/raw/.`);
}

function onDiscard() {
  state.sequence = [];
  $("recSaveBtn").disabled = true;
  $("recDiscardBtn").hidden = true;
  setStatus("Discarded. Click Start to record again.");
}

async function bootstrap() {
  state.videoEl = $("recordVideo");
  state.canvasEl = $("recordCanvas");
  state.ctx = state.canvasEl.getContext("2d");

  const defaults = getQueryDefaults();
  if (defaults.exercise) $("recExerciseId").value = defaults.exercise;
  if (defaults.label) $("recLabel").value = defaults.label;
  if (defaults.recorder) $("recRecorder").value = defaults.recorder;

  $("recStartBtn").onclick = onStart;
  $("recSaveBtn").onclick = onSave;
  $("recDiscardBtn").onclick = onDiscard;

  setStatus("Loading MediaPipe...");
  try {
    state.landmarker = await loadVision();
  } catch (e) {
    setStatus(`MediaPipe load failed: ${e.message}`);
    return;
  }
  setStatus("Requesting camera...");
  try {
    await startCamera();
  } catch (e) {
    setStatus(`Camera failed: ${e.message}`);
    return;
  }
  setStatus("Ready. Fill fields and click Start.");
  loop();
}

window.addEventListener("beforeunload", stopCamera);
bootstrap();
