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

// ---------------------------------------------------------------------------
// Step state (persisted in localStorage for demo)
// ---------------------------------------------------------------------------

let currentStep = 1;
let intakeComplete = localStorage.getItem("rehab_intake_complete") === "1";

document.addEventListener("DOMContentLoaded", () => {
  document.getElementById("dateDisplay").textContent = new Date().toLocaleDateString(
    "en-US", { weekday: "long", month: "long", day: "numeric" }
  );
  initStepState();
  // Auto-start intake on first load
  if (!intakeComplete) startIntakeFlow();
});

function initStepState() {
  if (intakeComplete) {
    for (let i = 2; i <= 4; i++) {
      document.getElementById(`stepBtn${i}`)?.classList.remove("locked");
    }
    document.getElementById("stepBtn1")?.classList.add("done");
    // Sidebar only revealed after plan is generated
    const planComplete = localStorage.getItem("rehab_plan_complete") === "1";
    if (planComplete) {
      document.getElementById("stepBtn2")?.classList.add("done");
      const sidebar = document.getElementById("sidebar");
      if (sidebar) sidebar.hidden = false;
      loadSidebar();
      loadProtocol();
    }
  }
}

function switchStep(n) {
  if (n !== 1 && !intakeComplete) {
    showToast("Complete intake first to unlock this step.", "error");
    return;
  }
  currentStep = n;
  for (let i = 1; i <= 4; i++) {
    document.getElementById(`stepBtn${i}`)?.classList.toggle("active", i === n);
    document.getElementById(`stepBtn${i}`)?.setAttribute("aria-selected", String(i === n));
    const pane = document.getElementById(`pane${i}`);
    if (pane) pane.hidden = i !== n;
  }
  if (n === 3 && !activeFlow) startCheckinFlow();
  if (n === 4) loadExercises();
}

function onIntakeComplete() {
  intakeComplete = true;
  localStorage.setItem("rehab_intake_complete", "1");
  for (let i = 2; i <= 4; i++) {
    document.getElementById(`stepBtn${i}`)?.classList.remove("locked");
  }
  document.getElementById("stepBtn1")?.classList.add("done");
  // Sidebar stays hidden until the weekly plan is generated (onPlanGenerated)
  showToast("Intake complete — now generate your weekly plan!", "info");
  setTimeout(() => switchStep(2), 1600);
}

function onPlanGenerated() {
  localStorage.setItem("rehab_plan_complete", "1");
  document.getElementById("stepBtn2")?.classList.add("done");
  const sidebar = document.getElementById("sidebar");
  if (sidebar) {
    sidebar.hidden = false;
    loadSidebar();
    loadProtocol();
  }
}

// ---------------------------------------------------------------------------
// Agent status chip
// ---------------------------------------------------------------------------

