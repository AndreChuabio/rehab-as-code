/*
 * pose_classifier.js -- KNN form-quality classifier on MediaPipe pose embeddings.
 *
 * Runtime is a tiny pure-JS module. No deps. Loads `pose_refs/index.json`
 * once at boot and serves classify() calls from in-memory KNN against the
 * cosine distance.
 *
 * Wire-in (separate PR): `frontend/pose.js` calls
 *   poseClassifier.classify(repKeypointSequence, exerciseId)
 * after each completed rep when the URL has `?pose_knn=1`. The result is
 * piped into the per-rep card and (optionally) the pose_metrics payload
 * sent to /pose/session.
 *
 * Schema of pose_refs/index.json (built by frontend/scripts/build_pose_refs.js):
 *   {
 *     "<exercise_id>": [
 *        { "label": "good" | "bad", "embedding": number[1980] }, ...
 *     ],
 *     ...
 *   }
 *
 * Embedding shape: 20 frames * 33 landmarks * 3 dims = 1980. See
 * `embedRep()` for the exact normalization. The classifier and the
 * builder MUST stay in lockstep on this; if you change one, change both.
 */

(function () {
  "use strict";

  // BlazePose 33-point output. Each landmark has {x, y, z, visibility}.
  // We keep x/y/z, drop visibility (low-confidence frames already filter
  // upstream in pose.js).
  const N_LANDMARKS = 33;
  const N_DIMS = 3;
  const FRAMES_PER_EMBEDDING = 20;
  const EMBEDDING_LEN = FRAMES_PER_EMBEDDING * N_LANDMARKS * N_DIMS;

  // KNN config. k=3 is reasonable when reference set is small (~10 per
  // class). Tune up to k=5 once each exercise has >=15 references.
  const DEFAULT_K = 3;

  // Normalization origin: the midpoint between left + right hip. Each
  // landmark is translated so the hip-mid is at (0, 0, 0). Then we scale
  // by torso length (hip-mid to shoulder-mid) so different patient
  // heights collapse onto the same range. This is the standard
  // BlazePose embedding pre-step.
  const LEFT_HIP = 23;
  const RIGHT_HIP = 24;
  const LEFT_SHOULDER = 11;
  const RIGHT_SHOULDER = 12;

  let _refs = null; // { exercise_id: [ {label, embedding} ] }
  let _refsPromise = null;

  /**
   * Load the reference index from /static/pose_refs/index.json. Cached
   * for the page lifetime. Resolves to {} if the file is missing or
   * empty so the runtime can still be called - it just always returns
   * status="no_refs".
   */
  function loadRefs(url) {
    if (_refsPromise) return _refsPromise;
    const fetchUrl = url || "/static/pose_refs/index.json";
    _refsPromise = fetch(fetchUrl)
      .then((r) => (r.ok ? r.json() : {}))
      .catch(() => ({}))
      .then((json) => {
        _refs = json || {};
        return _refs;
      });
    return _refsPromise;
  }

  /**
   * Reset the module-level cache. Used by the recording UI when it
   * wants to round-trip a freshly built index without reloading the page.
   */
  function resetCache() {
    _refs = null;
    _refsPromise = null;
  }

  /**
   * Convert one frame of MediaPipe landmarks into a flat normalized
   * vector of length 33*3 = 99.
   *
   * Returns null if the frame's hip / shoulder anchors are not visible
   * enough to compute a stable scale. Caller should drop the frame.
   */
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

    const dx = sx - ox;
    const dy = sy - oy;
    const dz = sz - oz;
    const torso = Math.sqrt(dx * dx + dy * dy + dz * dz);
    if (!isFinite(torso) || torso < 1e-6) return null;

    const out = new Float32Array(N_LANDMARKS * N_DIMS);
    for (let i = 0; i < N_LANDMARKS; i++) {
      const p = landmarks[i] || { x: ox, y: oy, z: oz };
      out[i * 3 + 0] = (p.x - ox) / torso;
      out[i * 3 + 1] = (p.y - oy) / torso;
      out[i * 3 + 2] = ((p.z || 0) - oz) / torso;
    }
    return out;
  }

  /**
   * Sample N evenly-spaced frames from a sequence and concatenate their
   * per-frame embeddings into a single 1980-dim vector.
   *
   * `sequence` is an array of MediaPipe `landmarks` arrays (one per frame
   * captured during the rep). Sequences shorter than 4 frames return
   * null - we won't classify a rep we couldn't observe.
   */
  function embedRep(sequence) {
    if (!Array.isArray(sequence) || sequence.length < 4) return null;
    const indices = sampleIndices(sequence.length, FRAMES_PER_EMBEDDING);
    const out = new Float32Array(EMBEDDING_LEN);
    let written = 0;
    for (const idx of indices) {
      const frame = embedFrame(sequence[idx]);
      if (!frame) {
        // Reuse the last successful frame if a single frame fails - the
        // sequence is generally smooth, so a one-frame interpolation is
        // safer than discarding the rep entirely.
        if (written === 0) return null;
        out.set(out.subarray(written - N_LANDMARKS * N_DIMS, written),
                written);
      } else {
        out.set(frame, written);
      }
      written += N_LANDMARKS * N_DIMS;
    }
    return out;
  }

  function sampleIndices(total, count) {
    if (count >= total) return Array.from({ length: total }, (_, i) => i);
    const out = [];
    for (let i = 0; i < count; i++) {
      out.push(Math.floor((i * total) / count));
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

  /**
   * Classify a rep's keypoint sequence against the loaded reference index.
   *
   * Returns:
   *   { status: "ok", label, confidence, k, refs_per_class }   - successful KNN
   *   { status: "no_refs" }                                    - no clips for this exercise
   *   { status: "no_sequence" }                                - sequence too short / unembeddable
   *   { status: "no_index" }                                   - index never loaded
   *
   * `confidence` is votes-for-winning-label / k.
   */
  async function classify(sequence, exerciseId, opts) {
    const k = (opts && opts.k) || DEFAULT_K;
    if (!_refs) {
      try { await loadRefs(); }
      catch (_) { return { status: "no_index" }; }
    }
    const exRefs = _refs && _refs[exerciseId];
    if (!Array.isArray(exRefs) || exRefs.length === 0) {
      return { status: "no_refs" };
    }
    const query = embedRep(sequence);
    if (!query) return { status: "no_sequence" };

    const distances = exRefs.map((r) => ({
      label: r.label,
      d: cosineDistance(query, Float32Array.from(r.embedding || [])),
    }));
    distances.sort((a, b) => a.d - b.d);
    const top = distances.slice(0, Math.min(k, distances.length));
    const votes = {};
    for (const t of top) votes[t.label] = (votes[t.label] || 0) + 1;
    let winner = null, winnerVotes = 0;
    for (const [label, v] of Object.entries(votes)) {
      if (v > winnerVotes) { winner = label; winnerVotes = v; }
    }
    return {
      status: "ok",
      label: winner,
      confidence: winnerVotes / top.length,
      k: top.length,
      refs_per_class: exRefs.length,
    };
  }

  // Public surface. Keep the export minimal so wire-in is cheap to read.
  window.poseClassifier = {
    loadRefs,
    resetCache,
    classify,
    embedFrame,    // exposed for the recording page
    embedRep,      // exposed for the recording page
    EMBEDDING_LEN, // exposed for sanity checks in the build script
    FRAMES_PER_EMBEDDING,
    N_LANDMARKS,
  };
})();
