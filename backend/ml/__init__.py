"""ML / stats package for rehab-as-code.

Layout:
  ml/
    adherence/   - 14-day dropout risk score (heuristic today, XGBoost later).

Convention: each task lives in its own subpackage with extract / features /
heuristic / predict / artifacts. The public entry point is always
`<task>.predict.score(token)` so endpoints don't care which layer (rules
vs trained model) is currently active.

See ml/README.md for how to run notebooks, where artifacts go, and how to
swap heuristics for trained models.
"""