function setAgentStatus(state, label) {
  const chip = document.querySelector(".agent-status");
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
    const cls = val >= 80 ? "good" : val >= 60 ? "ok" : "low";
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
  if (!list) return;
  if (!events || events.length === 0) {
    list.innerHTML = "<li class='loading-text'>No events today</li>";
    return;
  }
  list.innerHTML = events
    .map((e) => `
    <li class="event-item ${e.type === "high_stakes" ? "high-stakes" : ""}">
      <span class="event-time">${e.time}</span>
      <span class="event-title">${e.title}</span>
    </li>`)
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

function renderProtocol({ protocol }) {
  const meta = `${protocol.patient || "patient"} - ${protocol.phase || "rehab"} - week ${protocol.week ?? "?"}`;
  const metaEl = document.getElementById("protocolMeta");
  if (metaEl) metaEl.textContent = meta;
  const list = document.getElementById("protocolExercises");
  if (!list) return;
  const exercises = protocol.exercises || [];
  if (!exercises.length) {
    list.innerHTML = "<li class='loading-text'>(empty protocol)</li>";
    return;
  }
  list.innerHTML = exercises.map((ex) => {
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
    </li>`;
  }).join("");
}

async function refreshProtocol() {
  try {
    const res = await fetch(`${API_BASE}/protocol`);
    const data = await res.json();
    renderProtocol(data);
  } catch (e) {
    console.error("protocol refresh failed:", e);
  }
}

// ---------------------------------------------------------------------------
// Step 4: exercise session view
// ---------------------------------------------------------------------------

async function loadExercises() {
  const grid = document.getElementById("exerciseGrid");
  if (!grid) return;
  try {
    const res = await fetch(`${API_BASE}/protocol/exercises`);
    const data = await res.json();
    const titleEl = document.getElementById("sessionTitle");
    const metaEl  = document.getElementById("sessionMeta");
    if (titleEl) titleEl.textContent = `${data.patient || "Patient"}'s Session`;
    if (metaEl)  metaEl.textContent  = `${data.phase || "rehab"} · Week ${data.week ?? "?"}`;
    if (!data.exercises?.length) {
      grid.innerHTML = "<p class='loading-text'>No exercises found in current protocol.</p>";
      return;
    }
    grid.innerHTML = data.exercises.map((ex) => {
      const cuesHtml = ex.cues.map((c) => `<li>${escapeHtml(c)}</li>`).join("");
      const thumb = ex.thumbnail_url
        ? `<a href="${escapeHtml(ex.youtube_watch_url || "#")}" target="_blank" rel="noopener">
             <img class="ex-thumb" src="${escapeHtml(ex.thumbnail_url)}" alt="${escapeHtml(ex.name)}" />
             <span class="ex-thumb-play">▶</span>
           </a>`
        : `<div class="ex-thumb-placeholder">${escapeHtml(ex.name)}</div>`;
      return `
      <div class="session-exercise-card">
        <div class="ex-thumb-wrap">${thumb}</div>
        <div class="ex-info">
          <div class="ex-title-row">
            <span class="ex-name-lg">${escapeHtml(ex.name)}</span>
            <span class="ex-spec-badge">${escapeHtml(ex.spec)}</span>
          </div>
          ${cuesHtml ? `<ul class="ex-cues-list">${cuesHtml}</ul>` : ""}
          ${ex.youtube_watch_url
            ? `<a class="ex-watch-btn" href="${escapeHtml(ex.youtube_watch_url)}" target="_blank" rel="noopener">Watch video</a>`
            : ""}
        </div>
      </div>`;
    }).join("");
  } catch (e) {
    console.error("loadExercises failed:", e);
    if (grid) grid.innerHTML = "<p class='loading-text'>Could not load exercises.</p>";
  }
}

// ---------------------------------------------------------------------------
// Cloud agent invocation + SSE trace
// ---------------------------------------------------------------------------

async function invokeAgent(flow, body = {}) {
  // For step 2 (weekly plan), use the dedicated plan pane trace/diff.
  // For other steps, stream into the active chat log.
  const isStep2 = currentStep === 2;
  if (isStep2) {
    const planCta = document.getElementById("planCta");
    const traceCard = document.getElementById("agentTraceCard");
    if (planCta) planCta.style.display = "none";
    if (traceCard) traceCard.style.display = "block";
  }

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

    const traceTarget = isStep2
      ? document.getElementById("traceLog")
      : document.getElementById(currentStep === 3 ? "checkinLog" : "chatLog");

    streamTrace(invocation_id, traceTarget, isStep2, () => {
      if (pr_url) renderPullRequest(pr_url, branch, isStep2);
      setAgentButtonsDisabled(false);
      setAgentStatus("done", "ready");
      refreshProtocol();
      if (flow === "weekly_plan") onPlanGenerated();
    });
  } catch (e) {
    console.error(e);
    showToast(`Agent invoke failed: ${e.message}`, "error");
    setAgentButtonsDisabled(false);
    setAgentStatus("error", "failed");
  }
}

function streamTrace(invocationId, logEl, flatMode, onDone) {
  if (!logEl) return;
  let listEl;
  if (flatMode) {
    // Step 2: append <li> items directly to the trace log div
    listEl = logEl;
    const metaEl = document.getElementById("traceMeta");
    if (metaEl) metaEl.textContent = invocationId.slice(0, 8);
  } else {
    // Chat bubble mode
    const empty = logEl.querySelector(".chat-empty");
    if (empty) empty.remove();
    const bubble = document.createElement("div");
    bubble.className = "chat-bubble agent-trace";
    bubble.innerHTML = `
      <div class="trace-header">cloud agent / ${escapeHtml(invocationId.slice(0, 8))}</div>
      <ol class="trace-list"></ol>
    `;
    logEl.appendChild(bubble);
    scrollLog(logEl);
    listEl = bubble.querySelector(".trace-list");
  }

  const url = `${API_BASE}/agent/stream/${encodeURIComponent(invocationId)}`;
  const source = new EventSource(url);
  let activeSubagent = null;

  source.onmessage = (e) => {
    try {
      const event = JSON.parse(e.data);
      const subagent = event?.payload?.subagent;
      if (subagent) { activateTeamNode(subagent); activeSubagent = subagent; setAgentStatus("working", `${subagent} working`); }
      else if (event.type === "pr_opened" || event.type === "agent_completed") { activeSubagent = null; }

      const li = document.createElement(flatMode ? "div" : "li");
      li.className = `trace-event trace-${event.type}`;
      if (!flatMode && activeSubagent && !subagent) {
        li.classList.add("trace-child");
        li.dataset.subagent = activeSubagent;
      }
      const glyph = TRACE_GLYPH[event.type] || "[event]";
      const subBadge = subagent ? `<span class="trace-subagent">${escapeHtml(subagent)}</span>` : "";
      li.innerHTML = `
        <span class="trace-glyph">${glyph}</span>
        <span class="trace-ts">${event.timestamp.toFixed(1)}s</span>
        ${subBadge}
        <span class="trace-label">${escapeHtml(event.label)}</span>
      `;
      listEl.appendChild(li);
      scrollLog(logEl);
    } catch (err) {
      console.error("trace parse error", err);
    }
  };

  source.addEventListener("done", () => { source.close(); onDone?.(); });
  source.onerror = (err) => { console.warn("SSE closed", err); source.close(); onDone?.(); };
}

function resetAgentTeam() {
  document.querySelectorAll(".team-mini-node").forEach((n) => n.classList.remove("active"));
}
function activateTeamNode(role) {
  document.querySelector(`.team-mini-node[data-role="${role}"]`)?.classList.add("active");
}

function renderPullRequest(prUrl, branch, inPlanPane) {
  if (inPlanPane) {
    const card = document.getElementById("prDiffCard");
    const content = document.getElementById("prDiffContent");
    if (card) card.style.display = "block";
    if (content) content.innerHTML = `
      ${branch ? `<div class="pr-result-branch">branch: <code>${escapeHtml(branch)}</code></div>` : ""}
      <div class="plan-approve-row">
        <span class="plan-approve-label">Protocol PR is open — review and merge to apply the new plan.</span>
        <a class="pr-result-cta" href="${escapeHtml(prUrl)}" target="_blank" rel="noopener" style="white-space:nowrap">View on GitHub</a>
        <button class="approve-btn" id="approveMergeBtn" onclick="approveMergePR('${escapeHtml(prUrl)}')">
          Approve &amp; Merge
        </button>
      </div>
    `;
    // Also show the current protocol exercises as a preview
    loadPlanPreview();
    return;
  }
  const logId = currentStep === 3 ? "checkinLog" : "chatLog";
  const log = document.getElementById(logId);
  if (!log) return;
  const bubble = document.createElement("div");
  bubble.className = "chat-bubble pr-result";
  bubble.innerHTML = `
    <div class="pr-result-header">pull request opened</div>
    ${branch ? `<div class="pr-result-branch">branch: ${escapeHtml(branch)}</div>` : ""}
    <a class="pr-result-cta" href="${escapeHtml(prUrl)}" target="_blank" rel="noopener">View on GitHub</a>
  `;
  log.appendChild(bubble);
  scrollLog(log);
}

// ---------------------------------------------------------------------------
// Approve & merge PR (step 2)
// ---------------------------------------------------------------------------

async function approveMergePR(prUrl) {
  const btn = document.getElementById("approveMergeBtn");
  if (btn) { btn.disabled = true; btn.textContent = "Merging…"; }
  try {
    const res = await fetch(`${API_BASE}/pr/merge`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ pr_url: prUrl }),
    });
    const data = await res.json();
    if (data.ok) {
      if (btn) { btn.textContent = "✓ Merged"; btn.style.background = "var(--accent)"; }
      showToast("Protocol PR merged — protocol updated!", "info");
      setTimeout(refreshProtocol, 2000);
    } else {
      if (btn) { btn.disabled = false; btn.textContent = "Approve & Merge"; }
      showToast(`Merge failed: ${data.error}`, "error");
    }
  } catch (e) {
    if (btn) { btn.disabled = false; btn.textContent = "Approve & Merge"; }
    showToast(`Merge error: ${e.message}`, "error");
  }
}

