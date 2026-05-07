"""Adherence / 14-day dropout risk for rehab-as-code patients.

Public surface: `from ml.adherence import predict; predict.score(token)`.

The endpoint hits `predict.score()`; the score function decides internally
whether to run the heuristic (`_LAYER = "heuristic"`) or load a joblib
classifier (`_LAYER = "xgb"`). Same return shape either way.
"""
