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
let approvedPlanExercises = []; // exercises the user added in step 2

// ---------------------------------------------------------------------------
// Hash-based routing  /#intake  /#plan  /#exercise  /#checkin
// ---------------------------------------------------------------------------

const STEP_ROUTES = {
  intake:   () => navigateToIntake(),
  plan:     () => triggerGeneratePlan(),
  exercise: () => triggerExercise(),
  checkin:  () => triggerCheckin(),
};

// Route #intake to the structured modal for authed users; fall back to the
// legacy demo-mode chat flow when there's no JWT (so the UI is still walkable
// in demo mode without sign-in).
//
// IMPORTANT: this runs from routeFromHash() on DOMContentLoaded, which fires
// before RehabAuth.init() has resolved the cached session. If we synchronously
// check getJwt() at that moment we get null even for returning signed-in
// users, and the legacy chat opens by mistake. So await the init first.
async function navigateToIntake() {
  setActiveStepBtn("intake");
  try {
    if (window.RehabAuth?.init) await window.RehabAuth.init();
  } catch (_) {
    // init failure is handled in bootstrapAuth (toast + overlay); fall through
    // to legacy chat so the page is still walkable.
  }
  const authed = !!window.RehabAuth?.getJwt?.();
  if (authed) {
    // refreshPatientState picks the right modal (#intakeModal vs #planGenModal)
    // based on server state. Don't second-guess by opening intake unconditionally.
    refreshPatientState().catch((e) => console.warn("state refresh failed", e));
  } else {
    triggerIntake();
  }
}

function navigateTo(step) {
  if (window.location.hash !== `#${step}`) {
    history.pushState(null, "", `#${step}`);
  }
  const fn = STEP_ROUTES[step];
  if (fn) fn();
}

function routeFromHash() {
  const hash = window.location.hash.replace("#", "") || "intake";
  const fn = STEP_ROUTES[hash];
  if (fn) fn(); else triggerIntake();
}

window.addEventListener("hashchange", routeFromHash);

document.addEventListener("DOMContentLoaded", () => {
  document.getElementById("dateDisplay").textContent = new Date().toLocaleDateString(
    "en-US", { weekday: "long", month: "long", day: "numeric" }
  );
  loadSidebar();
  loadProtocol();
  switchStage("chat");
  applyStepLocks();
  routeFromHash(); // honour the URL on load; defaults to #intake
  bootstrapAuth();
  wireIntakeModal();
  wirePlanGenModal();
});

function wireIntakeModal() {
  const form = document.getElementById("intakeForm");
  const input = document.getElementById("intakeInput");
  if (!form || !input) return;
  form.addEventListener("submit", (e) => {
    e.preventDefault();
    const text = (input.value || "").trim();
    if (!text) return;
    appendIntakeBubble("user", text);
    submitIntakeTurn(text);
  });
}

function wirePlanGenModal() {
  const cont = document.getElementById("planGenContinue");
  if (!cont) return;
  cont.addEventListener("click", async () => {
    closePlanGenModal();
    // Ask the server again — protocol_state now has last_pr_url, so state
    // should flip to "ready" and the rest of the UI unlocks.
    await refreshPatientState();
    setActiveStepBtn("exercise");
    if (window.location.hash !== "#exercise") history.pushState(null, "", "#exercise");
  });
}

// ── Supabase auth bootstrap (soft-gate) ────────────────────────────────────
//
// On load: show the overlay unless the user has a session OR has explicitly
// skipped sign-in this session. Skipping flips localStorage.authSkipped so
// they're not nagged on every refresh, but they can sign in via the pill in
// the header at any time.

const AUTH_SKIP_KEY = "authSkipped";

function authedFetch(path, options = {}) {
  const jwt = window.RehabAuth?.getJwt?.();
  const hdrs = new Headers(options.headers || {});
  if (jwt) hdrs.set("Authorization", `Bearer ${jwt}`);
  return fetch(path, { ...options, headers: hdrs });
}

async function bootstrapAuth() {
  const overlay = document.getElementById("authOverlay");
  const pill    = document.getElementById("authPill");
  const pillEm  = document.getElementById("authPillEmail");
  const signOut = document.getElementById("authPillSignout");
  const form    = document.getElementById("authForm");
  const email   = document.getElementById("authEmail");
  const submit  = document.getElementById("authSubmit");
  const status  = document.getElementById("authStatus");
  const skipBtn = document.getElementById("authSkip");

  function showOverlay(show) { if (overlay) overlay.hidden = !show; }
  function showPill(user) {
    if (!pill) return;
    if (user) {
      pillEm.textContent = user.email || "signed in";
      pill.hidden = false;
    } else {
      pill.hidden = true;
    }
  }

  // Render initial state synchronously so the overlay doesn't flash on every
  // returning visit. RehabAuth.init() will refine this when the SDK loads.
  const skipped = localStorage.getItem(AUTH_SKIP_KEY) === "1";
  const cachedJwt = localStorage.getItem("supabaseJwt");
  if (!cachedJwt && !skipped) showOverlay(true);

  if (!window.RehabAuth) {
    console.warn("auth.js not loaded; running in unauthenticated mode");
    return;
  }
  try {
    await window.RehabAuth.init();
  } catch (e) {
    console.warn("Supabase init failed:", e);
    showOverlay(false);  // don't trap the user behind a broken auth
    showToast(`Sign-in unavailable: ${e.message}`, "error");
    return;
  }

  window.RehabAuth.onChange((session) => {
    if (session) {
      localStorage.removeItem(AUTH_SKIP_KEY);
      showOverlay(false);
      showPill(window.RehabAuth.getUser());
      // Server-driven state machine: ask the backend whether this patient
      // needs intake / plan-gen / nothing, and route to the right modal.
      refreshPatientState().catch((e) => console.warn("state refresh failed", e));
    } else {
      showPill(null);
      closeIntakeModal();
      closePlanGenModal();
      patientState = null;
      // Re-show the overlay if it isn't a deliberate skip and user has no
      // session — but never on the magic-link redirect, which fires onChange
      // with a fresh session right after.
      if (localStorage.getItem(AUTH_SKIP_KEY) !== "1") showOverlay(true);
    }
  });

  if (form) {
    form.addEventListener("submit", async (e) => {
      e.preventDefault();
      const v = (email.value || "").trim();
      if (!v) return;
      submit.disabled = true;
      submit.textContent = "Sending…";
      status.hidden = true;
      try {
        await window.RehabAuth.sendMagicLink(v);
        status.hidden = false;
        status.textContent = `Check ${v} for the magic link. Click it on this device.`;
        status.className = "auth-status auth-status-ok";
      } catch (err) {
        status.hidden = false;
        status.textContent = `Couldn't send link: ${err.message || err}`;
        status.className = "auth-status auth-status-err";
      } finally {
        submit.disabled = false;
        submit.textContent = "Send magic link";
      }
    });
  }
  if (skipBtn) {
    skipBtn.addEventListener("click", () => {
      localStorage.setItem(AUTH_SKIP_KEY, "1");
      showOverlay(false);
      showToast("Demo mode — chat and form-check log won't save", "info");
    });
  }
  if (signOut) {
    signOut.addEventListener("click", async () => {
      try { await window.RehabAuth.signOut(); } catch (_) {}
    });
  }
}

