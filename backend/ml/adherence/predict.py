"""
predict.py - public entry point for adherence scoring.

`score(token)` is what the /clinician/risk-cohort endpoint hits. It runs
extract -> features -> heuristic (today) and returns a fixed-shape dict.

When the trained-model layer ships:
  1. Drop a model.joblib in artifacts/.
  2. Set `_LAYER = "xgb"` and update `_load_model()`.
  3. The endpoint, the heuristic, and the tests don't change. The dict
     shape is the contract.

PHI hygiene: the returned dict carries `token` (which IS the patient's
auth.uid()). Callers must NEVER log the full result. Logs in this module
emit only `(token_hash, band)` - same convention as classify_freetext in
clinical_taxonomy.
"""
from __future__ import annotations

import hashlib
import logging
from typing import Any

logger = logging.getLogger(__name__)

# Active layer. Flip to "xgb" + update _load_model when artifacts/ has a
# trained classifier we trust.
_LAYER = "heuristic"


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()[:12]


def score(token: str) -> dict[str, Any]:
    """Compute the dropout-risk record for one patient.

    Returns
    -------
    dict
      token            : str   (the patient's auth.uid - PHI)
      risk             : float 0-1
      band             : "high" | "med" | "low"
      factors          : list[dict] - which rules / features fired
      layer            : "heuristic" | "xgb"
      patient_name     : str | None  - for the dashboard
      injury_category  : str | None
      snapshot_at      : str ISO8601 UTC
    """
    from . import extract
    from . import features as feat
    from . import heuristic

    snap = extract.snapshot(token)
    features = feat.compute(snap)

    if _LAYER == "heuristic":
        result = heuristic.score(features)
    else:
        # Reserved for future XGBoost path. Until artifacts/ has a real
        # model file we keep this dead - see ml/README.md for the swap
        # protocol.
        result = heuristic.score(features)
        result["layer"] = _LAYER

    user = snap.get("user") or {}
    out = {
        "token": token,
        "risk": result["risk"],
        "band": result["band"],
        "factors": result["factors"],
        "layer": result["layer"],
        "patient_name": user.get("patient_name"),
        "injury_category": user.get("injury_category") or features.get("injury_category"),
        "snapshot_at": snap["snapshot_at"],
    }
    logger.info(
        "adherence score token_hash=%s band=%s risk=%.2f n_factors=%d layer=%s",
        _hash_token(token), out["band"], out["risk"],
        len(out["factors"]), out["layer"],
    )
    return out
