// Node-runnable unit test for the clinician auto-applied review feed.
//
// Usage:
//   node frontend/tests/auto_applied_feed.test.js
//
// Same shape as pose_guided.test.js / maya_voice_gate.test.js — no Jest, no
// jsdom. The render helpers under test (autoAppliedSummary, autoAppliedRowHtml,
// renderAutoApplied) live in clinician.js inside the browser IIFE. We mirror
// them here byte-for-byte against a minimal element/document stub so the file
// runs with no install. When the render rules change in clinician.js, change
// them here too.
//
// What this covers:
//   * escapeHtml: the XSS guard applied to every summary before innerHTML
//   * autoAppliedSummary: server summary wins; payload-derived fallback; the
//     "Coach Maya updated the plan" default when nothing is known
//   * autoAppliedRowHtml: row markup carries the summary + a Revert control
//   * renderAutoApplied: an empty feed renders nothing intrusive (zero rows),
//     and a populated feed appends one row per entry with the summary escaped

const assert = require("node:assert/strict");

// ── Mirror of clinician.js escapeHtml (byte-equivalent) ────────────────────
function escapeHtml(s) {
  return String(s ?? "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}

// ── Mirror of clinician.js auto-applied render helpers ─────────────────────
// The feed row shape comes straight off GET /protocols/auto-applied
// (protocol_repo.list_auto_applied_open): { id, token, payload, created_at,
// ... }. The rows carry no top-level `summary` column today, so the summary
// is derived from the payload with a stable default — but a `summary` field,
// if a future backend adds one, takes precedence.
function autoAppliedSummary(row) {
  if (row && typeof row.summary === "string" && row.summary.trim()) {
    return row.summary.trim();
  }
  const p = (row && row.payload) || {};
  const bits = [];
  if (p.body_region) bits.push(String(p.body_region).replace(/_/g, " "));
  if (p.phase) bits.push(String(p.phase));
  if (p.week != null) bits.push("wk " + p.week);
  if (bits.length) return "Coach Maya updated the plan — " + bits.join(" · ");
  return "Coach Maya updated the plan";
}

function autoAppliedRowHtml(row) {
  const summary = escapeHtml(autoAppliedSummary(row));
  const id = escapeHtml(String(row && row.id != null ? row.id : ""));
  return `<span class="aa-summary">${summary}</span>`
    + `<button type="button" class="aa-revert" data-id="${id}">Revert</button>`
    + `<button type="button" class="aa-ack" data-id="${id}">Acknowledge</button>`;
}

// renderAutoApplied mirrors clinician.js but takes an explicit `doc` stub in
// place of the global `document`/`$` so it runs headless. The DOM-free logic
// (clear host, hide the section when empty, append one row per entry) is the
// part under test; the click wiring is stubbed.
function renderAutoApplied(rows, doc) {
  const host = doc.getElementById("autoAppliedList");
  if (!host) return;
  host.innerHTML = "";
  const list = Array.isArray(rows) ? rows.filter(Boolean) : [];
  const section = doc.getElementById("autoAppliedSection");
  if (section) section.hidden = list.length === 0;
  list.forEach((r) => {
    const el = doc.createElement("div");
    el.className = "auto-applied-row";
    el.innerHTML = autoAppliedRowHtml(r);
    host.appendChild(el);
  });
}

// ── Minimal element + document stubs ───────────────────────────────────────
function makeEl() {
  return {
    className: "",
    innerHTML: "",
    hidden: false,
    children: [],
    appendChild(child) { this.children.push(child); },
    querySelector() { return { addEventListener() {} }; },
    addEventListener() {},
    remove() {},
  };
}

function makeDoc() {
  const host = makeEl();
  const section = makeEl();
  return {
    _host: host,
    _section: section,
    createElement() { return makeEl(); },
    getElementById(id) {
      if (id === "autoAppliedList") return host;
      if (id === "autoAppliedSection") return section;
      return null;
    },
  };
}

// ── escapeHtml: the XSS guard ──────────────────────────────────────────────
assert.equal(escapeHtml("<script>"), "&lt;script&gt;");
assert.equal(escapeHtml('a & "b" \'c\''), "a &amp; &quot;b&quot; &#39;c&#39;");
assert.equal(escapeHtml(null), "");

// ── autoAppliedSummary: server summary wins ────────────────────────────────
assert.equal(
  autoAppliedSummary({ id: "p1", summary: "Swapped lunge -> step-down" }),
  "Swapped lunge -> step-down",
);

// ── autoAppliedSummary: payload-derived fallback ───────────────────────────
assert.equal(
  autoAppliedSummary({ id: "p2", payload: { body_region: "knee", phase: "strength", week: 3 } }),
  "Coach Maya updated the plan — knee · strength · wk 3",
);
// body_region underscores normalize to spaces.
assert.equal(
  autoAppliedSummary({ id: "p3", payload: { body_region: "lower_leg" } }),
  "Coach Maya updated the plan — lower leg",
);

// ── autoAppliedSummary: default when nothing is known ──────────────────────
assert.equal(autoAppliedSummary({ id: "p4" }), "Coach Maya updated the plan");
assert.equal(autoAppliedSummary({ id: "p5", summary: "   " }), "Coach Maya updated the plan");
assert.equal(autoAppliedSummary(null), "Coach Maya updated the plan");

// ── autoAppliedRowHtml: carries summary + a Revert control ─────────────────
{
  const html = autoAppliedRowHtml({ id: "p1", summary: "Swapped lunge -> step-down" });
  // The summary text appears in the row markup. The ">" in "->" is HTML-
  // escaped to "&gt;", so we assert on the escape-stable substring.
  assert.ok(html.includes("Swapped lunge -&gt; step-down"), "row shows the summary");
  assert.match(html, /revert/i, "row offers a revert control");
  assert.ok(html.includes('data-id="p1"'), "controls carry the protocol id");
}

// ── autoAppliedRowHtml: summary is escaped before innerHTML (XSS) ──────────
{
  const html = autoAppliedRowHtml({ id: "x", summary: "<img src=x onerror=alert(1)>" });
  assert.ok(html.includes("&lt;img"), "angle brackets are escaped");
  assert.ok(!html.includes("<img"), "no raw tag survives into the markup");
}

// ── renderAutoApplied: empty feed renders nothing intrusive ────────────────
{
  const doc = makeDoc();
  renderAutoApplied([], doc);
  assert.equal(doc._host.children.length, 0, "no rows for an empty feed");
  assert.equal(doc._host.innerHTML, "", "host is cleared");
  assert.equal(doc._section.hidden, true, "section is hidden when empty");
}

// ── renderAutoApplied: populated feed appends one row per entry ────────────
{
  const doc = makeDoc();
  renderAutoApplied(
    [
      { id: "p1", summary: "Swapped lunge -> step-down", token: "t1", created_at: "2026-06-28" },
      { id: "p2", payload: { body_region: "ankle" } },
    ],
    doc,
  );
  assert.equal(doc._host.children.length, 2, "one row per feed entry");
  assert.equal(doc._section.hidden, false, "section is shown when populated");
  assert.ok(doc._host.children[0].innerHTML.includes("Swapped lunge -&gt; step-down"));
  assert.match(doc._host.children[0].innerHTML, /revert/i);
}

// ── renderAutoApplied: skips falsy entries defensively ─────────────────────
{
  const doc = makeDoc();
  renderAutoApplied([null, { id: "p1", summary: "ok" }, undefined], doc);
  assert.equal(doc._host.children.length, 1, "falsy rows are filtered out");
}

console.log("OK: clinician auto-applied feed — all assertions passed");