// ---------------------------------------------------------------------------
// Server-driven patient state machine.
//
// Replaces the legacy localStorage rehab_intake_complete flag for authed users.
// Truth lives in the intake_records / protocol_state tables; the frontend just
// asks /patient/me/intake-status and routes to the right modal.
//
//   state="needs_intake" → open #intakeModal, run /patient/interact loop
//   state="needs_plan"   → open #planGenModal, POST force=plan_generation
//   state="ready"        → close all modals, surface main UI
//
// Demo mode (no JWT) keeps the old triggerIntake() chat flow as a fallback so
// the UI is still walkable without sign-in.
// ---------------------------------------------------------------------------

let patientState = null; // { state, has_intake, has_protocol, last_pr_url, ... }
let intakeHistory = [];  // [{role, content}] for the intake modal conversation
let planGenStreamES = null; // EventSource for the live plan-gen trace

async function refreshPatientState({ openModalIfNeeded = true } = {}) {
  // Only meaningful when authed; demo mode falls back to local flags.
  if (!window.RehabAuth?.getJwt?.()) {
    patientState = null;
    return null;
  }
  try {
    const res = await authedFetch(`${API_BASE}/patient/me/intake-status`);
    if (!res.ok) {
      const body = await res.text().catch(() => "");
      console.warn("intake-status fetch failed:", res.status, body);
      // Surface 401s loudly — usually means SUPABASE_JWT_SECRET is missing
      // or wrong on the server. Without this toast the modal silently
      // doesn't open and the user falls back to the legacy chat with no
      // explanation.
      if (res.status === 401) {
        showToast(
          "Auth check failed (401). Verify SUPABASE_JWT_SECRET in Vercel.",
          "error",
        );
      } else {
        showToast(`Patient state check failed (${res.status})`, "error");
      }
      patientState = null;
      return null;
    }
    patientState = await res.json();
  } catch (e) {
    console.warn("intake-status error:", e);
    showToast(`Couldn't load patient state: ${e.message || e}`, "error");
    patientState = null;
    return null;
  }

  if (!openModalIfNeeded) return patientState;

  if (patientState.state === "needs_intake") {
    showIntakeModal();
  } else if (patientState.state === "needs_plan") {
    showPlanGenModal({ kickoff: true });
  } else {
    closeIntakeModal();
    closePlanGenModal();
    // Mirror local flag so the rest of the legacy-demo gating logic
    // (sidebar locks, /protocol load) keeps working without rewrites.
    intakeComplete = true;
    localStorage.setItem("rehab_intake_complete", "1");
    if (patientState.has_protocol) {
      localStorage.setItem("rehab_plan_approved", "1");
    }
    applyStepLocks();
    loadProtocol();
  }
  return patientState;
}

function showIntakeModal() {
  const modal = document.getElementById("intakeModal");
  const log = document.getElementById("intakeLog");
  const fill = document.getElementById("intakeProgressFill");
  if (!modal) return;
  // If the legacy demo chat was already running (e.g., the page loaded with
  // #intake before auth resolved), tear it down so the modal isn't competing
  // with a half-filled legacy conversation behind it.
  if (typeof activeFlow !== "undefined" && activeFlow) {
    activeFlow = null;
    if (typeof updateFlowUI === "function") updateFlowUI(false);
    if (typeof resetInputPlaceholder === "function") resetInputPlaceholder();
    if (typeof clearChatLog === "function") clearChatLog();
  }
  modal.hidden = false;
  if (log && log.childElementCount === 0) {
    intakeHistory = [];
    appendIntakeBubble(
      "coach",
      "Hi — I'll ask a few short questions to build your rehab plan.",
    );
    // Kick off the agent with an empty user turn so it asks the first question.
    submitIntakeTurn("");
  }
  if (fill) fill.style.width = "0%";
  document.getElementById("intakeInput")?.focus();
}

function closeIntakeModal() {
  const modal = document.getElementById("intakeModal");
  if (modal) modal.hidden = true;
  const log = document.getElementById("intakeLog");
  if (log) log.innerHTML = "";
  intakeHistory = [];
}

function appendIntakeBubble(role, text) {
  const log = document.getElementById("intakeLog");
  if (!log) return;
  const div = document.createElement("div");
  div.className = `intake-bubble intake-bubble-${role === "coach" ? "coach" : "user"}`;
  div.textContent = text;
  log.appendChild(div);
  log.scrollTop = log.scrollHeight;
}

function setIntakeStatus(text, kind = "info") {
  const status = document.getElementById("intakeStatus");
  if (!status) return;
  if (!text) { status.hidden = true; return; }
  status.hidden = false;
  status.textContent = text;
  status.className = `intake-status auth-status-${kind === "error" ? "err" : "ok"}`;
}

