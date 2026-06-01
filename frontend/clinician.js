// clinician.js — Clinician dashboard logic.
//
// Lifecycle:
//   1. Init Supabase auth (shared with patient app via auth.js).
//   2. Verify the signed-in user is a clinician via GET /me/role.
//      - role=anonymous → redirect to /
//      - role=patient   → redirect to /
//      - role=clinician → load the queue
//   3. Render pending protocols; on selection load detail + render diff.
//   4. Approve / reject hits the existing /protocols/{id}/approve|reject
//      endpoints (added in PR #53). The patient sidebar at / refreshes
//      independently the next time the patient loads it.
//
// Vanilla JS, matching the rest of the codebase.

(function () {
  const API_BASE = "";

  function $(id) { return document.getElementById(id); }
  function authedFetch(path, options = {}) {
    const jwt = window.RehabAuth?.getJwt?.();
    const hdrs = new Headers(options.headers || {});
    if (jwt) hdrs.set("Authorization", `Bearer ${jwt}`);
    return fetch(path, { ...options, headers: hdrs });
  }

  let queue = [];
  let selectedId = null;
  let pendingAction = null; // 'approve' | 'reject'
  let currentPatientToken = null; // for raw-context audit logging

  // Guided onboarding tour (tour.js). Detail-pane steps (#patientSummary,
  // #diffStructured, #safetyConcerns, the action buttons) resolve only when a
  // queue item is open — on first landing they are filtered out, so the
  // auto-tour covers the queue + controls and the full walk appears once the
  // clinician opens a patient and re-runs via "Take the tour". Bump `key` to
  // re-show after a material change.
  const CLINICIAN_TOUR = {
    key: "clinician_v1",
    steps: [
      { selector: ".clinician-queue", placement: "right", title: "Your review queue",
        body: "Every AI-drafted protocol revision waiting for your sign-off. Rows flagged SAFETY jump to the top. Click any patient to open their draft." },
      { selector: "#patientSummary", placement: "right", title: "Patient at a glance",
        body: "A scannable summary — injury, phase, pain trend, symptoms, goals — so you have context before reading the changes." },
      { selector: "#diffStructured", placement: "left", title: "What changed",
        body: "The proposed changes as readable exercise cards. Clinically significant changes — reps up, ROM down, a removed safety regression — are pinned at the top and cannot be collapsed." },
      { selector: "#safetyConcerns", placement: "left", title: "Safety review",
        body: "Concerns the safety reviewer flagged on this draft. Read these before approving." },
      { selector: ".clinician-detail-actions", placement: "bottom", title: "Approve or reject",
        body: "You are the gate — nothing reaches the patient until you approve. Rejections require a note that flows back to the pipeline." },
      { selector: "#clinicianAsPatient", placement: "bottom", title: "View as patient",
        body: "See exactly what the patient sees, without leaving your session." },
    ],
  };

  async function bootstrap() {
    if (!window.RehabAuth) {
      redirectToPatient("auth library missing");
      return;
    }
    try {
      await window.RehabAuth.init();
    } catch (e) {
      console.error("auth init failed", e);
      redirectToPatient("auth init failed");
      return;
    }

    const jwt = window.RehabAuth.getJwt?.();
    if (!jwt) {
      redirectToPatient("not signed in");
      return;
    }

    let role;
    try {
      const res = await authedFetch(`${API_BASE}/me/role`);
      const data = await res.json();
      role = data.role;
    } catch (e) {
      console.error("role check failed", e);
      redirectToPatient("role check failed");
      return;
    }

    // admin is a strict superset of clinician — both belong on the
    // dashboard. Without this check ac233/Nikki/andre102599 (all
    // admins after the staff_users migration) would land here and
    // immediately bounce back to / in a one-second redirect loop.
    if (role !== "clinician" && role !== "admin") {
      redirectToPatient(`role=${role}`);
      return;
    }

    const user = window.RehabAuth.getUser?.();
    if (user?.email && $("clinicianEmail")) $("clinicianEmail").textContent = user.email;

    bindHandlers();

    // Reveal the segmented control for ALL clinicians so they get the
    // Patients/History tab (admins additionally get Pipeline debug, which
    // clinician_admin.js owns). This single handler owns the review<->patients
    // toggle; clinician_admin._setMode reacts to the same click only for debug.
    const modeSwitch = $("adminModeSwitch");
    if (modeSwitch) {
      modeSwitch.hidden = false;
      if (role !== "admin") {
        const dbg = $("modeDebugBtn");
        if (dbg) dbg.hidden = true;
      }
      modeSwitch.addEventListener("click", (e) => {
        const btn = e.target.closest(".admin-mode-btn");
        if (btn) setDashboardMode(btn.dataset.mode);
      });
    }
    $("patientRosterRefresh")?.addEventListener("click", loadPatientRoster);
    $("patientSearch")?.addEventListener("input", filterRoster);

    await loadQueue();
    // First-run tour once the dashboard is up. Delay lets the queue render so
    // the spotlight can anchor to it.
    if (window.Tour) setTimeout(() => window.Tour.autoStart(CLINICIAN_TOUR), 600);
  }

  function redirectToPatient(reason) {
    console.info("clinician dashboard: redirecting to /", reason);
    window.location.replace("/");
  }

  function bindHandlers() {
    $("queueRefresh")?.addEventListener("click", loadQueue);
    $("clinicianSignout")?.addEventListener("click", async () => {
      try { await window.RehabAuth.signOut(); } catch (_) {}
      try {
        localStorage.removeItem("authSkipped");
        localStorage.removeItem("supabaseJwt");
        sessionStorage.removeItem("asPatient");
      } catch (_) {}
      window.location.replace("/");
    });
    // "View as patient" — set the override flag and navigate to /. The
    // patient page checks sessionStorage.asPatient and skips its
    // auto-redirect-to-clinician when it's set.
    $("clinicianAsPatient")?.addEventListener("click", () => {
      sessionStorage.setItem("asPatient", "1");
      window.location.replace("/");
    });
    $("clinicianTourTrigger")?.addEventListener("click", () => {
      window.Tour?.start(CLINICIAN_TOUR);
    });
    $("approveBtn")?.addEventListener("click", () => beginAction("approve"));
    $("rejectBtn")?.addEventListener("click", () => beginAction("reject"));
    $("notesCancel")?.addEventListener("click", cancelAction);
    $("notesConfirm")?.addEventListener("click", confirmAction);

    // Raw-context disclosure logging. Toggling the <details open> emits
    // a server log line so we can audit accidental PHI reveals. Token
    // UUIDs only — never the patient name.
    $("rawContextDetails")?.addEventListener("toggle", () => {
      if (!$("rawContextDetails")?.open) return;
      logRawContextRevealed();
    });

    // Narrator retry: re-fetch the current protocol detail. Used when
    // narrator_status === "sdk_error" so the clinician can retry without
    // losing their place in the queue.
    $("narratorRetry")?.addEventListener("click", () => {
      if (!selectedId) return;
      selectItem(selectedId);
    });
  }

  async function loadQueue() {
    try {
      const res = await authedFetch(`${API_BASE}/protocols/pending`);
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const data = await res.json();
      queue = data.pending || [];
    } catch (e) {
      console.error("queue load failed", e);
      toast(`Couldn't load queue: ${e.message}`, "error");
      return;
    }
    renderQueue();
  }

  function renderQueue() {
    const list = $("queueList");
    const empty = $("queueEmpty");
    const count = $("queueCount");
    if (count) count.textContent = String(queue.length);
    if (!list) return;
    list.innerHTML = "";

    if (queue.length === 0) {
      if (empty) empty.hidden = false;
      // Clear detail pane if the just-acted-on row was the only one.
      hideDetail();
      return;
    }
    if (empty) empty.hidden = true;

    // Sort: needs_clinician_review rows jump to the top so high-severity
    // safety flags surface immediately. Server already returns rows in
    // this order (status sort, then created_at DESC) but we re-sort
    // defensively in case a future patch changes the endpoint shape.
    const sortedQueue = [...queue].sort((a, b) => {
      const aFlagged = a.status === "needs_clinician_review" ? 0 : 1;
      const bFlagged = b.status === "needs_clinician_review" ? 0 : 1;
      if (aFlagged !== bFlagged) return aFlagged - bFlagged;
      return 0;
    });

    for (const item of sortedQueue) {
      const li = document.createElement("li");
      const flagged = item.status === "needs_clinician_review";
      li.className = "queue-item"
        + (item.id === selectedId ? " selected" : "")
        + (flagged ? " queue-item-flagged" : "");
      li.dataset.id = item.id;
      const phase = item.phase || "—";
      const week = item.week != null ? `wk ${item.week}` : "";
      const when = item.created_at ? relativeTime(item.created_at) : "";
      const flagBadge = flagged
        ? `<span class="queue-flag-badge">SAFETY</span>`
        : "";
      li.innerHTML = `
        <div class="queue-item-name">${flagBadge}${escapeHtml(item.patient_name || item.token || "(unknown patient)")}</div>
        <div class="queue-item-meta">${escapeHtml(phase)}${week ? ` · ${escapeHtml(week)}` : ""}${when ? ` · ${escapeHtml(when)}` : ""}</div>
      `;
      li.addEventListener("click", () => selectItem(item.id));
      list.appendChild(li);
    }
  }

  async function selectItem(id) {
    selectedId = id;
    renderQueue();
    cancelAction();

    let detail;
    try {
      const res = await authedFetch(`${API_BASE}/protocols/${encodeURIComponent(id)}`);
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      detail = await res.json();
    } catch (e) {
      console.error("detail load failed", e);
      toast(`Couldn't load detail: ${e.message}`, "error");
      return;
    }
    renderDetail(detail);
  }

  function hideDetail() {
    selectedId = null;
    if ($("detailEmpty")) $("detailEmpty").hidden = false;
    if ($("detailBody")) $("detailBody").hidden = true;
  }

  function renderDetail(detail) {
    if ($("detailEmpty")) $("detailEmpty").hidden = true;
    if ($("detailBody")) $("detailBody").hidden = false;

    // Stash on the closure so the raw-context toggle handler knows which
    // patient token + reviewer to log on disclosure. We hold tokens
    // (UUIDs) only — never the patient_name.
    currentPatientToken = detail.patient?.token || null;

    const target = detail.target || {};
    const active = detail.active;
    const patient = detail.patient || {};

    $("detailPatient").textContent = patient.patient_name || patient.token || "(unknown patient)";
    const phase = (target.payload || {}).phase || "—";
    const week = (target.payload || {}).week;
    const agent = target.created_by_agent || "";
    const created = target.created_at ? new Date(target.created_at).toLocaleString() : "";
    $("detailSub").textContent = `${phase}${week != null ? ` · wk ${week}` : ""} · by ${agent}${created ? ` · ${created}` : ""}`;

    // Patient-at-a-glance card. Server computes the structured summary +
    // pain trend so the frontend doesn't denormalize. Falls back to a
    // hidden card if the backend predates this endpoint shape.
    renderPatientSummary(detail.patient_summary, detail.pain_trend);

    // Data-integrity banner (amber). Catches stale protocols where the
    // exercise body_region doesn't match the patient's injury. Hidden
    // on status === "ok"; populated when status === "region_mismatch".
    renderDataIntegrity(detail.data_integrity, detail.patient_summary);

    // Raw context lives behind a <details> toggle, defaulting to closed.
    // user-select:none on the <pre> to disrupt accidental Cmd-C; this is
    // friction, not security — DevTools defeats it. The goal is "no
    // accidental clipboard PHI on a queue scroll-by".
    if ($("rawContextDetails")) $("rawContextDetails").open = false;
    $("detailContext").textContent = JSON.stringify({
      intake: patient.intake,
      recent_sessions: patient.recent_sessions,
    }, null, 2);

    // Safety review concerns. The backend attaches these (and may set
    // status='needs_clinician_review') when the SafetyReviewAgent flagged
    // the draft. Render at the top of the detail pane in red so the
    // clinician sees them before scanning the diff. Visually distinct
    // from the diff narrator (muted blue accent) — safety is red accent.
    const safetyBlock = $("safetyConcerns");
    const safetyBody = $("safetyConcernsBody");
    const safetyLabel = $("safetyConcernsLabel");
    if (safetyBlock && safetyBody) {
      const concerns = (target.safety_concerns || []).filter(Boolean);
      const flagged = target.status === "needs_clinician_review";
      if (concerns.length > 0 || flagged) {
        const items = concerns.length > 0
          ? concerns.map((c) => {
              const sev = (c.severity || "low").toLowerCase();
              return `<li class="safety-concern safety-${escapeHtml(sev)}">
                <span class="safety-check">${escapeHtml(c.check || "concern")}</span>
                <span class="safety-severity">${escapeHtml(sev.toUpperCase())}</span>
                <div class="safety-detail">${escapeHtml(c.detail || "")}</div>
              </li>`;
            }).join("")
          : `<li class="safety-concern safety-high">
              <span class="safety-detail">Flagged for clinician review.</span>
            </li>`;
        safetyBody.innerHTML = `<ul class="safety-concerns-list">${items}</ul>`;
        if (safetyLabel) {
          safetyLabel.textContent = flagged
            ? "Safety review — clinician sign-off required"
            : "Safety review — concerns flagged";
        }
        safetyBlock.classList.toggle("safety-flagged", flagged);
        safetyBlock.hidden = false;
      } else {
        safetyBlock.hidden = true;
      }
    }

    // AI-generated diff narration. The backend returns:
    //   * narrator_summary: text on success, null otherwise
    //   * narrator_status: one of "no_diff" | "no_api_key" | "sdk_error"
    //                      | "empty_response" | "ok"
    //   * both fields absent entirely on patient self-fetch (clinician-only)
    // We render four distinct micro-states based on status so the
    // clinician knows WHY the summary is missing instead of seeing a
    // single generic "Summary unavailable" string.
    const safetyConcernsExist = ((target.safety_concerns || []).filter(Boolean).length > 0)
      || target.status === "needs_clinician_review";
    renderNarrator(detail.narrator_summary, detail.narrator_status, safetyConcernsExist);

    // Structured, exercise-level semantic diff (primary view). Readable
    // exercise cards instead of a JSON wall; significant changes pinned.
    renderStructuredDiff(active && active.payload, target.payload || {});

    // Raw line-by-line JSON diff retained behind a collapsed <details> as the
    // advanced/fallback view. Re-collapse on every queue selection.
    if ($("rawDiffDetails")) $("rawDiffDetails").open = false;
    $("diffProposed").innerHTML = renderDiffPane(target.payload || {}, active && active.payload, "right");
    $("diffActive").innerHTML = renderDiffPane(active && active.payload, target.payload || {}, "left");

    // Adherence panel: last 7 days of public.sessions for this patient.
    // RLS allows clinicians read-across, but the FastAPI endpoint also
    // gates by is_clinician() server-side.
    loadRecentSessions(patient.token).catch((e) =>
      console.warn("recent sessions load failed", e),
    );
  }

  async function loadRecentSessions(patientToken, hostId = "detailSessions") {
    const host = $(hostId);
    if (!host) return;
    host.innerHTML = `<div class="clinician-sessions-empty">Loading...</div>`;
    if (!patientToken) {
      host.innerHTML = `<div class="clinician-sessions-empty">No patient token.</div>`;
      return;
    }
    let data;
    try {
      const res = await authedFetch(
        `${API_BASE}/sessions/recent?days=7&token=${encodeURIComponent(patientToken)}`,
      );
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      data = await res.json();
    } catch (e) {
      host.innerHTML = `<div class="clinician-sessions-empty">Failed: ${escapeHtml(e.message)}</div>`;
      return;
    }
    const sessions = data.sessions || [];
    if (!sessions.length) {
      host.innerHTML = `<div class="clinician-sessions-empty">No sessions in the last 7 days.</div>`;
      return;
    }
    // Bucket by created_at (UTC date). Lightweight; the dashboard isn't
    // the place to do timezone-precise day boundaries.
    const byDay = new Map();
    for (const s of sessions) {
      const d = (s.created_at || "").slice(0, 10) || "unknown";
      if (!byDay.has(d)) byDay.set(d, []);
      byDay.get(d).push(s);
    }
    const days = Array.from(byDay.entries()).sort((a, b) => a[0].localeCompare(b[0]));
    const rows = days.map(([day, list]) => {
      const planned = list.filter((s) => s.status === "planned").length;
      const inProg = list.filter((s) => s.status === "in_progress").length;
      const completed = list.filter((s) => s.status === "completed").length;
      const skipped = list.filter((s) => s.status === "skipped").length;
      const items = list.map((s) => {
        const meta = [];
        if (s.pose_metrics?.rep_count != null) meta.push(`${s.pose_metrics.rep_count} reps`);
        if (s.pose_metrics?.worst_status) meta.push(s.pose_metrics.worst_status);
        const metaStr = meta.length ? ` (${meta.join(", ")})` : "";
        // PR-T2 + PR-U7: dim + label rows ONLY when body_region is
        // confirmed and differs from the active protocol. Planner-
        // generated exercise IDs that aren't in the library come back
        // with body_region: null; we used to render those as "prior
        // region: unknown" + dimmed, which falsely flagged current-
        // region regressions as adherence-from-elsewhere. Treat null
        // as "no info" and render normally.
        const knownOutOfRegion = s.is_current_region === false && !!s.body_region;
        const outOfRegionClass = knownOutOfRegion ? " out-of-region" : "";
        const regionTag = knownOutOfRegion
          ? `<span class="session-region-tag">prior region: ${escapeHtml(s.body_region)}</span>`
          : "";
        return `<li class="session-item ${s.status}${outOfRegionClass}">
          <span class="session-status">${escapeHtml(s.status)}</span>
          <span class="session-ex">${escapeHtml(s.exercise_id)}</span>
          <span class="session-meta">${escapeHtml(metaStr)}</span>
          ${regionTag}
        </li>`;
      }).join("");
      return `
        <div class="session-day">
          <div class="session-day-header">
            <strong>${escapeHtml(day)}</strong>
            <span class="session-day-counts">
              ${completed} completed, ${planned} planned, ${inProg} in progress, ${skipped} skipped
            </span>
          </div>
          <ul class="session-day-list">${items}</ul>
        </div>`;
    }).join("");
    host.innerHTML = rows;
  }

  // ── Patient-at-a-glance card ──────────────────────────────────────────
  //
  // Renders the structured summary computed by GET /protocols/{id}. We
  // accept a `summary` object and an oldest-first `painTrend` list and
  // populate a fixed-shape card. Missing fields hide their row; we don't
  // ---- Patients / History mode ----------------------------------------
  // A clinician-visible third mode (Review queue | Patients | Pipeline debug).
  // Lets a clinician look back at a patient after the review queue empties:
  // protocol timeline (all statuses) + at-a-glance summary + recent sessions.
  // Backed by GET /clinician/patients and /clinician/patient/{token}/history.

  let _roster = [];

  function setDashboardMode(mode) {
    // reviewMain is the first .clinician-main that is neither admin nor patients.
    const reviewMain = document.querySelector(
      "main.clinician-main:not(.admin-main):not(.patients-main)",
    );
    const adminMain = $("adminMain");
    const patientsMain = $("patientsMain");
    const strip = $("adminModeStrip");
    const title = $("clinicianHeaderTitle");
    document.querySelectorAll(".admin-mode-btn").forEach((b) => {
      const on = b.dataset.mode === mode;
      b.classList.toggle("active", on);
      b.setAttribute("aria-selected", on ? "true" : "false");
    });
    const isReview = mode === "review";
    const isPatients = mode === "patients";
    const isDebug = mode === "debug";
    if (reviewMain) reviewMain.hidden = !isReview;
    if (patientsMain) patientsMain.hidden = !isPatients;
    if (adminMain) adminMain.hidden = !isDebug;
    if (strip) strip.hidden = !isDebug;
    if (title) {
      title.textContent = isDebug
        ? "Pipeline debug"
        : isPatients ? "Patients" : "Pending review";
    }
    if (isPatients) loadPatientRoster();
    // Debug content load stays owned by clinician_admin.js, which reacts to
    // the same click for mode === "debug".
  }

  async function loadPatientRoster() {
    const list = $("patientRosterList");
    const empty = $("patientRosterEmpty");
    if (!list) return;
    list.innerHTML = `<li class="clinician-queue-empty">Loading…</li>`;
    if (empty) empty.hidden = true;
    try {
      const res = await authedFetch(`${API_BASE}/clinician/patients`);
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const data = await res.json();
      _roster = data.patients || [];
    } catch (e) {
      list.innerHTML = `<li class="clinician-queue-empty">Failed: ${escapeHtml(e.message)}</li>`;
      return;
    }
    renderRoster(_roster);
  }

  function filterRoster() {
    const q = ($("patientSearch")?.value || "").toLowerCase().trim();
    if (!q) return renderRoster(_roster);
    renderRoster(_roster.filter((p) =>
      (p.patient_name || p.token || "").toLowerCase().includes(q)
      || (p.body_region || "").toLowerCase().includes(q)
    ));
  }

  function renderRoster(rows) {
    const list = $("patientRosterList");
    const empty = $("patientRosterEmpty");
    if (!list) return;
    if (!rows.length) {
      list.innerHTML = "";
      if (empty) empty.hidden = false;
      return;
    }
    if (empty) empty.hidden = true;
    list.innerHTML = rows.map((p) => {
      const name = escapeHtml(p.patient_name || p.token || "(unknown patient)");
      const region = p.body_region ? escapeHtml(p.body_region) : "";
      const status = escapeHtml(p.latest_status || "");
      const when = p.latest_created_at ? relativeTime(p.latest_created_at) : "";
      return `<li class="queue-item roster-item" data-token="${escapeHtml(p.token)}">
        <div class="queue-item-name">${name}</div>
        <div class="queue-item-meta">${region ? region + " · " : ""}${status}${when ? " · " + when : ""}</div>
      </li>`;
    }).join("");
    list.querySelectorAll(".roster-item").forEach((li) => {
      li.addEventListener("click", () => selectPatient(li.dataset.token));
    });
  }

  async function selectPatient(token) {
    document.querySelectorAll("#patientRosterList .roster-item").forEach((li) => {
      li.classList.toggle("selected", li.dataset.token === token);
    });
    if ($("historyEmpty")) $("historyEmpty").hidden = true;
    if ($("historyBody")) $("historyBody").hidden = false;
    if ($("historyTimeline")) {
      $("historyTimeline").innerHTML = `<li class="clinician-sessions-empty">Loading…</li>`;
    }
    let data;
    try {
      const res = await authedFetch(
        `${API_BASE}/clinician/patient/${encodeURIComponent(token)}/history`,
      );
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      data = await res.json();
    } catch (e) {
      if ($("historyTimeline")) {
        $("historyTimeline").innerHTML = `<li class="clinician-sessions-empty">Failed: ${escapeHtml(e.message)}</li>`;
      }
      return;
    }
    const summary = data.patient_summary || {};
    if ($("historyName")) $("historyName").textContent = data.patient_name || "(unknown patient)";
    if ($("historySub")) {
      const bits = [
        summary.injury_type ? escapeHtml(summary.injury_type) : null,
        summary.body_region ? `region: ${escapeHtml(summary.body_region)}` : null,
      ].filter(Boolean);
      $("historySub").innerHTML = bits.join(" · ");
    }
    renderHistorySummary(summary);
    renderTimeline(data.timeline || []);
    loadRecentSessions(token, "historySessions");
  }

  function renderHistorySummary(s) {
    const host = $("historySummary");
    if (!host) return;
    const parts = [];
    const idline = [
      s.display_name ? escapeHtml(s.display_name) : null,
      s.age != null ? escapeHtml(s.age) : null,
    ].filter(Boolean).join(" · ");
    if (idline) parts.push(`<div class="history-sum-id">${idline}</div>`);
    const pw = [
      s.phase ? escapeHtml(s.phase) : null,
      s.week != null ? `week ${escapeHtml(s.week)}` : null,
    ].filter(Boolean).join(" · ");
    if (pw) parts.push(`<div class="history-sum-line history-sum-muted">${pw}</div>`);
    const sym = (s.symptoms || []).slice(0, 4).map(escapeHtml).join(", ");
    if (sym) parts.push(`<div class="history-sum-line"><span class="history-cap">Symptoms</span> ${sym}</div>`);
    const goals = (s.goals || []).slice(0, 3).map(escapeHtml).join(", ");
    if (goals) parts.push(`<div class="history-sum-line"><span class="history-cap">Goals</span> ${goals}</div>`);
    host.innerHTML = parts.join("");
  }

  const _STATUS_LABEL = {
    active: "ACTIVE",
    superseded: "SUPERSEDED",
    rejected: "REJECTED",
    pending_review: "PENDING",
    needs_clinician_review: "NEEDS REVIEW",
  };

  function renderTimeline(timeline) {
    const host = $("historyTimeline");
    if (!host) return;
    if (!timeline.length) {
      host.innerHTML = `<li class="clinician-sessions-empty">No protocols yet.</li>`;
      return;
    }
    host.innerHTML = timeline.map((t) => {
      const status = t.status || "";
      const label = _STATUS_LABEL[status] || status.toUpperCase();
      const pw = [
        t.phase ? escapeHtml(t.phase) : null,
        t.week != null ? `wk ${escapeHtml(t.week)}` : null,
      ].filter(Boolean).join(" · ");
      const when = t.created_at ? relativeTime(t.created_at) : "";
      const rev = t.reviewer_initials ? ` · ${escapeHtml(t.reviewer_initials)}` : "";
      const notes = t.notes_excerpt
        ? `<div class="timeline-notes">${escapeHtml(t.notes_excerpt)}</div>` : "";
      const reviewBtn = (status === "pending_review" || status === "needs_clinician_review")
        ? `<button type="button" class="timeline-review-btn" data-id="${escapeHtml(t.id)}">Review →</button>`
        : "";
      return `<li class="timeline-node status-${escapeHtml(status)}">
        <div class="timeline-row">
          <span class="timeline-status status-${escapeHtml(status)}">${escapeHtml(label)}</span>
          <span class="timeline-pw">${pw}</span>
          <span class="timeline-when">${when}${rev}</span>
          ${reviewBtn}
        </div>
        ${notes}
      </li>`;
    }).join("");
    host.querySelectorAll(".timeline-review-btn").forEach((b) => {
      b.addEventListener("click", () => {
        setDashboardMode("review");
        selectItem(b.dataset.id);
      });
    });
  }

  // render "—" placeholders that read as broken UI.
  //
  // Colour scale for pain levels follows a 3-band scheme: green ≤3,
  // amber 4-6, red ≥7. Number is ALWAYS displayed alongside the colour
  // chip so the card remains readable for colour-blind reviewers.
  function renderPatientSummary(summary, painTrend) {
    const block = $("patientSummary");
    if (!block) return;
    if (!summary) {
      block.hidden = true;
      return;
    }
    block.hidden = false;

    // Identity row.
    $("psName").textContent = summary.display_name || "(no name on file)";
    if (summary.age != null) {
      $("psAge").textContent = `· ${summary.age}`;
      $("psAge").hidden = false;
    } else {
      $("psAge").hidden = true;
    }

    if (summary.body_region) {
      $("psRegion").textContent = String(summary.body_region).replace("_", " ");
      $("psRegion").hidden = false;
    } else {
      $("psRegion").hidden = true;
    }

    // Injury / phase row.
    $("psInjury").textContent = summary.injury_type || "Injury type not recorded";
    if (summary.post_op_days != null) {
      $("psPostOp").textContent = `· ${summary.post_op_days} days post-op`;
      $("psPostOp").hidden = false;
    } else {
      $("psPostOp").hidden = true;
    }
    if (summary.phase) {
      const wk = summary.week != null ? `, week ${summary.week}` : "";
      $("psPhase").textContent = `Phase: ${summary.phase}${wk}`;
      $("psPhase").hidden = false;
    } else {
      $("psPhase").hidden = true;
    }

    // Pain row. We pull the most-recent pain_level from painTrend.
    // No trend, no row — keeps the card honest about what we know.
    const painRow = $("psPainRow");
    const trend = Array.isArray(painTrend) ? painTrend : [];
    if (trend.length > 0) {
      const latest = trend[trend.length - 1];
      $("psPainNum").textContent = `${latest.level}/10`;
      $("psPainNum").className = "patient-summary-pain-num " + painBandClass(latest.level);
      $("psPainBar").innerHTML = renderPainBar(latest.level);
      $("psTrend").innerHTML = renderPainTrend(trend);
      painRow.hidden = false;
    } else {
      painRow.hidden = true;
    }

    // Symptoms / goals chips. Truncate gracefully — the card is meant
    // for a 3-second scan, not a wall of text.
    const symptoms = Array.isArray(summary.symptoms) ? summary.symptoms : [];
    const goals = Array.isArray(summary.goals) ? summary.goals : [];
    if (symptoms.length > 0) {
      $("psSymptomsVal").textContent = symptoms.slice(0, 4).join(", ")
        + (symptoms.length > 4 ? `, +${symptoms.length - 4} more` : "");
      $("psSymptoms").hidden = false;
    } else {
      $("psSymptoms").hidden = true;
    }
    if (goals.length > 0) {
      $("psGoalsVal").textContent = goals.slice(0, 3).join(", ")
        + (goals.length > 3 ? `, +${goals.length - 3} more` : "");
      $("psGoals").hidden = false;
    } else {
      $("psGoals").hidden = true;
    }
  }

  function painBandClass(level) {
    if (level >= 7) return "pain-high";
    if (level >= 4) return "pain-mid";
    return "pain-low";
  }

  function renderPainBar(level) {
    // 10 dots; first N filled, rest empty. Coloured by band so the bar
    // and the number reinforce each other visually.
    const filled = Math.max(0, Math.min(10, Number(level) || 0));
    const cls = painBandClass(filled);
    let dots = "";
    for (let i = 0; i < 10; i++) {
      dots += `<span class="pain-dot ${i < filled ? cls : "pain-empty"}"></span>`;
    }
    return dots;
  }

  function renderPainTrend(trend) {
    // "Trend: 6 → 4 (last 5 check-ins)" — uses ASCII arrow to stay
    // emoji-free per project rule. Direction inferred from first vs last.
    if (trend.length < 2) {
      return `<span class="pain-trend-cap">Trend:</span> single reading`;
    }
    const first = trend[0].level;
    const last = trend[trend.length - 1].level;
    const arrow = last < first ? "↘" : (last > first ? "↗" : "→");
    const cap = `<span class="pain-trend-cap">Trend:</span>`;
    return `${cap} ${arrow} ${first} → ${last} (last ${trend.length} check-in${trend.length === 1 ? "" : "s"})`;
  }

  // ── Data-integrity banner ─────────────────────────────────────────────
  //
  // Renders the amber "data integrity" banner when the backend reports a
  // region mismatch between intake.injury_type and the exercises in the
  // active or proposed protocol. Status `ok` (or absent) hides it
  // entirely. Status `region_mismatch` shows: patient injury, regions
  // present in active, regions present in proposed, and a collapsible
  // list of the offending exercise ids.
  //
  // Visually distinct from the red safety-concerns block (PR-C) and the
  // green patient-summary card. The colour signals "data hygiene" not
  // "patient safety", so the clinician knows it's a review-the-staleness
  // prompt rather than a safety-block.
  function renderDataIntegrity(integrity, summary) {
    const block = $("dataIntegrity");
    if (!block) return;
    if (!integrity || integrity.status !== "region_mismatch") {
      block.hidden = true;
      block.open = false;
      return;
    }

    const expected = integrity.expected_region
      ? String(integrity.expected_region).replace("_", " ")
      : "unknown";
    const injuryType = (summary && summary.injury_type) || "(injury type not on file)";
    $("diIntake").textContent = `${expected} (${injuryType})`;

    const activeRegions = Array.isArray(integrity.active_regions) ? integrity.active_regions : [];
    const proposedRegions = Array.isArray(integrity.proposed_regions) ? integrity.proposed_regions : [];

    const fmt = (regions) => regions.map((r) => String(r).replace("_", " ")).join(", ");
    if (activeRegions.length > 0) {
      $("diActiveRegions").textContent = `${fmt(activeRegions)} exercises`;
      $("diActiveRow").hidden = false;
    } else {
      $("diActiveRow").hidden = true;
    }
    if (proposedRegions.length > 0) {
      $("diProposedRegions").textContent = `${fmt(proposedRegions)} exercises`;
      $("diProposedRow").hidden = false;
    } else {
      $("diProposedRow").hidden = true;
    }

    const list = $("diMismatchList");
    const mismatches = Array.isArray(integrity.mismatches) ? integrity.mismatches : [];
    if (list) {
      list.innerHTML = mismatches.map((m) => {
        const loc = escapeHtml(m.location || "?");
        const exId = escapeHtml(m.exercise_id || "(unnamed)");
        const region = escapeHtml(String(m.actual_region || "").replace("_", " "));
        return `<li class="data-integrity-item">
          <span class="data-integrity-loc">${loc}</span>
          <span class="data-integrity-ex">${exId}</span>
          <span class="data-integrity-region">${region}</span>
        </li>`;
      }).join("");
    }

    block.hidden = false;
  }

  // ── Narrator status switch ────────────────────────────────────────────
  //
  // Four micro-states map to four user-facing surfaces. Old behaviour
  // collapsed every non-ok case to "Summary unavailable, see diff below"
  // which made it impossible to tell from the screen whether the model
  // errored, the key was unset, or the diff was empty.
  function renderNarrator(summary, status, safetyConcernsExist) {
    const block = $("narratorSummary");
    const body = $("narratorSummaryBody");
    const label = $("narratorSummaryLabel");
    const retry = $("narratorRetry");
    const noDiffBlock = $("narratorNoDiff");
    if (!block || !body || !noDiffBlock) return;

    body.classList.remove("narrator-summary-fallback");
    body.classList.remove("narrator-summary-error");
    body.classList.remove("narrator-summary-info");
    if (retry) retry.hidden = true;
    noDiffBlock.hidden = true;

    // Backwards-compat: when the backend predates narrator_status, fall
    // back to the old "summary present == ok" semantics.
    let effectiveStatus = status;
    if (!effectiveStatus) {
      effectiveStatus = (typeof summary === "string" && summary.trim()) ? "ok" : "empty_response";
    }

    switch (effectiveStatus) {
      case "ok": {
        if (label) label.textContent = "AI-generated summary";
        body.textContent = (typeof summary === "string" && summary.trim())
          ? summary
          : "(empty)";
        block.hidden = false;
        return;
      }
      case "no_diff": {
        // Hide the narrator block entirely. If safety concerns exist,
        // surface a one-liner pointing the clinician at them.
        block.hidden = true;
        if (safetyConcernsExist) {
          noDiffBlock.textContent = "No protocol changes — review the safety flag below.";
          noDiffBlock.hidden = false;
        }
        return;
      }
      case "no_api_key": {
        if (label) label.textContent = "AI-generated summary — offline";
        body.textContent = "AI summary unavailable — Anthropic key not configured.";
        body.classList.add("narrator-summary-info");
        block.hidden = false;
        return;
      }
      case "sdk_error": {
        if (label) label.textContent = "AI-generated summary — error";
        body.textContent = "AI summary couldn't reach the model. Diff is below.";
        body.classList.add("narrator-summary-error");
        if (retry) retry.hidden = false;
        block.hidden = false;
        return;
      }
      case "empty_response":
      default: {
        if (label) label.textContent = "AI-generated summary — unavailable";
        body.textContent = "AI summary couldn't be produced for this diff. See below.";
        body.classList.add("narrator-summary-fallback");
        block.hidden = false;
        return;
      }
    }
  }

  // ── Raw context disclosure audit ──────────────────────────────────────
  //
  // Toggling the <details open> writes a console line + a server-side
  // log line so we have a record of who looked at the raw JSON. Token
  // UUID only — patient name is intentionally not logged. This is
  // friction + logging, not access control. RLS still gates the
  // underlying data.
  function logRawContextRevealed() {
    if (!currentPatientToken) return;
    console.info("clinician revealed raw context", {
      target_token: currentPatientToken,
    });
    try {
      authedFetch(`${API_BASE}/audit/raw-context-revealed`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ target_token: currentPatientToken }),
      }).catch(() => {});
    } catch (_) {
      // Endpoint might not exist on older deploys; the console line
      // stays as the local audit trail until the backend lands.
    }
  }

  // ---- Structured, exercise-level protocol diff ------------------------
  // Primary clinician view. Computed entirely client-side from
  // active.payload + target.payload (no backend change). Renders readable
  // exercise cards instead of a wall of JSON. Deterministic significance
  // flags only — no free-text "load" prose direction parsing (that heuristic
  // is clinically loaded and needs clinician sign-off; deferred). The raw
  // line diff (renderDiffPane below) is retained behind a collapsed
  // "advanced" <details> as the escape hatch.

  // Ordered field spec drives both the per-field diff and the render order.
  // kind: num (signed-delta compare) | prose (whole-value old → new) |
  // list (references: added/removed entries).
  const EX_FIELD_SPEC = [
    { field: "sets", label: "Sets", kind: "num" },
    { field: "reps", label: "Reps", kind: "num" },
    { field: "load", label: "Load", kind: "prose" },
    { field: "ROM_target_deg", label: "ROM target", kind: "num", unit: "°" },
    { field: "intensity", label: "Intensity", kind: "prose" },
    { field: "duration_min", label: "Duration", kind: "num", unit: " min" },
    { field: "progression_criteria", label: "Progression", kind: "prose" },
    { field: "regression_criteria", label: "Regression", kind: "prose" },
    { field: "references", label: "References", kind: "list" },
  ];

  const SESSION_TARGET_SPEC = [
    { field: "frequency_per_week", label: "Frequency", unit: "×/week" },
    { field: "duration_min", label: "Duration", unit: " min" },
    { field: "max_pain_during_session", label: "Max pain", unit: "/10" },
  ];

  // Lowercase, drop parenthetical suffixes + punctuation, collapse space.
  // Makes "Seated Heel Raise (Regressed from X)" and
  // "Seated Heel Raise (Regressed - Reduced Volume)" collide on the stem.
  function normalizeName(name) {
    return String(name ?? "")
      .toLowerCase()
      .replace(/\([^)]*\)/g, "")
      .replace(/[^a-z0-9 ]/g, "")
      .replace(/\s+/g, " ")
      .trim();
  }

  function exerciseKey(ex) {
    if (!ex) return "";
    return String(ex.id || ex.name || "").trim().toLowerCase();
  }

  function hasValue(v) {
    return v !== undefined && v !== null && v !== "";
  }

  function valuesEqual(kind, a, b) {
    if (kind === "num") {
      const na = Number(a);
      const nb = Number(b);
      if (Number.isNaN(na) || Number.isNaN(nb)) {
        return String(a ?? "").trim() === String(b ?? "").trim();
      }
      return na === nb;
    }
    if (kind === "list") {
      const la = (Array.isArray(a) ? a : []).map(String).sort();
      const lb = (Array.isArray(b) ? b : []).map(String).sort();
      return JSON.stringify(la) === JSON.stringify(lb);
    }
    return String(a ?? "").trim() === String(b ?? "").trim();
  }

  // Returns only the changed fields as { field, label, kind, unit, oldVal,
  // newVal, delta }. delta present (signed) only for numeric fields.
  function diffExerciseFields(active, proposed) {
    const a = active || {};
    const p = proposed || {};
    const out = [];
    for (const spec of EX_FIELD_SPEC) {
      const oldVal = a[spec.field];
      const newVal = p[spec.field];
      if (!hasValue(oldVal) && !hasValue(newVal)) continue;
      if (valuesEqual(spec.kind, oldVal, newVal)) continue;
      const change = {
        field: spec.field,
        label: spec.label,
        kind: spec.kind,
        unit: spec.unit || "",
        oldVal: hasValue(oldVal) ? oldVal : null,
        newVal: hasValue(newVal) ? newVal : null,
      };
      if (spec.kind === "num") {
        const na = Number(oldVal);
        const nb = Number(newVal);
        if (!Number.isNaN(na) && !Number.isNaN(nb)) change.delta = nb - na;
      }
      out.push(change);
    }
    return out;
  }

  // Two-pass greedy match (each exercise consumed once):
  //   1. exact key (name/id), 2. normalized-name (strips parenthetical
  //   suffixes so "(Regressed from X)" → "(Reduced Volume)" still pairs).
  // Deliberately NO index fallback: a single leftover on each side is far
  // more likely a genuine remove + add than a rename, and pairing them would
  // bury a removed exercise (and any dropped safety regression) inside a
  // "modified" card. Unmatched items surface as separate added/removed, both
  // flagged significant — louder but safe for a clinical approval gate.
  // Returns rows: { status, active, proposed, changedFields } where status is
  // added | removed | modified | unchanged.
  function matchExercises(activeExercises, proposedExercises) {
    const actives = (Array.isArray(activeExercises) ? activeExercises : []).filter(Boolean);
    const proposed = (Array.isArray(proposedExercises) ? proposedExercises : []).filter(Boolean);
    const activeUsed = new Array(actives.length).fill(false);
    const proposedUsed = new Array(proposed.length).fill(false);
    const pairs = [];

    // Pass 1: exact key.
    const byKey = new Map();
    proposed.forEach((ex, i) => {
      const k = exerciseKey(ex);
      if (k && !byKey.has(k)) byKey.set(k, i);
    });
    actives.forEach((ex, i) => {
      const k = exerciseKey(ex);
      if (!k) return;
      const j = byKey.get(k);
      if (j !== undefined && !proposedUsed[j]) {
        pairs.push({ activeIdx: i, proposedIdx: j });
        activeUsed[i] = proposedUsed[j] = true;
      }
    });

    // Pass 2: normalized name on the leftovers.
    const byNorm = new Map();
    proposed.forEach((ex, i) => {
      if (proposedUsed[i]) return;
      const n = normalizeName(ex.name);
      if (n && !byNorm.has(n)) byNorm.set(n, i);
    });
    actives.forEach((ex, i) => {
      if (activeUsed[i]) return;
      const n = normalizeName(ex.name);
      if (!n) return;
      const j = byNorm.get(n);
      if (j !== undefined && !proposedUsed[j]) {
        pairs.push({ activeIdx: i, proposedIdx: j });
        activeUsed[i] = proposedUsed[j] = true;
      }
    });

    const pairByProposed = new Map(pairs.map((pr) => [pr.proposedIdx, pr]));
    const rows = [];
    // Proposed order for matched/added; removed appended after.
    proposed.forEach((pex, j) => {
      const pair = pairByProposed.get(j);
      if (pair) {
        const aex = actives[pair.activeIdx];
        const changedFields = diffExerciseFields(aex, pex);
        if ((aex.name || "") !== (pex.name || "")) {
          changedFields.unshift({
            field: "name", label: "Renamed", kind: "prose", unit: "",
            oldVal: aex.name || null, newVal: pex.name || null,
          });
        }
        rows.push({
          status: changedFields.length ? "modified" : "unchanged",
          active: aex, proposed: pex, changedFields,
        });
      } else {
        rows.push({ status: "added", active: null, proposed: pex, changedFields: [] });
      }
    });
    actives.forEach((aex, i) => {
      if (!activeUsed[i]) {
        rows.push({ status: "removed", active: aex, proposed: null, changedFields: [] });
      }
    });
    return rows;
  }

  // Deterministic significance — exact comparisons only, no prose parsing.
  // Returns { significant, badges } where badges spell direction in text so
  // colour is never the only signal.
  function flagSignificance(row) {
    const badges = [];
    if (row.status === "added") badges.push("NEW");
    if (row.status === "removed") {
      badges.push("REMOVED");
      if (row.active && String(row.active.regression_criteria || "").trim()) {
        badges.push("SAFETY GUARD REMOVED");
      }
    }
    for (const c of row.changedFields) {
      if (c.field === "reps" && c.delta > 0) badges.push("REPS UP");
      if (c.field === "sets" && c.delta > 0) badges.push("SETS UP");
      if (c.field === "ROM_target_deg" && c.delta < 0) badges.push("ROM DOWN");
      if (c.field === "regression_criteria" && c.oldVal && !c.newVal) {
        badges.push("SAFETY GUARD REMOVED");
      }
    }
    return { significant: badges.length > 0, badges };
  }

  function badgesHtml(badges) {
    return badges.map((b) => {
      const cls = b === "NEW" ? "diff-badge-new" : "diff-badge-danger";
      return `<span class="diff-badge ${cls}">${escapeHtml(b)}</span>`;
    }).join(" ");
  }

  function fieldChangeHtml(c) {
    const unit = c.unit || "";
    if (c.kind === "num") {
      const oldStr = c.oldVal !== null ? `${escapeHtml(c.oldVal)}${escapeHtml(unit)}` : "(none)";
      const newStr = c.newVal !== null ? `${escapeHtml(c.newVal)}${escapeHtml(unit)}` : "(none)";
      let dir = "";
      let dirCls = "";
      if (typeof c.delta === "number" && c.delta !== 0) {
        dir = c.delta > 0 ? "▲" : "▼";
        dirCls = c.delta > 0 ? "up" : "down";
      }
      return `<div class="field-change">
        <span class="field-change-label">${escapeHtml(c.label)}</span>
        <span class="field-change-vals">
          <span class="field-change-old">${oldStr}</span>
          <span class="field-change-arrow" aria-hidden="true">→</span>
          <span class="field-change-new">${newStr}</span>
          ${dir ? `<span class="field-delta ${dirCls}" aria-hidden="true">${dir}</span>` : ""}
        </span>
      </div>`;
    }
    if (c.kind === "list") {
      const oldArr = Array.isArray(c.oldVal) ? c.oldVal : [];
      const newArr = Array.isArray(c.newVal) ? c.newVal : [];
      const removed = oldArr.filter((x) => !newArr.includes(x));
      const added = newArr.filter((x) => !oldArr.includes(x));
      const chips = [
        ...removed.map((x) => `<span class="ref-chip ref-chip-removed">− ${escapeHtml(x)}</span>`),
        ...added.map((x) => `<span class="ref-chip ref-chip-added">+ ${escapeHtml(x)}</span>`),
      ].join(" ");
      return `<div class="field-change">
        <span class="field-change-label">${escapeHtml(c.label)}</span>
        <span class="field-change-vals">${chips || "(changed)"}</span>
      </div>`;
    }
    // prose
    const oldStr = c.oldVal ? escapeHtml(c.oldVal) : "(none)";
    const newStr = c.newVal ? escapeHtml(c.newVal) : "(none)";
    return `<div class="field-change field-change-prose">
      <span class="field-change-label">${escapeHtml(c.label)}</span>
      <div class="field-change-prose-vals">
        <div class="field-change-old">${oldStr}</div>
        <div class="field-change-arrow" aria-hidden="true">↓</div>
        <div class="field-change-new">${newStr}</div>
      </div>
    </div>`;
  }

  function summarizeExercise(ex) {
    if (!ex) return "";
    const lines = [];
    const head = [];
    if (ex.reps != null) head.push(`${escapeHtml(ex.reps)} reps`);
    if (ex.sets != null) head.push(`${escapeHtml(ex.sets)} sets`);
    if (head.length) lines.push(`<div class="ex-sum-head">${head.join(" · ")}</div>`);
    if (ex.load) lines.push(`<div class="ex-sum-line"><span class="ex-sum-cap">Load</span> ${escapeHtml(ex.load)}</div>`);
    if (ex.regression_criteria) lines.push(`<div class="ex-sum-line"><span class="ex-sum-cap">Regression</span> ${escapeHtml(ex.regression_criteria)}</div>`);
    return lines.join("");
  }

  function renderExerciseCard(row) {
    const { significant, badges } = flagSignificance(row);
    const name = (row.proposed && row.proposed.name)
      || (row.active && row.active.name) || "(unnamed exercise)";
    const statusLabel = { added: "ADDED", removed: "REMOVED", modified: "MODIFIED", unchanged: "UNCHANGED" }[row.status] || row.status.toUpperCase();
    const glyph = { added: "+", removed: "−", modified: "±", unchanged: "=" }[row.status] || "";
    let cls = `exercise-diff-card ${row.status}`;
    if (significant) cls += " significant";

    let body = "";
    if (row.status === "modified") {
      body = `<div class="exercise-diff-fields">${row.changedFields.map(fieldChangeHtml).join("")}</div>`;
    } else if (row.status === "added") {
      body = `<div class="exercise-diff-summary">${summarizeExercise(row.proposed)}</div>`;
    } else if (row.status === "removed") {
      const reg = row.active && String(row.active.regression_criteria || "").trim();
      body = `<div class="exercise-diff-summary removed-summary">${summarizeExercise(row.active)}</div>`
        + (reg ? `<div class="removed-guard">⚠ Removed safety regression: ${escapeHtml(reg)}</div>` : "");
    }

    return `<article class="${cls}">
      <div class="exercise-diff-head">
        <span class="exercise-diff-glyph" aria-hidden="true">${glyph}</span>
        <span class="exercise-diff-status status-${row.status}">${statusLabel}</span>
        <span class="exercise-diff-name">${escapeHtml(name)}</span>
        ${badges.length ? `<span class="exercise-diff-badges">${badgesHtml(badges)}</span>` : ""}
      </div>
      ${body}
    </article>`;
  }

  function targetChanged(oldV, newV) {
    const na = Number(oldV);
    const nb = Number(newV);
    if (Number.isNaN(na) || Number.isNaN(nb)) return String(oldV ?? "") !== String(newV ?? "");
    return na !== nb;
  }

  // Returns { html, significant }. html is "" when nothing changed.
  function renderSessionTargetsDiff(activeTargets, proposedTargets) {
    if (activeTargets == null && proposedTargets == null) return { html: "", significant: false };
    const a = activeTargets || {};
    const p = proposedTargets || {};
    const rows = [];
    let anyChange = false;
    let significant = false;
    for (const spec of SESSION_TARGET_SPEC) {
      const oldV = a[spec.field];
      const newV = p[spec.field];
      if (!hasValue(oldV) && !hasValue(newV)) continue;
      const oldStr = hasValue(oldV) ? `${escapeHtml(oldV)}${escapeHtml(spec.unit)}` : "(none)";
      const newStr = hasValue(newV) ? `${escapeHtml(newV)}${escapeHtml(spec.unit)}` : "(none)";
      if (!targetChanged(oldV, newV)) {
        rows.push(`<div class="field-change field-change-static">
          <span class="field-change-label">${escapeHtml(spec.label)}</span>
          <span class="field-change-vals"><span class="field-change-same">${newStr}</span> <span class="field-change-nochange">no change</span></span>
        </div>`);
        continue;
      }
      anyChange = true;
      const painUp = spec.field === "max_pain_during_session"
        && hasValue(oldV) && hasValue(newV) && Number(newV) > Number(oldV);
      if (painUp) significant = true;
      let dir = "";
      let dirCls = "";
      if (hasValue(oldV) && hasValue(newV) && Number(newV) !== Number(oldV)) {
        dir = Number(newV) > Number(oldV) ? "▲" : "▼";
        dirCls = Number(newV) > Number(oldV) ? "up" : "down";
      }
      rows.push(`<div class="field-change">
        <span class="field-change-label">${escapeHtml(spec.label)}</span>
        <span class="field-change-vals">
          <span class="field-change-old">${oldStr}</span>
          <span class="field-change-arrow" aria-hidden="true">→</span>
          <span class="field-change-new">${newStr}</span>
          ${dir ? `<span class="field-delta ${dirCls}" aria-hidden="true">${dir}</span>` : ""}
          ${painUp ? `<span class="diff-badge diff-badge-danger">PAIN CEILING UP</span>` : ""}
        </span>
      </div>`);
    }
    if (!anyChange) return { html: "", significant: false };
    const cls = significant ? "exercise-diff-card session-targets-card significant" : "exercise-diff-card session-targets-card modified";
    return {
      significant,
      html: `<article class="${cls}">
        <div class="exercise-diff-head">
          <span class="exercise-diff-status status-modified">SESSION TARGETS</span>
        </div>
        <div class="exercise-diff-fields">${rows.join("")}</div>
      </article>`,
    };
  }

  // Orchestrator: writes the structured diff into #diffStructured. Replaces
  // the raw JSON wall as the primary view; the raw diff lives behind the
  // collapsed #rawDiffDetails toggle.
  function renderStructuredDiff(activePayload, proposedPayload) {
    const host = $("diffStructured");
    if (!host) return;
    const ap = activePayload || null;
    const pp = proposedPayload || {};
    const rows = matchExercises(ap && ap.exercises, pp.exercises);

    const significant = rows.filter((r) => flagSignificance(r).significant);
    const otherChanged = rows.filter((r) => !flagSignificance(r).significant && r.status !== "unchanged");
    const unchanged = rows.filter((r) => r.status === "unchanged");
    const session = renderSessionTargetsDiff(ap && ap.session_targets, pp.session_targets);

    const parts = [];

    if (!ap) {
      parts.push(`<div class="structured-diff-banner">Initial protocol — no prior version to compare. All exercises shown as new.</div>`);
    }

    const sigCount = significant.length + (session.significant ? 1 : 0);
    const modCount = rows.filter((r) => r.status === "modified").length;
    const addCount = rows.filter((r) => r.status === "added").length;
    const remCount = rows.filter((r) => r.status === "removed").length;
    const counts = [];
    if (sigCount) counts.push(`<span class="count-sig">${sigCount} significant</span>`);
    counts.push(`${modCount} modified`, `${addCount} added`, `${remCount} removed`);
    parts.push(`<div class="structured-diff-summary" aria-live="polite">${counts.join(" · ")}</div>`);
    parts.push(`<div class="structured-diff-caption">Significant changes flagged by exact rule (added/removed, reps/sets up, ROM down, pain ceiling up, safety regression removed).</div>`);

    if (session.html) parts.push(session.html);

    if (significant.length) {
      parts.push(`<div class="structured-diff-group structured-diff-significant">
        <div class="structured-diff-group-label">Significant changes — review before approving</div>
        ${significant.map(renderExerciseCard).join("")}
      </div>`);
    }

    if (otherChanged.length) {
      parts.push(`<details class="structured-diff-group" open>
        <summary class="structured-diff-group-label">Other changes (${otherChanged.length})</summary>
        ${otherChanged.map(renderExerciseCard).join("")}
      </details>`);
    }

    if (unchanged.length) {
      const names = unchanged.map((r) => escapeHtml((r.proposed && r.proposed.name) || (r.active && r.active.name) || "(unnamed)")).join(", ");
      parts.push(`<details class="structured-diff-group structured-diff-unchanged">
        <summary class="structured-diff-group-label">${unchanged.length} exercise${unchanged.length === 1 ? "" : "s"} unchanged</summary>
        <div class="structured-diff-unchanged-list">${names}</div>
      </details>`);
    }

    if (!rows.length && !session.html) {
      parts.push(`<div class="structured-diff-empty">No exercises in this protocol.</div>`);
    } else if (!significant.length && !otherChanged.length && !session.html) {
      parts.push(`<div class="structured-diff-empty">No changes detected.</div>`);
    }

    host.innerHTML = parts.join("");
  }

  // Cheap line-by-line diff: serialize both payloads as pretty JSON, compare
  // line-by-line, mark mismatches. Not a true structural diff (key reorders
  // would be flagged), but JSONB.payload preserves insertion order for the
  // shape we control. Retained as the collapsed "advanced" raw view; the
  // structured diff above is the primary surface.
  function renderDiffPane(self, other, side) {
    const selfText = self ? JSON.stringify(self, null, 2) : "(no active protocol yet)";
    if (!other) {
      return escapeHtml(selfText);
    }
    const selfLines = selfText.split("\n");
    const otherLines = JSON.stringify(other, null, 2).split("\n");
    const out = [];
    const max = Math.max(selfLines.length, otherLines.length);
    for (let i = 0; i < max; i++) {
      const a = selfLines[i] ?? "";
      const b = otherLines[i] ?? "";
      if (a === b) {
        out.push(escapeHtml(a));
      } else if (!a) {
        out.push(`<span class="diff-removed">${escapeHtml(b)}</span>`);
      } else if (!b) {
        out.push(`<span class="diff-added">${escapeHtml(a)}</span>`);
      } else {
        const cls = side === "right" ? "diff-changed-new" : "diff-changed-old";
        out.push(`<span class="${cls}">${escapeHtml(a)}</span>`);
      }
    }
    return out.join("\n");
  }

  function beginAction(action) {
    if (!selectedId) return;
    pendingAction = action;
    const block = $("notesBlock");
    const required = $("notesRequired");
    const textarea = $("reviewNotes");
    if (block) block.hidden = false;
    if (required) required.hidden = action !== "reject";
    if (textarea) {
      textarea.value = "";
      textarea.placeholder = action === "reject"
        ? "Why is this rejected? (required)"
        : "Optional approval notes";
      textarea.focus();
    }
  }

  function cancelAction() {
    pendingAction = null;
    if ($("notesBlock")) $("notesBlock").hidden = true;
    if ($("reviewNotes")) $("reviewNotes").value = "";
  }

  async function confirmAction() {
    if (!selectedId || !pendingAction) return;
    const notes = $("reviewNotes")?.value?.trim() || "";
    if (pendingAction === "reject" && !notes) {
      toast("Notes required to reject", "error");
      return;
    }

    const path = `${API_BASE}/protocols/${encodeURIComponent(selectedId)}/${pendingAction}`;
    const body = pendingAction === "reject"
      ? { notes }
      : (notes ? { notes } : {});

    setActionsBusy(true);
    try {
      const res = await authedFetch(path, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      if (!res.ok) {
        const err = await res.json().catch(() => ({}));
        throw new Error(err.detail || `HTTP ${res.status}`);
      }
      toast(pendingAction === "approve" ? "Approved" : "Rejected", "ok");
      cancelAction();
      // Drop from queue, clear detail, refresh.
      queue = queue.filter((i) => i.id !== selectedId);
      selectedId = null;
      renderQueue();
      hideDetail();
      // Light refetch in case the queue changed concurrently.
      loadQueue();
    } catch (e) {
      console.error("action failed", e);
      toast(`${pendingAction} failed: ${e.message}`, "error");
    } finally {
      setActionsBusy(false);
    }
  }

  function setActionsBusy(busy) {
    for (const id of ["approveBtn", "rejectBtn", "notesConfirm"]) {
      const el = $(id);
      if (el) el.disabled = busy;
    }
  }

  function toast(msg, kind) {
    const el = $("clinicianToast");
    if (!el) return;
    el.textContent = msg;
    el.className = "clinician-toast " + (kind === "error" ? "error" : "ok");
    el.hidden = false;
    setTimeout(() => { el.hidden = true; }, 3000);
  }

  function relativeTime(iso) {
    const ts = new Date(iso).getTime();
    if (Number.isNaN(ts)) return "";
    const diff = Math.max(0, Date.now() - ts);
    const min = Math.floor(diff / 60000);
    if (min < 1) return "just now";
    if (min < 60) return `${min}m ago`;
    const hr = Math.floor(min / 60);
    if (hr < 24) return `${hr}h ago`;
    const d = Math.floor(hr / 24);
    return `${d}d ago`;
  }

  function escapeHtml(s) {
    return String(s ?? "")
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;")
      .replace(/'/g, "&#39;");
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", bootstrap);
  } else {
    bootstrap();
  }
})();
