// Node-runnable unit tests for the PR-R state-aware Maya greeting helpers.
//
// Usage:
//   node frontend/tests/state_aware_greeting.test.js
//
// Same shape as flow_stitching.test.js / pose_guided.test.js — no Jest, no
// jsdom. The helpers under test (selectGreetingCopy, daysSinceLastActive,
// sanitizeDisplayName) are intentionally DOM-free so they run in plain Node.
// They live in app.js as window.__greetingHelpers; this file mirrors the
// implementation byte-for-byte. When the rules change in app.js, change them
// here too.
//
// What this covers:
//   * sanitizeDisplayName: trims, drops whitespace-only, caps length
//   * daysSinceLastActive: <24h returns null, >=24h returns whole days
//   * selectGreetingCopy: 4-state matrix + review_status branches +
//     daysAway prepend rules + display_name elision

const assert = require("node:assert/strict");

// ── Mirror of helpers from app.js ───────────────────────────────────────────

const GREETING_DAYS_GAP_MS = 24 * 60 * 60 * 1000;

function daysSinceLastActive(lastActiveIso, nowMs) {
  if (!lastActiveIso) return null;
  const ts = Date.parse(lastActiveIso);
  if (Number.isNaN(ts)) return null;
  const now = typeof nowMs === "number" ? nowMs : Date.now();
  const deltaMs = now - ts;
  if (deltaMs < GREETING_DAYS_GAP_MS) return null;
  return Math.floor(deltaMs / (24 * 60 * 60 * 1000));
}

function sanitizeDisplayName(raw) {
  if (typeof raw !== "string") return null;
  const trimmed = raw.trim();
  if (!trimmed) return null;
  return trimmed.length > 60 ? trimmed.slice(0, 60) : trimmed;
}

function selectGreetingCopy(status, opts) {
  const o = opts || {};
  const daysAway = typeof o.daysAway === "number" && o.daysAway >= 1
    ? o.daysAway
    : null;

  const state = status && status.state;
  const reviewState = (status && status.review_status && status.review_status.state) || null;
  const name = sanitizeDisplayName(status && status.display_name);
  const welcomeBack = name ? `Welcome back, ${name}.` : "Welcome back.";

  let body;
  if (state === "needs_intake") {
    return (
      "Hi, I'm Coach Maya — your AI rehab partner. To build your plan, "
      + "I'll ask a few quick questions about your injury. Tap Start "
      + "intake below, or just tell me about your injury here in chat. "
      + "I work with knee, ankle, hip, low-back, shoulder, and elbow rehab."
    );
  } else if (state === "needs_plan") {
    if (
      reviewState === "pending_review"
      || reviewState === "needs_clinician_review"
    ) {
      body = (
        `${welcomeBack} Your draft plan is with your PT for review — `
        + "they'll have eyes on it shortly. While we wait, anything new "
        + "today? Pain, swelling, sleep changes, or did anything aggravate "
        + "the injury?"
      );
    } else {
      body = (
        `${welcomeBack} Your intake is in. Want me to draft your first `
        + "weekly plan? Tap Draft next week or just say so here."
      );
    }
  } else if (state === "ready") {
    if (reviewState === "recently_approved") {
      body = (
        `${welcomeBack} Your PT just approved your plan. Want to start `
        + "today's session, log a check-in, or talk about how this week's "
        + "going?"
      );
    } else if (reviewState === "recently_rejected") {
      body = (
        `${welcomeBack} Your PT had some notes on your last draft — see `
        + "the chat for details. We can revisit when you're ready."
      );
    } else {
      body = (
        `${welcomeBack} How are you doing? Want to log a check-in, browse `
        + "exercises, or talk about progress?"
      );
    }
  } else {
    body = "Hi, I'm Coach Maya. How can I help you today?";
  }

  if (daysAway && state !== "needs_intake") {
    const dayWord = daysAway === 1 ? "day" : "days";
    return `Good to see you again — it's been ${daysAway} ${dayWord}. ${body}`;
  }
  return body;
}

// ── sanitizeDisplayName ─────────────────────────────────────────────────────
{
  assert.equal(sanitizeDisplayName(undefined), null);
  assert.equal(sanitizeDisplayName(null), null);
  assert.equal(sanitizeDisplayName(""), null, "empty string -> null");
  assert.equal(sanitizeDisplayName("   "), null, "whitespace-only -> null");
  assert.equal(sanitizeDisplayName(42), null, "non-string -> null");
  assert.equal(sanitizeDisplayName("Andre"), "Andre");
  assert.equal(sanitizeDisplayName("  Andre  "), "Andre", "trims surrounding ws");

  // Cap at 60 chars to defend against pathological intake input.
  const long = "A".repeat(120);
  assert.equal(sanitizeDisplayName(long).length, 60, "caps at 60 chars");
}

// ── daysSinceLastActive ─────────────────────────────────────────────────────
{
  assert.equal(daysSinceLastActive(null), null, "null input -> null");
  assert.equal(daysSinceLastActive(undefined), null);
  assert.equal(daysSinceLastActive(""), null);
  assert.equal(daysSinceLastActive("not-a-date"), null, "unparseable -> null");

  // Anchor "now" to a fixed moment so this test is deterministic.
  const now = Date.parse("2026-05-07T12:00:00Z");

  // <24h gap -> null (don't pester same-day returners).
  assert.equal(
    daysSinceLastActive("2026-05-07T08:00:00Z", now),
    null,
    "<24h gap -> null",
  );
  assert.equal(
    daysSinceLastActive("2026-05-06T13:00:00Z", now),
    null,
    "23h gap -> null",
  );

  // Exactly 24h -> 1 day.
  assert.equal(
    daysSinceLastActive("2026-05-06T12:00:00Z", now),
    1,
    "24h -> 1 day",
  );

  // Multi-day gaps round down to whole days.
  assert.equal(
    daysSinceLastActive("2026-05-04T12:00:00Z", now),
    3,
    "3-day gap -> 3",
  );
  assert.equal(
    daysSinceLastActive("2026-04-22T12:00:00Z", now),
    15,
    "15-day gap -> 15",
  );
}