async function submitIntakeTurn(userText) {
  const submit = document.getElementById("intakeSubmit");
  const input = document.getElementById("intakeInput");
  if (submit) submit.disabled = true;
  if (input) input.disabled = true;
  setIntakeStatus("Coach Maya is thinking…", "info");

  try {
    const body = {
      message: userText || "Let's start the intake.",
      history: intakeHistory.slice(),
      metadata: {},
    };
    const res = await authedFetch(`${API_BASE}/patient/interact`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    if (!res.ok) {
      const detail = await res.text().catch(() => "");
      throw new Error(`HTTP ${res.status}${detail ? ` — ${detail}` : ""}`);
    }
    const payload = await res.json();
    setIntakeStatus("");

    if (userText) {
      intakeHistory.push({ role: "user", content: userText });
    }
    if (payload.message) {
      intakeHistory.push({ role: "assistant", content: payload.message });
      appendIntakeBubble("coach", payload.message);
    }

    // Crude progress indicator: bump fill by 1/8 each round.
    const fill = document.getElementById("intakeProgressFill");
    if (fill) {
      const turns = intakeHistory.filter((m) => m.role === "user").length;
      fill.style.width = `${Math.min(100, (turns / 7) * 100)}%`;
    }

    if (payload.intake_complete) {
      // IntakeAgent saved + already kicked plan_generation. Close intake,
      // open plan-gen modal, surface the PR card if we already have one.
      closeIntakeModal();
      const inv = payload.data?.invocation_id || null;
      const pr = payload.data?.pr_url || null;
      showPlanGenModal({ invocation_id: inv, pr_url: pr });
    }
  } catch (err) {
    console.error("intake turn failed:", err);
    setIntakeStatus(`Couldn't reach coach: ${err.message || err}`, "error");
  } finally {
    if (submit) submit.disabled = false;
    if (input) {
      input.disabled = false;
      input.value = "";
      input.focus();
    }
  }
}

async function showPlanGenModal({ invocation_id = null, pr_url = null, kickoff = false } = {}) {
  const modal = document.getElementById("planGenModal");
  const trace = document.getElementById("planGenTrace");
  const prCard = document.getElementById("planGenPrCard");
  const cont = document.getElementById("planGenContinue");
  if (!modal) return;
  modal.hidden = false;
  if (trace) trace.innerHTML = "";
  if (prCard) { prCard.hidden = true; prCard.innerHTML = ""; }
  if (cont) cont.hidden = true;

  appendPlanGenLine("[start]", "Calling plan generator…");

  // If we don't already have an invocation, force one now.
  if (!invocation_id && (kickoff || !pr_url)) {
    try {
      const body = {
        message: "Generate my rehab plan.",
        history: [],
        metadata: { force: "plan_generation" },
      };
      const res = await authedFetch(`${API_BASE}/patient/interact`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const payload = await res.json();
      invocation_id = payload.data?.invocation_id || null;
      pr_url = payload.data?.pr_url || pr_url;
      if (payload.message) appendPlanGenLine("[plan]", payload.message);
    } catch (err) {
      appendPlanGenLine("[fail]", `Plan generation failed: ${err.message || err}`, true);
    }
  }

  if (invocation_id) {
    streamPlanGenTrace(invocation_id, () => {
      if (pr_url) renderPlanGenPr(pr_url);
      if (cont) cont.hidden = false;
    });
  } else if (pr_url) {
    // Cached_replay returns pr_url directly with no streamable invocation.
    renderPlanGenPr(pr_url);
    if (cont) cont.hidden = false;
  } else {
    if (cont) cont.hidden = false;
  }
}

function appendPlanGenLine(glyph, text, isFail = false) {
  const trace = document.getElementById("planGenTrace");
  if (!trace) return;
  const line = document.createElement("span");
  line.className = `trace-line${isFail ? " trace-fail" : ""}`;
  line.innerHTML = `<span class="trace-glyph">${escapeHtml(glyph)}</span>${escapeHtml(text)}`;
  trace.appendChild(line);
  trace.scrollTop = trace.scrollHeight;
}

function renderPlanGenPr(prUrl) {
  const prCard = document.getElementById("planGenPrCard");
  if (!prCard) return;
  prCard.hidden = false;
  prCard.innerHTML = `<span>PR opened →</span> <a href="${prUrl}" target="_blank" rel="noopener">${prUrl}</a>`;
}

function streamPlanGenTrace(invocationId, onDone) {
  if (planGenStreamES) {
    try { planGenStreamES.close(); } catch (_) {}
    planGenStreamES = null;
  }
  const url = `${API_BASE}/agent/stream/${encodeURIComponent(invocationId)}`;
  try {
    planGenStreamES = new EventSource(url);
  } catch (e) {
    appendPlanGenLine("[fail]", `Couldn't open trace stream: ${e.message}`, true);
    if (onDone) onDone();
    return;
  }
  planGenStreamES.onmessage = (ev) => {
    try {
      const data = JSON.parse(ev.data);
      const glyph = TRACE_GLYPH[data.type] || `[${data.type}]`;
      const text = data.message || data.detail || JSON.stringify(data.payload || {});
      appendPlanGenLine(glyph, text, data.type === "agent_failed");
      if (data.type === "pr_opened" && data.payload?.url) {
        renderPlanGenPr(data.payload.url);
      }
    } catch (_) {
      appendPlanGenLine("[trace]", ev.data);
    }
  };
  planGenStreamES.addEventListener("done", () => {
    try { planGenStreamES.close(); } catch (_) {}
    planGenStreamES = null;
    if (onDone) onDone();
  });
  planGenStreamES.onerror = () => {
    try { planGenStreamES.close(); } catch (_) {}
    planGenStreamES = null;
    if (onDone) onDone();
  };
}

function closePlanGenModal() {
  const modal = document.getElementById("planGenModal");
  if (modal) modal.hidden = true;
  if (planGenStreamES) {
    try { planGenStreamES.close(); } catch (_) {}
    planGenStreamES = null;
  }
}

function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, (c) => (
    { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]
  ));
}

function applyStepLocks() {
  const locked = !intakeComplete;
  ["generatePlanBtn", "exerciseBtn", "triggerCheckinBtn"].forEach((id) => {
    const btn = document.getElementById(id);
    if (!btn) return;
    btn.disabled = locked;
  });
}

function setActiveStepBtn(step) {
  const map = {
    intake:   "triggerIntakeBtn",
    plan:     "generatePlanBtn",
    exercise: "exerciseBtn",
    checkin:  "triggerCheckinBtn",
  };
  Object.entries(map).forEach(([s, id]) => {
    document.getElementById(id)?.classList.toggle("primary", s === step);
  });
}

function onIntakeComplete() {
  intakeComplete = true;
  localStorage.setItem("rehab_intake_complete", "1");
  applyStepLocks();
  document.getElementById("triggerIntakeBtn")?.classList.remove("primary");
  document.getElementById("generatePlanBtn")?.classList.add("primary");
  showToast("Intake complete — now generate your weekly plan!", "info");
}

