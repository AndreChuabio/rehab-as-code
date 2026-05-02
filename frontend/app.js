const API_BASE = "";

const TRACE_GLYPH = {
  agent_started:   "[start]",
  file_read:       "[read]",
  file_edit:       "[edit]",
  tool_call:       "[tool]",
  branch_created:  "[branch]",
  commit_created:  "[commit]",
  pr_opened:       "[pr]",
  agent_completed: "[done]",
  agent_failed:    "[fail]",
};

let intakeComplete = localStorage.getItem("rehab_intake_complete") === "1";

document.addEventListener("DOMContentLoaded", () => {
  document.getElementById("dateDisplay").textContent = new Date().toLocaleDateString(
    "en-US", { weekday: "long", month: "long", day: "numeric" }
  );
  loadSidebar();
  loadProtocol();
  switchStage("chat");
  applyStepLocks();
  if (!intakeComplete) triggerIntake();
});

function applyStepLocks() {
  const locked = !intakeComplete;
  ["generatePlanBtn", "triggerCheckinBtn", "guidedExerciseBtn"].forEach((id) => {
    const btn = document.getElementById(id);
    if (!btn) return;
    btn.disabled = locked;
    btn.title = locked ? "Complete intake first" : btn.getAttribute("data-orig-title") || btn.title;
    if (!btn.getAttribute("data-orig-title")) btn.setAttribute("data-orig-title", btn.title);
  });
}

function onIntakeComplete() {
  intakeComplete = true;
  localStorage.setItem("rehab_intake_complete", "1");
  applyStepLocks();
  // Shift the primary highlight from intake → weekly plan
  document.getElementById("triggerIntakeBtn")?.classList.remove("primary");
  document.getElementById("generatePlanBtn")?.classList.add("primary");
  showToast("Intake complete — now generate your weekly plan!", "info");
}

// ---------------------------------------------------------------------------
// Stage tab toggle (chat | video)
// ---------------------------------------------------------------------------

function switchStage(mode) {
  const chatPane  = document.getElementById("stageChat");
  const videoPane = document.getElementById("stageVideo");
  const chatTab   = document.getElementById("tabChat");
  const videoTab  = document.getElementById("tabVideo");
  const isChat = mode === "chat";

  chatPane.hidden  = !isChat;
  videoPane.hidden =  isChat;
  chatTab.classList.toggle("active", isChat);
  videoTab.classList.toggle("active", !isChat);
  chatTab.setAttribute("aria-selected", isChat);
  videoTab.setAttribute("aria-selected", !isChat);

  if (isChat) {
    // Stop Tavus iframe when leaving video to free camera/mic
    const frame = document.getElementById("tavusFrame");
    if (frame && frame.style.display !== "none" && frame.src) {
      // Just hide; reload iframe only if user clicks Start Session again
    }
    document.getElementById("chatInput")?.focus();
  }
}

// ---------------------------------------------------------------------------
// Agent status chip (left rail)
// ---------------------------------------------------------------------------

function setAgentStatus(state, label) {
  const chip = document.querySelector(".agent-status");
  const dot  = document.getElementById("agentStatusDot");
  const txt  = document.getElementById("agentStatusLabel");
  if (!chip || !txt) return;
  chip.classList.remove("working", "done", "error");
  if (state) chip.classList.add(state);
  txt.textContent = label || state || "idle";
}

// ---------------------------------------------------------------------------
// Sidebar: wearable signals + calendar
// ---------------------------------------------------------------------------

async function loadSidebar() {
  try {
    const [healthRes, calRes] = await Promise.all([
      fetch(`${API_BASE}/health-data`),
      fetch(`${API_BASE}/calendar`),
    ]);
    const health = await healthRes.json();
    const cal = await calRes.json();
    renderHealth(health);
    renderCalendar(cal.events);
  } catch (e) {
    console.error("Failed to load sidebar data:", e);
    showToast("Could not connect to backend - is it running?", "error");
  }
}

function renderHealth(health) {
  const score = (val) => {
    const pct = val;
    const cls = pct >= 80 ? "good" : pct >= 60 ? "ok" : "low";
    return `<span class="score ${cls}">${val}</span>`;
  };
  document.getElementById("sleepScore").innerHTML =
    score(health.sleep_score) + "<small>/100</small>";
  document.getElementById("hrv").innerHTML = `${health.hrv_ms}<small>ms</small>`;
  document.getElementById("recovery").innerHTML =
    score(health.recovery_score) + "<small>/100</small>";

  const isLive = health.source === "apple_watch";
  const badge = document.getElementById("dataSourceBadge");
  if (badge) {
    badge.textContent = isLive ? "Live" : "Mock";
    badge.className = `source-badge ${isLive ? "live" : "mock"}`;
    badge.title = isLive
      ? `Apple Watch synced ${health.date}`
      : "Mock data - run the iOS Shortcut to sync Watch data";
  }
}