async function loadPlanPreview() {
  try {
    const res = await fetch(`${API_BASE}/protocol/exercises`);
    const data = await res.json();
    const content = document.getElementById("prDiffContent");
    if (!content || !data.exercises?.length) return;
    const rows = data.exercises.map((ex) => `
      <div class="plan-exercise-row">
        <span class="plan-exercise-name">${escapeHtml(ex.name)}</span>
        <span class="plan-exercise-spec">${escapeHtml(ex.spec)}</span>
      </div>`).join("");
    const preview = document.createElement("div");
    preview.className = "plan-weekly-preview";
    preview.innerHTML = `<h4>New protocol · ${escapeHtml(data.phase || "")} · Week ${data.week ?? "?"}</h4>${rows}`;
    content.appendChild(preview);
  } catch (e) {
    console.error("loadPlanPreview failed", e);
  }
}

// ---------------------------------------------------------------------------
// Guided flows: intake (step 1) / check-in (step 3)
// ---------------------------------------------------------------------------

const INTAKE_QUESTIONS = [
  { key: "name",     q: "What's your name?",                                          default: "Andre",                        hint: "e.g. Andre" },
  { key: "age",      q: "How old are you?",                                            default: "26",                           hint: "e.g. 26" },
  { key: "injury",   q: "What was your injury or surgery?",                            default: "ACL reconstruction",           hint: "e.g. ACL reconstruction" },
  { key: "timing",   q: "When was your surgery?",                                      default: "3 weeks ago",                  hint: "e.g. 3 weeks ago" },
  { key: "pain",     q: "Current pain level 1–10?",                                   default: "3",                            hint: "e.g. 3" },
  { key: "symptoms", q: "Any specific symptoms? (or press Enter for the default)",    default: "mild pain at 110° flexion",    hint: "e.g. mild pain at 110° flexion" },
];

