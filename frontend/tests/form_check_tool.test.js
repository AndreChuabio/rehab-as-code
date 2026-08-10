// Node-runnable unit tests for the monitor_exercise_form app-message parser.
//
// Usage:
//   node frontend/tests/form_check_tool.test.js
//
// Same shape as flow_stitching.test.js / state_aware_greeting.test.js - no Jest,
// no jsdom. parseTavusToolCall is DOM-free + side-effect-free so it runs in
// plain Node. It lives in app.js as window.__formCheckTool.parseTavusToolCall;
// this file mirrors it byte-for-byte. When the parser changes in app.js, change
// it here too.
//
// What this covers: the discriminator (only conversation.tool_call for
// monitor_exercise_form passes), the doc-unverified pieces the handler must
// absorb (properties nesting vs flat, arguments JSON-string vs object), the
// start/stop mapping + start default, and the channel noise it must ignore
// (utterances, perception tool calls, our own echo/interrupt, other tools).

const assert = require("node:assert/strict");

// ── Mirror of parseTavusToolCall from app.js ────────────────────────────────

function parseTavusToolCall(data) {
  if (!data || data.message_type !== "conversation"
      || data.event_type !== "conversation.tool_call") return null;
  const p = (data.properties && typeof data.properties === "object")
    ? data.properties : data;
  const name = p.name || p.tool_name;
  if (name !== "monitor_exercise_form") return null;
  let args = p.arguments;
  if (typeof args === "string") {
    try { args = JSON.parse(args || "{}"); } catch (_) { args = {}; }
  }
  if (!args || typeof args !== "object") args = {};
  const action = String(args.action || "start").toLowerCase() === "stop"
    ? "stop" : "start";
  return { action, toolCallId: p.tool_call_id || null };
}

// ── Helpers ─────────────────────────────────────────────────────────────────

function toolCall(properties) {
  return {
    message_type: "conversation",
    event_type: "conversation.tool_call",
    conversation_id: "c123",
    properties,
  };
}

let passed = 0;
function check(label, got, want) {
  assert.deepEqual(got, want, label);
  passed++;
}

// ── Happy path: properties nesting, arguments as a JSON string ───────────────

check(
  "start: properties + JSON-string args",
  parseTavusToolCall(toolCall({
    name: "monitor_exercise_form",
    arguments: JSON.stringify({ action: "start" }),
    tool_call_id: "call_1",
  })),
  { action: "start", toolCallId: "call_1" },
);

check(
  "stop: properties + JSON-string args",
  parseTavusToolCall(toolCall({
    name: "monitor_exercise_form",
    arguments: JSON.stringify({ action: "stop" }),
    tool_call_id: "call_2",
  })),
  { action: "stop", toolCallId: "call_2" },
);

// ── Defensive: arguments as an OBJECT (app_message may pre-parse) ────────────

check(
  "start: arguments already an object",
  parseTavusToolCall(toolCall({
    name: "monitor_exercise_form",
    arguments: { action: "start" },
  })),
  { action: "start", toolCallId: null },
);

// ── Defensive: FLAT payload (no properties nesting) ─────────────────────────

check(
  "stop: flat payload, no properties key",
  parseTavusToolCall({
    message_type: "conversation",
    event_type: "conversation.tool_call",
    name: "monitor_exercise_form",
    arguments: JSON.stringify({ action: "stop" }),
    tool_call_id: "call_flat",
  }),
  { action: "stop", toolCallId: "call_flat" },
);

// ── Defaults + malformed args ───────────────────────────────────────────────

check(
  "missing action -> defaults to start",
  parseTavusToolCall(toolCall({ name: "monitor_exercise_form", arguments: "{}" })),
  { action: "start", toolCallId: null },
);

check(
  "unparseable arguments string -> defaults to start",
  parseTavusToolCall(toolCall({ name: "monitor_exercise_form", arguments: "{not json" })),
  { action: "start", toolCallId: null },
);

check(
  "unknown action value -> falls back to start",
  parseTavusToolCall(toolCall({
    name: "monitor_exercise_form",
    arguments: JSON.stringify({ action: "pause" }),
  })),
  { action: "start", toolCallId: null },
);

check(
  "STOP uppercased -> normalized to stop",
  parseTavusToolCall(toolCall({
    name: "monitor_exercise_form",
    arguments: JSON.stringify({ action: "STOP" }),
  })),
  { action: "stop", toolCallId: null },
);

// ── Channel noise the handler MUST ignore (returns null) ─────────────────────

check("null data -> null", parseTavusToolCall(null), null);
check("undefined data -> null", parseTavusToolCall(undefined), null);

check(
  "utterance event -> null",
  parseTavusToolCall({
    message_type: "conversation", event_type: "conversation.utterance",
    properties: { text: "hello" },
  }),
  null,
);

check(
  "our own echo event -> null",
  parseTavusToolCall({
    message_type: "conversation", event_type: "conversation.echo",
    properties: { text: "seven" },
  }),
  null,
);

check(
  "interrupt event -> null",
  parseTavusToolCall({ message_type: "conversation", event_type: "conversation.interrupt" }),
  null,
);

check(
  "perception_tool_call (vision-origin) -> null",
  parseTavusToolCall({
    message_type: "conversation", event_type: "conversation.perception_tool_call",
    properties: { name: "monitor_exercise_form", frames: [] },
  }),
  null,
);

check(
  "wrong message_type -> null",
  parseTavusToolCall({
    message_type: "system", event_type: "conversation.tool_call",
    properties: { name: "monitor_exercise_form" },
  }),
  null,
);

check(
  "a DIFFERENT tool's tool_call -> null",
  parseTavusToolCall(toolCall({
    name: "get_patient_protocols", arguments: "{}", tool_call_id: "call_x",
  })),
  null,
);

console.log(`form_check_tool.test.js: ${passed}/${passed} checks passed`);
