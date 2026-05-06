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

    if (role !== "clinician") {
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
      window.location.replace("/");
    });
    $("approveBtn")?.addEventListener("click", () => beginAction("approve"));
    $("rejectBtn")?.addEventListener("click", () => beginAction("reject"));
    $("notesCancel")?.addEventListener("click", cancelAction);
    $("notesConfirm")?.addEventListener("click", confirmAction);
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

    for (const item of queue) {
      const li = document.createElement("li");
      li.className = "queue-item" + (item.id === selectedId ? " selected" : "");
      li.dataset.id = item.id;
      const phase = item.phase || "—";
      const week = item.week != null ? `wk ${item.week}` : "";
      const when = item.created_at ? relativeTime(item.created_at) : "";
      li.innerHTML = `
        <div class="queue-item-name">${escapeHtml(item.patient_name || item.token || "(unknown patient)")}</div>
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

    const target = detail.target || {};
    const active = detail.active;
    const patient = detail.patient || {};

    $("detailPatient").textContent = patient.patient_name || patient.token || "(unknown patient)";
    const phase = (target.payload || {}).phase || "—";
    const week = (target.payload || {}).week;
    const agent = target.created_by_agent || "";
    const created = target.created_at ? new Date(target.created_at).toLocaleString() : "";
    $("detailSub").textContent = `${phase}${week != null ? ` · wk ${week}` : ""} · by ${agent}${created ? ` · ${created}` : ""}`;

    $("detailContext").textContent = JSON.stringify({
      intake: patient.intake,
      recent_sessions: patient.recent_sessions,
    }, null, 2);

    $("diffProposed").innerHTML = renderDiffPane(target.payload || {}, active && active.payload, "right");
    $("diffActive").innerHTML = renderDiffPane(active && active.payload, target.payload || {}, "left");
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