const CHECKIN_QUESTIONS = [
  { key: "symptoms",  q: "Any new pain or discomfort since last session?",            default: "mild knee ache",               hint: "e.g. mild knee ache, or 'none'" },
  { key: "location",  q: "Where? (or 'none')",                                        default: "inner knee",                   hint: "e.g. inner knee" },
  { key: "rating",    q: "Session rating today 1–10?",                                default: "8",                            hint: "e.g. 8" },
  { key: "completed", q: "Which exercises did you complete?",                         default: "heel slides, quad sets, stationary bike", hint: "e.g. heel slides, quad sets" },
  { key: "notes",     q: "Anything else to flag for your coach?",                     default: "none",                         hint: "e.g. none" },
];

const FLOW_META = {
  intake:  { questions: INTAKE_QUESTIONS,  label: "Intake",   logId: "chatLog",    progressId: "intakeProgressBar",  labelId: "intakeProgressLabel",  fillId: "intakeProgressFill",  cancelId: "intakeCancelBtn",  inputId: "chatInput"   },
  checkin: { questions: CHECKIN_QUESTIONS, label: "Check-in", logId: "checkinLog", progressId: "checkinProgressBar", labelId: "checkinProgressLabel", fillId: "checkinProgressFill", cancelId: "checkinCancelBtn", inputId: "checkinInput" },
};

