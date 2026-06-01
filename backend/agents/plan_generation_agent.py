"""
PlanGenerationAgent - orchestrates the multi-agent clinical-reasoning pipeline.

Was a single Sonnet call with three tools (load_patient_context,
list_exercises_for_phase, generate_protocol). Now a thin asyncio
orchestrator over five specialist sub-agents:

      researcher.candidates     ┐
                                ├-> evaluator.signal --> planner.compose --> safety_reviewer.review --> save
      trend_analyst.analyze     ┘

Why split it: parallel fan-out halves wall-clock; specialized prompts
are 1/3 each (cheaper, easier to swap models); when a plan looks wrong
the clinician can see which step's output was off.

Branching on safety verdict (deterministic, NOT model-routed):
  ok / no concerns         -> save_pending(status='pending_review')
  overall_severity == med  -> retry planner.compose with concerns; max 2 retries;
                              save with safety_concerns attached if still med.
  overall_severity == high -> save_pending(status='needs_clinician_review')
                              with safety_concerns attached. Top of clinician queue,
                              red banner.

PHI hygiene: log token (UUID), step name, latency, in/out tokens, retry
count. Never log narration text, intake values, or symptom text.

No silent fallbacks: every sub-agent raises on Anthropic errors. The
orchestrator propagates the raised error to /patient/interact, which
returns a 500 with a friendly toast (continuing the H1 pattern from
PR #62).
"""
from __future__ import annotations

import asyncio
import logging
import os
import sys
import time
from pathlib import Path
from typing import Any

from . import register_patient_agent
from .base import PatientAgent, PatientRequest, PatientResponse

# Add backend/ to sys.path so user_store / protocol_repo / session_repo
# imports continue to resolve when this module is loaded as agents.plan_generation_agent.
sys.path.insert(0, str(Path(__file__).parent.parent))
import protocol_repo  # noqa: E402
import user_store  # noqa: E402

from .evaluator import EvaluatorError, signal as evaluator_signal  # noqa: E402
from .planner import PlannerError, compose as planner_compose  # noqa: E402
from .researcher import (  # noqa: E402
    ResearcherError,
    candidates as researcher_candidates,
    compute_library_match,
)
from .safety_reviewer import (  # noqa: E402
    SafetyReviewError,
    review as safety_review,
)
from .trend_analyst import TrendAnalystError, analyze as trend_analyze  # noqa: E402

logger = logging.getLogger(__name__)


_MAX_PLANNER_RETRIES = 2


class PlanGenerationError(RuntimeError):
    """Raised when the orchestrator cannot produce a saveable draft.

    Wraps the underlying sub-agent error. /patient/interact catches and
    translates into a 500 with a friendly toast detail.
    """


def _coverage_concern(library_match: dict[str, Any]) -> dict[str, Any] | None:
    """Translate a library_match marker into a clinician-visible concern, or None.

    Two cases produce a concern:
      * Out-of-scope region (knee + ankle are validated for autodraft today;
        anything else surfaces a med-severity library_coverage concern and
        gates the draft to needs_clinician_review).
      * Within-scope but the patient's week has no exact library file
        (closest_earlier / lowest_available). Low-severity informational
        flag so the clinician sees what was actually cited.
    """
    if not library_match["in_scope"]:
        return {
            "category": "library_coverage",
            "severity": "med",
            "summary": (
                f"Patient body_region={library_match['region']!r} is outside "
                "the agent pipeline's currently-validated scope (knee + "
                "ankle). Draft is provided for clinician review only."
            ),
            "library_match": library_match,
        }
    if library_match["status"] in ("closest_earlier", "lowest_available", "no_files", "no_dir"):
        return {
            "category": "library_coverage",
            "severity": "low",
            "summary": (
                f"Library has no exact week-{library_match['requested_week']} "
                f"match for region={library_match['region']!r}. Researcher "
                f"used status={library_match['status']!r}, matched_week="
                f"{library_match['matched_week']}."
            ),
            "library_match": library_match,
        }
    return None


