# protocols/

The patient's rehab program, lived in version control and updated by Cursor
cloud agents. Lives under `protocols/` of `AndreChuabio/rehab-as-code` so
the agent's PRs and the app code share one repo and one audit trail.

## What this directory is

The current week's protocol lives in `protocol.yaml`. The cloud coding agent
(AG2 multi-agent pipeline by default; Cursor SDK as an alternate) reads the
patient's wearable data (under `data/`) and reported symptoms, consults
`protocol-library/` for evidence-based progressions, and opens draft PRs
that update `protocol.yaml`. The clinician approves each PR from the
RehabAsCode UI (which calls `POST /pr/apply` to squash-merge it onto main);
the next flow's agent then reads the updated state.

This is **rehab as code**: every change to the program is a commit, every
weekly progression is a reviewable diff, every adjustment cites the
evidence-based reference it is based on.

## Layout

```
protocol.yaml                     - the patient's current program
protocol-library/                 - evidence-based reference (read-only)
  post-acl-week-3.yaml
  post-acl-week-4.yaml
  regressions/
    single_leg_squat.yaml
data/                             - wearable + symptom inputs (agent reads these)
.cursorrules                      - agent system prompt and clinical guardrails
schema.json                       - schema for protocol.yaml
.github/ISSUE_TEMPLATE/
  rehab-update.md                 - structured invocation template
```

## How to invoke an update

Three ways, in order of how the demo uses them:

1. **From the UI**: click `1 intake`, `2 weekly plan`, `3 check-in`, or
   `4 symptom` in the dashboard. The backend funnels through
   `_invoke_with_fallback()` and fires the cursor cloud agent with the
   right per-flow prompt.
2. **From chat**: type natural language to Coach Maya
   ("I am ready to progress - plan next week"); GPT-4o-mini routes to
   the matching `fire_*_trigger` tool, same backend path as the buttons.
3. **From curl**: `POST /triggers/{intake,weekly-cron,checkin,symptom}`
   with the relevant body fields. See root `README.md` endpoints table.

The agent reads `protocol.yaml`, `.cursorrules`, the `data/wearables-{date}.json`
the backend just wrote, and the relevant `protocol-library/` entries.
It opens a draft PR; the clinician approves from the UI.

PR body always includes: Reasoning, Cited library entries, Wearable data
summary (the `.cursorrules` requires this).