let activeFlow = null;

function startIntakeFlow() {
  activeFlow = { type: "intake", step: 0, answers: {} };
  updateFlowUI(true);
  appendFlowBubble("coach",
    "I'll walk you through a quick intake. Press Enter to use each default answer — it's fast for demo.\n\n" +
    INTAKE_QUESTIONS[0].q
  );
  prefillFlowInput();
}

function startCheckinFlow() {
  clearLog("checkinLog");
  activeFlow = { type: "checkin", step: 0, answers: {} };
  updateFlowUI(true);
  appendFlowBubble("coach",
    "Daily check-in time! Press Enter to accept each default.\n\n" +
    CHECKIN_QUESTIONS[0].q
  );
  prefillFlowInput();
}

function cancelFlow() {
  const label = activeFlow ? FLOW_META[activeFlow.type]?.label : "Flow";
  const meta = activeFlow ? FLOW_META[activeFlow.type] : null;
  activeFlow = null;
  if (meta) updateFlowUI(false);
  appendFlowBubble("coach", `${label} cancelled.`);
  resetFlowInput();
}

function updateFlowUI(active) {
  if (!activeFlow) return;
  const meta = FLOW_META[activeFlow.type];
  const bar      = document.getElementById(meta.progressId);
  const cancelBtn = document.getElementById(meta.cancelId);
  if (bar) bar.style.display = active ? "flex" : "none";
  if (cancelBtn) cancelBtn.style.display = active ? "inline-flex" : "none";
  if (active) updateFlowProgress();
}

function updateFlowProgress() {
  if (!activeFlow) return;
  const meta  = FLOW_META[activeFlow.type];
  const total = meta.questions.length;
  const labelEl = document.getElementById(meta.labelId);
  const fillEl  = document.getElementById(meta.fillId);
  if (labelEl) labelEl.textContent = `${meta.label} — question ${activeFlow.step + 1} of ${total}`;
  if (fillEl)  fillEl.style.width  = `${(activeFlow.step / total) * 100}%`;
  prefillFlowInput();
}

function prefillFlowInput() {
  if (!activeFlow) return;
  const meta = FLOW_META[activeFlow.type];
  const q = meta.questions[activeFlow.step];
  const input = document.getElementById(meta.inputId);
  if (input && q) {
    input.value = q.default || "";
    input.placeholder = q.hint || "Type your answer...";
    input.select();
    input.focus();
  }
}

function resetFlowInput() {
  ["chatInput", "checkinInput"].forEach((id) => {
    const el = document.getElementById(id);
    if (el) {
      el.placeholder = "Type your answer or press Enter to use the default...";
      el.value = "";
    }
  });
}

function appendFlowBubble(role, text) {
  if (!activeFlow) return;
  const logId = FLOW_META[activeFlow.type]?.logId || "chatLog";
  const log = document.getElementById(logId);
  if (!log) return;
  const empty = log.querySelector(".chat-empty");
  if (empty) empty.style.display = "none";
  const bubble = document.createElement("div");
  bubble.className = `chat-bubble ${role}`;
  bubble.innerHTML = renderCoachMarkdown(text);
  log.appendChild(bubble);
  scrollLog(log);
}

