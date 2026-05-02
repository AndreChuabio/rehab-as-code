# rehab-orchestrator

Cursor cloud-agent orchestrator for RehabAsCode. Wraps `@cursor/sdk` so the
Python backend can run a parent cloud agent with named sub-agents without
needing a TypeScript runtime of its own.

## Layout

```
orchestrator/
  src/
    orchestrator.ts   entry point. reads JSON from stdin, streams NDJSON to stdout
    config.ts         YAML config loader + schema
  configs/
    care-plan.yaml    the RehabAsCode orchestrator (parent + 3 sub-agents)
  package.json
  tsconfig.json
```

## Adding an orchestrator

1. Drop a new `configs/{name}.yaml` with parent prompt, sub-agents, scope,
   and flows (see `care-plan.yaml` as the reference shape).
2. Invoke with `--config {name}` via stdin request. No code changes.

Two orchestrators sharing the same repo should declare disjoint
`scope.writes` paths to avoid PR collisions.

## Contract

### stdin (JSON)

```json
{
  "config": "care-plan",
  "flow": "weekly_plan",
  "repoUrl": "https://github.com/AndreChuabio/rehab-as-code",
  "extraPrompt": "optional task addendum",
  "contextFiles": { "data/wearables.json": "..." }
}
```

### stdout (NDJSON)

```
{"type":"trace","event":{"type":"agent_started","timestamp":0.1,"label":"...","payload":{...}}}
{"type":"trace","event":{"type":"tool_call","timestamp":2.3,"label":"spawn sub-agent: researcher","payload":{"subagent":"researcher"}}}
...
{"type":"result","agentId":"bc-...","runId":"...","prUrl":"https://...","branch":"week-4","status":"finished"}
```

### exit codes

- 0: finished successfully
- 1: startup failure (auth / config / network)
- 2: run started but ended in error

## Run directly (for testing without Python)

```bash
cd orchestrator
npm install
export CURSOR_API_KEY=cursor_...
echo '{"config":"care-plan","flow":"weekly_plan","repoUrl":"https://github.com/AndreChuabio/rehab-as-code"}' \
  | npx tsx src/orchestrator.ts
```

## Notes on sub-agent support

Sub-agents are a cloud-only feature at v1. The SDK wires the YAML
`subagents` map through to `customSubagents` on the cloud create call. Keep
`cloud:` explicit in every request; the SDK silently falls back to local if
you omit it. See `@cursor/sdk` docs for the full shape.
