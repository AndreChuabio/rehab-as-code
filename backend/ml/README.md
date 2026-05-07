# `backend/ml/` — ML / stats for rehab-as-code

Per-task subpackages. Each task ships in two layers:

- **Layer 1 — heuristic.** Pure rules, no training. Ships day 1, interpretable, no labels needed.
- **Layer 2 — learned model.** Same feature pipeline; a trained classifier replaces the rules. Activated when production data accumulates.

The split is the point: feature extraction is shared, the endpoint never knows which layer is active.

## Adherence

```
ml/adherence/
  extract.py       # Pull per-patient snapshot from Supabase via existing repos
  features.py      # Pure functions on a snapshot dict
  heuristic.py     # Layer 1: rules-based risk score
  predict.py       # score(token) -> {risk, band, factors, layer}
  notebooks/
    eda.ipynb         # EDA on production data once it accumulates
    train_xgb.ipynb   # Layer 2 training notebook (stub)
  artifacts/
    .gitkeep         # joblib model files land here when Layer 2 ships
```

### Updating heuristic thresholds

Open `heuristic.py`. Constants are at the top of the module. Update, run `pytest backend/tests/test_risk_cohort.py`, commit.

### Swapping in a trained model

When `notebooks/train_xgb.ipynb` produces a `model.joblib` artifact you want to ship:

1. Drop the artifact in `ml/adherence/artifacts/model_<YYYYMMDD>.joblib`. Use a date suffix; never overwrite a previous artifact in place.
2. In `predict.py`, set `_LAYER` to `"xgb"` and update `_load_model()` to point at the new filename.
3. The endpoint, the heuristic, and the tests don't change — `score(token)` returns the same shape regardless of layer.
4. Add an A/B harness test that asserts the trained model's precision@k on the held-out set is ≥ heuristic's precision@k. If it isn't, don't ship.

### Why heuristic first

No labeled data exists at launch. The heuristic encodes Andre + Nikki's clinical priors as rules clinicians can read and audit. The same feature pipeline that powers the heuristic feeds the eventual XGBoost, so the upgrade is one PR rather than a rebuild.
