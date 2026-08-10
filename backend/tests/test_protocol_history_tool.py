"""Tests for the get_protocol_history read tool in coach_chat._dispatch_tool."""
import asyncio

import coach_chat


async def _dummy_executor(flow, payload):
    return {}


def test_get_protocol_history_returns_versions(monkeypatch):
    rows = [
        {
            "status": "active",
            "created_at": "2026-06-20T00:00:00Z",
            "payload": {"exercises": [{"name": "SL squat"}]},
            "created_by_agent": "coach_checkin",
        },
        {
            "status": "superseded",
            "created_at": "2026-06-10T00:00:00Z",
            "payload": {"exercises": [{"name": "Step-down"}]},
            "created_by_agent": "planner",
        },
    ]
    monkeypatch.setattr(
        coach_chat,
        "protocol_repo",
        type("R", (), {"list_by_token": staticmethod(lambda token, **k: rows)})(),
    )
    result, events = asyncio.run(
        coach_chat._dispatch_tool(
            "get_protocol_history", {}, _dummy_executor, user_token="tok"
        )
    )
    assert len(result["versions"]) == 2
    assert result["versions"][0]["status"] == "active"


def test_get_protocol_history_no_token(monkeypatch):
    """Without a user token, the tool returns an error dict and no events."""
    result, events = asyncio.run(
        coach_chat._dispatch_tool(
            "get_protocol_history", {}, _dummy_executor, user_token=None
        )
    )
    assert result["ok"] is False
    assert "no authenticated patient" in result["error"]


def test_get_protocol_history_exercise_names(monkeypatch):
    """Exercise names in the payload are projected into the versions list."""
    rows = [
        {
            "status": "active",
            "created_at": "2026-06-20T00:00:00Z",
            "payload": {"exercises": [{"name": "SL squat"}, {"name": "Wall sit"}]},
            "created_by_agent": "planner",
        },
    ]
    monkeypatch.setattr(
        coach_chat,
        "protocol_repo",
        type("R", (), {"list_by_token": staticmethod(lambda token, **k: rows)})(),
    )
    result, events = asyncio.run(
        coach_chat._dispatch_tool(
            "get_protocol_history", {}, _dummy_executor, user_token="tok"
        )
    )
    assert result["versions"][0]["exercises"] == ["SL squat", "Wall sit"]
    assert result["versions"][0]["why"] == "planner"


def test_get_protocol_history_capped_at_eight(monkeypatch):
    """Only the 8 most recent versions are returned."""
    rows = [
        {
            "status": "superseded",
            "created_at": f"2026-0{i + 1}-01T00:00:00Z",
            "payload": {"exercises": []},
            "created_by_agent": "planner",
        }
        for i in range(9)
    ]
    monkeypatch.setattr(
        coach_chat,
        "protocol_repo",
        type("R", (), {"list_by_token": staticmethod(lambda token, **k: rows)})(),
    )
    result, events = asyncio.run(
        coach_chat._dispatch_tool(
            "get_protocol_history", {}, _dummy_executor, user_token="tok"
        )
    )
    assert len(result["versions"]) == 8


def test_get_protocol_history_empty_payload(monkeypatch):
    """Rows with None or missing payload do not crash."""
    rows = [
        {
            "status": "pending_review",
            "created_at": "2026-06-01T00:00:00Z",
            "payload": None,
            "created_by_agent": None,
        },
    ]
    monkeypatch.setattr(
        coach_chat,
        "protocol_repo",
        type("R", (), {"list_by_token": staticmethod(lambda token, **k: rows)})(),
    )
    result, events = asyncio.run(
        coach_chat._dispatch_tool(
            "get_protocol_history", {}, _dummy_executor, user_token="tok"
        )
    )
    assert len(result["versions"]) == 1
    assert result["versions"][0]["exercises"] == []
