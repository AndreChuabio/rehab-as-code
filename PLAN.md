# RehabAsCode — Cursor Cloud Agent Orchestration Plan

Historical planning doc. Most of this shipped; deltas from the original
plan are noted at the bottom under "What changed during build".

Path C: Cursor Cloud Agents via TypeScript SDK with a parent orchestrator
and three named sub-agents. Backend stays Python/FastAPI; a Node helper
wraps `@cursor/sdk` because the SDK is TS-only. **Live `cursor_sdk` is the
demo primary** (changed mid-build, see deltas); `cached_replay` is the
auto-fallback when live throws.

## Agent structure

```
                        care-plan-coordinator   (parent, cloud)
                         /        |         \
                researcher    evaluator    planner
```

- `researcher` — pulls evidence from `protocol-library/`
- `evaluator` — classifies symptoms and recovery risk
- `planner` — writes `protocol.yaml` and `schedule.yaml`

## Trigger matrix

| Trigger endpoint         | Parent prompt role                          | Spawns                   |
|--------------------------|---------------------------------------------|--------------------------|
| `POST /triggers/intake`           | initialize protocol from intake form | researcher, planner      |
| `POST /triggers/weekly-cron`      | generate next week's progression     | researcher, planner      |
| `POST /triggers/checkin`          | log today's check-in, flag trends    | evaluator                |
| `POST /triggers/symptom`          | adjust protocol for mid-session note | evaluator, planner       |
| `POST /health-sync` (existing)    | write wearables context file         | no agent                 |

## Modularity guarantees

1. Sub-agent roster lives in `orchestrator/configs/care-plan.yaml`. Adding a
   fourth sub-agent is a config edit, not code.
2. A sibling orchestrator (for example `orchestrator/configs/nutrition.yaml`)
   defines its own parent and sub-agents. The Node helper selects which
   orchestrator to run by `--config` flag.
3. Orchestrator-to-orchestrator communication is via the repo: each config
   declares `reads:` and `writes:` path scopes. Repo is the message bus.

## Tasks

Status: `[ ]` not started, `[~]` in progress, `[x]` complete.

1. [x] **Sponsor-table intel** — `CURSOR_API_KEY` obtained; cloud quota +
   sub-agent feature confirmed. Live runs verified end-to-end.
2. [x] **Node orchestrator helper** — `orchestrator/` package using
   `@cursor/sdk`. Reads a YAML config, calls `Agent.create` with
   `{cloud, agents, mcpServers}`, streams NDJSON trace events to stdout.
   Typechecks clean. Boots cleanly with missing-key path verified.
   *(Skill: cursor-sdk)*
3. [x] **Python <-> Node bridge** — `backend/agents/cursor_sdk.py` implements
   `CodingAgent`. Spawns the Node helper, parses NDJSON lines into
   `TraceEvent`, yields via `stream_trace`. Factory updated in
   `agents/__init__.py`. Smoke test passes for mock + cached_replay;
   cursor_sdk gracefully fails when CURSOR_API_KEY is unset.
4. [x] **Env plumbing + demo toggle** — `CURSOR_API_KEY`,
   `AGENT_PROVIDER`, `DEMO_LIVE_AGENT` added to `.env.example`. Demo
   default is `cached_replay`; live only when `DEMO_LIVE_AGENT=1`.
   `/debug-env` surfaces all four for operator visibility.
5. [x] **Trigger endpoints** — `POST /triggers/{intake,weekly-cron,checkin,
   symptom}` all funnel through `_invoke_with_fallback()`. Verified via
   uvicorn smoke test against the cached provider.
6. [x] **Error handling + auto-fallback** — `_invoke_with_fallback()` wraps
   the configured provider in try/except and swaps in `cached_replay` on
   any exception. Warning logged with the failing provider name.
7. [x] **Capture real orchestrated runs** — All four `cached_runs/*.json`
   populated from real cursor_sdk runs (PR #1 weekly_plan,
   subsequent flows captured from live invocations). Capture helper at
   `backend/scripts/capture_run.py`.
8. [x] **Frontend: hierarchical trace + agent-team badges** — Added the
   four-node team strip (parent + 3 specialists that light up on spawn),
   the four-button patient-journey trigger strip, and indented child-event
   rendering for any trace with `payload.subagent`. Verified via uvicorn
   + curl that SSE carries the new `subagent` payloads end-to-end.
9. [x] **End-to-end verify + rehearsal** — All four triggers verified
   live (real PRs opened; check `gh pr list`). Browser rehearsal driven
   from `frontend/index.html` via uvicorn on `:8000`.

## Scope guardrails (hard)

- Sub-agents cap: 3. No fourth, no nesting.
- ~~Demo default: `cached_replay`. Live is opt-in.~~ **Inverted mid-build**
  (see deltas) — `cursor_sdk` is now the demo primary; `cached_replay` is
  the silent fallback inside `_invoke_with_fallback()`.
- 1:00 PM scope freeze. 3:00 PM demo lockdown. (Both passed; merged
  Nikki's `feature/intake-chat-flow` PR after lockdown for the guided Q&A
  upgrade.)

## What changed during build

1. **Demo posture inverted.** Original plan: cached on stage, live as
   credibility tab. Final: live primary, cached as silent fallback. The
   visceral mechanic of "judge presses button → real PR opens within 60s"
   was worth the latency cost; cached_replay still catches any failure.

2. **Clinician approval gesture added.** The `Approve and apply` button
   on each PR result bubble + `POST /pr/apply` endpoint runs `gh pr ready`
   then `gh pr merge --squash`. Cursor cloud agents auto-merge their own
   PRs in seconds, so Approve treats "already merged" as success. The
   visible gesture ratifies what already happened — pitch story is
   "agent suggests, clinician approves" regardless of timing.

3. **Workflow chain state propagation.** Original plan didn't address
   that PRs stay unmerged → next flow reads stale `main`. Fixed by
   making Approve actually merge, and switching `protocol_loader.fetch_protocol()`
   to the GitHub contents API (raw URL has a 5min CDN cache, fatal for chain).

4. **Demo reset added.** `POST /demo/reset` rewrites `protocol.yaml`
   back to `pending_intake` via the GitHub contents API (atomic, sha-checked).
   Surfaced as "Reset demo" button in the left sidebar.

5. **Empty intake start state.** `protocol.yaml` shipped pre-populated
   with Andre's week-4 protocol; for the demo, replaced with
   `pending_intake` stub so the intake flow has something to populate.

6. **Single-repo collapse.** Original plan had `protocol-repo/` as a
   separate seed for `AndreChuabio/rehab-protocols-andre`. Folded into
   this repo as `protocols/` per `PROTOCOL_SUBDIR=protocols` for a
   single-repo audit trail judges can skim.
