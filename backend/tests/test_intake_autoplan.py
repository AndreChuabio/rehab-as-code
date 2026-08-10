"""Task 7: conversational new-intake auto-triggers gated plan generation.

A complete mode="new" capture (no fields_missing) fires the plan-generation
pipeline and the tool result reports plan_generation="queued". The pipeline
saves its own gated row (pending_review / needs_clinician_review) and is never
auto-applied here. An incomplete capture, or an update, reports
plan_generation="skipped" and never fires the pipeline.

_dispatch_tool is async and takes a trigger_executor positional (unused by the
start_intake_tool branch); these tests drive it via asyncio.run with a no-op
executor and stub _capture_intake / _run_plan_generation so nothing reaches
Supabase or Anthropic.
"""

import asyncio

import coach_chat


async def _noop_executor(flow, payload):
    return {}


def test_complete_new_intake_queues_plan(monkeypatch):
    monkeypatch.setattr(
        coach_chat,
        "_capture_intake",
        lambda tok, fields, mode: {
            "fields_captured": ["injury_type", "pain_level"],
            "fields_missing": [],
            "mode": "new",
        },
    )
    called = {}
    monkeypatch.setattr(
        coach_chat, "_run_plan_generation", lambda tok: called.update(ran=True)
    )
    result, _ = asyncio.run(
        coach_chat._dispatch_tool(
            "start_intake_tool",
            {"injury_type": "knee", "pain_level": 3, "mode": "new"},
            _noop_executor,
            user_token="tok",
        )
    )
    assert result["plan_generation"] == "queued"
    assert called.get("ran")


def test_incomplete_intake_does_not_queue_plan(monkeypatch):
    monkeypatch.setattr(
        coach_chat,
        "_capture_intake",
        lambda tok, fields, mode: {
            "fields_captured": ["injury_type"],
            "fields_missing": ["pain_level"],
            "mode": "new",
        },
    )
    monkeypatch.setattr(
        coach_chat,
        "_run_plan_generation",
        lambda tok: (_ for _ in ()).throw(AssertionError("should not run")),
    )
    result, _ = asyncio.run(
        coach_chat._dispatch_tool(
            "start_intake_tool",
            {"injury_type": "knee", "mode": "new"},
            _noop_executor,
            user_token="tok",
        )
    )
    assert result["plan_generation"] == "skipped"


def test_complete_update_does_not_queue_plan(monkeypatch):
    """An update to an existing intake never auto-fires plan generation even
    when nothing is missing - only a fresh mode="new" intake does."""
    monkeypatch.setattr(
        coach_chat,
        "_capture_intake",
        lambda tok, fields, mode: {
            "fields_captured": ["pain_level"],
            "fields_missing": [],
            "mode": "update",
        },
    )
    monkeypatch.setattr(
        coach_chat,
        "_run_plan_generation",
        lambda tok: (_ for _ in ()).throw(AssertionError("should not run")),
    )
    result, _ = asyncio.run(
        coach_chat._dispatch_tool(
            "start_intake_tool",
            {"pain_level": 2, "mode": "update"},
            _noop_executor,
            user_token="tok",
        )
    )
    assert result["plan_generation"] == "skipped"