function onPlanApproved() {
  localStorage.setItem("rehab_plan_approved", "1");
  document.getElementById("generatePlanBtn")?.classList.remove("primary");
  document.getElementById("exerciseBtn")?.classList.add("primary");
  loadProtocol(); // sidebar now shows the approved protocol
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
  const planApproved = localStorage.getItem("rehab_plan_approved") === "1";
  if (!intakeComplete || !planApproved) {
    renderProtocol({ protocol: { phase: "pending_intake", exercises: [] } });
    return;
  }
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
    meta.textContent = "no protocol yet";
    list.innerHTML = `
      <li class="protocol-empty">
        <div class="empty-headline">No protocol yet</div>
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
      const recPanel = document.getElementById("recommendations");
      if (recPanel) {
        renderFocus(data.recommendations);
        recPanel.style.display = "block";
      }
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
  if (!container) return;
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

    streamTrace(invocation_id, async () => {
      setAgentButtonsDisabled(false);
      setAgentStatus("done", "ready");
      refreshProtocol();
      if (flow === "intake") {
        // After intake, drop straight into the plan builder (days + exercises)
        appendChatBubble("coach", "Intake complete! Now set your training days and add exercises below.");
        await renderPlanBuilder();
      }
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
  if (window.location.hash !== "#intake") history.pushState(null, "", "#intake");
  setActiveStepBtn("intake");
  // Always reset state so sidebar starts empty for a fresh run
  intakeComplete = false;
  localStorage.removeItem("rehab_intake_complete");
  localStorage.removeItem("rehab_plan_approved");
  approvedPlanExercises = [];
  applyStepLocks();
  // Reset primary button highlight back to step 1
  document.getElementById("triggerIntakeBtn")?.classList.add("primary");
  document.getElementById("generatePlanBtn")?.classList.remove("primary");
  document.getElementById("exerciseBtn")?.classList.remove("primary");
  loadProtocol(); // will now render "awaiting intake"

  switchStage("chat");
  clearChatLog();
  activeFlow = { type: "intake", step: 0, answers: {} };
  updateFlowUI(true);
  appendChatBubble("coach",
    "I'll walk you through a quick intake. Press Enter to use each default — it's fast for demo.\n\n" +
    INTAKE_QUESTIONS[0].q
  );
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
  if (window.location.hash !== "#checkin") history.pushState(null, "", "#checkin");
  setActiveStepBtn("checkin");
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
  ["generatePlanBtn", "triggerIntakeBtn", "triggerCheckinBtn", "exerciseBtn"].forEach((id) => {
    const btn = document.getElementById(id);
    if (btn) btn.disabled = disabled;
  });
}

// ---------------------------------------------------------------------------
// Step 2: Generate Plan — show plan + Approve button
// ---------------------------------------------------------------------------

const DAYS = ["M", "T", "W", "Th", "F", "S", "Su"];
const DAY_LABELS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"];
let selectedDays = new Set(DAYS); // default: every day

function triggerGeneratePlan() {
  if (window.location.hash !== "#plan") history.pushState(null, "", "#plan");
  setActiveStepBtn("plan");
  switchStage("chat");
  clearChatLog();
  selectedDays = new Set(DAYS);
  _pendingPlan.length = 0;
  renderPlanBuilder();
}

async function renderPlanBuilder() {
  const log = document.getElementById("chatLog");

  const wrap = document.createElement("div");
  wrap.className = "chat-bubble freq-picker-wrap";
  wrap.id = "planBuilderWrap";

  const dayBtns = DAYS.map((d, i) => `
    <button class="day-btn active"
            data-day="${d}"
            title="${DAY_LABELS[i]}"
            onclick="toggleDay(this, '${d}')">
      ${d}
    </button>`).join("");

  wrap.innerHTML = `
    <div class="freq-picker-label">Training days</div>
    <div class="day-btn-row">${dayBtns}</div>
    <div class="freq-summary" id="freqSummary">Every day (7 days/week)</div>
    <div class="freq-picker-label" style="margin-top:14px">Add exercises to your plan</div>
    <div class="plan-rows" id="planRowsInner"><div class="plan-loading">Loading exercises…</div></div>
    <div class="pr-result-actions" style="margin-top:12px">
      <button class="pr-approve-btn" id="confirmFreqBtn" disabled onclick="confirmFrequencyAndGenerate()">
        Generate Plan (add exercises first)
      </button>
    </div>`;
  log.appendChild(wrap);
  scrollChatLog();

  // Fetch exercises and inject rows
  try {
    const res = await fetch(`${API_BASE}/protocol/exercises`);
    if (!res.ok) throw new Error(`status ${res.status}`);
    const data = await res.json();
    const exercises = data.exercises || [];
    const rowsEl = document.getElementById("planRowsInner");
    if (!rowsEl) return;
    rowsEl.innerHTML = exercises.map(ex => `
      <div class="plan-row">
        <div class="plan-row-info">
          <span class="plan-row-name">${escapeHtml(ex.name)}</span>
          <span class="plan-row-spec">${escapeHtml(ex.spec || ex.default_dose || "")}</span>
        </div>
        <button class="plan-add-btn"
                data-ex-id="${escapeHtml(ex.id || ex.name)}"
                data-ex-name="${escapeHtml(ex.name)}"
                data-ex-spec="${escapeHtml(ex.spec || ex.default_dose || "")}"
                data-ex-gen-url="${escapeHtml(ex.generated_video_url || "")}"
                data-ex-yt-id="${escapeHtml(ex.youtube_id || "")}"
                data-ex-watch-url="${escapeHtml(ex.youtube_watch_url || "")}"
                data-ex-thumb-url="${escapeHtml(ex.thumbnail_url || "")}"
                data-ex-cues="${escapeHtml(JSON.stringify(ex.cues || []))}"
                onclick="addToPlan(this)">
          + Add
        </button>
      </div>`).join("");
    scrollChatLog();
  } catch (e) {
    const rowsEl = document.getElementById("planRowsInner");
    if (rowsEl) rowsEl.innerHTML = `<div style="color:var(--danger);font-size:12px">Could not load exercises: ${escapeHtml(e.message)}</div>`;
  }
}

function toggleDay(btn, day) {
  if (selectedDays.has(day)) {
    selectedDays.delete(day);
    btn.classList.remove("active");
  } else {
    selectedDays.add(day);
    btn.classList.add("active");
  }
  updateFreqSummary();
}

function updateFreqSummary() {
  const el = document.getElementById("freqSummary");
  const confirmBtn = document.getElementById("confirmFreqBtn");
  if (!el) return;
  const count = selectedDays.size;
  if (count === 0) {
    el.textContent = "No days selected";
    if (confirmBtn) confirmBtn.disabled = true;
    return;
  }
  if (confirmBtn) confirmBtn.disabled = false;
  const ordered = DAYS.filter(d => selectedDays.has(d));
  if (count === 7) {
    el.textContent = "Every day (7 days/week)";
  } else if (count === 5 && !selectedDays.has("S") && !selectedDays.has("Su")) {
    el.textContent = "Weekdays only (Mon–Fri)";
  } else if (count === 2 && selectedDays.has("S") && selectedDays.has("Su")) {
    el.textContent = "Weekends only";
  } else {
    el.textContent = `${ordered.join(", ")} (${count} day${count > 1 ? "s" : ""}/week)`;
  }
}

function confirmFrequencyAndGenerate() {
  const btn = document.getElementById("confirmFreqBtn");
  if (btn) { btn.disabled = true; btn.textContent = "Generating..."; }

  // Commit the pending exercise selections
  approvedPlanExercises = [..._pendingPlan];
  _pendingPlan.length = 0;

  const ordered = DAYS.filter(d => selectedDays.has(d));
  const freqNote = `Training days: ${ordered.join(", ")} (${ordered.length}/week). Exercises: ${approvedPlanExercises.map(e => e.name).join(", ")}.`;

  appendChatBubble("coach", `Scheduled reminder set for ${ordered.join(", ")}. Generating your protocol...`);
  onPlanApproved();
  invokeAgent("weekly_plan", { intake_text: freqNote });
  setTimeout(() => triggerExercise(), 2400);
}

// Exercises the user staged in step 2 (by clicking "+ Add to plan")
// These are the only ones shown in step 3 guided exercise.
const _pendingPlan = []; // { id, name, spec, ...card data }

async function showPlanWithApprove() {
  const log = document.getElementById("chatLog");
  if (!log) return;
  try {
    const res = await fetch(`${API_BASE}/protocol/exercises`);
    if (!res.ok) throw new Error(`status ${res.status}`);
    const data = await res.json();
    const exercises = data.exercises || [];

    const header = document.createElement("div");
    header.className = "chat-bubble coach";
    header.innerHTML = `<strong>Week ${data.week || 1} Protocol — ${data.phase || "rehab"}</strong><br>
      Add each exercise to your plan, then hit Generate Plan.`;
    log.appendChild(header);

    // Per-exercise rows with "Add to plan" buttons
    const planWrap = document.createElement("div");
    planWrap.className = "chat-bubble pr-result plan-builder";
    planWrap.id = "planBuilderWrap";

    const rows = exercises.map(ex => `
      <div class="plan-row" id="plan-row-${escapeHtml(ex.id || ex.name)}">
        <div class="plan-row-info">
          <span class="plan-row-name">${escapeHtml(ex.name)}</span>
          <span class="plan-row-spec">${escapeHtml(ex.spec || ex.default_dose || "")}</span>
        </div>
        <button class="plan-add-btn"
                data-ex-id="${escapeHtml(ex.id || ex.name)}"
                data-ex-name="${escapeHtml(ex.name)}"
                data-ex-spec="${escapeHtml(ex.spec || ex.default_dose || "")}"
                data-ex-gen-url="${escapeHtml(ex.generated_video_url || "")}"
                data-ex-yt-id="${escapeHtml(ex.youtube_id || "")}"
                data-ex-watch-url="${escapeHtml(ex.youtube_watch_url || "")}"
                data-ex-thumb-url="${escapeHtml(ex.thumbnail_url || "")}"
                data-ex-cues="${escapeHtml(JSON.stringify(ex.cues || []))}"
                onclick="addToPlan(this)">
          + Add to plan
        </button>
      </div>
    `).join("");

    planWrap.innerHTML = `
      <div class="pr-result-header">Protocol ready — add exercises to your plan</div>
      <div class="plan-rows">${rows}</div>
      <div class="pr-result-actions" style="margin-top:12px">
        <button class="pr-approve-btn" id="generatePlanFinalBtn"
                disabled onclick="finalizePlan()">
          Generate Plan (0 selected)
        </button>
      </div>`;
    log.appendChild(planWrap);
    scrollChatLog();
  } catch (e) {
    appendChatBubble("error", `Could not load plan: ${e.message}`);
  }
}

function addToPlan(btn) {
  const id       = btn.dataset.exId;
  const name     = btn.dataset.exName;
  if (_pendingPlan.some(e => e.id === id)) return;

  _pendingPlan.push({
    id,
    name,
    spec:              btn.dataset.exSpec,
    generated_video_url: btn.dataset.exGenUrl,
    youtube_id:        btn.dataset.exYtId,
    youtube_watch_url: btn.dataset.exWatchUrl,
    thumbnail_url:     btn.dataset.exThumbUrl,
    cues:              (() => { try { return JSON.parse(btn.dataset.exCues); } catch { return []; } })(),
  });

  btn.textContent = "✓ Added";
  btn.disabled = true;
  btn.classList.add("added");

  const genBtn = document.getElementById("confirmFreqBtn");
  if (genBtn) {
    genBtn.disabled = false;
    genBtn.textContent = `Generate Plan (${_pendingPlan.length} exercise${_pendingPlan.length > 1 ? "s" : ""} selected)`;
  }
}

function finalizePlan() {
  if (!_pendingPlan.length) return;
  approvedPlanExercises = [..._pendingPlan];
  _pendingPlan.length = 0;

  const genBtn = document.getElementById("generatePlanFinalBtn");
  if (genBtn) { genBtn.textContent = "✓ Plan generated"; genBtn.disabled = true; }

  onPlanApproved();
  // Auto-advance to step 3 — load guided exercise cards immediately
  setTimeout(() => triggerExercise(), 600);
}

// ---------------------------------------------------------------------------
// Step 3: Exercise — load exercises, reveal Sora video on Add to today
// ---------------------------------------------------------------------------

function triggerExercise() {
  if (window.location.hash !== "#exercise") history.pushState(null, "", "#exercise");
  setActiveStepBtn("exercise");
  switchStage("chat");
  clearChatLog();
  loadExerciseCards();
}

async function loadExerciseCards() {
  const log = document.getElementById("chatLog");

  const render = (exercises) => {
    if (!exercises.length) {
      appendChatBubble("coach", "No exercises in your plan yet — complete step 2 first.");
      return;
    }
    renderExerciseGallery(exercises);
    scrollChatLog();
  };

  if (approvedPlanExercises.length) {
    render(approvedPlanExercises);
    return;
  }
  try {
    const res = await fetch(`${API_BASE}/protocol/exercises`);
    if (!res.ok) throw new Error(`status ${res.status}`);
    const data = await res.json();
    render(data.exercises || []);
  } catch (e) {
    appendChatBubble("error", `Could not load exercises: ${e.message}`);
  }
}

// Single gallery: large main player + thumbnail strip to switch exercises
function renderExerciseGallery(exercises) {
  const log = document.getElementById("chatLog");

  const wrap = document.createElement("div");
  wrap.className = "exercise-gallery";

  // Build per-exercise data (video src + thumb src)
  const items = exercises.map((ex) => {
    const genUrl   = ex.generated_video_url || "";
    const ytId     = ex.youtube_id || "";
    const watchUrl = ex.youtube_watch_url || "";
    const thumb    = ex.thumbnail_url || (ytId ? `https://img.youtube.com/vi/${ytId}/hqdefault.jpg` : "");
    return { ex, genUrl, ytId, watchUrl, thumb };
  });

  const thumbsHtml = items.map((item, i) => {
    const thumbContent = item.genUrl
      ? `<video src="${escapeHtml(item.genUrl)}" muted preload="metadata" class="gallery-thumb-video"></video>`
      : item.thumb
        ? `<img src="${escapeHtml(item.thumb)}" alt="${escapeHtml(item.ex.name)}" class="gallery-thumb-img" />`
        : `<div class="gallery-thumb-blank"></div>`;
    return `
      <button class="gallery-thumb-btn${i === 0 ? " active" : ""}" data-idx="${i}" onclick="switchGalleryItem(${i})">
        ${thumbContent}
        <span class="gallery-thumb-label">${escapeHtml(item.ex.name || item.ex.id || "")}</span>
      </button>`;
  }).join("");

  wrap.innerHTML = `
    <div class="gallery-thumbs">${thumbsHtml}</div>
    <div class="gallery-main">
      <div class="gallery-video-wrap" id="galleryVideoWrap"></div>
      <div class="gallery-main-info">
        <span class="gallery-main-title" id="galleryTitle"></span>
        <span class="gallery-main-dose" id="galleryDose"></span>
        <span class="gallery-main-badge" id="galleryBadge"></span>
      </div>
      <ul class="gallery-cues" id="galleryCues"></ul>
    </div>
  `;
  log.appendChild(wrap);

  // Store items on the element for switchGalleryItem to access
  wrap._galleryItems = items;
  window._galleryWrap = wrap;

  switchGalleryItem(0);
}