// ── selectGreetingCopy: needs_intake ────────────────────────────────────────
{
  // Brand-new patient — no welcome-back, no name, no days-away prepend.
  const copy = selectGreetingCopy({
    state: "needs_intake",
    display_name: null,
    review_status: null,
  });
  assert.match(copy, /Hi, I'm Coach Maya/, "starts with intro");
  assert.match(copy, /Tap Start intake below/, "mentions intake CTA");
  assert.doesNotMatch(copy, /Welcome back/, "no welcome-back for new patient");
  assert.doesNotMatch(copy, /Good to see you again/, "no days-away for new");

  // daysAway is ignored for needs_intake — they haven't been "away."
  const copyWithDays = selectGreetingCopy(
    { state: "needs_intake", display_name: null, review_status: null },
    { daysAway: 30 },
  );
  assert.equal(copyWithDays, copy, "daysAway suppressed for needs_intake");
}

// ── selectGreetingCopy: needs_plan ──────────────────────────────────────────
{
  // pending_review branch — addresses by name, asks check-in question.
  const pending = selectGreetingCopy({
    state: "needs_plan",
    display_name: "Andre",
    review_status: { state: "pending_review" },
  });
  assert.match(pending, /Welcome back, Andre\./);
  assert.match(pending, /draft plan is with your PT/);
  assert.match(pending, /anything new today\?/);

  // needs_clinician_review behaves identically to pending_review.
  const flagged = selectGreetingCopy({
    state: "needs_plan",
    display_name: "Andre",
    review_status: { state: "needs_clinician_review" },
  });
  assert.match(flagged, /draft plan is with your PT/, "same copy as pending_review");

  // No review_status -> "intake done, want a draft?" branch.
  const intakeOnly = selectGreetingCopy({
    state: "needs_plan",
    display_name: "Andre",
    review_status: null,
  });
  assert.match(intakeOnly, /intake is in/);
  assert.match(intakeOnly, /Draft next week/);

  // Name elision: null display_name -> "Welcome back." with no comma-name.
  const noName = selectGreetingCopy({
    state: "needs_plan",
    display_name: null,
    review_status: { state: "pending_review" },
  });
  assert.match(noName, /^Welcome back\. /, "name elided cleanly");
  assert.doesNotMatch(noName, /Welcome back, /);
}

// ── selectGreetingCopy: ready ───────────────────────────────────────────────
{
  // recently_approved — celebratory + start-session CTA.
  const approved = selectGreetingCopy({
    state: "ready",
    display_name: "Andre",
    review_status: { state: "recently_approved" },
  });
  assert.match(approved, /Welcome back, Andre\./);
  assert.match(approved, /just approved your plan/);
  assert.match(approved, /start today's session/);

  // recently_rejected — softer, points to chat for notes.
  const rejected = selectGreetingCopy({
    state: "ready",
    display_name: "Andre",
    review_status: { state: "recently_rejected" },
  });
  assert.match(rejected, /had some notes/);
  assert.match(rejected, /see the chat for details/);

  // Generic returning user — no review_status pill in flight.
  const generic = selectGreetingCopy({
    state: "ready",
    display_name: "Andre",
    review_status: null,
  });
  assert.match(generic, /How are you doing\?/);
  assert.match(generic, /log a check-in/);
}

// ── selectGreetingCopy: daysAway prepend ────────────────────────────────────
{
  // Single day -> singular "day", multi-day -> plural "days".
  const oneDay = selectGreetingCopy(
    { state: "ready", display_name: "Andre", review_status: null },
    { daysAway: 1 },
  );
  assert.match(oneDay, /Good to see you again — it's been 1 day\. /);

  const tenDays = selectGreetingCopy(
    { state: "ready", display_name: "Andre", review_status: null },
    { daysAway: 10 },
  );
  assert.match(tenDays, /Good to see you again — it's been 10 days\. /);

  // daysAway < 1 / NaN / null is suppressed.
  const noPrepend = selectGreetingCopy(
    { state: "ready", display_name: "Andre", review_status: null },
    { daysAway: 0 },
  );
  assert.doesNotMatch(noPrepend, /Good to see you again/);
}

// ── selectGreetingCopy: defensive fallback for unknown state ────────────────
{
  const unknown = selectGreetingCopy({
    state: "completely_made_up",
    display_name: "Andre",
    review_status: null,
  });
  // Doesn't fake a known state — falls back to a generic intro that does
  // NOT claim to know the patient's intake / protocol status.
  assert.match(unknown, /Hi, I'm Coach Maya/);
  assert.doesNotMatch(unknown, /intake is in/);
  assert.doesNotMatch(unknown, /Welcome back/);
}

// ── selectGreetingCopy: PHI hygiene — no display_name leak when null ────────
{
  // Sanity check that an empty/whitespace name doesn't render a stray
  // ", " in the copy. Regression guard against the "Welcome back, ."
  // bug the spec called out.
  const wsName = selectGreetingCopy({
    state: "ready",
    display_name: "   ",
    review_status: null,
  });
  assert.doesNotMatch(wsName, /Welcome back, /);
  assert.doesNotMatch(wsName, /, \./);
}

console.log("OK: state_aware_greeting helpers — all assertions passed");
