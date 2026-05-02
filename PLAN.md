# RehabAsCode — Cursor Cloud Agent Orchestration Plan

Path C: Cursor Cloud Agents via TypeScript SDK with a parent orchestrator and
three named sub-agents. Backend stays Python/FastAPI; a Node helper wraps
`@cursor/sdk` because the SDK is TS-only. Demo default is always
`cached_replay`; live runs are opt-in via env flag.

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

1. [~] **Sponsor-table intel** — Andre grabs `CURSOR_API_KEY` and confirms
   cloud quota + sub-agent feature on the account. Blocks task 7.
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
7. [ ] **Capture real orchestrated runs** — Capture helper written at
   `backend/scripts/capture_run.py`. Once `CURSOR_API_KEY` lands,
   `python3 -m scripts.capture_run --all` refreshes all four
   `backend/cached_runs/*.json` with real sub-agent-inclusive traces.
   Placeholder traces for `intake` and `checkin` are in place so the
   cached demo works end-to-end today.
8. [x] **Frontend: hierarchical trace + agent-team badges** — Added the
   four-node team strip (parent + 3 specialists that light up on spawn),
   the four-button patient-journey trigger strip, and indented child-event
   rendering for any trace with `payload.subagent`. Verified via uvicorn
   + curl that SSE carries the new `subagent` payloads end-to-end.
9. [ ] **End-to-end verify + rehearsal** — All four triggers verified via
   curl smoke test. Browser rehearsal pending Andre's review; pitch.md
   update pending.

## Scope guardrails (hard)

- Sub-agents cap: 3. No fourth, no nesting.
- Demo default: `cached_replay`. Live is opt-in.
- If task 7 fails twice, stop — cached replay already demos perfectly.
- 1:00 PM scope freeze. 3:00 PM demo lockdown.
