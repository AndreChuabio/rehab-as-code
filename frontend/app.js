const LOCAL_API_BASE = "http://localhost:8000";
const PRODUCTION_API_BASE = "https://rehab-as-code-production.up.railway.app";
const API_BASE = window.location.hostname.includes("localhost")
  ? LOCAL_API_BASE
  : PRODUCTION_API_BASE;

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

async function invokeAgent(flow, symptomText = "") {
  const traceCard = document.getElementById("agentTraceCard");
  const traceList = document.getElementById("traceList");
  const prCard = document.getElementById("prDiffCard");
  traceCard.style.display = "block";
  traceList.innerHTML = "";
  prCard.style.display = "none";

  setAgentButtonsDisabled(true);

  try {
    const res = await fetch(`${API_BASE}/agent/invoke`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ flow, symptom_text: symptomText }),
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

  source.onmessage = (e) => {
    try {
      const event = JSON.parse(e.data);
      const li = document.createElement("li");
      li.className = `trace-event trace-${event.type}`;
      const glyph = TRACE_GLYPH[event.type] || "[event]";
      li.innerHTML = `
        <span class="trace-glyph">${glyph}</span>
        <span class="trace-ts">${event.timestamp.toFixed(1)}s</span>
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
  invokeAgent("symptom_adjustment", text.trim());
}

function setAgentButtonsDisabled(disabled) {
  document.getElementById("generatePlanBtn").disabled = disabled;
  document.getElementById("reportSymptomBtn").disabled = disabled;
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
