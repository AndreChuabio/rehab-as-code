# rehab-protocols-andre

Andre's personal rehab protocol, version-controlled and updated by Cursor cloud agents.

## What this repo is

The current week's protocol lives in `protocol.yaml`. Cursor cloud agents read
the patient's wearable data (under `data/`) and reported symptoms, consult
`protocol-library/` for evidence-based progressions, and open PRs that update
`protocol.yaml`. A human (Andre or his physical therapist) reviews and merges.

This is **rehab as code**: every change to the program is a commit, every
weekly progression is a reviewable diff, every adjustment cites the
evidence-based reference it's based on.

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

Either:

1. Open an issue using the `Rehab protocol update` template and mention
   `@cursor` in the body, or
2. POST to the RehabAsCode backend's `/agent/invoke` endpoint, which writes
   wearable + symptom data into `data/` on a fresh branch and opens the
   issue for you.

The agent will clone the repo, read the context, draft a PR, and post it
back. The PR body includes Reasoning, Cited library entries, and a Wearable
data summary.