function handleFlowAnswer(text) {
  if (!activeFlow) return false;
  if (text.toLowerCase() === "cancel") { cancelFlow(); return true; }

  const meta = FLOW_META[activeFlow.type];
  const q = meta.questions[activeFlow.step];
  // Use default if empty (Enter with pre-filled value already submitted as-is)
  activeFlow.answers[q.key] = text || q.default || "";
  activeFlow.step++;

  if (activeFlow.step < meta.questions.length) {
    updateFlowProgress();
    appendFlowBubble("coach", meta.questions[activeFlow.step].q);
    return true;
  }

  // All done — build payload
  const a = activeFlow.answers;
  const type = activeFlow.type;
  const logId = meta.logId;
  activeFlow = null;
  updateFlowUI(false);
  resetFlowInput();

  if (type === "intake") {
    const intake_text =
      `${a.name}, ${a.age} years old. Injury: ${a.injury}, ${a.timing}. ` +
      `Pain level ${a.pain}/10. Symptoms: ${a.symptoms}`;
    const log = document.getElementById(logId);
    if (log) {
      const bubble = document.createElement("div");
      bubble.className = "chat-bubble coach";
      bubble.innerHTML = renderCoachMarkdown("Got it! Submitting your intake and generating your protocol...");
      log.appendChild(bubble);
      scrollLog(log);
    }
    invokeAgent("intake", { intake_text });
    onIntakeComplete();

  } else if (type === "checkin") {
    const checkin_text =
      `Symptoms: ${a.symptoms} (${a.location}). Session rating ${a.rating}/10. ` +
      `Completed: ${a.completed}. Notes: ${a.notes}`;
    const log = document.getElementById(logId);
    if (log) {
      const bubble = document.createElement("div");
      bubble.className = "chat-bubble coach";
      bubble.innerHTML = renderCoachMarkdown("Check-in logged. Updating your record...");
      log.appendChild(bubble);
      scrollLog(log);
    }
    invokeAgent("checkin", { checkin_text });
  }

  return true;
}

// ---------------------------------------------------------------------------
// Step 1 chat form (intake)
// ---------------------------------------------------------------------------

const chatHistory = [];

const CHAT_TOOL_GLYPH = {
  recommend_exercise:        "video",
  list_phase_exercises:      "library",
  fire_symptom_trigger:      "symptom → PR",
  fire_intake_trigger:       "intake → PR",
  fire_checkin_trigger:      "check-in → PR",
  fire_weekly_plan_trigger:  "weekly plan → PR",
};

function onChatSubmit(event) {
  event.preventDefault();
  const input = document.getElementById("chatInput");
  const raw = input.value;
  const text = raw.trim();
  if (!text) return;
  input.value = "";
  const logId = activeFlow ? FLOW_META[activeFlow.type]?.logId : "chatLog";
  appendBubble(logId, "user", text);
  if (handleFlowAnswer(text)) return;
  sendChat(text, { skipUserBubble: true });
}

