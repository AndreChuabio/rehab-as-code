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
    await loadQueue();
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

    $("diffProposed").innerHTML = renderDiffPane(target.payload || {}, active && active.payload, "right");
    $("diffActive").innerHTML = renderDiffPane(active && active.payload, target.payload || {}, "left");

    // Adherence panel: last 7 days of public.sessions for this patient.
    // RLS allows clinicians read-across, but the FastAPI endpoint also
    // gates by is_clinician() server-side.
    loadRecentSessions(patient.token).catch((e) =>
      console.warn("recent sessions load failed", e),
    );
  }

  async function loadRecentSessions(patientToken) {
    const host = $("detailSessions");
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

  // Cheap line-by-line diff: serialize both payloads as pretty JSON, compare
  // line-by-line, mark mismatches. Not a true structural diff (key reorders
  // would be flagged), but JSONB.payload preserves insertion order for the
  // shape we control, so this reads well enough for the demo. Upgrade to a
  // proper diff lib in a follow-up if it gets noisy.
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