function switchGalleryItem(idx) {
  const wrap = window._galleryWrap;
  if (!wrap) return;
  const items = wrap._galleryItems;
  const item = items[idx];
  if (!item) return;

  // Update active thumbnail
  wrap.querySelectorAll(".gallery-thumb-btn").forEach((btn, i) => {
    btn.classList.toggle("active", i === idx);
  });

  // Pause any playing video
  const existing = wrap.querySelector("#galleryVideoWrap video");
  if (existing) existing.pause();

  // Build main video/media
  const videoWrap = wrap.querySelector("#galleryVideoWrap");
  let mediaHtml = "";
  let badge = "";
  if (item.genUrl) {
    mediaHtml = `<video src="${escapeHtml(item.genUrl)}" controls muted playsinline preload="metadata"></video>`;
    badge = `<span class="video-source sora">sora-2 generated</span>`;
  } else if (item.ytId || item.watchUrl) {
    const href = escapeHtml(item.watchUrl || `https://www.youtube.com/watch?v=${item.ytId}`);
    mediaHtml = `<a href="${href}" target="_blank" rel="noopener" class="exercise-video-thumb">
      <img src="${escapeHtml(item.thumb)}" alt="${escapeHtml(item.ex.name)}" />
      <span class="play-btn">▶</span>
    </a>`;
    badge = `<span class="video-source youtube">curated</span>`;
  } else {
    mediaHtml = `<div class="exercise-video-placeholder"><span class="video-placeholder-text">No video available</span></div>`;
  }
  videoWrap.innerHTML = mediaHtml;

  // Update info strip
  wrap.querySelector("#galleryTitle").textContent = item.ex.name || item.ex.id || "";
  wrap.querySelector("#galleryDose").textContent  = item.ex.default_dose || item.ex.spec || "";
  wrap.querySelector("#galleryBadge").innerHTML   = badge;
  const cuesEl = wrap.querySelector("#galleryCues");
  cuesEl.innerHTML = (item.ex.cues || []).map(c => `<li>${escapeHtml(c)}</li>`).join("");

  // Form Check (feature-flagged): swap demo video for webcam + pose overlay
  maybeAttachFormCheckBtn(wrap, item);
}

