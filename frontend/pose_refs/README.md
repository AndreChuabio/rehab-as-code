# Pose KNN reference clips

How Andre + Nikki record clips and build the index that drives
`pose_classifier.js`.

## Recording

1. Open `https://<deploy>/pose_record.html?exercise=<id>&label=<good|bad>` (the page is also reachable locally at `frontend/pose_record.html` via `python -m http.server`).
2. Stand in frame, click **Start**, perform one rep.
3. Click **Save**. A JSON file downloads with shape:

```json
{
  "exercise_id": "mini_squat",
  "label": "good",
  "recorded_at": "2026-05-07T17:14:23.000Z",
  "recorder": "andre",
  "keypoint_sequence": [ [ {x,y,z,visibility}, ... 33 landmarks ], ... ]
}
```

4. Move the file into `frontend/pose_refs/raw/` with name
   `<exercise_id>__<label>__<recorder>__<YYYYMMDD>.json`.

Target initial set: 5 clips × {good, bad} × {mini_squat, wall_sit,
ankle_calf_raises_single_leg, ankle_single_leg_balance} = 40 clips.
Andre + Nikki record all of them in one afternoon.

## Building the index

```
node frontend/scripts/build_pose_refs.js
```

The script:
- Reads every `frontend/pose_refs/raw/*.json`.
- Validates each clip has `exercise_id`, `label`, `keypoint_sequence`.
- Computes the per-rep embedding (same algorithm as
  `frontend/pose_classifier.js#embedRep` - they're written to stay in
  lockstep; if you change one, change both).
- Writes `frontend/pose_refs/index.json` keyed by `exercise_id`.
- Holds a 20% test split, prints accuracy + recall on the held-out
  clips. Target ≥80% on the initial 40-clip set; if below, more clips
  are needed before turning the patient-side flag on by default.

## Hygiene

- Raw clips contain pose keypoints which are **not** PHI by themselves
  (no faces, no names) BUT the recording was made on a person's body.
  Treat them with the same caution as any patient-derived data.
- Never push raw clips to git that contain a patient's actual recovery
  session. The whole reason to do this in-house is that Andre + Nikki
  control the data.
- `index.json` is committed; it ships with the frontend. The raw
  directory has a `.gitignore`-style stance via `.gitkeep` only.
