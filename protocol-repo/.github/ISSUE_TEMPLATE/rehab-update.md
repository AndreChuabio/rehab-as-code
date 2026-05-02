---
name: Rehab protocol update
about: Trigger a Cursor cloud agent to update protocol.yaml
labels: agent
---

@cursor please update `protocol.yaml`.

## Flow

<!-- weekly_plan | symptom_adjustment -->

## Context branch

<!-- If the backend pre-pushed wearables/symptom data on a branch, name it here. -->

## Task

<!-- Plain-English description. The agent will also read .cursorrules + protocol-library/ -->

## Files to consult

- `data/` (recently pushed wearable + symptom data)
- `protocol.yaml` (current week's plan)
- `protocol-library/` (evidence-based reference)
- `.cursorrules` (schema + clinical guardrails)

Open a PR following the conventions in `.cursorrules`.