function renderCalendar(events) {
  const list = document.getElementById("eventList");
  if (!events || events.length === 0) {
    list.innerHTML = "<li class='loading-text'>No events today</li>";
    return;
  }
  list.innerHTML = events
    .map(
      (e) => `
    <li class="event-item ${e.type === "high_stakes" ? "high-stakes" : ""}">
      <span class="event-time">${e.time}</span>
      <span class="event-title">${e.title}</span>
    </li>
  `,
    )
    .join("");
}

// ---------------------------------------------------------------------------
// Protocol panel
// ---------------------------------------------------------------------------

async function loadProtocol() {
  try {
    const res = await fetch(`${API_BASE}/protocol`);
    const data = await res.json();
    renderProtocol(data);
    const link = document.getElementById("repoLink");
    if (link) {
      link.href = `https://github.com/${data.repo}/tree/main/protocols`;
      link.textContent = data.repo;
    }
  } catch (e) {
    console.error("Failed to load protocol:", e);
  }
}

async function demoReset() {
  const btn = document.getElementById("demoResetBtn");
  if (!confirm("Reset protocol.yaml on main to pending_intake? Wipes whatever the last demo run populated.")) {
    return;
  }
  if (btn) {
    btn.disabled = true;
    btn.textContent = "Resetting...";
  }
  try {
    const res = await fetch(`${API_BASE}/demo/reset`, { method: "POST" });
    if (!res.ok) {
      const err = await res.json().catch(() => ({}));
      throw new Error(err.detail || `reset failed: ${res.status}`);
    }
    showToast?.("Demo reset — protocol back to pending_intake", "info");
    loadProtocol();
  } catch (e) {
    console.error("demo reset failed", e);
    showToast?.(`Reset failed: ${e.message}`, "error");
  } finally {
    if (btn) {
      btn.disabled = false;
      btn.textContent = "Reset demo";
    }
  }
}

function renderProtocol({ protocol }) {
  const list = document.getElementById("protocolExercises");
  const meta = document.getElementById("protocolMeta");
  const exercises = protocol.exercises || [];
  const isPendingIntake =
    !exercises.length ||
    protocol.phase === "pending_intake" ||
    !protocol.patient;

  if (isPendingIntake) {
    meta.textContent = "no patient yet";
    list.innerHTML = `
      <li class="protocol-empty">
        <div class="empty-headline">Awaiting intake</div>
        <div class="empty-sub">Click <strong>1 intake</strong> below to onboard the patient. The cloud agent will generate the initial protocol.</div>
      </li>
    `;
    return;
  }

  meta.textContent = `${protocol.patient} - ${protocol.phase || "rehab"} - week ${protocol.week ?? "?"}`;
  list.innerHTML = exercises
    .map((ex) => {
      const parts = [];
      if (ex.sets && ex.reps) parts.push(`${ex.sets}x${ex.reps}`);
      if (ex.duration_min) parts.push(`${ex.duration_min} min`);
      if (ex.ROM_target_deg != null) parts.push(`ROM ${ex.ROM_target_deg} deg`);
      if (ex.intensity) parts.push(ex.intensity);
      const spec = parts.length ? parts.join(" - ") : "see protocol";
      return `
    <li class="protocol-exercise">
      <span class="ex-name">${escapeHtml(ex.name || "unnamed")}</span>
      <span class="ex-spec">${escapeHtml(spec)}</span>
    </li>
  `;
    })
    .join("");
}

// ---------------------------------------------------------------------------
// Tavus session
// ---------------------------------------------------------------------------

async function startSession() {
  const preSession = document.getElementById("preSession");
  const loading = document.getElementById("loadingSession");
  const frame = document.getElementById("tavusFrame");
  const btn = document.getElementById("startBtn");

  btn.disabled = true;
  preSession.style.display = "none";
  loading.style.display = "flex";

  try {
    const res = await fetch(`${API_BASE}/start-session`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ user_name: "Andre" }),
    });
    if (!res.ok) throw new Error(`Server error: ${res.status}`);
    const data = await res.json();

    loading.style.display = "none";

    if (data.conversation_url && !data.conversation_url.includes("mock")) {
      frame.src = data.conversation_url;
      frame.style.display = "block";
    } else {
      preSession.style.display = "flex";
      preSession.querySelector(".avatar-subtitle").textContent =
        data.greeting || "Mock mode - set Tavus keys for live video";
      btn.style.display = "none";
      showToast("Mock mode - set TAVUS_API_KEY for live video", "info");
    }

    if (data.recommendations?.length) {
      renderFocus(data.recommendations);
      document.getElementById("recommendations").style.display = "block";
    }
  } catch (e) {
    loading.style.display = "none";
    preSession.style.display = "flex";
    btn.disabled = false;
    console.error(e);
    showToast(`Error: ${e.message}`, "error");
  }
}

