"""POST /patient/interact — happy path + auth-rejected.

The endpoint forks on whether an intake row already exists. We exercise
the "no intake yet" branch (IntakeAgent runs, returns a question) which
is the one a brand-new patient hits. The plan_generation handoff branch
is exercised separately by integration tests; covering both shapes
sub-graph mocks more than is useful here.
"""
from __future__ import annotations


class _StubIntakeAgent:
    """Stand-in for IntakeAgent that returns a fixed PatientResponse."""
    name = "intake"

    async def handle(self, request):
        from agents.base import PatientResponse
        return PatientResponse(
            agent_name="intake",
            message="What's your injury?",
            next_agent=None,
            data={},
            artifacts=[],
        )


def test_patient_interact_happy_path(authed_client, fake_user_id, monkeypatch):
    captured: dict = {}

    def _ensure_user(token, slack_user_id=None):
        captured["ensure_token"] = token
        return token

    monkeypatch.setattr("main.ensure_user", _ensure_user)

    # No intake yet -> IntakeAgent path.
    monkeypatch.setattr("main.user_store.get_intake", lambda token: None)

    def _get_patient_agent(name):
        assert name == "intake"
        return _StubIntakeAgent()

    monkeypatch.setattr("agents.get_patient_agent", _get_patient_agent)

    resp = authed_client.post(
        "/patient/interact",
        json={"message": "I tweaked my knee.", "history": [], "metadata": {}},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()

    assert body["agent"] == "intake"
    assert body["intake_complete"] is False
    assert "What's your injury?" in body["message"]
    assert captured["ensure_token"] == fake_user_id


def test_patient_interact_rejects_unauthenticated(unauthed_client):
    resp = unauthed_client.post(
        "/patient/interact",
        json={"message": "hi", "history": [], "metadata": {}},
    )
    assert resp.status_code == 401, resp.text
