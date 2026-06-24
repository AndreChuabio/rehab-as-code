"""Lock the SESSION_RULES rewrite + session_ref embedding.

SESSION_RULES used to be the wellness-coach scaffold (TCM tongue reading,
affirmations, the salamander exercise). It is now physical-therapy doctrine
for Coach Maya. These tests assert the wellness scaffold is gone, PT doctrine
is present, the project style rules hold (no emoji, no exclamation mark), and
create_conversation embeds the opaque session_ref the proxy recovers.
"""
from __future__ import annotations

import tavus_client


_WELLNESS_TERMS = ["salamander", "tongue", "Chinese medicine", "affirmation", "visualization"]
_PT_TERMS = ["clinician", "pain", "library"]


def test_wellness_scaffold_removed():
    rules_lower = tavus_client.SESSION_RULES.lower()
    for term in _WELLNESS_TERMS:
        assert term.lower() not in rules_lower, f"stale wellness term present: {term}"


def test_pt_doctrine_present():
    rules_lower = tavus_client.SESSION_RULES.lower()
    for term in _PT_TERMS:
        assert term.lower() in rules_lower, f"missing PT term: {term}"
    # ROM doctrine, either spelling.
    assert "rom" in rules_lower or "range of motion" in rules_lower


def test_no_emoji_no_exclamation():
    rules = tavus_client.SESSION_RULES
    assert "!" not in rules
    # No non-ASCII (catches emoji); the rules are plain English ASCII.
    assert rules.isascii(), "SESSION_RULES should be ASCII-only (no emoji)"


def test_create_conversation_embeds_session_ref(monkeypatch):
    captured: dict = {}

    class _FakeResp:
        status_code = 200

        @staticmethod
        def json():
            return {
                "conversation_url": "https://tavus.daily.co/xyz",
                "conversation_id": "conv_xyz",
                "status": "active",
            }

        text = ""

    def _fake_post(url, headers=None, json=None, timeout=None):  # noqa: A002
        captured["payload"] = json
        return _FakeResp()

    monkeypatch.setattr("tavus_client.requests.post", _fake_post)
    monkeypatch.setenv("TAVUS_API_KEY", "k")
    monkeypatch.setenv("TAVUS_REPLICA_ID", "rep_1")
    monkeypatch.setenv("TAVUS_PERSONA_ID", "per_1")

    out = tavus_client.create_conversation(
        system_prompt="SP", greeting="G", user_name="Andre", session_ref="ref_xyz",
    )
    assert out["conversation_id"] == "conv_xyz"

    context = captured["payload"]["conversational_context"]
    assert "[RAC_SESSION_REF]: ref_xyz" in context
    # And the PT SESSION_RULES rode along, not the wellness scaffold.
    assert "salamander" not in context.lower()


def test_create_conversation_omits_ref_when_absent(monkeypatch):
    captured: dict = {}

    class _FakeResp:
        status_code = 200

        @staticmethod
        def json():
            return {
                "conversation_url": "https://tavus.daily.co/xyz",
                "conversation_id": "conv_xyz",
                "status": "active",
            }

        text = ""

    def _fake_post(url, headers=None, json=None, timeout=None):  # noqa: A002
        captured["payload"] = json
        return _FakeResp()

    monkeypatch.setattr("tavus_client.requests.post", _fake_post)
    monkeypatch.setenv("TAVUS_API_KEY", "k")
    monkeypatch.setenv("TAVUS_REPLICA_ID", "rep_1")
    monkeypatch.setenv("TAVUS_PERSONA_ID", "per_1")

    tavus_client.create_conversation(system_prompt="SP", greeting="G")
    assert "RAC_SESSION_REF" not in captured["payload"]["conversational_context"]