function renderFocus(items) {
  const container = document.getElementById("recCards");
  container.innerHTML = items
    .map(
      (r) => `
    <div class="rec-card priority-${r.priority}">
      <div class="rec-header">
        <span class="rec-title">${r.title}</span>
        <span class="rec-tag">${r.category}</span>
      </div>
      <p class="rec-detail">${r.detail}</p>
    </div>
  `,
    )
    .join("");
}

// ---------------------------------------------------------------------------
// Cloud agent invocation + SSE trace stream
// ---------------------------------------------------------------------------

async function invokeAgent(flow, body = {}) {
  // Make sure the trace shows up where the user is looking.
  switchStage("chat");
  resetAgentTeam();
  activateTeamNode("parent");
  setAgentStatus("working", `coordinator (${flow})`);
  setAgentButtonsDisabled(true);

  try {
    const payload = { flow, ...body };
    const res = await fetch(`${API_BASE}/agent/invoke`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    if (!res.ok) throw new Error(`invoke failed: ${res.status}`);
    const { invocation_id, pr_url, branch, provider } = await res.json();
    const providerEl = document.getElementById("providerName");
    if (providerEl) providerEl.textContent = provider;

    streamTrace(invocation_id, () => {
      if (pr_url) {
        renderPullRequest(pr_url, branch);
      }
      setAgentButtonsDisabled(false);
      setAgentStatus("done", "ready");
      // Pull the latest protocol from GitHub so the left rail reflects any
      // changes the agent committed. checkin / symptom flows may not change
      // protocol.yaml, but refreshing is cheap and keeps state consistent.
      refreshProtocol();
    });
  } catch (e) {
    console.error(e);
    showToast(`Agent invoke failed: ${e.message}`, "error");
    setAgentButtonsDisabled(false);
    setAgentStatus("error", "failed");
  }
}

// streamTrace renders trace events as a single inline chat bubble. Each
// invocation gets its own bubble; events stream into its <ol>. This
// preserves the chat-as-single-surface UX — no peer panels.
function streamTrace(invocationId, onDone) {
  const log = document.getElementById("chatLog");
  if (!log) return;

  const bubble = document.createElement("div");
  bubble.className = "chat-bubble agent-trace";
  bubble.innerHTML = `
    <div class="trace-header">cloud agent / ${escapeHtml(invocationId.slice(0, 8))}</div>
    <ol class="trace-list"></ol>
  `;
  log.appendChild(bubble);
  scrollChatLog?.();
  const traceList = bubble.querySelector(".trace-list");

  const url = `${API_BASE}/agent/stream/${encodeURIComponent(invocationId)}`;
  const source = new EventSource(url);
  let activeSubagent = null;

  source.onmessage = (e) => {
    try {
      const event = JSON.parse(e.data);

      const subagent = event?.payload?.subagent;
      if (subagent) {
        activateTeamNode(subagent);
        activeSubagent = subagent;
        setAgentStatus("working", `${subagent} working`);
      } else if (event.type === "pr_opened" || event.type === "agent_completed") {
        activeSubagent = null;
      }

      const li = document.createElement("li");
      li.className = `trace-event trace-${event.type}`;
      if (activeSubagent && !subagent) {
        li.classList.add("trace-child");
        li.dataset.subagent = activeSubagent;
      }
      const glyph = TRACE_GLYPH[event.type] || "[event]";
      const subBadge = subagent
        ? `<span class="trace-subagent">${escapeHtml(subagent)}</span>`
        : "";
      li.innerHTML = `
        <span class="trace-glyph">${glyph}</span>
        <span class="trace-ts">${event.timestamp.toFixed(1)}s</span>
        ${subBadge}
        <span class="trace-label">${escapeHtml(event.label)}</span>
      `;
      traceList.appendChild(li);
      scrollChatLog?.();
    } catch (err) {
      console.error("trace parse error", err);
    }
  };

  source.addEventListener("done", () => {
    source.close();
    onDone?.();
  });

  source.onerror = (err) => {
    console.warn("SSE closed", err);
    source.close();
    onDone?.();
  };
}

function resetAgentTeam() {
  document.querySelectorAll(".team-mini-node").forEach((n) => {
    n.classList.remove("active");
  });
}

function activateTeamNode(role) {
  const node = document.querySelector(`.team-mini-node[data-role="${role}"]`);
  if (node) node.classList.add("active");
}

// PR result also lives inline in chat. The clinician approves each PR
// explicitly via the Approve button — that's the audit story for judges:
// agent suggests, human applies. Click-through to GitHub for the diff.
function renderPullRequest(prUrl, branch) {
  const log = document.getElementById("chatLog");
  if (!log) return;
  const bubble = document.createElement("div");
  bubble.className = "chat-bubble pr-result";
  const safeUrl = escapeHtml(prUrl);
  bubble.innerHTML = `
    <div class="pr-result-header">pull request opened — awaiting approval</div>
    ${branch ? `<div class="pr-result-branch">branch: ${escapeHtml(branch)}</div>` : ""}
    <div class="pr-result-actions">
      <button class="pr-approve-btn" data-pr-url="${safeUrl}">Approve and apply</button>
      <a class="pr-result-cta" href="${safeUrl}" target="_blank" rel="noopener">View on GitHub</a>
    </div>
    <a class="pr-result-link" href="${safeUrl}" target="_blank" rel="noopener">${safeUrl}</a>
  `;
  bubble.querySelector(".pr-approve-btn").addEventListener("click", (e) => {
    applyPullRequest(prUrl, e.currentTarget);
  });
  log.appendChild(bubble);
  scrollChatLog?.();
}

async function applyPullRequest(prUrl, btn) {
  btn.disabled = true;
  btn.textContent = "Applying...";
  try {
    const res = await fetch(`${API_BASE}/pr/apply`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ pr_url: prUrl }),
    });
    if (!res.ok) {
      const err = await res.json().catch(() => ({}));
      throw new Error(err.detail || `apply failed: ${res.status}`);
    }
    const { pr_number } = await res.json();
    btn.textContent = `Applied to main (PR #${pr_number})`;
    btn.classList.add("applied");
    // Refresh Current Protocol card so the new state appears in the sidebar
    loadProtocol();
  } catch (e) {
    console.error("apply failed", e);
    btn.disabled = false;
    btn.textContent = "Approve and apply";
    showToast?.(`Apply failed: ${e.message}`, "error");
  }
}