// Step 3 check-in form
function onCheckinSubmit(event) {
  event.preventDefault();
  const input = document.getElementById("checkinInput");
  const text = input.value.trim();
  if (!text) return;
  input.value = "";
  appendBubble("checkinLog", "user", text);
  handleFlowAnswer(text);
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
  if (!skipUserBubble) appendBubble("chatLog", "user", message);
  const coachBubble = appendBubble("chatLog", "coach", "", { thinking: true });

  let coachBuffer = "";
  let coachClosed = false;
  const closeCoach = () => {
    if (coachClosed) return;
    coachClosed = true;
    coachBubble.classList.remove("thinking");
    if (!coachBuffer.trim()) { coachBubble.remove(); return; }
    coachBubble.innerHTML = renderCoachMarkdown(coachBuffer);
  };

  try {
    const res = await fetch(`${API_BASE}/chat`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ session_id: "default", message, history: chatHistory.slice(-10) }),
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
        for (const line of lines) if (line.startsWith("data:")) dataLine += line.slice(5).trim();
        if (!dataLine || dataLine === "{}") continue;
        let ev;
        try { ev = JSON.parse(dataLine); } catch (e) { continue; }
        handleChatEvent(ev, coachBubble, (delta) => { coachBuffer += delta; });
        if (ev.type === "done") closeCoach();
      }
    }
  } catch (err) {
    console.error("chat error", err);
    appendBubble("chatLog", "error", `chat failed: ${err.message}`);
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
  const log = document.getElementById("chatLog");
  switch (event.type) {
    case "token":
      coachBubble.classList.remove("thinking");
      coachBubble.textContent += event.delta || "";
      appendDelta(event.delta || "");
      scrollLog(log);
      break;
    case "card":
      renderExerciseCard(event.card);
      break;
    case "tool_call":
      renderToolLine(event);
      if (String(event.name || "").startsWith("fire_")) {
        resetAgentTeam(); activateTeamNode("parent"); setAgentStatus("working", "coordinator (chat)");
      }
      break;
    case "tool_result":
      if (event.result?.invocation_id) {
        renderToolResultLine(event);
        streamTrace(event.result.invocation_id, log, false, () => {
          if (event.result.pr_url) renderPullRequest(event.result.pr_url, event.result.branch, false);
          setAgentStatus("done", "ready");
          refreshProtocol();
        });
        const providerEl = document.getElementById("providerName");
        if (providerEl) providerEl.textContent = event.result.provider || "cached_replay";
      }
      break;
    case "error":
      appendBubble("chatLog", "error", event.message || "chat error");
      break;
  }
}

// ---------------------------------------------------------------------------
// Chat bubble helpers
// ---------------------------------------------------------------------------

function appendBubble(logId, role, text, opts = {}) {
  const log = document.getElementById(logId);
  if (!log) return document.createElement("div");
  const empty = log.querySelector(".chat-empty");
  if (empty) empty.style.display = "none";
  const bubble = document.createElement("div");
  bubble.className = `chat-bubble ${role}${opts.thinking ? " thinking" : ""}`;
  bubble.textContent = text;
  log.appendChild(bubble);
  scrollLog(log);
  return bubble;
}

// Legacy alias used by handleChatEvent
function appendChatBubble(role, text, opts = {}) {
  return appendBubble("chatLog", role, text, opts);
}

function clearLog(logId) {
  const log = document.getElementById(logId);
  if (!log) return;
  log.innerHTML = `<div class="chat-empty" style="display:none"></div>`;
}

function scrollLog(logEl) {
  if (logEl) logEl.scrollTop = logEl.scrollHeight;
}
function scrollChatLog() { scrollLog(document.getElementById("chatLog")); }

function renderToolLine(event) {
  const log = document.getElementById("chatLog");
  const line = document.createElement("div");
  line.className = "chat-tool-line";
  const label = CHAT_TOOL_GLYPH[event.name] || event.name;
  const args = event.arguments || {};
  let detail = "";
  if (args.exercise_id) detail = args.exercise_id;
  else if (args.phase)   detail = `phase: ${args.phase}`;
  else if (args.symptom_text) detail = `"${truncate(args.symptom_text, 50)}"`;
  else if (args.intake_text)  detail = `"${truncate(args.intake_text, 50)}"`;
  else if (args.checkin_text) detail = `"${truncate(args.checkin_text, 50)}"`;
  line.innerHTML = `<span class="tool-glyph">[${escapeHtml(label)}]</span> <span>${escapeHtml(detail)}</span>`;
  log.appendChild(line);
  scrollLog(log);
}