// ---------------------------------------------------------------------------
// Pose form check (feature spike — flag-gated, throwaway)
// Toggle with /?pose=1 (persists to localStorage). See frontend/pose.js.
// ---------------------------------------------------------------------------

function maybeAttachFormCheckBtn(wrap, item) {
  if (!window.PoseFormCheck) {
    console.warn("PoseFormCheck not loaded — pose.js failed to initialize");
    return;
  }
  // Only show the button on exercises that have pose criteria defined.
  // Without an EXERCISES entry the rep tracker no-ops silently and the
  // patient gets a confusing "0 reps" experience.
  if (!window.PoseFormCheck.EXERCISES?.[item.ex.id]) return;
  const videoWrap = wrap.querySelector("#galleryVideoWrap");
  if (!videoWrap || videoWrap.parentElement.querySelector(".pose-form-check-btn")) return;
  const btn = document.createElement("button");
  btn.type = "button";
  btn.className = "pose-form-check-btn";
  btn.dataset.state = "off";
  btn.textContent = "Form Check (webcam)";
  btn.onclick = () => togglePoseFormCheck(wrap, item, btn);
  videoWrap.parentElement.insertBefore(btn, videoWrap);
}

// ── Voice cues (Web Speech API) ─────────────────────────────────────────────
//
// Browser-native, free, instant. Voice toggle defaults OFF on first session
// so we don't surprise the user; first toggle warms up speechSynthesis to
// satisfy mobile-Safari's autoplay gate.

function poseVoiceEnabled() {
  return localStorage.getItem("poseVoice") === "1";
}

function setPoseVoiceEnabled(on) {
  localStorage.setItem("poseVoice", on ? "1" : "0");
}

let _poseVoiceObj = null;

function pickVoice() {
  if (_poseVoiceObj) return _poseVoiceObj;
  const voices = window.speechSynthesis?.getVoices?.() || [];
  if (!voices.length) return null;
  _poseVoiceObj =
    voices.find((v) =>
      v.lang.startsWith("en") &&
      /samantha|victoria|google us english|female/i.test(v.name)
    ) || voices.find((v) => v.lang.startsWith("en")) || voices[0];
  return _poseVoiceObj;
}

function speakCue(text) {
  if (!poseVoiceEnabled()) return;
  if (!window.speechSynthesis || !window.SpeechSynthesisUtterance) return;
  const u = new SpeechSynthesisUtterance(String(text));
  u.rate   = 1.05;
  u.pitch  = 1.0;
  u.volume = 0.9;
  const v = pickVoice();
  if (v) u.voice = v;
  window.speechSynthesis.speak(u);
}

// ── Pose set telemetry (POST /pose/session) ─────────────────────────────────

function poseAuthHeaders() {
  // Mirrors the /chat behaviour: include the Supabase JWT if one is in
  // localStorage (set by the future magic-link login). Until that lands,
  // the POST will 401 in production — we degrade gracefully (toast + skip).
  const jwt = localStorage.getItem("supabaseJwt");
  return jwt ? { Authorization: `Bearer ${jwt}` } : {};
}