function reportSymptom() {
  switchStage("chat");
  clearChatLog();
  activeFlow = { type: "symptom", step: 0, answers: {} };
  updateFlowUI(true);
  appendChatBubble("coach",
    "Let's log your symptom. Press Enter to use each default.\n\n" +
    SYMPTOM_QUESTIONS[0].q
  );
  setTimeout(prefillFlowInput, 50);
}

// ---------------------------------------------------------------------------
// Guided flows: intake / symptom / check-in
// ---------------------------------------------------------------------------

const INTAKE_QUESTIONS = [
  { key: "name",     q: "What's your name?",                                         default: "Andre",                     hint: "e.g. Andre" },
  { key: "age",      q: "How old are you?",                                           default: "26",                        hint: "e.g. 26" },
  { key: "injury",   q: "What was your injury or surgery?",                           default: "ACL reconstruction",        hint: "e.g. ACL reconstruction" },
  { key: "timing",   q: "When was your surgery or injury?",                           default: "3 weeks ago",               hint: "e.g. 3 weeks ago" },
  { key: "pain",     q: "On a scale of 1–10, what's your current pain level?",       default: "3",                         hint: "e.g. 3" },
  { key: "symptoms", q: "Any specific symptoms? (press Enter to use the default)",   default: "mild pain at 110° flexion", hint: "e.g. mild pain at 110° flexion" },
];

const SYMPTOM_QUESTIONS = [
  { key: "location",  q: "Where is the pain or discomfort?",          default: "inner knee",               hint: "e.g. inner knee" },
  { key: "type",      q: "How would you describe it?",                 default: "dull ache",                hint: "e.g. sharp, dull, ache, tightness" },
  { key: "level",     q: "Pain level 1–10?",                           default: "4",                        hint: "e.g. 4" },
  { key: "trigger",   q: "When does it happen?",                       default: "during single-leg squats", hint: "e.g. during single-leg squats" },
  { key: "duration",  q: "How long has this been going on?",           default: "started today",            hint: "e.g. started today" },
];

const CHECKIN_QUESTIONS = [
  { key: "rating",     q: "How did today's session go overall? (1–10)",                             default: "8",                                        hint: "e.g. 8" },
  { key: "completed",  q: "Which exercises did you complete?",                                      default: "heel slides, quad sets, stationary bike",   hint: "e.g. heel slides, quad sets" },
  { key: "strong",     q: "What felt strong or improved today?",                                    default: "quad set felt stronger",                   hint: "e.g. quad set felt stronger than yesterday" },
  { key: "difficult",  q: "Anything that felt difficult or caused discomfort? (or type \"none\")", default: "none",                                      hint: "e.g. single-leg balance was shaky" },
];

const FLOW_META = {
  intake:  { questions: INTAKE_QUESTIONS,  label: "Intake" },
  symptom: { questions: SYMPTOM_QUESTIONS, label: "Symptom" },
  checkin: { questions: CHECKIN_QUESTIONS, label: "Check-in" },
};

let activeFlow = null; // { type, step, answers }

