// Node-runnable unit test for the Settings v2 Coach Maya voice gate.
//
// Usage:
//   node frontend/tests/maya_voice_gate.test.js
//
// Same shape as payer_goals.test.js / state_aware_greeting.test.js — no Jest,
// no jsdom. The gate under test (mayaVoiceEnabled + the guard at the top of
// echoMayaCount) lives in app.js. This file mirrors that logic byte-for-byte
// with a localStorage stub. When the gate changes in app.js, change it here too.
//
// What this covers:
//   * mayaVoiceEnabled: default ON (no key), ON ("1"), OFF ("0")
//   * echoMayaCount: a no-op when voice is OFF (no Tavus interaction emitted),
//     and emits when voice is ON — proving one guard line silences both the
//     rep-count echo and the in-call form cue.

const assert = require("node:assert/strict");

// ── localStorage stub ─────────────────────────────────────────────────────
function makeLocalStorage(initial = {}) {
  const store = { ...initial };
  return {
    getItem: (k) => (k in store ? store[k] : null),
    setItem: (k, v) => { store[k] = String(v); },
    removeItem: (k) => { delete store[k]; },
  };
}

// ── Mirror of helpers from app.js ─────────────────────────────────────────
const MAYA_VOICE_KEY = "rac-maya-voice";

function mayaVoiceEnabled(localStorage) {
  try {
    return localStorage.getItem(MAYA_VOICE_KEY) !== "0";
  } catch (_) {
    return true;
  }
}

// echoMayaCount, reduced to the gate + a recording stub for _sendTavusInteraction.
// Mirrors the real guard order: voice gate FIRST, then the null guards.
function echoMayaCount(localStorage, ctx, word, cue) {
  const sent = [];
  function _sendTavusInteraction(payload) { sent.push(payload); }
  // The load-bearing gate (app.js):
  if (!mayaVoiceEnabled(localStorage)) return sent;
  if (!ctx.tavusCall || !ctx.tavusConvId || !word) return sent;
  _sendTavusInteraction({ event_type: "conversation.echo", text: String(word) });
  if (cue) _sendTavusInteraction({ event_type: "conversation.echo", text: String(cue) });
  return sent;
}

// ── mayaVoiceEnabled: default ON ──────────────────────────────────────────
assert.equal(mayaVoiceEnabled(makeLocalStorage()), true, "default (no key) is ON");
assert.equal(mayaVoiceEnabled(makeLocalStorage({ [MAYA_VOICE_KEY]: "1" })), true);
assert.equal(mayaVoiceEnabled(makeLocalStorage({ [MAYA_VOICE_KEY]: "0" })), false);

// ── echoMayaCount: OFF => no-op (silences count AND form cue) ──────────────
{
  const ls = makeLocalStorage({ [MAYA_VOICE_KEY]: "0" });
  const ctx = { tavusCall: {}, tavusConvId: "c1" };
  const sent = echoMayaCount(ls, ctx, "three", "knees over toes");
  assert.equal(sent.length, 0, "voice OFF must emit no Tavus interaction");
}

// ── echoMayaCount: ON => emits count + cue ─────────────────────────────────
{
  const ls = makeLocalStorage({ [MAYA_VOICE_KEY]: "1" });
  const ctx = { tavusCall: {}, tavusConvId: "c1" };
  const sent = echoMayaCount(ls, ctx, "three", "knees over toes");
  assert.equal(sent.length, 2, "voice ON emits the count + the form cue");
  assert.equal(sent[0].text, "three");
  assert.equal(sent[1].text, "knees over toes");
}

// ── echoMayaCount: ON but no active call => still a no-op (null guards) ─────
{
  const ls = makeLocalStorage({ [MAYA_VOICE_KEY]: "1" });
  const sent = echoMayaCount(ls, { tavusCall: null, tavusConvId: null }, "three");
  assert.equal(sent.length, 0, "no active Tavus call => no echo even when voice ON");
}

console.log("OK: maya voice gate — all assertions passed");