async function postPoseSession(exercise, repsHistory, warnings, repSummary) {
  const startedAt = new Date(
    Date.now() - Math.max(60_000, repsHistory.length * 4_000)
  ).toISOString();
  // Aggregate warnings by id with a count.
  const counts = {};
  for (const w of warnings || []) {
    if (!w?.id) continue;
    counts[w.id] = counts[w.id] || { id: w.id, msg: w.msg, count: 0 };
    counts[w.id].count += 1;
  }
  const body = {
    exercise_id: exercise.id,
    exercise_name: exercise.name,
    started_at: startedAt,
    ended_at: new Date().toISOString(),
    target_dose: exercise.default_dose || null,
    reps: repsHistory.map((r) => ({
      rep: r.repNumber,
      depth_min: r.depthMin,
      status: r.status,
      msg: r.msg,
    })),
    warnings: Object.values(counts),
    client: "web/pose-v1",
  };
  try {
    const res = await fetch(`${API_BASE}/pose/session`, {
      method: "POST",
      headers: { "Content-Type": "application/json", ...poseAuthHeaders() },
      body: JSON.stringify(body),
    });
    if (res.status === 401) {
      showToast("Set not logged — sign in to save your progress", "info");
      return;
    }
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    showToast("Set logged — Maya can see it now", "info");
  } catch (e) {
    console.warn("postPoseSession failed:", e);
    showToast(`Set log failed: ${e.message}`, "error");
  }
}

function renderPoseMetrics(container, payload, exercise) {
  if (!container) return;
  const metrics = payload.metrics || [];
  const repPill = payload.repSummary && payload.repSummary.repCount > 0
    ? `<span class="metric-pill good reps-pill">
        <span class="metric-pill-label">reps</span>
        <span class="metric-pill-value">${payload.repSummary.repCount}</span>
      </span>
      ${payload.repSummary.bestDepth != null ? `<span class="metric-pill good">
        <span class="metric-pill-label">best</span>
        <span class="metric-pill-value">${payload.repSummary.bestDepth}°</span>
      </span>` : ""}`
    : "";
  if (!metrics.length && !repPill) {
    container.innerHTML = `<span class="metric-pill idle">no body in frame</span>`;
    return;
  }
  const pills = metrics.map((m) => {
    const valStr  = m.value != null ? `${m.value}${m.unit || ""}` : "--";
    const tgtStr  = m.target != null ? ` / ${m.target}${m.unit || ""}` : "";
    const pctStr  = m.percent != null ? ` (${m.percent}%)` : "";
    return `<span class="metric-pill ${m.status}">
      <span class="metric-pill-label">${escapeHtml(m.label || m.id)}</span>
      <span class="metric-pill-value">${escapeHtml(valStr)}${escapeHtml(tgtStr)}${escapeHtml(pctStr)}</span>
    </span>`;
  }).join("");
  container.innerHTML = repPill + pills;
}

function renderPoseWarnings(container, payload) {
  if (!container) return;
  const warns = payload.warnings || [];
  if (!warns.length) { container.hidden = true; container.innerHTML = ""; return; }
  container.hidden = false;
  container.innerHTML = warns.map((w) =>
    `<span class="alignment-warning ${w.status}">⚠ ${escapeHtml(w.msg)}</span>`
  ).join("");
}

function renderPoseSession(container, repsHistory, repSummary) {
  if (!container) return;
  if (!repsHistory.length) { container.hidden = true; container.innerHTML = ""; return; }
  const recent = repsHistory.slice(-6);
  const glyph = (s) => s === "bad" ? "✗" : s === "warn" ? "⚠" : "✓";
  const total = repSummary?.repCount ?? repsHistory.length;
  const rows = recent.map((r) => `
    <div class="pose-rep-row ${r.status}">
      <span class="pose-rep-num">Rep ${r.repNumber}</span>
      <span class="pose-rep-status">${glyph(r.status)}</span>
      <span class="pose-rep-depth">${r.depthMin}°</span>
      <span class="pose-rep-msg">${escapeHtml(r.msg || "")}</span>
    </div>
  `).join("");
  container.hidden = false;
  container.innerHTML = `
    <div class="pose-rep-header">SET — ${total} REP${total === 1 ? "" : "S"}</div>
    ${rows}
  `;
}