function triggerIntake() {
  switchStage("chat");
  clearChatLog();
  activeFlow = { type: "intake", step: 0, answers: {} };
  updateFlowUI(true);
  appendChatBubble("coach",
    "I'll walk you through a quick intake. Press Enter to use each default — it's fast for demo.\n\n" +
    INTAKE_QUESTIONS[0].q
  );
  // Defer so the input is pre-filled after any pending DOM flushes
  setTimeout(prefillFlowInput, 50);
}

function prefillFlowInput() {
  if (!activeFlow) return;
  const meta = FLOW_META[activeFlow.type];
  const q = meta.questions[activeFlow.step];
  const input = document.getElementById("chatInput");
  if (input && q) {
    input.value = q.default || "";
    input.placeholder = q.hint || "Type your answer...";
    input.select();
    input.focus();
  }
}

function cancelFlow() {
  const label = activeFlow ? FLOW_META[activeFlow.type]?.label : "Flow";
  activeFlow = null;
  updateFlowUI(false);
  resetInputPlaceholder();
  appendChatBubble("coach", `${label} cancelled.`);
}

function updateFlowUI(active) {
  const bar = document.getElementById("intakeProgressBar");
  const cancelBtn = document.getElementById("intakeCancelBtn");
  const suggestions = document.getElementById("chatSuggestions");
  const quickActions = document.querySelector(".quick-actions");
  if (bar) bar.style.display = active ? "flex" : "none";
  if (cancelBtn) cancelBtn.style.display = active ? "inline-flex" : "none";
  if (suggestions) suggestions.style.display = active ? "none" : "flex";
  if (quickActions) quickActions.style.display = active ? "none" : "flex";
  if (active) updateFlowProgress();
}

function updateFlowProgress() {
  if (!activeFlow) return;
  const meta = FLOW_META[activeFlow.type];
  const total = meta.questions.length;
  const label = document.getElementById("intakeProgressLabel");
  if (label) label.textContent = `${meta.label} — question ${activeFlow.step + 1} of ${total}`;
  const fill = document.getElementById("intakeProgressFill");
  if (fill) fill.style.width = `${(activeFlow.step / total) * 100}%`;
  prefillFlowInput();
}

function resetInputPlaceholder() {
  const input = document.getElementById("chatInput");
  if (input) input.placeholder = "Type to Coach Maya - ask, swap, plan, log...";
}

function clearChatLog() {
  const log = document.getElementById("chatLog");
  if (!log) return;
  log.innerHTML = `<div class="chat-empty" id="chatEmpty" style="display:none"></div>`;
}

function handleFlowAnswer(text) {
  if (!activeFlow) return false;
  if (text.toLowerCase() === "cancel") { cancelFlow(); return true; }

  const meta = FLOW_META[activeFlow.type];
  const q = meta.questions[activeFlow.step];
  activeFlow.answers[q.key] = text || q.default || "";
  activeFlow.step++;

  if (activeFlow.step < meta.questions.length) {
    updateFlowProgress();
    appendChatBubble("coach", meta.questions[activeFlow.step].q);
    return true;
  }

  // All questions answered — build payload and submit
  const a = activeFlow.answers;
  const type = activeFlow.type;
  activeFlow = null;
  updateFlowUI(false);
  resetInputPlaceholder();

  if (type === "intake") {
    const intake_text =
      `${a.name}, ${a.age} years old. Injury: ${a.injury}, ${a.timing}. ` +
      `Pain level ${a.pain}/10. Symptoms: ${a.symptoms}`;
    appendChatBubble("coach", "Got it! Generating your personalized protocol...");
    invokeAgent("intake", { intake_text });
    onIntakeComplete();

  } else if (type === "symptom") {
    const symptom_text =
      `${a.location} — ${a.type}, level ${a.level}/10. ` +
      `Occurs ${a.trigger}. Duration: ${a.duration}`;
    appendChatBubble("coach", "Logged. Adjusting your protocol...");
    invokeAgent("symptom_adjustment", { symptom_text });

  } else if (type === "checkin") {
    const checkin_text =
      `Session rating ${a.rating}/10. Completed: ${a.completed}. ` +
      `Strong: ${a.strong}. Difficult: ${a.difficult}`;
    appendChatBubble("coach", "Check-in logged! Starting your video session with Coach Maya...");
    invokeAgent("checkin", { checkin_text });
    // Auto-switch to video call after check-in
    setTimeout(() => switchStage("video"), 1800);
  }

  return true;
}

function triggerCheckin() {
  switchStage("chat");
  clearChatLog();
  activeFlow = { type: "checkin", step: 0, answers: {} };
  updateFlowUI(true);
  appendChatBubble("coach",
    "Quick session check-in! Press Enter to accept each default.\n\n" +
    CHECKIN_QUESTIONS[0].q
  );
  setTimeout(prefillFlowInput, 50);
}

