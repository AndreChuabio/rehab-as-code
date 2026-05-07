// clinician_admin.js — Pipeline debug mode for the clinician dashboard.
//
// A4 surface: segmented control (Review queue | Pipeline debug), cross-
// patient run feed, pipeline run timeline (Gantt), decision-trace card,
// per-agent expandable cards.
//
// Lives alongside clinician.js. Activates only after /admin/me confirms
// the user has role='admin'. Plain clinicians never see the admin chip
// or the segmented control — page is byte-identical to today.
//
// PHI defaults: redacted by default. Patient identifiers shown as
// pt_<6char>; output_summary collapsed; no PHI in URLs. Per-field
// reveal toggles ship in A5.

(function () {
  const API_BASE = ""; // same-origin
  const POLL_TTL_MS = 30_000;

  // ── Module state ────────────────────────────────────────────────────────────
  let _isAdmin = false;
  let _activeMode = "review";
  let _runs = [];
  let _activeRequestId = null;

  // ── Header / segmented control wiring ──────────────────────────────────────

  async function init() {
    // Wait until auth.js has resolved a JWT. clinician.js bootstraps the
    // queue once auth is ready; we hook the same window event so the
    // admin probe runs after auth, not before.
    if (window.RehabAuth?.getJwt?.()) {
      await _probeAdmin();
    } else {
      window.addEventListener("rehab-auth-ready", _probeAdmin, { once: true });
    }
    _wireSegmented();
    _wireFilters();
  }

  async function _probeAdmin() {
    try {
      const res = await _authedFetch("/admin/me");
      if (!res.ok) return;
      const body = await res.json();
      if (body.role === "admin") {
        _isAdmin = true;
        _enableAdminChrome();
      }
    } catch (e) {
      // 401/403/network — silently stay in clinician-only mode.
    }
  }

  function _enableAdminChrome() {
    const adminBadge = document.getElementById("adminBadge");
    const adminSwitch = document.getElementById("adminModeSwitch");
    if (adminBadge) adminBadge.hidden = false;
    if (adminSwitch) adminSwitch.hidden = false;
  }

  function _wireSegmented() {
    const sw = document.getElementById("adminModeSwitch");
    if (!sw) return;
    sw.addEventListener("click", (e) => {
      const btn = e.target.closest(".admin-mode-btn");
      if (!btn) return;
      _setMode(btn.dataset.mode);
    });
  }

  function _setMode(mode) {
    if (!_isAdmin) return;
    _activeMode = mode;
    const reviewMain = document.querySelector("main.clinician-main:not(.admin-main)");
    const adminMain = document.getElementById("adminMain");
    const strip = document.getElementById("adminModeStrip");
    const title = document.getElementById("clinicianHeaderTitle");
    document.querySelectorAll(".admin-mode-btn").forEach((b) => {
      const isActive = b.dataset.mode === mode;
      b.classList.toggle("active", isActive);
      b.setAttribute("aria-selected", isActive ? "true" : "false");
    });
    if (mode === "debug") {
      if (reviewMain) reviewMain.hidden = true;
      if (adminMain) adminMain.hidden = false;
      if (strip) strip.hidden = false;
      if (title) title.textContent = "Pipeline debug";
      _loadRuns();
      _loadMetrics();
    } else {
      if (reviewMain) reviewMain.hidden = false;
      if (adminMain) adminMain.hidden = true;
      if (strip) strip.hidden = true;
      if (title) title.textContent = "Pending review";
    }
  }

  function _wireFilters() {
    document.getElementById("adminFilterAgent")?.addEventListener("change", _loadRuns);
    document.getElementById("adminFilterErrored")?.addEventListener("change", _loadRuns);
    document.getElementById("adminRefreshBtn")?.addEventListener("click", () => {
      _loadRuns();
      _loadMetrics();
    });
  }

  // ── Run feed ───────────────────────────────────────────────────────────────

  async function _loadRuns() {
    const params = new URLSearchParams({ limit: "50" });
    const agent = document.getElementById("adminFilterAgent")?.value;
    const errored = document.getElementById("adminFilterErrored")?.value;
    if (agent) params.set("agent", agent);
    if (errored) params.set("errored", errored);
    const list = document.getElementById("adminRunsList");
    const empty = document.getElementById("adminRunsEmpty");
    if (!list) return;
    list.innerHTML = `<li class="loading-text">Loading…</li>`;
    try {
      const res = await _authedFetch(`/admin/pipeline_runs?${params}`);
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const body = await res.json();
      _runs = body.runs || [];
      if (!_runs.length) {
        list.innerHTML = "";
        if (empty) empty.hidden = false;
        return;
      }
      if (empty) empty.hidden = true;
      list.innerHTML = _runs.map(_renderRunListItem).join("");
      list.querySelectorAll("li.run-item").forEach((li) => {
        li.addEventListener("click", () => _openRun(li.dataset.requestId));
      });
    } catch (e) {
      list.innerHTML = `<li class="loading-text">Failed: ${_escape(e.message)}</li>`;
    }
  }

  function _renderRunListItem(r) {
    const ago = _timeAgo(r.started_at);
    const statusGlyph = r.n_errors > 0 ? "❗" : "✓";
    const decision = r.terminal_decision || "—";
    const agentChips = (r.agents || [])
      .map((a) => {
        const cls = a.status === "ok" ? "agent-chip-ok" : "agent-chip-err";
        return `<span class="agent-chip ${cls}" title="${_escape(a.agent)} ${a.duration_ms}ms">${_escape(_short(a.agent))}</span>`;
      })
      .join("");
    return `<li class="run-item" data-request-id="${_escape(r.request_id)}">
      <div class="run-item-row1">
        <span class="run-item-time">${_escape(ago)}</span>
        <span class="run-item-patient">${_escape(_pt(r.patient_uid))}</span>
        <span class="run-item-status">${statusGlyph}</span>
      </div>
      <div class="run-item-row2">
        ${agentChips}
        <span class="run-item-decision">${_escape(decision)}</span>
      </div>
    </li>`;
  }

  // ── Run detail ─────────────────────────────────────────────────────────────

  async function _openRun(requestId) {
    if (!requestId) return;
    _activeRequestId = requestId;
    document.querySelectorAll("li.run-item").forEach((li) => {
      li.classList.toggle("active", li.dataset.requestId === requestId);
    });
    const empty = document.getElementById("adminRunEmpty");
    const body = document.getElementById("adminRunBody");
    if (empty) empty.hidden = true;
    if (body) body.hidden = false;
    try {
      const res = await _authedFetch(`/admin/pipeline_runs/${encodeURIComponent(requestId)}`);
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const data = await res.json();
      _renderRunDetail(data);
    } catch (e) {
      document.getElementById("adminRunHeader").textContent = "Failed to load run";
      document.getElementById("adminRunSub").textContent = e.message;
    }
  }

  function _renderRunDetail(data) {
    const header = document.getElementById("adminRunHeader");
    const sub = document.getElementById("adminRunSub");
    const trace = document.getElementById("adminDecisionTrace");
    const timeline = document.getElementById("adminTimeline");
    const cards = document.getElementById("adminAgentCards");

    const totalMs = (data.agents || []).reduce((s, a) => s + (a.duration_ms || 0), 0);
    const totalIn = (data.agents || []).reduce((s, a) => s + (a.tokens_in || 0), 0);
    const totalOut = (data.agents || []).reduce((s, a) => s + (a.tokens_out || 0), 0);
    if (header) header.textContent = `Run · ${_short(data.request_id)}`;
    if (sub) {
      // PHI redacted by default. Render an opaque hash + a Reveal button
      // that audits the access via POST /admin/phi-reveal BEFORE swapping
      // in the real display_name.
      const ptHash = _pt(data.patient.uid);
      sub.innerHTML = `
        <span class="phi-field" data-field="patient_uid">
          <span class="phi-value-hashed">${_escape(ptHash)}</span>
          <button type="button" class="phi-reveal-btn"
                  data-target-uid="${_escape(data.patient.uid || "")}"
                  data-real-name="${_escape(data.patient.display_name || "(no name)")}"
                  data-request-id="${_escape(data.request_id)}">Reveal</button>
        </span>
        · ${(data.agents || []).length} agents · ${totalMs}ms · ${totalIn}+${totalOut} tokens
      `;
      sub.querySelector(".phi-reveal-btn")?.addEventListener("click", _onPhiReveal);
    }

    // Decision trace card — four chips, color-coded
    if (trace) {
      const evaluator = (data.agents || []).find((a) => a.agent === "evaluator");
      const planner = (data.agents || []).find((a) => a.agent === "planner");
      const safety = (data.agents || []).find((a) => a.agent === "safety_reviewer");
      const proto = data.protocol;
      trace.innerHTML = `
        <div class="trace-chip trace-chip-evaluator" data-status="${_escape(evaluator?.status || "missing")}">
          <span class="trace-chip-label">evaluator</span>
          <span class="trace-chip-value">${_escape(evaluator?.decision || "—")}</span>
        </div>
        <div class="trace-chip trace-chip-planner" data-status="${_escape(planner?.status || "missing")}">
          <span class="trace-chip-label">planner</span>
          <span class="trace-chip-value">${_escape((planner?.output_summary?.n_exercises ?? "—") + " ex")}</span>
        </div>
        <div class="trace-chip trace-chip-safety" data-status="${_escape(safety?.status || "missing")}">
          <span class="trace-chip-label">safety</span>
          <span class="trace-chip-value">${_escape(safety?.decision || "—")}</span>
        </div>
        <div class="trace-chip trace-chip-narrator" data-status="${_escape(proto?.narrator_status || "missing")}">
          <span class="trace-chip-label">narrator</span>
          <span class="trace-chip-value">${_escape(proto?.narrator_status || "—")}</span>
        </div>
      `;
    }

    // Timeline — horizontal Gantt of agent spans
    if (timeline) {
      timeline.innerHTML = _renderTimeline(data.agents || []);
    }

    // Per-agent cards (output_summary collapsed by default)
    if (cards) {
      cards.innerHTML = (data.agents || [])
        .map(_renderAgentCard)
        .join("");
      cards.querySelectorAll(".agent-card-toggle").forEach((btn) => {
        btn.addEventListener("click", () => {
          const card = btn.closest(".agent-card");
          card?.classList.toggle("expanded");
        });
      });
    }
  }

  function _renderTimeline(agents) {
    if (!agents.length) return "";
    // Compute relative offsets so the bars line up vs the run start.
    const sorted = agents.slice().sort((a, b) => a.step_index - b.step_index);
    const t0 = new Date(sorted[0].started_at).getTime();
    const totalMs = Math.max(
      1,
      sorted.reduce((max, a) => {
        const t = new Date(a.started_at).getTime() - t0 + (a.duration_ms || 0);
        return Math.max(max, t);
      }, 0),
    );
    const bars = sorted.map((a) => {
      const offMs = new Date(a.started_at).getTime() - t0;
      const left = Math.max(0, (offMs / totalMs) * 100);
      const width = Math.max(1, ((a.duration_ms || 0) / totalMs) * 100);
      const cls = a.status === "ok" ? "bar-ok" : "bar-err";
      const label = `${a.agent} · ${a.duration_ms}ms${a.tokens_out ? ` · ${a.tokens_out} tok` : ""}`;
      return `<div class="timeline-row">
        <span class="timeline-label">${_escape(a.agent)}</span>
        <div class="timeline-track">
          <div class="timeline-bar ${cls}" style="left: ${left}%; width: ${width}%;" title="${_escape(label)}"></div>
        </div>
      </div>`;
    }).join("");
    return `<div class="timeline-wrap">${bars}</div>`;
  }

  function _renderAgentCard(a) {
    const cls = a.status === "ok" ? "card-ok" : "card-err";
    const summary = a.output_summary
      ? `<pre class="agent-card-summary">${_escape(JSON.stringify(a.output_summary, null, 2))}</pre>`
      : `<div class="agent-card-summary-empty">no summary</div>`;
    const errBlock = a.error_class
      ? `<div class="agent-card-err">${_escape(a.error_class)}: ${_escape(a.error_message || "")}</div>`
      : "";
    return `<div class="agent-card ${cls}">
      <div class="agent-card-head">
        <span class="agent-card-name">${_escape(a.agent)}</span>
        <span class="agent-card-status">${_escape(a.status)}</span>
        <span class="agent-card-time">${a.duration_ms}ms</span>
        <span class="agent-card-tokens">${a.tokens_in || 0}+${a.tokens_out || 0} tok</span>
        <button type="button" class="agent-card-toggle">expand</button>
      </div>
      ${errBlock}
      <div class="agent-card-body">
        ${summary}
      </div>
    </div>`;
  }

  // ── Metrics roll-up ─────────────────────────────────────────────────────────

  async function _loadMetrics() {
    const el = document.getElementById("adminMetricsRoll");
    if (!el) return;
    try {
      const res = await _authedFetch("/admin/metrics/agents?window=24h");
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const body = await res.json();
      const agents = body.agents || [];
      if (!agents.length) {
        el.innerHTML = `<span class="metrics-empty">No agent runs in last 24h.</span>`;
        return;
      }
      el.innerHTML = agents.map((a) => {
        const errPct = a.n_runs ? Math.round((a.n_errors / a.n_runs) * 100) : 0;
        return `<span class="metrics-pill">
          <span class="metrics-pill-name">${_escape(_short(a.agent))}</span>
          <span class="metrics-pill-val">p50 ${a.p50_ms || 0}ms</span>
          <span class="metrics-pill-val">p95 ${a.p95_ms || 0}ms</span>
          <span class="metrics-pill-val ${errPct ? "err" : ""}">${errPct}% err</span>
        </span>`;
      }).join("");
    } catch (e) {
      el.innerHTML = `<span class="metrics-empty">Failed: ${_escape(e.message)}</span>`;
    }
  }

  // ── Helpers ────────────────────────────────────────────────────────────────

  function _escape(s) {
    return String(s ?? "").replace(/[&<>"']/g, (c) => ({
      "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
    })[c]);
  }

  function _pt(uid) {
    if (!uid) return "pt_?";
    return `pt_${String(uid).slice(0, 6)}`;
  }

  function _short(s) {
    return String(s || "").length > 18 ? String(s).slice(0, 16) + "…" : String(s || "");
  }

  function _timeAgo(iso) {
    if (!iso) return "—";
    const t = new Date(iso).getTime();
    const dt = (Date.now() - t) / 1000;
    if (dt < 60) return `${Math.round(dt)}s`;
    if (dt < 3600) return `${Math.round(dt / 60)}m`;
    if (dt < 86400) return `${Math.round(dt / 3600)}h`;
    return `${Math.round(dt / 86400)}d`;
  }

  async function _authedFetch(path, opts = {}) {
    const jwt = window.RehabAuth?.getJwt?.();
    const headers = new Headers(opts.headers || {});
    if (jwt) headers.set("Authorization", `Bearer ${jwt}`);
    return fetch(`${API_BASE}${path}`, { ...opts, headers });
  }

  // ── PHI reveal (A5) ─────────────────────────────────────────────────────
  //
  // Audit BEFORE reveal: POST /admin/phi-reveal lands a row in
  // admin_phi_reveals BEFORE we swap in the real value. If the audit
  // call fails we don't reveal — the trail integrity is the whole
  // point. Reveals auto-collapse on tab blur (60s spec deferred; tab
  // blur is the high-value case for "I'm screensharing" failure mode).
  async function _onPhiReveal(e) {
    const btn = e.currentTarget;
    const fieldEl = btn.closest(".phi-field");
    if (!btn || !fieldEl) return;
    btn.disabled = true;
    try {
      const res = await _authedFetch("/admin/phi-reveal", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          target_user_id: btn.dataset.targetUid,
          field: fieldEl.dataset.field || "unknown",
          request_id: btn.dataset.requestId || null,
        }),
      });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      // Swap hashed -> real value, mark phi-revealed for auto-collapse.
      const valEl = fieldEl.querySelector(".phi-value-hashed");
      if (valEl) {
        valEl.textContent = btn.dataset.realName || "(unknown)";
        valEl.classList.add("phi-revealed");
      }
      btn.remove();
    } catch (err) {
      btn.disabled = false;
      btn.textContent = "Reveal failed";
      console.warn("phi-reveal audit failed:", err);
    }
  }

  // Auto-collapse PHI reveals on tab blur. Re-renders the hashed form
  // by stripping the .phi-revealed class — the next run-detail render
  // re-installs the Reveal button from scratch.
  document.addEventListener("visibilitychange", () => {
    if (document.hidden && _activeRequestId) {
      // Re-fetching the run detail re-renders with PHI redacted. Cheaper
      // than maintaining per-field reset logic.
      _openRun(_activeRequestId);
    }
  });

  // ── Bootstrap ───────────────────────────────────────────────────────────────
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