function renderToolResultLine(event) {
  const log = document.getElementById("chatLog");
  const line = document.createElement("div");
  line.className = "chat-tool-line";
  const result = event.result || {};
  const pr = result.pr_url
    ? `<a href="${escapeHtml(result.pr_url)}" target="_blank">${escapeHtml(result.branch || "PR")}</a>`
    : `<span>queued</span>`;
  line.innerHTML = `<span class="tool-glyph">[orchestrator]</span> <span>${escapeHtml(result.provider || "agent")}</span> ${pr}`;
  log.appendChild(line);
  scrollLog(log);
}

function renderExerciseCard(card) {
  if (!card) return;
  const log = document.getElementById("chatLog");
  const wrap = document.createElement("div");
  wrap.className = "exercise-card";
  const cuesHtml = (card.cues || []).map((c) => `<li>${escapeHtml(c)}</li>`).join("");
  const dose = card.default_dose ? `<span class="exercise-dose">${escapeHtml(card.default_dose)}</span>` : "";
  let embed, sourceBadge = "";
  if (card.generated_video_url) {
    embed = `<div class="exercise-video-wrap"><video src="${escapeHtml(card.generated_video_url)}" controls autoplay muted playsinline loop preload="metadata"></video></div>`;
    sourceBadge = `<span class="video-source sora">sora-2 generated</span>`;
  } else if (card.youtube_embed_url) {
    embed = `<div class="exercise-video-wrap"><iframe src="${escapeHtml(card.youtube_embed_url)}" title="${escapeHtml(card.name || "exercise video")}" allow="accelerometer; autoplay; clipboard-write; encrypted-media; gyroscope; picture-in-picture" allowfullscreen></iframe></div>`;
    sourceBadge = `<span class="video-source youtube">curated</span>`;
  } else {
    embed = `<div class="exercise-video-wrap"><img src="${escapeHtml(card.thumbnail_url || "")}" alt="${escapeHtml(card.name || "")}" /></div>`;
  }
  const watch = card.youtube_watch_url
    ? `<a class="exercise-action-btn" href="${escapeHtml(card.youtube_watch_url)}" target="_blank">reference clip</a>`
    : "";
  wrap.innerHTML = `${embed}<div class="exercise-meta"><div class="exercise-title-row"><span class="exercise-title">${escapeHtml(card.name || card.id || "")}</span>${dose}${sourceBadge}</div><ul class="exercise-cues">${cuesHtml}</ul><div class="exercise-actions"><button class="exercise-action-btn primary" data-add-id="${escapeHtml(card.id || "")}" data-add-name="${escapeHtml(card.name || card.id || "")}" onclick="addToTodayFromBtn(this)">Add to today</button>${watch}</div></div>`;
  log.appendChild(wrap);
  scrollLog(log);
}

// ---------------------------------------------------------------------------
// Misc helpers
// ---------------------------------------------------------------------------

function setAgentButtonsDisabled(disabled) {
  ["generatePlanBtn"].forEach((id) => {
    const btn = document.getElementById(id);
    if (btn) btn.disabled = disabled;
  });
}

const todaySession = [];
function addToTodayFromBtn(btn) {
  const id = btn.dataset.addId || "";
  const name = btn.dataset.addName || id || "exercise";
  if (!id || todaySession.some((e) => e.id === id)) {
    showToast(`${name} is already in today's session`, "info");
    return;
  }
  todaySession.push({ id, name });
  appendBubble("chatLog", "coach", `Added ${name} to today's session.`);
}

function setChatBusy(busy, sendBtn, input) {
  if (sendBtn) sendBtn.disabled = busy;
  if (input) input.disabled = busy;
}

function truncate(s, n) {
  s = String(s || "");
  return s.length > n ? s.slice(0, n - 1) + "…" : s;
}

function renderCoachMarkdown(text) {
  return escapeHtml(text)
    .replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>")
    .replace(/\[([^\]]+)\]\((https?:\/\/[^\s)]+)\)/g, '<a href="$2" target="_blank" rel="noopener">$1</a>');
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