function setAgentButtonsDisabled(disabled) {
  ["generatePlanBtn", "triggerIntakeBtn", "triggerCheckinBtn", "guidedExerciseBtn"].forEach((id) => {
    const btn = document.getElementById(id);
    if (btn) btn.disabled = disabled;
  });
}

// ---------------------------------------------------------------------------
// Step 4: Guided Exercise — load all protocol exercises as video cards
// ---------------------------------------------------------------------------

async function loadGuidedExercises() {
  switchStage("chat");
  clearChatLog();
  const log = document.getElementById("chatLog");

  const header = document.createElement("div");
  header.className = "chat-bubble coach";
  header.innerHTML = "<strong>Your Guided Exercise Session</strong><br>Here's your full plan for this week. Watch each video, then chat with me below.";
  log.appendChild(header);
  scrollChatLog();

  try {
    const res = await fetch(`${API_BASE}/protocol/exercises`);
    if (!res.ok) throw new Error(`status ${res.status}`);
    const data = await res.json();
    const exercises = data.exercises || [];

    if (!exercises.length) {
      const empty = document.createElement("div");
      empty.className = "chat-bubble coach";
      empty.textContent = "No exercises found — generate your weekly plan first (step 2).";
      log.appendChild(empty);
      scrollChatLog();
      return;
    }

    exercises.forEach((ex) => renderExerciseCard(ex));

    const footer = document.createElement("div");
    footer.className = "chat-bubble coach";
    footer.textContent = `${exercises.length} exercises loaded. Ask me about any of them or tell me how the session went.`;
    log.appendChild(footer);
    scrollChatLog();
  } catch (e) {
    const err = document.createElement("div");
    err.className = "chat-bubble error";
    err.textContent = `Could not load exercises: ${e.message}`;
    log.appendChild(err);
    scrollChatLog();
  }
}

// ---------------------------------------------------------------------------
// Coach chat (OpenAI-driven, grounded in exercise library)
// ---------------------------------------------------------------------------

// Conversation history sent to /chat on each turn. Coach-bubble text is
// reconstructed from streamed token deltas before being committed here.
const chatHistory = [];

const CHAT_TOOL_GLYPH = {
  recommend_exercise:        "video",
  list_phase_exercises:      "library",
  fire_symptom_trigger:      "symptom \u2192 PR",
  fire_intake_trigger:       "intake \u2192 PR",
  fire_checkin_trigger:      "check-in \u2192 PR",
  fire_weekly_plan_trigger:  "weekly plan \u2192 PR",
};

function onChatSubmit(event) {
  event.preventDefault();
  const input = document.getElementById("chatInput");
  const text = input.value.trim();
  if (!text) return;
  input.value = "";
  appendChatBubble("user", text);
  if (handleFlowAnswer(text)) return;
  sendChat(text, { skipUserBubble: true });
}

function sendChatPreset(text) {
  const input = document.getElementById("chatInput");
  if (input) input.value = "";
  sendChat(text);
}

async function sendChat(message, { skipUserBubble = false } = {}) {
  const empty = document.getElementById("chatEmpty");
  if (empty) empty.remove();

  const sendBtn = document.getElementById("chatSendBtn");
  const input = document.getElementById("chatInput");
  setChatBusy(true, sendBtn, input);

  if (!skipUserBubble) appendChatBubble("user", message);
  const coachBubble = appendChatBubble("coach", "", { thinking: true });

  let coachBuffer = "";
  let coachClosed = false;
  const closeCoach = () => {
    if (coachClosed) return;
    coachClosed = true;
    coachBubble.classList.remove("thinking");
    if (!coachBuffer.trim()) {
      coachBubble.remove();
      return;
    }
    coachBubble.innerHTML = renderCoachMarkdown(coachBuffer);
  };

  try {
    const res = await fetch(`${API_BASE}/chat`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        session_id: "default",
        message,
        history: chatHistory.slice(-10),
      }),
    });
    if (!res.ok || !res.body) throw new Error(`chat failed: ${res.status}`);

    const reader = res.body.getReader();
    const decoder = new TextDecoder();
    let buf = "";

    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      buf += decoder.decode(value, { stream: true });

      let sepIndex;
      while ((sepIndex = buf.indexOf("\n\n")) !== -1) {
        const block = buf.slice(0, sepIndex);
        buf = buf.slice(sepIndex + 2);
        const lines = block.split("\n");
        let dataLine = "";
        for (const line of lines) {
          if (line.startsWith("data:")) dataLine += line.slice(5).trim();
        }
        if (!dataLine || dataLine === "{}") continue;

        let event;
        try { event = JSON.parse(dataLine); } catch (e) { continue; }
        handleChatEvent(event, coachBubble, (delta) => { coachBuffer += delta; });
        if (event.type === "done") {
          closeCoach();
        }
      }
    }
  } catch (err) {
    console.error("chat error", err);
    appendChatBubble("error", `chat failed: ${err.message}`);
  } finally {
    closeCoach();
    if (coachBuffer.trim()) {
      chatHistory.push({ role: "user", content: message });
      chatHistory.push({ role: "assistant", content: coachBuffer.trim() });
    } else {
      chatHistory.push({ role: "user", content: message });
    }
    setChatBusy(false, sendBtn, input);
    input?.focus();
  }
}