async function togglePoseFormCheck(wrap, item, btn) {
  const videoWrap = wrap.querySelector("#galleryVideoWrap");
  if (!videoWrap) return;

  if (btn.dataset.state === "on") {
    window.PoseFormCheck.stop();
    btn.dataset.state = "off";
    btn.textContent = "Form Check";
    document.body.classList.remove("pose-active");
    // Restore demo video by re-running switchGalleryItem on the active idx
    const activeIdx = Array.from(wrap.querySelectorAll(".gallery-thumb-btn"))
      .findIndex((b) => b.classList.contains("active"));
    switchGalleryItem(activeIdx >= 0 ? activeIdx : 0);
    return;
  }

  btn.disabled = true;
  btn.textContent = "Loading model...";
  try {
    await window.PoseFormCheck.init();
  } catch (e) {
    btn.disabled = false;
    btn.textContent = "Form Check";
    document.body.classList.remove("pose-active");
    showToast(`Pose model failed to load: ${e.message}`, "error");
    return;
  }
  document.body.classList.add("pose-active");

  // Side-by-side: keep the Sora reference video looping next to the live
  // webcam + skeleton overlay. Patient mirrors the demo while the model
  // tracks them.
  const refSrc = item.ex.generated_video_url || item.ex.video_url || "";
  const voiceOn = poseVoiceEnabled();
  videoWrap.innerHTML = `
    <div class="pose-split">
      <div class="pose-split-ref">
        <video src="${escapeHtml(refSrc)}" playsinline muted autoplay loop></video>
        <div class="pose-split-ref-label">Reference</div>
      </div>
      <div class="pose-root pose-split-live" id="poseRoot">
        <div class="pose-toolbar">
          <div class="pose-title" title="${escapeHtml(item.ex.name)}">
            <span class="pose-title-name">${escapeHtml(item.ex.name)}</span>
            <span class="pose-title-dose">${escapeHtml(item.ex.default_dose || "")}</span>
          </div>
          <div class="pose-metrics" id="poseMetrics">
            <span class="metric-pill idle">starting camera...</span>
          </div>
          <button class="pose-voice-btn" id="poseVoiceBtn" data-on="${voiceOn ? "1" : "0"}" title="Voice cues">
            ${voiceOn ? "🔊 Voice" : "🔇 Voice"}
          </button>
          <button class="pose-fullscreen-btn" id="poseFullscreenBtn" title="Toggle fullscreen">⛶</button>
        </div>
        <div class="pose-warnings" id="poseWarnings" hidden></div>
        <div class="pose-stage" id="poseStage">
          <video id="poseVideo" playsinline muted autoplay></video>
          <canvas id="poseCanvas" class="pose-overlay-canvas"></canvas>
        </div>
        <div class="pose-session-card" id="poseSession" hidden></div>
      </div>
    </div>
  `;
  const root        = videoWrap.querySelector("#poseRoot");
  const fsBtn       = videoWrap.querySelector("#poseFullscreenBtn");
  const voiceBtn    = videoWrap.querySelector("#poseVoiceBtn");
  const metricsEl   = videoWrap.querySelector("#poseMetrics");
  const warningsEl  = videoWrap.querySelector("#poseWarnings");
  const sessionEl   = videoWrap.querySelector("#poseSession");
  const repsHistory = [];
  const warningsAcc = [];
  let posted = false;
  fsBtn.onclick = () => {
    if (document.fullscreenElement) document.exitFullscreen();
    else root.requestFullscreen?.();
  };
  voiceBtn.onclick = () => {
    const next = !poseVoiceEnabled();
    setPoseVoiceEnabled(next);
    voiceBtn.dataset.on = next ? "1" : "0";
    voiceBtn.textContent = next ? "🔊 Voice" : "🔇 Voice";
    if (next) {
      // Warm up speechSynthesis on the user gesture (Safari autoplay gate).
      try { speakCue("voice ready"); } catch (_) {}
    } else {
      try { window.speechSynthesis?.cancel?.(); } catch (_) {}
    }
  };
  const videoEl  = videoWrap.querySelector("#poseVideo");
  const canvasEl = videoWrap.querySelector("#poseCanvas");

  try {
    await window.PoseFormCheck.start(
      videoEl,
      canvasEl,
      item.ex.id,
      (payload) => {
        if (payload.repEvents && payload.repEvents.length) {
          for (const ev of payload.repEvents) repsHistory.push(ev);
          renderPoseSession(sessionEl, repsHistory, payload.repSummary);
        }
        if (payload.warnings && payload.warnings.length) {
          for (const w of payload.warnings) warningsAcc.push(w);
        }
        renderPoseMetrics(metricsEl, payload, item.ex);
        renderPoseWarnings(warningsEl, payload);
        // setComplete fires once when the patient hits the prescribed reps.
        // POST the rolled-up set to the backend so Maya sees it on her
        // next reply.
        if (payload.setComplete && !posted) {
          posted = true;
          postPoseSession(item.ex, repsHistory, warningsAcc, payload.repSummary);
        }
      },
      {
        exerciseName: item.ex.name,
        targetDose: item.ex.default_dose,
        voice: speakCue,
      },
    );
    btn.disabled = false;
    btn.dataset.state = "on";
    btn.textContent = "Stop";
  } catch (e) {
    btn.disabled = false;
    btn.textContent = "Form Check";
    document.body.classList.remove("pose-active");
    showToast(`Camera error: ${e.message}`, "error");
    // Restore demo video on failure
    const activeIdx = Array.from(wrap.querySelectorAll(".gallery-thumb-btn"))
      .findIndex((b) => b.classList.contains("active"));
    switchGalleryItem(activeIdx >= 0 ? activeIdx : 0);
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
    const res = await authedFetch(`${API_BASE}/chat`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        session_id: "default",
        message,
        history: chatHistory.slice(-10),
      }),
    });
    if (res.status === 401) {
      appendChatBubble("error", "Sign in to chat with Coach Maya.");
      return;
    }
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

  const cuesHtml = (card.cues || []).map((c) => `<li>${escapeHtml(c)}</li>`).join("");
  const dose = card.default_dose
    ? `<span class="exercise-dose">${escapeHtml(card.default_dose)}</span>` : "";

  // Store video data on the element — revealed after "Add to today"
  wrap.dataset.generatedUrl = card.generated_video_url || "";
  wrap.dataset.youtubeId    = card.youtube_id || "";
  wrap.dataset.watchUrl     = card.youtube_watch_url || "";
  wrap.dataset.thumbUrl     = card.thumbnail_url || "";
  wrap.dataset.cardName     = card.name || card.id || "";

  wrap.innerHTML = `
    <div class="exercise-video-placeholder">
      <span class="video-placeholder-text">Add to today to load video</span>
    </div>
    <div class="exercise-meta">
      <div class="exercise-title-row">
        <span class="exercise-title">${escapeHtml(card.name || card.id || "")}</span>
        ${dose}
      </div>
      <ul class="exercise-cues">${cuesHtml}</ul>
      <div class="exercise-actions">
        <button class="exercise-action-btn primary"
                data-add-id="${escapeHtml(card.id || "")}"
                data-add-name="${escapeHtml(card.name || card.id || "")}"
                onclick="addToTodayFromBtn(this)">
          ＋ Add to today
        </button>
      </div>
    </div>
  `;
  log.appendChild(wrap);
  scrollChatLog();
}

function revealVideoOnCard(wrap) {
  const genUrl   = wrap.dataset.generatedUrl;
  const ytId     = wrap.dataset.youtubeId;
  const watchUrl = wrap.dataset.watchUrl;
  const thumb    = wrap.dataset.thumbUrl || (ytId ? `https://img.youtube.com/vi/${ytId}/hqdefault.jpg` : "");
  const name     = wrap.dataset.cardName;

  let embed = "";
  let badge = "";

  if (genUrl) {
    embed = `<div class="exercise-video-wrap">
      <video src="${escapeHtml(genUrl)}" controls autoplay muted playsinline loop preload="auto"></video>
    </div>`;
    badge = `<span class="video-source sora">sora-2 generated</span>`;
  } else if (ytId || watchUrl) {
    const href = escapeHtml(watchUrl || `https://www.youtube.com/watch?v=${ytId}`);
    embed = `<a class="exercise-video-wrap exercise-video-thumb" href="${href}" target="_blank" rel="noopener">
      <img src="${escapeHtml(thumb)}" alt="${escapeHtml(name)}" />
      <span class="play-btn">▶</span>
    </a>`;
    badge = `<span class="video-source youtube">curated</span>`;
  }

  if (!embed) return;

  // Swap placeholder → video
  const placeholder = wrap.querySelector(".exercise-video-placeholder");
  if (placeholder) placeholder.outerHTML = embed;

  // Add source badge next to title
  const titleRow = wrap.querySelector(".exercise-title-row");
  if (titleRow && badge) titleRow.insertAdjacentHTML("beforeend", badge);
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

  // Reveal the video on the card now that the exercise is confirmed
  const wrap = btn.closest(".exercise-card");
  if (wrap) revealVideoOnCard(wrap);

  btn.textContent = "✓ Added";
  btn.disabled = true;
  btn.classList.remove("primary");
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
