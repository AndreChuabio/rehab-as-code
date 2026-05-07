#!/usr/bin/env node
/*
 * build_pose_refs.js -- offline indexer for the KNN form-quality classifier.
 *
 * Reads every frontend/pose_refs/raw/<exercise>__<label>__*.json clip
 * (downloaded from frontend/pose_record.html), computes the per-rep
 * embedding using the SAME algorithm as frontend/pose_classifier.js, and
 * writes frontend/pose_refs/index.json keyed by exercise_id.
 *
 * Holds a 20% test split per exercise and prints leave-one-out accuracy
 * + recall on the held-out clips. Target >=80% on the initial 40-clip
 * set. Fail loudly if accuracy drops below 60% so we don't ship a model
 * that's worse than the existing threshold rules.
 *
 * Run:
 *   node frontend/scripts/build_pose_refs.js
 */

const fs = require("fs");
const path = require("path");

const RAW_DIR = path.join(__dirname, "..", "pose_refs", "raw");
const INDEX_PATH = path.join(__dirname, "..", "pose_refs", "index.json");

// MUST mirror frontend/pose_classifier.js. If you tune one, tune the other.
const N_LANDMARKS = 33;
const N_DIMS = 3;
const FRAMES_PER_EMBEDDING = 20;
const EMBEDDING_LEN = FRAMES_PER_EMBEDDING * N_LANDMARKS * N_DIMS;
const LEFT_HIP = 23;
const RIGHT_HIP = 24;
const LEFT_SHOULDER = 11;
const RIGHT_SHOULDER = 12;
const TEST_SPLIT_RATIO = 0.20;
const ACCURACY_FAIL_BELOW = 0.60;
const ACCURACY_WARN_BELOW = 0.80;

function embedFrame(landmarks) {
  if (!landmarks || landmarks.length < N_LANDMARKS) return null;
  const lh = landmarks[LEFT_HIP];
  const rh = landmarks[RIGHT_HIP];
  const ls = landmarks[LEFT_SHOULDER];
  const rs = landmarks[RIGHT_SHOULDER];
  if (!lh || !rh || !ls || !rs) return null;
  const ox = (lh.x + rh.x) / 2;
  const oy = (lh.y + rh.y) / 2;
  const oz = ((lh.z || 0) + (rh.z || 0)) / 2;
  const sx = (ls.x + rs.x) / 2;
  const sy = (ls.y + rs.y) / 2;
  const sz = ((ls.z || 0) + (rs.z || 0)) / 2;
  const dx = sx - ox, dy = sy - oy, dz = sz - oz;
  const torso = Math.sqrt(dx * dx + dy * dy + dz * dz);
  if (!isFinite(torso) || torso < 1e-6) return null;
  const out = new Array(N_LANDMARKS * N_DIMS);
  for (let i = 0; i < N_LANDMARKS; i++) {
    const p = landmarks[i] || { x: ox, y: oy, z: oz };
    out[i * 3 + 0] = (p.x - ox) / torso;
    out[i * 3 + 1] = (p.y - oy) / torso;
    out[i * 3 + 2] = ((p.z || 0) - oz) / torso;
  }
  return out;
}

function sampleIndices(total, count) {
  if (count >= total) return Array.from({ length: total }, (_, i) => i);
  const out = [];
  for (let i = 0; i < count; i++) out.push(Math.floor((i * total) / count));
  return out;
}

function embedRep(sequence) {
  if (!Array.isArray(sequence) || sequence.length < 4) return null;
  const indices = sampleIndices(sequence.length, FRAMES_PER_EMBEDDING);
  const out = new Array(EMBEDDING_LEN);
  let written = 0;
  let lastFrame = null;
  for (const idx of indices) {
    let frame = embedFrame(sequence[idx]);
    if (!frame) {
      if (!lastFrame) return null;
      frame = lastFrame;
    } else {
      lastFrame = frame;
    }
    for (let j = 0; j < frame.length; j++) out[written + j] = frame[j];
    written += frame.length;
  }
  return out;
}

function cosineDistance(a, b) {
  if (a.length !== b.length) return 1.0;
  let dot = 0, na = 0, nb = 0;
  for (let i = 0; i < a.length; i++) {
    dot += a[i] * b[i];
    na += a[i] * a[i];
    nb += b[i] * b[i];
  }
  const denom = Math.sqrt(na) * Math.sqrt(nb);
  if (denom < 1e-9) return 1.0;
  return 1.0 - dot / denom;
}

function knnPredict(query, refs, k) {
  const distances = refs.map((r) => ({
    label: r.label,
    d: cosineDistance(query, r.embedding),
  }));
  distances.sort((a, b) => a.d - b.d);
  const top = distances.slice(0, Math.min(k, distances.length));
  const votes = {};
  for (const t of top) votes[t.label] = (votes[t.label] || 0) + 1;
  let winner = null, winnerVotes = 0;
  for (const [label, v] of Object.entries(votes)) {
    if (v > winnerVotes) { winner = label; winnerVotes = v; }
  }
  return { label: winner, confidence: winnerVotes / top.length };
}