function handleChatEvent(event, coachBubble, appendDelta) {
  switch (event.type) {
    case "token":
      coachBubble.classList.remove("thinking");
      coachBubble.textContent += event.delta || "";
      appendDelta(event.delta || "");
      scrollChatLog();
      break;

    case "card":
      renderExerciseCard(event.card);
      break;

    case "tool_call":
      renderToolLine(event);
      // Light up the team-mini strip the moment chat fires an agent so the
      // audience sees the chat-to-orchestrator link without a panel switch.
      if (String(event.name || "").startsWith("fire_")) {
        resetAgentTeam();
        activateTeamNode("parent");
        setAgentStatus("working", "coordinator (chat)");
      }
      break;

    case "tool_result":
      // Real fire_*_trigger results carry an invocation_id; stream the trace
      // inline as a chat bubble. Library lookup tools have no invocation_id;
      // they render as the existing tool-result line only.
      if (event.result?.invocation_id) {
        renderToolResultLine(event);
        streamTrace(event.result.invocation_id, () => {
          if (event.result.pr_url) {
            renderPullRequest(event.result.pr_url, event.result.branch);
          }
          setAgentStatus("done", "ready");
          refreshProtocol();
        });
        const providerEl = document.getElementById("providerName");
        if (providerEl) {
          providerEl.textContent = event.result.provider || "cached_replay";
        }
      }
      break;

    case "error":
      appendChatBubble("error", event.message || "chat error");
      break;

    case "done":
      // closeCoach() is invoked by the caller
      break;
  }
}

function appendChatBubble(role, text, opts = {}) {
  const log = document.getElementById("chatLog");
  const bubble = document.createElement("div");
  bubble.className = `chat-bubble ${role}${opts.thinking ? " thinking" : ""}`;
  bubble.textContent = text;
  log.appendChild(bubble);
  scrollChatLog();
  return bubble;
}

function renderToolLine(event) {
  const log = document.getElementById("chatLog");
  const line = document.createElement("div");
  line.className = "chat-tool-line";
  const label = CHAT_TOOL_GLYPH[event.name] || event.name;
  let detail = "";
  const args = event.arguments || {};
  if (args.exercise_id) detail = `${args.exercise_id}`;
  else if (args.phase) detail = `phase: ${args.phase}`;
  else if (args.symptom_text) detail = `"${truncate(args.symptom_text, 50)}"`;
  else if (args.intake_text) detail = `"${truncate(args.intake_text, 50)}"`;
  else if (args.checkin_text) detail = `"${truncate(args.checkin_text, 50)}"`;
  line.innerHTML = `
    <span class="tool-glyph">[${escapeHtml(label)}]</span>
    <span>${escapeHtml(detail)}</span>
  `;
  log.appendChild(line);
  scrollChatLog();
}

function renderToolResultLine(event) {
  const log = document.getElementById("chatLog");
  const line = document.createElement("div");
  line.className = "chat-tool-line";
  const result = event.result || {};
  const provider = result.provider || "agent";
  const pr = result.pr_url
    ? `<a href="${escapeHtml(result.pr_url)}" target="_blank">${escapeHtml(result.branch || "PR")}</a>`
    : `<span>queued</span>`;
  line.innerHTML = `
    <span class="tool-glyph">[orchestrator]</span>
    <span>${escapeHtml(provider)}</span>
    ${pr}
  `;
  log.appendChild(line);
  scrollChatLog();
}

