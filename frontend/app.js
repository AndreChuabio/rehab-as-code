// Backend serves this HTML at the same origin, so relative URLs always work:
// localhost, 127.0.0.1, phone hotspot IP, Railway prod - same code path.
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

document.addEventListener("DOMContentLoaded", () => {
  document.getElementById("dateDisplay").textContent = new Date().toLocaleDateString(
    "en-US", { weekday: "long", month: "long", day: "numeric" }
  );
  loadSidebar();
  loadProtocol();
});

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
    document.getElementById("repoLink").href = `https://github.com/${data.repo}`;
    document.getElementById("repoLink").textContent = data.repo;
  } catch (e) {
    console.error("Failed to load protocol:", e);
  }
}

function renderProtocol({ protocol }) {
  const meta = `${protocol.patient || "patient"} - ${protocol.phase || "rehab"} - week ${protocol.week ?? "?"}`;
  document.getElementById("protocolMeta").textContent = meta;

  const list = document.getElementById("protocolExercises");
  const exercises = protocol.exercises || [];
  if (!exercises.length) {
    list.innerHTML = "<li class='loading-text'>(empty protocol)</li>";
    return;
  }
  list.innerHTML = exercises
    .map(
      (ex) => `
    <li class="protocol-exercise">
      <span class="ex-name">${ex.name || "unnamed"}</span>
      <span class="ex-spec">${ex.sets ?? "?"}x${ex.reps ?? "?"} - ROM ${ex.ROM_target_deg ?? "?"} deg</span>
    </li>
  `,
    )
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
  const traceCard = document.getElementById("agentTraceCard");
  const traceList = document.getElementById("traceList");
  const prCard = document.getElementById("prDiffCard");
  traceCard.style.display = "block";
  traceList.innerHTML = "";
  prCard.style.display = "none";

  resetAgentTeam();
  activateTeamNode("parent");
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
    document.getElementById("providerName").textContent = provider;

    streamTrace(invocation_id, () => {
      if (pr_url) {
        renderPullRequest(pr_url, branch);
      }
      setAgentButtonsDisabled(false);
    });
  } catch (e) {
    console.error(e);
    showToast(`Agent invoke failed: ${e.message}`, "error");
    setAgentButtonsDisabled(false);
  }
}

function streamTrace(invocationId, onDone) {
  const traceList = document.getElementById("traceList");
  const url = `${API_BASE}/agent/stream/${encodeURIComponent(invocationId)}`;
  const source = new EventSource(url);
  let activeSubagent = null;

  source.onmessage = (e) => {
    try {
      const event = JSON.parse(e.data);

      // Light up the team-node strip when a sub-agent is spawned.
      const subagent = event?.payload?.subagent;
      if (subagent) {
        activateTeamNode(subagent);
        activeSubagent = subagent;
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
      traceList.parentElement.scrollTop = traceList.parentElement.scrollHeight;
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
  document.querySelectorAll(".team-node").forEach((n) => {
    n.classList.remove("active");
  });
}

function activateTeamNode(role) {
  const node = document.querySelector(`.team-node[data-role="${role}"]`);
  if (node) node.classList.add("active");
}

function renderPullRequest(prUrl, branch) {
  const card = document.getElementById("prDiffCard");
  document.getElementById("prLink").href = prUrl;
  document.getElementById("prLink").textContent = prUrl;
  document.getElementById("prBranch").textContent = branch
    ? `branch: ${branch}`
    : "";
  card.style.display = "block";
}

function reportSymptom() {
  const text = prompt(
    "What did you feel during the exercise? (e.g., 'knee felt tweaky on single-leg squats')",
  );
  if (!text || !text.trim()) return;
  invokeAgent("symptom_adjustment", { symptom_text: text.trim() });
}

function triggerIntake() {
  const text = prompt(
    "Patient intake (free-text): age / injury / date of surgery / current pain level",
    "Andre, 26, ACL reconstruction 3 weeks ago, mild pain at 110 flexion",
  );
  if (!text || !text.trim()) return;
  invokeAgent("intake", { intake_text: text.trim() });
}

function triggerCheckin() {
  const text = prompt(
    "Today's check-in (how did the session go?)",
    "Hit all 3 sets of heel slides, quad set felt stronger than yesterday",
  );
  if (!text || !text.trim()) return;
  invokeAgent("checkin", { checkin_text: text.trim() });
}

function setAgentButtonsDisabled(disabled) {
  ["generatePlanBtn", "reportSymptomBtn", "triggerIntakeBtn", "triggerCheckinBtn"].forEach(
    (id) => {
      const btn = document.getElementById(id);
      if (btn) btn.disabled = disabled;
    },
  );
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
  sendChat(text);
}

function sendChatPreset(text) {
  const input = document.getElementById("chatInput");
  if (input) input.value = "";
  sendChat(text);
}

async function sendChat(message) {
  const empty = document.getElementById("chatEmpty");
  if (empty) empty.remove();

  const sendBtn = document.getElementById("chatSendBtn");
  const input = document.getElementById("chatInput");
  setChatBusy(true, sendBtn, input);

  appendChatBubble("user", message);
  const coachBubble = appendChatBubble("coach", "", { thinking: true });

  let coachBuffer = "";
  let coachClosed = false;
  const closeCoach = () => {
    if (coachClosed) return;
    coachClosed = true;
    coachBubble.classList.remove("thinking");
    if (!coachBuffer.trim()) {
      coachBubble.remove();
    }
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
      // Light up the right-panel team strip so the audience sees the link
      // between chat and orchestrator immediately.
      if (String(event.name || "").startsWith("fire_")) {
        resetAgentTeam();
        activateTeamNode("parent");
        document.getElementById("agentTraceCard").style.display = "block";
        document.getElementById("traceList").innerHTML = "";
        document.getElementById("prDiffCard").style.display = "none";
      }
      break;

    case "tool_result":
      // For fire_*_trigger results we have a real invocation - hand it off
      // to the existing streamTrace() so the right-panel UI mirrors a button
      // press exactly. recommend_exercise / list_phase_exercises results
      // do not include invocation_id; they're frontend-only.
      if (event.result?.invocation_id) {
        renderToolResultLine(event);
        streamTrace(event.result.invocation_id, () => {
          if (event.result.pr_url) {
            renderPullRequest(event.result.pr_url, event.result.branch);
          }
        });
        document.getElementById("providerName").textContent =
          event.result.provider || "cached_replay";
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
                onclick="sendChatPreset('Add ${escapeHtml(card.id)} to today and log a check-in.')">
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
