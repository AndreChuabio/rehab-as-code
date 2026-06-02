"""
Patient-name resolution on the Tavus avatar surface.

Regression guard for the "Good morning, Christian" class of bug: the greeting +
persona context must take the patient's name ONLY from the caller-supplied
canonical name (user_store.get_display_name), never from protocol.payload.patient
— that field drifts across revisions and once mis-greeted a live patient.

These tests exercise the no-API-key fallback path so they run offline and
deterministically (no Anthropic call).
"""
import context_builder


def _health():
    return {
        "sleep_hours": 7.5, "sleep_score": 82, "hrv_ms": 58, "hrv_7day_avg": 60,
        "resting_hr": 54, "recovery_score": 70, "steps_yesterday": 6000,
    }


def _protocol_with_drifted_name():
    # Simulates the real DB state: payload.patient is a stale "Christian" while
    # the canonical display name (passed in) is "Andre".
    return {"patient": "Christian", "phase": "post-op", "week": 5, "exercises": []}


def test_greeting_uses_canonical_name_not_payload(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    out = context_builder.build_system_prompt(
        _health(), events=[], protocol=_protocol_with_drifted_name(),
        patient_name="Andre",
    )
    assert "Andre" in out["greeting"]
    assert "Christian" not in out["greeting"]
    # The persona/system block must not leak the payload name either.
    assert "Christian" not in out["system_prompt"]
    assert "Patient: Andre" in out["system_prompt"]


def test_missing_name_falls_back_to_neutral_not_payload(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    out = context_builder.build_system_prompt(
        _health(), events=[], protocol=_protocol_with_drifted_name(),
        patient_name=None,
    )
    # No canonical name -> neutral fallback, never the drifted payload value.
    assert "Christian" not in out["greeting"]
    assert "Christian" not in out["system_prompt"]
    assert "there" in out["greeting"]


def test_blank_name_is_treated_as_missing(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    out = context_builder.build_system_prompt(
        _health(), events=[], protocol=_protocol_with_drifted_name(),
        patient_name="   ",
    )
    assert "Christian" not in out["system_prompt"]
    assert "Patient: the patient" in out["system_prompt"]
