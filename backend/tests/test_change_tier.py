import change_tier as ct

KNEE = "knee"
def _p(region, exs):  # exs: list of (id, load)
    return {"body_region": region,
            "exercises": [{"exercise_id": i, "load": l} for i, l in exs]}

def test_regression_swap_in_region_clean_is_auto():
    prior = _p(KNEE, [("knee_sl_squat", 20)])
    draft = _p(KNEE, [("knee_step_down", 0)])  # easier, no new id beyond library
    assert ct.classify(prior, draft, []) == "auto"

def test_load_decrease_is_auto():
    prior = _p(KNEE, [("knee_sl_squat", 20)])
    draft = _p(KNEE, [("knee_sl_squat", 10)])
    assert ct.classify(prior, draft, []) == "auto"

def test_load_increase_is_gate():
    prior = _p(KNEE, [("knee_sl_squat", 20)])
    draft = _p(KNEE, [("knee_sl_squat", 30)])
    assert ct.classify(prior, draft, []) == "gate"

def test_brand_new_exercise_added_is_gate():
    prior = _p(KNEE, [("knee_sl_squat", 20)])
    draft = _p(KNEE, [("knee_sl_squat", 20), ("knee_hop", 0)])
    assert ct.classify(prior, draft, []) == "gate"

def test_high_severity_safety_is_gate():
    prior = _p(KNEE, [("knee_sl_squat", 20)])
    draft = _p(KNEE, [("knee_step_down", 0)])
    concerns = [{"check": "pain_ceiling", "severity": "high", "detail": "x"}]
    assert ct.classify(prior, draft, concerns) == "gate"

def test_out_of_region_is_gate():
    prior = _p("shoulder", [("sh_press", 5)])
    draft = _p("shoulder", [("sh_press", 3)])
    assert ct.classify(prior, draft, []) == "gate"

def test_missing_prior_is_gate():
    # No active plan yet = brand-new plan of care = clinician-owned.
    assert ct.classify(None, _p(KNEE, [("a", 0)]), []) == "gate"

def test_exception_defaults_to_gate():
    assert ct.classify({"exercises": "not-a-list"}, _p(KNEE, []), []) == "gate"