function renderExerciseCard(card) {
  if (!card) return;
  const log = document.getElementById("chatLog");
  const wrap = document.createElement("div");
  wrap.className = "exercise-card";
  const cuesHtml = (card.cues || [])
    .map((c) => `<li>${escapeHtml(c)}</li>`)
    .join("");
  const dose = card.default_dose
    ? `<span class="exercise-dose">${escapeHtml(card.default_dose)}</span>`
    : "";

  // Source-of-truth precedence: Sora-generated MP4 > YouTube iframe > thumbnail.
  // The agent abstraction picks one server-side; the frontend just renders.
  let embed;
  let sourceBadge = "";
  if (card.generated_video_url) {
    embed = `<div class="exercise-video-wrap">
      <video
        src="${escapeHtml(card.generated_video_url)}"
        controls
        autoplay
        muted
        playsinline
        loop
        preload="metadata"></video>
    </div>`;
    sourceBadge = `<span class="video-source sora">sora-2 generated</span>`;
  } else if (card.youtube_embed_url) {
    embed = `<div class="exercise-video-wrap">
      <iframe
        src="${escapeHtml(card.youtube_embed_url)}"
        title="${escapeHtml(card.name || "exercise video")}"
        allow="accelerometer; autoplay; clipboard-write; encrypted-media; gyroscope; picture-in-picture"
        allowfullscreen></iframe>
    </div>`;
    sourceBadge = `<span class="video-source youtube">curated</span>`;
  } else {
    embed = `<div class="exercise-video-wrap">
      <img src="${escapeHtml(card.thumbnail_url || "")}" alt="${escapeHtml(card.name || "")}" />
    </div>`;
  }

  const watch = card.youtube_watch_url
    ? `<a class="exercise-action-btn" href="${escapeHtml(card.youtube_watch_url)}" target="_blank">reference clip</a>`
    : "";

  wrap.innerHTML = `
    ${embed}
    <div class="exercise-meta">
      <div class="exercise-title-row">
        <span class="exercise-title">${escapeHtml(card.name || card.id || "")}</span>
        ${dose}
        ${sourceBadge}
      </div>
      <ul class="exercise-cues">${cuesHtml}</ul>
      <div class="exercise-actions">
        <button class="exercise-action-btn primary"
                data-add-id="${escapeHtml(card.id || "")}"
                data-add-name="${escapeHtml(card.name || card.id || "")}"
                onclick="addToTodayFromBtn(this)">
          Add to today
        </button>
        ${watch}
      </div>
    </div>
  `;
  log.appendChild(wrap);
  scrollChatLog();
}

function scrollChatLog() {
  const log = document.getElementById("chatLog");
  if (log) log.scrollTop = log.scrollHeight;
}

// ---------------------------------------------------------------------------
// Today's session (ephemeral, local-only)
// ---------------------------------------------------------------------------
// Adds from the chat exercise cards land here, NOT in the protocol. Protocol
// changes go through a real cloud-agent flow (weekly_plan / symptom /
// intake / checkin). "Add to today" is a click-through to "I'll do this in
// today's workout" - no PR, no waiting.

const todaySession = [];

function addToTodayFromBtn(btn) {
  const id   = btn.dataset.addId   || "";
  const name = btn.dataset.addName || id || "exercise";
  if (!id) return;
  if (todaySession.some((e) => e.id === id)) {
    showToast(`${name} is already in today's session`, "info");
    return;
  }
  todaySession.push({ id, name, addedAt: new Date().toISOString() });
  renderTodaySession();
  appendChatBubble("coach", `Added ${name} to today's session.`);
}

function renderTodaySession() {
  const card = document.getElementById("todaySessionCard");
  const list = document.getElementById("todaySessionList");
  if (!card || !list) return;
  if (!todaySession.length) {
    card.style.display = "none";
    return;
  }
  card.style.display = "block";
  list.innerHTML = todaySession
    .map(
      (e) => `
    <li class="today-session-item">
      <span class="today-session-name">${escapeHtml(e.name)}</span>
      <button class="today-session-remove" onclick="removeFromToday('${escapeHtml(e.id)}')"
              title="Remove">x</button>
    </li>
  `,
    )
    .join("");
}

function removeFromToday(id) {
  const i = todaySession.findIndex((e) => e.id === id);
  if (i >= 0) {
    todaySession.splice(i, 1);
    renderTodaySession();
  }
}

// ---------------------------------------------------------------------------
// Protocol re-fetch (called after a real protocol-changing PR opens)
// ---------------------------------------------------------------------------
async function refreshProtocol() {
  try {
    const res = await fetch(`${API_BASE}/protocol`);
    const data = await res.json();
    renderProtocol(data);
  } catch (e) {
    console.error("protocol refresh failed:", e);
  }
}

function setChatBusy(busy, sendBtn, input) {
  if (sendBtn) sendBtn.disabled = busy;
  if (input) input.disabled = busy;
  document.querySelectorAll(".chat-chip").forEach((c) => { c.disabled = busy; });
}

function truncate(s, n) {
  s = String(s || "");
  return s.length > n ? s.slice(0, n - 1) + "\u2026" : s;
}

// ---------------------------------------------------------------------------
// Utilities
// ---------------------------------------------------------------------------

function renderCoachMarkdown(text) {
  return escapeHtml(text).replace(
    /\[([^\]]+)\]\((https?:\/\/[^\s)]+)\)/g,
    '<a href="$2" target="_blank" rel="noopener">$1</a>',
  );
}

function escapeHtml(s) {
  return String(s)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}

function showToast(msg, type = "info") {
  const toast = document.getElementById("toast");
  toast.textContent = msg;
  toast.className = `toast show ${type}`;
  setTimeout(() => toast.classList.remove("show"), 4000);
}