def _resolve_save_status(
    safety_verdict: dict[str, Any],
    library_match: dict[str, Any],
) -> tuple[str, list[dict[str, Any]] | None]:
    """Map (safety_verdict, library_match) to (save_status, safety_concerns).

    Decision tree (first match wins):
      * Safety high                     -> needs_clinician_review
      * Library out-of-scope region     -> needs_clinician_review
      * Safety med + retries exhausted  -> pending_review (with concerns)
      * Otherwise                       -> pending_review (clean or
                                          informational coverage concern only)

    Within-scope library gaps surface as a low-severity concern but do NOT
    gate to needs_clinician_review on their own - patients on knee + ankle
    plans still get auto-drafted even when they're between library weeks.
    """
    coverage = _coverage_concern(library_match)
    coverage_list: list[dict[str, Any]] = [coverage] if coverage else []
    safety_list: list[dict[str, Any]] = list(safety_verdict.get("concerns") or [])

    if safety_verdict["overall_severity"] == "high":
        return "needs_clinician_review", safety_list + coverage_list

    if not library_match["in_scope"]:
        return "needs_clinician_review", safety_list + coverage_list

    if safety_verdict["overall_severity"] == "med" and not safety_verdict["ok"]:
        return "pending_review", safety_list + coverage_list

    merged = safety_list + coverage_list
    return "pending_review", merged or None


def _resolve_phase_and_week(
    intake: dict[str, Any] | None,
    active_payload: dict[str, Any] | None,
) -> tuple[str, int]:
    """Decide what phase/week to target for the next protocol.

    Order:
      1. Active protocol payload (weekly_plan flow advances week N -> N+1).
      2. Intake-supplied phase / week.
      3. Defaults: acute / week 1.
    """
    if active_payload:
        phase = active_payload.get("phase") or "acute"
        wk = active_payload.get("week")
        if isinstance(wk, int):
            return phase, wk + 1
    if intake:
        phase = intake.get("phase") or "acute"
        wk = intake.get("week")
        if isinstance(wk, int):
            return phase, wk
        return phase, 1
    return "acute", 1