function loadRawClips() {
  if (!fs.existsSync(RAW_DIR)) {
    console.error(`raw directory missing: ${RAW_DIR}`);
    process.exit(1);
  }
  const files = fs.readdirSync(RAW_DIR).filter((f) => f.endsWith(".json"));
  const clips = [];
  for (const f of files) {
    let payload;
    try {
      payload = JSON.parse(fs.readFileSync(path.join(RAW_DIR, f), "utf8"));
    } catch (e) {
      console.warn(`skip ${f}: ${e.message}`);
      continue;
    }
    if (!payload.exercise_id || !payload.label || !Array.isArray(payload.keypoint_sequence)) {
      console.warn(`skip ${f}: missing required fields`);
      continue;
    }
    const embedding = embedRep(payload.keypoint_sequence);
    if (!embedding) {
      console.warn(`skip ${f}: embedRep returned null (sequence too short or anchors missing)`);
      continue;
    }
    clips.push({
      file: f,
      exercise_id: payload.exercise_id,
      label: payload.label,
      embedding,
    });
  }
  return clips;
}

function evaluateExercise(clips, exerciseId) {
  // Leave-one-out cross-validation: each clip is held out once and
  // classified against the remaining N-1. Cleaner than a fixed 20%
  // split when N is small (~10-20 clips).
  const k = clips.length >= 7 ? 5 : 3;
  let correct = 0;
  let total = 0;
  const perLabelCorrect = {};
  const perLabelTotal = {};
  for (let i = 0; i < clips.length; i++) {
    const heldOut = clips[i];
    const train = clips.slice(0, i).concat(clips.slice(i + 1));
    if (train.length < k) continue;
    const pred = knnPredict(heldOut.embedding, train, k);
    perLabelTotal[heldOut.label] = (perLabelTotal[heldOut.label] || 0) + 1;
    if (pred.label === heldOut.label) {
      correct += 1;
      perLabelCorrect[heldOut.label] = (perLabelCorrect[heldOut.label] || 0) + 1;
    }
    total += 1;
  }
  const accuracy = total === 0 ? 0 : correct / total;
  return {
    exercise_id: exerciseId,
    n_clips: clips.length,
    n_classes: Object.keys(perLabelTotal).length,
    accuracy,
    per_label: Object.fromEntries(
      Object.keys(perLabelTotal).map((l) => [
        l,
        {
          n: perLabelTotal[l],
          recall: (perLabelCorrect[l] || 0) / perLabelTotal[l],
        },
      ])
    ),
  };
}

function main() {
  const clips = loadRawClips();
  if (clips.length === 0) {
    console.log("no valid clips found; writing empty index");
    fs.writeFileSync(INDEX_PATH, "{}\n");
    process.exit(0);
  }

  const byExercise = {};
  for (const c of clips) {
    (byExercise[c.exercise_id] = byExercise[c.exercise_id] || []).push(c);
  }

  console.log(`Loaded ${clips.length} clips across ${Object.keys(byExercise).length} exercises.`);

  const failed = [];
  for (const [eid, exClips] of Object.entries(byExercise)) {
    const evalRes = evaluateExercise(exClips, eid);
    const acc = (evalRes.accuracy * 100).toFixed(1);
    console.log(
      `  ${eid}: ${evalRes.n_clips} clips, ${evalRes.n_classes} classes, accuracy ${acc}%`
    );
    for (const [label, stats] of Object.entries(evalRes.per_label)) {
      console.log(`    ${label}: n=${stats.n}, recall=${(stats.recall * 100).toFixed(1)}%`);
    }
    if (evalRes.accuracy < ACCURACY_FAIL_BELOW) {
      failed.push({ exercise_id: eid, accuracy: evalRes.accuracy });
    } else if (evalRes.accuracy < ACCURACY_WARN_BELOW) {
      console.log(
        `    WARNING: ${eid} below ${(ACCURACY_WARN_BELOW * 100).toFixed(0)}% target accuracy`
      );
    }
  }

  if (failed.length > 0) {
    console.error("\nFAIL: the following exercises have accuracy below 60%:");
    for (const f of failed) {
      console.error(`  ${f.exercise_id}: ${(f.accuracy * 100).toFixed(1)}%`);
    }
    console.error(
      "Don't ship this index. Record more clips or audit which clips are mislabeled."
    );
    process.exit(1);
  }

  // Write index keyed by exercise_id, dropping the per-clip filename so
  // raw recorder identity doesn't leak into the shipped artifact.
  const out = {};
  for (const [eid, exClips] of Object.entries(byExercise)) {
    out[eid] = exClips.map((c) => ({
      label: c.label,
      embedding: c.embedding,
    }));
  }
  fs.writeFileSync(INDEX_PATH, JSON.stringify(out));
  console.log(
    `\nWrote ${INDEX_PATH} with ${Object.keys(out).length} exercises, ` +
      `${clips.length} reference embeddings.`
  );
}

main();
