// Node-runnable unit tests for the payer-aware goal + super-bill helpers.
//
// Usage:
//   node frontend/tests/payer_goals.test.js
//
// Same shape as flow_stitching.test.js / state_aware_greeting.test.js — no
// Jest, no jsdom. The helpers under test (payerLabel, goalFlagChips,
// renderPayerGoalItem) are DOM-free string builders that live in
// clinician.js. This file mirrors that logic byte-for-byte. When the rules
// change in clinician.js, change them here too.
//
// What this covers:
//   * payerLabel: known modes, unknown mode passthrough, null -> "Not set"
//   * goalFlagChips: emits a dashed warning chip per data-integrity flag, in
//     priority order, and nothing when no flags are set
//   * renderPayerGoalItem: tied_to chip + measurable_target + flag chips, and
//     escapes goal text (no raw HTML injection)

const assert = require("node:assert/strict");

// ── Mirror of helpers from clinician.js ──────────────────────────────────

function escapeHtml(s) {
  return String(s ?? "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}

const PAYER_LABEL = {
  insurance: "Insurance",
  medicare: "Medicare",
  cash: "Cash / self-pay",
};

function payerLabel(mode) {
  return PAYER_LABEL[mode] || (mode ? String(mode) : "Not set");
}

const TIED_TO_LABEL = {
  adl: "ADL",
  fall_risk: "Fall risk",
  performance: "Performance",
  load_mgmt: "Load mgmt",
};

const GOAL_FLAG_CHIPS = [
  ["needs_clinician_review", "needs review"],
  ["citation_missing", "citation missing"],
  ["text_register_warning", "review register"],
  ["tied_to_coerced", "tie-in inferred"],
];

function goalFlagChips(goal) {
  if (!goal || typeof goal !== "object") return "";
  return GOAL_FLAG_CHIPS
    .filter(([flag]) => goal[flag])
    .map(([, label]) => `<span class="goal-flag-chip">${escapeHtml(label)}</span>`)
    .join("");
}

function renderPayerGoalItem(goal) {
  if (!goal || typeof goal !== "object") return "";
  const text = goal.text || "(no goal text)";
  const target = goal.measurable_target
    ? `<span class="payer-goal-target">${escapeHtml(goal.measurable_target)}</span>`
    : "";
  const tiedKey = goal.tied_to;
  const tiedChip = tiedKey
    ? `<span class="goal-tied-chip goal-tied-${escapeHtml(String(tiedKey))}">${escapeHtml(TIED_TO_LABEL[tiedKey] || String(tiedKey))}</span>`
    : "";
  const flags = goalFlagChips(goal);
  return `<li class="payer-goal">
      <div class="payer-goal-head">
        ${tiedChip}
        <span class="payer-goal-text">${escapeHtml(text)}</span>
      </div>
      <div class="payer-goal-meta">${target}${flags}</div>
    </li>`;
}

// ── Tests ────────────────────────────────────────────────────────────────

// payerLabel
assert.equal(payerLabel("insurance"), "Insurance");
assert.equal(payerLabel("medicare"), "Medicare");
assert.equal(payerLabel("cash"), "Cash / self-pay");
assert.equal(payerLabel("ppo"), "ppo", "unknown mode passes through");
assert.equal(payerLabel(null), "Not set");
assert.equal(payerLabel(undefined), "Not set");

// goalFlagChips — no flags -> empty
assert.equal(goalFlagChips({ text: "x" }), "");
assert.equal(goalFlagChips(null), "");

// goalFlagChips — single flag
assert.ok(goalFlagChips({ needs_clinician_review: true }).includes("needs review"));
assert.ok(goalFlagChips({ citation_missing: true }).includes("citation missing"));
assert.ok(goalFlagChips({ text_register_warning: true }).includes("review register"));
assert.ok(goalFlagChips({ tied_to_coerced: true }).includes("tie-in inferred"));

// goalFlagChips — priority order: needs_clinician_review before citation_missing
{
  const html = goalFlagChips({ needs_clinician_review: true, citation_missing: true });
  assert.ok(
    html.indexOf("needs review") < html.indexOf("citation missing"),
    "needs review chip must render before citation missing",
  );
  assert.equal((html.match(/goal-flag-chip/g) || []).length, 2);
}

// renderPayerGoalItem — tied_to chip + target rendered
{
  const html = renderPayerGoalItem({
    text: "Walk 100m unaided",
    measurable_target: "100m",
    tied_to: "adl",
  });
  assert.ok(html.includes("goal-tied-adl"));
  assert.ok(html.includes("ADL"));
  assert.ok(html.includes("Walk 100m unaided"));
  assert.ok(html.includes("100m"));
}

// renderPayerGoalItem — escapes goal text (no HTML injection)
{
  const html = renderPayerGoalItem({ text: "<img src=x onerror=1>" });
  assert.ok(!html.includes("<img"), "raw HTML must be escaped");
  assert.ok(html.includes("&lt;img"));
}

// renderPayerGoalItem — unknown tied_to passes through as its own label
{
  const html = renderPayerGoalItem({ text: "g", tied_to: "mobility" });
  assert.ok(html.includes("goal-tied-mobility"));
  assert.ok(html.includes("mobility"));
}

// renderPayerGoalItem — non-object returns empty string
assert.equal(renderPayerGoalItem(null), "");

console.log("OK: payer_goals helpers — all assertions passed");