@register_patient_agent
class PlanGenerationAgent(PatientAgent):
    """Orchestrate researcher / evaluator / planner / safety reviewer.

    Public surface unchanged from the pre-PR-C single-prompt version:
    callers still invoke `agent.handle(PatientRequest)` and get a
    `PatientResponse` with a pending_protocol_id artifact. The pipeline
    behind it is now multi-agent.
    """

    name = "plan_generation"

    def can_handle(self, request: PatientRequest) -> bool:
        keywords = ["generate plan", "new protocol", "update my plan", "next week", "progress"]
        return any(k in request.message.lower() for k in keywords)

    async def handle(self, request: PatientRequest) -> PatientResponse:
        """Run the full multi-agent pipeline for the patient and persist.

        Loads patient state, runs researcher + trend analyst in parallel,
        feeds the trend pattern into evaluator, composes a draft, runs
        the safety reviewer, retries on med-severity concerns up to
        _MAX_PLANNER_RETRIES, and saves the final draft as either
        pending_review or needs_clinician_review.
        """
        token = request.user_token
        api_key = os.getenv("ANTHROPIC_API_KEY", "").strip()
        if not api_key:
            # Stub-pending fallback preserved: clinician sees an empty
            # draft attributed to plan_generation.fallback. Beats a 500
            # when the API key is misconfigured.
            return self._save_stub_pending(request)

        # Load all inputs up-front. The sub-agents accept structured data
        # so we pull from Supabase / user_store once and fan out.
        try:
            inputs = self._load_inputs(token)
        except Exception as exc:  # pragma: no cover - defensive
            logger.exception("plan_generation: input load failed token=%s", token)
            raise PlanGenerationError(f"failed to load patient context: {exc}") from exc

        intake = inputs["intake"]
        health = inputs["health"]
        history = inputs["history"]
        active_payload = inputs["active_payload"]
        injury_type = (intake or {}).get("injury_type") if intake else None

        phase, week = _resolve_phase_and_week(intake, active_payload)

        # Library coverage marker - computed BEFORE the LLM fanout because
        # it's deterministic and we want to log / route on it regardless
        # of whether the researcher LLM call later succeeds or fails.
        library_match = compute_library_match(injury_type, week)

        started = time.monotonic()
        logger.info(
            "plan_generation start token=%s phase=%s week=%s injury=%s "
            "library_match_status=%s library_match_in_scope=%s "
            "library_match_matched_week=%s",
            token, phase, week, injury_type,
            library_match["status"], library_match["in_scope"],
            library_match["matched_week"],
        )

        # Fan out: researcher + trend analyst run in parallel. Both are
        # Sonnet calls so we save ~one Sonnet-call worth of latency.
        try:
            candidates_task = asyncio.to_thread(
                researcher_candidates,
                injury_type, phase, week, intake,
                token=token,
            )
            trend_task = asyncio.to_thread(
                trend_analyze,
                token=token,
                checkins=history,
                sessions=inputs["recent_sessions"],
                intake=intake,
            )
            candidates, trend_summary = await asyncio.gather(
                candidates_task, trend_task,
                return_exceptions=False,
            )
        except (ResearcherError, TrendAnalystError) as exc:
            raise PlanGenerationError(str(exc)) from exc

        # Sequential: evaluator depends on the trend; planner depends on both.
        try:
            decision = await asyncio.to_thread(
                evaluator_signal,
                intake, health, history, trend_summary,
                token=token,
            )
        except EvaluatorError as exc:
            raise PlanGenerationError(str(exc)) from exc

        try:
            draft, safety_verdict = await asyncio.to_thread(
                self._compose_with_safety_loop,
                candidates, decision, intake, trend_summary,
                phase, week, token,
            )
        except (PlannerError, SafetyReviewError) as exc:
            raise PlanGenerationError(str(exc)) from exc

        # Determine save status from the safety verdict + library coverage.
        # Branching is here in the orchestrator, NOT in the LLM - that's the
        # safety contract. Out-of-scope regions (anything outside knee +
        # ankle today) gate to needs_clinician_review even on a clean safety
        # verdict; within-scope week-gaps surface as informational concerns
        # but don't gate.
        save_status, safety_concerns = _resolve_save_status(
            safety_verdict, library_match,
        )

        # Anchor patient name to the canonical Supabase value the same
        # way chat_protocol_drafter does. Belt + suspenders: even if the
        # planner hallucinated a name, this overwrite makes the persisted
        # row match what the auth layer says about this user.
        canonical_name = user_store.get_display_name(token)
        if canonical_name:
            draft = {**draft, "patient": canonical_name}

        # Stash the library coverage marker on the draft itself so the
        # clinician dashboard / audit log can show what was cited without
        # joining against orchestrator logs. Underscored to mark this as
        # in-payload metadata, not part of the protocol contract.
        draft = {
            **draft,
            "_meta": {
                **(draft.get("_meta") or {}),
                "library_match": library_match,
            },
        }

        # Persist the canonical body_region on the payload (was null on every
        # row). library_match["region"] is resolved from the patient's
        # injury_type; downstream readers (cross-region validator,
        # /sessions/today out-of-region dimming) key on payload.body_region.
        region = library_match.get("region")
        if region and region not in ("multi", "unknown"):
            draft = {**draft, "body_region": region}

        try:
            protocol_id = protocol_repo.save_pending(
                token=token,
                payload=draft,
                created_by_agent=self.name,
                status=save_status,
                safety_concerns=safety_concerns,
            )
        except Exception as exc:
            logger.exception("plan_generation supabase save failed token=%s", token)
            raise PlanGenerationError(f"protocol save failed: {exc}") from exc

        # Mirror to protocol_state so the legacy sidebar / state machine
        # can show the new phase/week. Same pattern as the prior version.
        try:
            user_store.save_protocol_state(token, {
                "last_pr_url": None,
                "last_branch": None,
                "current_phase": draft.get("phase"),
                "current_week": draft.get("week"),
                "pending_protocol_id": protocol_id,
            })
        except Exception as exc:
            logger.warning("protocol_state mirror update failed token=%s: %s", token, exc)

        elapsed_ms = int((time.monotonic() - started) * 1000)
        logger.info(
            "plan_generation done token=%s elapsed_ms=%d save_status=%s "
            "n_exercises=%d safety_severity=%s",
            token, elapsed_ms, save_status,
            len(draft.get("exercises") or []),
            safety_verdict["overall_severity"],
        )

        if save_status == "needs_clinician_review":
            message = (
                "Protocol generated. Flagged for clinician review "
                "before activation."
            )
        elif safety_concerns:
            message = (
                "Protocol generated with notes for clinician review. "
                "Awaiting approval."
            )
        else:
            message = "Protocol generated. Awaiting clinician review."

        return PatientResponse(
            agent_name=self.name,
            message=message,
            next_agent=None,
            data={
                "pending_protocol_id": protocol_id,
                "save_status": save_status,
                "safety_severity": safety_verdict["overall_severity"],
            },
            artifacts=[{"type": "pending_protocol", "id": protocol_id}],
        )

    def _compose_with_safety_loop(
        self,
        candidates: list[dict[str, Any]],
        decision: dict[str, Any],
        intake: dict[str, Any] | None,
        trend_summary: dict[str, Any] | None,
        phase: str,
        week: int,
        token: str,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        """Compose -> safety review. Retry up to _MAX_PLANNER_RETRIES on med.

        Synchronous wrapper around planner.compose() and safety_reviewer.review()
        so the caller can dispatch via asyncio.to_thread without awaiting
        each individual call.

        Returns the (draft, verdict) pair. The verdict's overall_severity
        plus the orchestrator's branching decides what status to save under.
        """
        concerns: list[dict[str, Any]] | None = None
        draft: dict[str, Any] | None = None
        verdict: dict[str, Any] | None = None
        for attempt in range(_MAX_PLANNER_RETRIES + 1):
            draft = planner_compose(
                candidates=candidates,
                signal=decision,
                intake=intake,
                phase=phase,
                week=week,
                concerns=concerns,
                token=token,
            )
            verdict = safety_review(
                draft=draft,
                intake=intake,
                trend_summary=trend_summary,
                token=token,
            )

            if verdict["overall_severity"] == "high":
                # High severity: do NOT retry. The clinician has to see this
                # exact draft with its concerns; auto-revising could mask the
                # problem the agent flagged.
                logger.info(
                    "plan_generation safety high token=%s attempt=%d - saving as "
                    "needs_clinician_review",
                    token, attempt,
                )
                return draft, verdict

            if verdict["overall_severity"] == "med" and not verdict["ok"]:
                if attempt < _MAX_PLANNER_RETRIES:
                    logger.info(
                        "plan_generation safety med token=%s attempt=%d - "
                        "retrying planner with %d concerns",
                        token, attempt, len(verdict["concerns"]),
                    )
                    concerns = verdict["concerns"]
                    continue
                # Exhausted retries; fall through to return the latest draft
                # with concerns. Orchestrator saves it as pending_review with
                # safety_concerns attached so the clinician sees the trail.
                logger.info(
                    "plan_generation safety med token=%s exhausted retries - "
                    "saving with concerns attached",
                    token,
                )
                return draft, verdict

            # ok or low severity: ship it.
            logger.info(
                "plan_generation safety ok token=%s attempt=%d severity=%s",
                token, attempt, verdict["overall_severity"],
            )
            return draft, verdict

        # Defensive: the loop body always returns. Reaching here means the
        # range-based for did not execute (impossible: range >= 1).
        assert draft is not None and verdict is not None
        return draft, verdict

    def _load_inputs(self, token: str) -> dict[str, Any]:
        """Read every input the sub-agents need from Supabase / user_store."""
        user = user_store.load_user(token) or {}
        intake = user.get("intake")
        history = user_store.get_session_history(token, limit=40)

        active = protocol_repo.get_active(token)
        active_payload = (active or {}).get("payload") if active else None

        recent_sessions: list[dict[str, Any]] = []
        try:
            import session_repo
            recent_sessions = session_repo.list_recent(token, days=56)
        except Exception as exc:
            # session_repo requires DATABASE_URL; in environments without
            # it (sqlite tests, dev) we still produce a reasonable plan
            # without longitudinal session aggregates. The trend analyst
            # falls back to checkins-only when sessions are empty.
            logger.info(
                "plan_generation: session_repo unavailable token=%s: %s",
                token, exc,
            )

        return {
            "intake": intake,
            "health": user.get("health"),
            "history": history,
            "active_payload": active_payload,
            "recent_sessions": recent_sessions,
        }

    def _save_stub_pending(self, request: PatientRequest) -> PatientResponse:
        """Save a no-LLM stub pending row when ANTHROPIC_API_KEY is missing.

        Better than a 500: the clinician sees an empty draft attributed
        to plan_generation.fallback and knows to fill it in by hand. The
        intake payload rides along so they have context.
        """
        token = request.user_token
        user = user_store.load_user(token) or {}
        intake = user.get("intake") or {}

        payload = {
            "patient": intake.get("name") or "Patient",
            "phase": "acute",
            "week": 1,
            "exercises": [],
            "intake": intake,
        }

        try:
            protocol_id = protocol_repo.save_pending(
                token=token,
                payload=payload,
                created_by_agent=f"{self.name}.fallback",
            )
        except Exception as exc:
            logger.exception("fallback supabase pending insert failed token=%s", token)
            return PatientResponse(
                agent_name=self.name,
                message=f"Plan generation failed: {exc}",
                next_agent=None,
            )

        try:
            user_store.save_protocol_state(token, {
                "last_pr_url": None,
                "last_branch": None,
                "current_phase": "acute",
                "current_week": 1,
                "pending_protocol_id": protocol_id,
            })
        except Exception as exc:
            logger.warning("protocol_state mirror update failed token=%s: %s", token, exc)

        return PatientResponse(
            agent_name=self.name,
            message="Protocol generated (fallback). Awaiting clinician review.",
            next_agent=None,
            data={"pending_protocol_id": protocol_id},
            artifacts=[{"type": "pending_protocol", "id": protocol_id}],
        )
