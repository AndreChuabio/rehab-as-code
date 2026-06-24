const API_BASE = "";

let intakeComplete = localStorage.getItem("rehab_intake_complete") === "1";
let approvedPlanExercises = []; // exercises the user added in step 2

// Guided onboarding tour (tour.js). Steps whose target is missing/hidden are
// skipped automatically, so conditional cards (Today's Session, the review
// pill) drop out cleanly when not yet present. Bump the `key` version to
// re-show the tour after a material change.
const PATIENT_TOUR = {
  key: "patient_v1",
  steps: [
    { selector: "#healthStats", placement: "right", title: "Your wearable signals",
      body: "Sleep, HRV, and recovery from your wearable. Coach Maya and your PT use these trends to decide when to progress or hold your plan." },
    { selector: ".protocol-card", placement: "right", title: "Your current protocol",
      body: "The exercises your clinician approved for this week. Every change here is reviewed by a real PT before it reaches you." },
    { selector: ".quick-actions", placement: "top", title: "Quick actions",
      body: "Start your intake, draft next week's plan, browse the exercise library, or log a daily check-in — all from here." },
    { selector: "#chatLog", placement: "left", title: "Coach Maya",
      body: "Chat with Coach Maya any time — ask about an exercise, report pain, or request a swap. She is grounded in your approved plan." },
    { selector: "#tabVideo", placement: "bottom", title: "Video sessions",
      body: "Jump into a guided video session for live form coaching when you are ready to move." },
    { selector: "#reviewPill", placement: "bottom", title: "Review status",
      body: "Shows where your plan sits in clinician review — pending, approved, or needs attention. You are always in the loop." },
  ],
};

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

  // PR-V2: authed users get the production conversational intake
  // (IntakeAgent → /patient/interact → #intakeModal). The legacy
  // triggerIntake() chat flow with hardcoded INTAKE_QUESTIONS + "Andre"
  // defaults + "fast for demo" copy is for demo mode (no JWT) only.
  if (window.RehabAuth?.getJwt?.()) {
    const state = await refreshPatientState({ openModalIfNeeded: true })
      .catch((e) => { console.warn("state refresh failed", e); return null; });
    // refreshPatientState no longer auto-opens modals (that fired on every
    // page load / account switch / "View as patient"). The intake modal
    // opens ONLY here, on an explicit Start-intake click, and only for a new
    // patient — so a needs_intake patient isn't silently dead-ended.
    if (state && state.state === "needs_intake") {
      hideCoachWorkingIndicator();
      showIntakeModal();
    } else if (state) {
      // Returning patient (needs_plan / ready): clicking Start intake means
      // "I want to update something" — point them at Maya rather than
      // re-running the questionnaire.
      switchStage("chat");
      appendChatBubble(
        "coach",
        "Your intake is on file. Tell me what's changed — a new injury, a worse pain spot, anything — and I'll update your record."
      );
    }
    return;
  }
  // Demo mode (no JWT): keep the legacy chat flow so the public landing
  // page is still walkable without sign-in.
  triggerIntake();
}

function navigateTo(step) {
  if (window.location.hash !== `#${step}`) {
    history.pushState(null, "", `#${step}`);
  }
  // Quick-action buttons chain through chat tools whose result lands as a
  // bubble several lines below the input. Without a visible cue the click
  // looks "broken." Surface a transient indicator + scroll the chat to
  // bottom; showCoachWorkingIndicator drops itself when the next chat
  // event lands. Safe to call even on steps that don't fire chat tools
  // immediately - the indicator is removed in the same tick if no chat
  // activity follows within the watchdog window.
  showCoachWorkingIndicator();
  const fn = STEP_ROUTES[step];
  if (fn) fn();
}

function routeFromHash() {
  const hash = window.location.hash.replace("#", "");
  if (!hash) return;
  // Visual only — keep the step indicator in sync with the URL hash, but
  // do NOT auto-open intake / plan-gen / etc. modals just because the
  // URL had a hash on load or the user pressed Back. Modals open
  // explicitly when the user clicks a step button (which routes through
  // navigateTo() → STEP_ROUTES[step]()).
  setActiveStepBtn(hash);
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
  routeFromHash(); // honour URL on load — visual only; no auto-modal-opens.
  bootstrapAuth();
  wireIntakeModal();
  wirePlanGenModal();
  // "Take the tour" — always available, re-launches regardless of the flag.
  document.getElementById("tourTrigger")?.addEventListener("click", () => {
    window.Tour?.start(PATIENT_TOUR);
  });
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

// Role-based redirect: if the signed-in user is a clinician, send them to
// /clinician. The patient/clinician views are completely separate pages so
// nothing leaks across the role boundary in the DOM. This runs once per
// auth event; non-clinicians silently stay on the patient page.
//
// Override: sessionStorage.asPatient='1' lets a clinician stay on the
// patient view for testing — set by the "View as patient" button on the
// clinician dashboard. Cleared on sign-out and on the "Back to dashboard"
// chip click.
async function maybeRedirectToClinician() {
  if (sessionStorage.getItem("asPatient") === "1") return;
  try {
    const res = await authedFetch(`${API_BASE}/me/role`);
    if (!res.ok) return;
    const data = await res.json();
    // Admin is a strict superset of clinician — both belong on /clinician
    // (admins additionally see the Pipeline debug segmented control there).
    if (data.role === "clinician" || data.role === "admin") {
      window.location.replace("/clinician");
    }
  } catch (e) {
    console.warn("role check failed", e);
  }
}

// Render a "Back to dashboard" chip in the auth pill area when a
// clinician has chosen the "View as patient" override. Lets them flip
// back without retyping URLs.
async function maybeRenderBackToDashboard() {
  if (sessionStorage.getItem("asPatient") !== "1") return;
  try {
    const res = await authedFetch(`${API_BASE}/me/role`);
    if (!res.ok) return;
    const data = await res.json();
    if (data.role !== "clinician" && data.role !== "admin") {
      sessionStorage.removeItem("asPatient");
      return;
    }
  } catch (_) {
    return;
  }
  if (document.getElementById("backToDashChip")) return;
  const chip = document.createElement("button");
  chip.id = "backToDashChip";
  chip.type = "button";
  chip.className = "back-to-dash-chip";
  chip.textContent = "← Back to clinician dashboard";
  chip.addEventListener("click", () => {
    sessionStorage.removeItem("asPatient");
    window.location.replace("/clinician");
  });
  document.body.appendChild(chip);
}

async function bootstrapAuth() {
  const overlay = document.getElementById("authOverlay");
  const pill    = document.getElementById("authPill");
  const pillEm  = document.getElementById("authPillEmail");
  const pillDot = document.getElementById("authPillDot");
  const action  = document.getElementById("authPillAction");
  const form     = document.getElementById("authForm");
  const email    = document.getElementById("authEmail");
  const password = document.getElementById("authPassword");
  const submit   = document.getElementById("authSubmit");
  const magicBtn = document.getElementById("authMagicLink");
  const status   = document.getElementById("authStatus");
  const skipBtn  = document.getElementById("authSkip");

  function showOverlay(show) { if (overlay) overlay.hidden = !show; }

  const setPwBtn = document.getElementById("authPillSetPw");

  // Three pill states:
  //   "signed-in" → email + Set password + Sign out
  //   "demo"      → "Demo mode" + Sign in
  //   "hidden"    → covered by overlay, pill stays hidden
  let pillMode = "hidden";
  function setPill(mode, user) {
    pillMode = mode;
    const dashLink = document.getElementById("clinicianDashLink");
    if (!pill) return;
    if (mode === "signed-in") {
      pillEm.textContent = user?.email || "signed in";
      action.textContent = "Sign out";
      action.className = "auth-pill-action auth-pill-action-signout";
      if (pillDot) pillDot.className = "auth-pill-dot auth-pill-dot-ok";
      if (setPwBtn) setPwBtn.hidden = false;
      pill.hidden = false;
      // Header nav: show the clinician dashboard link to any signed-in
      // user. The server gates real access (require_clinician_id 403s
      // patients). During productionization Andre and Nikki need a
      // guaranteed nav path that doesn't depend on staff_users being
      // seeded on the current Supabase branch.
      if (dashLink) dashLink.hidden = false;
    } else if (mode === "demo") {
      pillEm.textContent = "Demo mode";
      action.textContent = "Sign in";
      action.className = "auth-pill-action auth-pill-action-signin";
      if (pillDot) pillDot.className = "auth-pill-dot auth-pill-dot-demo";
      if (setPwBtn) setPwBtn.hidden = true;
      pill.hidden = false;
      if (dashLink) dashLink.hidden = true;
    } else {
      if (setPwBtn) setPwBtn.hidden = true;
      pill.hidden = true;
      if (dashLink) dashLink.hidden = true;
    }
  }
  function showPill(user) { setPill(user ? "signed-in" : (localStorage.getItem(AUTH_SKIP_KEY) === "1" ? "demo" : "hidden"), user); }

  // Render initial state synchronously so the overlay doesn't flash on every
  // returning visit. RehabAuth.init() will refine this when the SDK loads.
  const skipped = localStorage.getItem(AUTH_SKIP_KEY) === "1";
  const cachedJwt = localStorage.getItem("supabaseJwt");
  if (!cachedJwt && !skipped) showOverlay(true);
  // If the user previously chose Demo mode, show the demo pill immediately
  // so they always have a path back to sign-in. Refined again below once the
  // Supabase SDK resolves the actual session.
  if (skipped && !cachedJwt) setPill("demo");

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
      // Role-based redirect: clinicians get the dashboard, patients stay
      // here. Best-effort — failure to look up role just leaves the
      // patient on / which is safe (clinician endpoints are 403-gated).
      maybeRedirectToClinician();
      // If they're a clinician overriding into patient view, surface a
      // "Back to dashboard" chip so they can flip back.
      maybeRenderBackToDashboard();
      // Server-driven state machine: ask the backend whether this patient
      // needs intake / plan-gen / nothing, and route to the right modal.
      // PR-R: chain renderStateAwareGreeting so the chat greets the
      // patient by name + state once patientState resolves. Idempotent —
      // guarded by _stateAwareGreetingShown so a second onChange (token
      // refresh) doesn't double-render.
      refreshPatientState()
        .then(() => renderStateAwareGreeting())
        .catch((e) => console.warn("state refresh failed", e));
      // Pull today's session log so the sidebar reflects what's already
      // logged on the server (persists across refresh, unlike the prior
      // in-memory array). Best-effort; swallowed errors above on /sessions/today.
      refreshTodaySession().catch(() => {});
    } else {
      showPill(null);
      closeIntakeModal();
      closePlanGenModal();
      patientState = null;
      todaySession = [];
      renderTodaySession();
      // Clear the trust pill on sign-out so it doesn't leak across sessions.
      renderReviewPill(null);
      // PR-R: reset greeting guard so a fresh sign-in re-greets.
      _stateAwareGreetingShown = false;
      // Re-show the overlay if it isn't a deliberate skip and user has no
      // session — but never on the magic-link redirect, which fires onChange
      // with a fresh session right after.
      if (localStorage.getItem(AUTH_SKIP_KEY) !== "1") showOverlay(true);
    }
  });

  // The form's primary submit signs in with email+password if a password is
  // filled; otherwise it falls back to sending a magic link. The "Send magic
  // link instead" button always goes through magic-link regardless of what's
  // in the password field — useful when the user genuinely doesn't have a
  // password set yet.
  async function sendMagicLinkFlow(v) {
    submit.disabled = true;
    if (magicBtn) magicBtn.disabled = true;
    const prev = submit.textContent;
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
      submit.textContent = prev;
      if (magicBtn) magicBtn.disabled = false;
    }
  }

  if (form) {
    form.addEventListener("submit", async (e) => {
      e.preventDefault();
      const v = (email.value || "").trim();
      if (!v) return;
      const pw = (password?.value || "").trim();

      if (!pw) {
        await sendMagicLinkFlow(v);
        return;
      }

      submit.disabled = true;
      if (magicBtn) magicBtn.disabled = true;
      submit.textContent = "Signing in…";
      status.hidden = true;
      try {
        await window.RehabAuth.signInWithPassword(v, pw);
        // Successful sign-in fires onChange which closes the overlay and
        // shows the signed-in pill — no further UI work needed here.
      } catch (err) {
        // "Invalid login credentials" is Supabase's catch-all for
        // (a) account doesn't exist and (b) wrong password. Try signing
        // up with the same email+password — if (a), it succeeds and the
        // user is signed in. If (b) (account exists with different password
        // or no password set), Supabase rejects with "User already
        // registered", which we surface as a clearer error.
        const msg = String(err.message || err).toLowerCase();
        const isCredsErr = msg.includes("invalid login credentials")
          || msg.includes("invalid_credentials");
        if (isCredsErr) {
          submit.textContent = "Creating account…";
          try {
            const result = await window.RehabAuth.signUp(v, pw);
            // signUp may or may not return a session depending on whether
            // email confirmation is required in Supabase project settings.
            if (!result?.session) {
              status.hidden = false;
              status.textContent = `Account created — check ${v} for a confirmation link.`;
              status.className = "auth-status auth-status-ok";
            }
            // If a session is returned, onChange handles the UI transition.
          } catch (signupErr) {
            const sm = String(signupErr.message || signupErr).toLowerCase();
            status.hidden = false;
            if (sm.includes("already registered") || sm.includes("user already")) {
              status.textContent = "Account exists with a different password. Use 'Send magic link' to sign in, then set a new password from the menu.";
            } else {
              status.textContent = `Sign-up failed: ${signupErr.message || signupErr}`;
            }
            status.className = "auth-status auth-status-err";
          }
        } else {
          status.hidden = false;
          status.textContent = `Sign-in failed: ${err.message || err}`;
          status.className = "auth-status auth-status-err";
        }
      } finally {
        submit.disabled = false;
        submit.textContent = "Sign in / Sign up";
        if (magicBtn) magicBtn.disabled = false;
      }
    });
  }
  if (magicBtn) {
    magicBtn.addEventListener("click", async () => {
      const v = (email.value || "").trim();
      if (!v) {
        status.hidden = false;
        status.textContent = "Enter your email first.";
        status.className = "auth-status auth-status-err";
        return;
      }
      await sendMagicLinkFlow(v);
    });
  }
  if (skipBtn) {
    skipBtn.addEventListener("click", () => {
      localStorage.setItem(AUTH_SKIP_KEY, "1");
      showOverlay(false);
      setPill("demo");
      showToast("Demo mode — chat and form-check log won't save", "info");
    });
  }
  if (action) {
    action.addEventListener("click", async () => {
      if (pillMode === "signed-in") {
        // Hard sign-out: clear all storage so we don't keep stale session
        // data, then go to '/' (NOT reload) — reload preserves the URL hash
        // which was opening the intake modal on the post-logout page.
        try { await window.RehabAuth.signOut(); } catch (_) {}
        try {
          localStorage.removeItem(AUTH_SKIP_KEY);
          localStorage.removeItem("supabaseJwt");
          sessionStorage.removeItem("asPatient");
        } catch (_) {}
        window.location.replace("/");
      } else if (pillMode === "demo") {
        // Escape demo mode — drop the skip flag and re-open the auth overlay
        // so the user can enter their email.
        localStorage.removeItem(AUTH_SKIP_KEY);
        setPill("hidden");
        showOverlay(true);
      }
    });
  }

  // Set-password modal: lets a signed-in user create or change a password
  // via Supabase's updateUser. Solves the magic-link-only-account case —
  // sign in once via magic link, set a password here, password sign-in
  // works forever after.
  const setPwModal  = document.getElementById("setPwModal");
  const setPwInput  = document.getElementById("setPwInput");
  const setPwSave   = document.getElementById("setPwSave");
  const setPwCancel = document.getElementById("setPwCancel");
  const setPwStatus = document.getElementById("setPwStatus");
  function openSetPw() {
    if (!setPwModal) return;
    if (setPwStatus) { setPwStatus.hidden = true; setPwStatus.textContent = ""; }
    if (setPwInput) { setPwInput.value = ""; }
    setPwModal.hidden = false;
    setTimeout(() => setPwInput?.focus(), 50);
  }
  function closeSetPw() {
    if (setPwModal) setPwModal.hidden = true;
  }
  if (setPwBtn) setPwBtn.addEventListener("click", openSetPw);
  if (setPwCancel) setPwCancel.addEventListener("click", closeSetPw);
  if (setPwSave) {
    setPwSave.addEventListener("click", async () => {
      const newPw = (setPwInput?.value || "").trim();
      if (newPw.length < 6) {
        if (setPwStatus) {
          setPwStatus.hidden = false;
          setPwStatus.textContent = "Password must be at least 6 characters.";
          setPwStatus.className = "setpw-status setpw-status-err";
        }
        return;
      }
      setPwSave.disabled = true;
      setPwSave.textContent = "Saving…";
      try {
        await window.RehabAuth.updatePassword(newPw);
        if (setPwStatus) {
          setPwStatus.hidden = false;
          setPwStatus.textContent = "Password saved. Use it next sign-in.";
          setPwStatus.className = "setpw-status setpw-status-ok";
        }
        setTimeout(closeSetPw, 1200);
      } catch (err) {
        if (setPwStatus) {
          setPwStatus.hidden = false;
          setPwStatus.textContent = `Couldn't save: ${err.message || err}`;
          setPwStatus.className = "setpw-status setpw-status-err";
        }
      } finally {
        setPwSave.disabled = false;
        setPwSave.textContent = "Save";
      }
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

  // PR-H trust pill: render whenever review_status comes back from the
  // server. Idempotent — a "none" state hides the pill, anything else
  // surfaces it next to the auth pill. See renderReviewPill below for
  // state -> copy mapping.
  renderReviewPill(patientState?.review_status || null);

  // PR-payer: read-only payer mode + payer-aware goals card. The patient
  // sees which payer mode their plan is written for; only the clinician can
  // change it. Idempotent — hides the card when no payer_model is present.
  renderPatientPayerGoals(patientState?.payer_model, patientState?.goals);

  // PR-M flow stitching: surface the "Start today's session" CTA when a
  // protocol was recently approved and the patient hasn't started today
  // yet. Decoupled from openModalIfNeeded — the CTA is an inline render,
  // not a blocking modal, so it should refresh on every state poll.
  // Errors are logged + swallowed; the CTA is a soft affordance and
  // should never break the rest of refreshPatientState.
  refreshTodaysFlowCTA().catch((e) =>
    console.warn("refreshTodaysFlowCTA failed:", e),
  );

  if (!openModalIfNeeded) return patientState;

  // We deliberately do NOT auto-open the intake or plan-gen modals based
  // on patientState anymore. Auto-popups were too aggressive — they fired
  // on every page load, on every account switch, on the clinician
  // "View as patient" preview, and (worst) for users who already had an
  // active protocol but no intake record (the backfilled-protocol case).
  // Patients explicitly start intake by clicking "1 intake" in the step
  // strip below the chat. The state machine still runs so step locks +
  // sidebar protocol fetch behave correctly; only the modal-open is
  // suppressed.
  if (patientState.state === "ready" || patientState.has_protocol) {
    closeIntakeModal();
    closePlanGenModal();
    intakeComplete = true;
    localStorage.setItem("rehab_intake_complete", "1");
    if (patientState.has_protocol) {
      localStorage.setItem("rehab_plan_approved", "1");
    }
    applyStepLocks();
    loadProtocol();
    // First-run guided tour — only once the dashboard is populated (ready),
    // so it never fights the intake/plan-gen modals or points at empty cards.
    // Delay lets the sidebar + review pill finish rendering before anchoring.
    setTimeout(() => window.Tour?.autoStart(PATIENT_TOUR), 800);
  }
  return patientState;
}

// ── Trust-loop review pill (PR-H, Phase S4) ───────────────────────────────
//
// Renders a small status pill in the header right column reflecting the
// most-recent /protocols row for this patient:
//
//   none                    -> pill hidden (clean header)
//   pending_review          -> "Awaiting your PT review · Xh ago"
//   needs_clinician_review  -> "Flagged for your PT · Xh ago"
//   recently_approved       -> "Approved by Dr. NH · Xh ago"
//   recently_rejected       -> "PT review · see chat for notes"
//
// Click the pill to expand a small panel underneath; the rejected state
// surfaces the clinician's review_notes excerpt so the patient can read
// why without bouncing out of the chat.

function renderReviewPill(reviewStatus) {
  const pill   = document.getElementById("reviewPill");
  const btn    = document.getElementById("reviewPillBtn");
  const dot    = document.getElementById("reviewPillDot");
  const label  = document.getElementById("reviewPillLabel");
  const panel  = document.getElementById("reviewPillPanel");
  const body   = document.getElementById("reviewPillPanelBody");
  if (!pill || !btn || !dot || !label || !panel || !body) return;

  const state = reviewStatus?.state || "none";
  if (!reviewStatus || state === "none") {
    pill.hidden = true;
    panel.hidden = true;
    btn.setAttribute("aria-expanded", "false");
    return;
  }

  // Mode -> dot/label CSS class. The CSS picks the colour.
  const modeClass = {
    pending_review:         "review-pill-pending",
    needs_clinician_review: "review-pill-flagged",
    recently_approved:      "review-pill-approved",
    recently_rejected:      "review-pill-rejected",
  }[state] || "review-pill-pending";
  pill.className = `review-pill ${modeClass}`;
  dot.className = `review-pill-dot ${modeClass}-dot`;

  // Label copy + dropdown body copy
  let labelText, panelBody;
  const ago = formatRelativeAgo(reviewStatus.submitted_at || reviewStatus.reviewed_at);
  const initials = reviewStatus.reviewer_initials || "PT";

  if (state === "pending_review") {
    labelText = ago ? `Awaiting your PT review · ${ago}` : "Awaiting your PT review";
    panelBody = "Your PT will see this in their queue. They'll approve, reject, or send notes back.";
  } else if (state === "needs_clinician_review") {
    labelText = ago ? `Flagged for your PT · ${ago}` : "Flagged for your PT";
    panelBody = "We surfaced this as high priority for your clinician. They'll respond shortly.";
  } else if (state === "recently_approved") {
    labelText = ago ? `Approved by Dr. ${initials} · ${ago}` : `Approved by Dr. ${initials}`;
    panelBody = "Your protocol is live. Continue with the plan in the sidebar.";
  } else if (state === "recently_rejected") {
    labelText = "PT review · see chat for notes";
    panelBody = reviewStatus.notes_excerpt
      ? `Note from Dr. ${initials}: ${reviewStatus.notes_excerpt}`
      : "Your PT sent a note back. Open the chat to see the next step.";
  } else {
    labelText = "Review status";
    panelBody = "";
  }
  label.textContent = labelText;
  body.textContent = panelBody;

  pill.hidden = false;
  // Wire toggle once. Idempotent — onclick reassign is fine.
  btn.onclick = () => {
    const expanded = btn.getAttribute("aria-expanded") === "true";
    btn.setAttribute("aria-expanded", expanded ? "false" : "true");
    panel.hidden = expanded;
  };
}

// ── Patient read-only payer mode + payer-aware goals ───────────────────────
//
// Mirrors the clinician goal renderer but read-only: the patient sees which
// payer mode their plan is written for and the generated goals, but cannot
// change the mode (that toggle lives on /clinician). Hidden entirely when no
// payer_model is on file so older accounts get a clean dashboard.

const PATIENT_PAYER_LABEL = {
  insurance: "Insurance",
  medicare: "Medicare",
  cash: "Cash / self-pay",
};

const PATIENT_TIED_TO_LABEL = {
  adl: "Daily living",
  fall_risk: "Fall risk",
  performance: "Performance",
  load_mgmt: "Load mgmt",
};

function patientPayerLabel(mode) {
  return PATIENT_PAYER_LABEL[mode] || (mode ? String(mode) : "Not set");
}

// Build one read-only goal <li>. Patient-facing: plain-language tie-in chip,
// the measurable target if present. We intentionally do NOT surface the
// clinician-only data-integrity flags (needs_clinician_review etc.) — those
// are review signals for the PT, not something to alarm the patient with.
function renderPatientPayerGoalItem(goal) {
  if (!goal || typeof goal !== "object") return "";
  const text = goal.text || "(goal not set)";
  const target = goal.measurable_target
    ? `<span class="payer-goal-target">${escapeHtml(goal.measurable_target)}</span>`
    : "";
  const tiedKey = goal.tied_to;
  const tiedChip = tiedKey
    ? `<span class="goal-tied-chip goal-tied-${escapeHtml(String(tiedKey))}">${escapeHtml(PATIENT_TIED_TO_LABEL[tiedKey] || String(tiedKey))}</span>`
    : "";
  return `<li class="payer-goal">
    <div class="payer-goal-head">
      ${tiedChip}
      <span class="payer-goal-text">${escapeHtml(text)}</span>
    </div>
    ${target ? `<div class="payer-goal-meta">${target}</div>` : ""}
  </li>`;
}

function renderPatientPayerGoals(payerModel, goals) {
  const card = document.getElementById("payerGoalsCard");
  const badge = document.getElementById("patientPayerBadge");
  const list = document.getElementById("patientPayerGoalsList");
  if (!card || !badge || !list) return;

  const arr = Array.isArray(goals) ? goals.filter(Boolean) : [];
  // Show the card when we have either a payer mode or generated goals.
  if (!payerModel && !arr.length) {
    card.style.display = "none";
    return;
  }

  if (payerModel) {
    badge.textContent = patientPayerLabel(payerModel);
    badge.className = "payer-badge payer-badge-" + payerModel;
    badge.hidden = false;
  } else {
    badge.hidden = true;
  }

  list.innerHTML = arr.length
    ? arr.map(renderPatientPayerGoalItem).join("")
    : `<li class="payer-goal-empty">Your goals will appear here once your plan is set.</li>`;
  card.style.display = "";

  // Draft super-bill self-view. Only relevant when a payer mode is set;
  // lazy-loaded on first open so the dashboard stays light. Wire the
  // toggle handler once.
  const sbWrap = document.getElementById("patientSuperbillWrap");
  if (sbWrap) {
    if (payerModel) {
      sbWrap.hidden = false;
      if (!sbWrap.dataset.bound) {
        sbWrap.dataset.bound = "1";
        sbWrap.addEventListener("toggle", () => {
          if (sbWrap.open && !sbWrap.dataset.loaded) {
            loadPatientSuperbill();
          }
        });
      }
    } else {
      sbWrap.hidden = true;
    }
  }
}

async function loadPatientSuperbill() {
  const wrap = document.getElementById("patientSuperbillWrap");
  const body = document.getElementById("patientSuperbillBody");
  if (!body) return;
  body.innerHTML = `<div class="payer-goal-empty">Loading…</div>`;
  let data;
  try {
    const res = await authedFetch(`${API_BASE}/patient/me/superbill`);
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    data = await res.json();
  } catch (e) {
    console.warn("patient superbill load failed", e);
    body.innerHTML = `<div class="payer-goal-empty">Couldn't load your draft super-bill right now.</div>`;
    return;
  }
  if (wrap) wrap.dataset.loaded = "1";
  body.innerHTML = renderPatientSuperbill(data);
}

// Patient-facing draft super-bill. Read-only, plain-language framing. Same
// DRAFT-attestation banner (dashed MOCK convention) so it is unmistakably
// not a real, payable bill.
function renderPatientSuperbill(data) {
  const items = Array.isArray(data.line_items) ? data.line_items : [];
  const totals = data.totals || {};
  const disclaimers = Array.isArray(data.disclaimers) ? data.disclaimers : [];

  const banner = `<div class="superbill-attestation">
    <span class="superbill-attestation-tag">DRAFT</span>
    <span class="superbill-attestation-text">This is a draft for your reference,
    built from your completed sessions. It is not a bill and has not been reviewed
    or signed by your clinician.</span>
  </div>`;

  if (!items.length) {
    return banner +
      `<div class="payer-goal-empty">No completed sessions yet — nothing to show.</div>`;
  }

  const rows = items.map((it) => {
    const dr = it.date_range || {};
    const range = (dr.start || dr.end)
      ? `${escapeHtml(dr.start || "?")} → ${escapeHtml(dr.end || "?")}`
      : "—";
    return `<tr>
      <td class="superbill-cpt">${escapeHtml(it.cpt || "—")}</td>
      <td>${escapeHtml(it.descriptor || "—")}</td>
      <td class="superbill-num">${escapeHtml(it.units != null ? it.units : "—")}</td>
      <td class="superbill-range">${range}</td>
    </tr>`;
  }).join("");

  const table = `<div class="superbill-table-wrap">
    <table class="superbill-table">
      <thead><tr>
        <th scope="col">CPT</th>
        <th scope="col">Descriptor</th>
        <th scope="col">Units</th>
        <th scope="col">Date range</th>
      </tr></thead>
      <tbody>${rows}</tbody>
    </table>
  </div>`;

  const totalBits = [
    totals.total_units != null ? `${escapeHtml(totals.total_units)} units` : null,
    totals.total_sessions != null ? `${escapeHtml(totals.total_sessions)} sessions` : null,
  ].filter(Boolean).join(" · ");
  const totalsRow = totalBits ? `<div class="superbill-totals">${totalBits}</div>` : "";

  const disc = disclaimers.length
    ? `<ul class="superbill-disclaimers">${disclaimers.map((d) => `<li>${escapeHtml(d)}</li>`).join("")}</ul>`
    : "";

  return banner + table + totalsRow + disc;
}

// Render a relative-time label like "10m" / "2h" / "1d" given an ISO8601
// timestamp. Falls back to empty string when the input is missing or
// unparseable so callers can omit the trailing dot cleanly.
function formatRelativeAgo(iso) {
  if (!iso) return "";
  const ts = Date.parse(iso);
  if (!Number.isFinite(ts)) return "";
  const deltaMs = Date.now() - ts;
  if (deltaMs < 0) return "just now";
  const min = Math.floor(deltaMs / 60_000);
  if (min < 1) return "just now";
  if (min < 60) return `${min}m ago`;
  const hr = Math.floor(min / 60);
  if (hr < 24) return `${hr}h ago`;
  const d = Math.floor(hr / 24);
  return `${d}d ago`;
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
      // open plan-gen modal with the pending_protocol_id so the patient
      // can self-approve (or hand off to a clinician).
      closeIntakeModal();
      const pending = payload.data?.pending_protocol_id || null;
      showPlanGenModal({ pending_protocol_id: pending });
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

async function showPlanGenModal({ pending_protocol_id = null } = {}) {
  const modal = document.getElementById("planGenModal");
  const trace = document.getElementById("planGenTrace");
  const prCard = document.getElementById("planGenPrCard");
  const cont = document.getElementById("planGenContinue");
  if (!modal) return;
  modal.hidden = false;
  if (trace) trace.innerHTML = "";
  if (prCard) { prCard.hidden = true; prCard.innerHTML = ""; }
  if (cont) cont.hidden = true;

  appendPlanGenLine("[start]", "Drafting your protocol…");

  // If we don't already have a pending row, force plan generation now.
  if (!pending_protocol_id) {
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
      pending_protocol_id = payload.data?.pending_protocol_id || null;
      if (payload.message) appendPlanGenLine("[plan]", payload.message);
    } catch (err) {
      appendPlanGenLine("[fail]", `Plan generation failed: ${err.message || err}`, true);
    }
  }

  if (pending_protocol_id) {
    renderPlanGenPending(pending_protocol_id);
  } else {
    appendPlanGenLine("[fail]", "No pending protocol was created. Try again or contact your clinician.", true);
  }
  if (cont) cont.hidden = false;
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

// Render the pending_review row from chat_protocol_drafter or PlanGenerationAgent.
// Patient view ONLY — there is no patient self-approve button. The clinician-
// in-the-loop is the safety gate; only the clinician dashboard at /clinician
// can promote a row to active. The patient sees a calm "sent to your PT for
// review" surface and the trust pill in the header for status.
function renderPlanGenPending(protocolId) {
  const prCard = document.getElementById("planGenPrCard");
  if (!prCard) return;
  const safeId = escapeHtml(protocolId);
  prCard.hidden = false;
  prCard.innerHTML = `
    <div class="pr-pending-status" data-protocol-id="${safeId}">
      <span class="pr-pending-dot" aria-hidden="true"></span>
      <div class="pr-pending-copy">
        <strong>Sent to your PT for review</strong>
        <p>You'll see an update here once they decide. No further action needed.</p>
      </div>
    </div>
  `;
}

function closePlanGenModal() {
  const modal = document.getElementById("planGenModal");
  if (modal) modal.hidden = true;
}

function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, (c) => (
    { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]
  ));
}

function applyStepLocks() {
  // Quick-action buttons used to be gated behind a localStorage
  // `rehab_intake_complete` flag, which only flipped to true when the
  // server returned state="ready" (intake done AND a clinician has
  // approved a protocol). That left every authed user in the
  // "needs_plan" state stuck with three disabled buttons that rendered
  // but didn't fire on click. The state machine is now server-driven
  // (/patient/me/intake-status) and each handler routes itself, so the
  // pre-emptive client-side lock is redundant. Always unlock; let
  // navigateTo() + the handlers decide what to do based on live state.
  ["generatePlanBtn", "exerciseBtn", "triggerCheckinBtn"].forEach((id) => {
    const btn = document.getElementById(id);
    if (!btn) return;
    btn.disabled = false;
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
  const chatPane    = document.getElementById("stageChat");
  const videoPane   = document.getElementById("stageVideo");
  const historyPane = document.getElementById("stageHistory");
  const chatTab     = document.getElementById("tabChat");
  const videoTab    = document.getElementById("tabVideo");
  const historyTab  = document.getElementById("tabHistory");

  const isChat = mode === "chat";
  const isVideo = mode === "video";
  const isHistory = mode === "history";

  if (chatPane) chatPane.hidden = !isChat;
  if (videoPane) videoPane.hidden = !isVideo;
  if (historyPane) historyPane.hidden = !isHistory;

  if (chatTab) {
    chatTab.classList.toggle("active", isChat);
    chatTab.setAttribute("aria-selected", String(isChat));
  }
  if (videoTab) {
    videoTab.classList.toggle("active", isVideo);
    videoTab.setAttribute("aria-selected", String(isVideo));
  }
  if (historyTab) {
    historyTab.classList.toggle("active", isHistory);
    historyTab.setAttribute("aria-selected", String(isHistory));
  }

  if (isChat) {
    // Leaving other panes: keep the video iframe in the DOM but don't tear
    // it down — the patient may flip back. End-session is the explicit
    // teardown.
    document.getElementById("chatInput")?.focus();
  } else if (isVideo) {
    // Entering video: refresh the "Continue last session" affordance so a
    // mid-day return doesn't show a stale row from a previous tab session.
    loadRecentTavusSessions();
  } else if (isHistory) {
    // Entering history: fetch the 30-day log. Caches in-flight via the
    // _historyLoading guard inside loadPatientHistory.
    loadPatientHistory();
  }
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
  // PR-V3: drop the localStorage `intakeComplete` gate. Same reasoning as
  // PR-U7's removal of the planApproved gate — localStorage is unreliable
  // (cleared on cache wipe, doesn't sync across devices), so andre102599
  // (38 protocols on the backend) was rendering "no protocol yet" on a
  // fresh browser session because the flag had never been set. /protocol
  // is the source of truth; if exercises are empty, render the empty
  // state, otherwise render what the backend returns.
  try {
    const res = await authedFetch(`${API_BASE}/protocol`);
    const data = await res.json();
    renderProtocol(data);
    // Legacy GitHub repo link is gone (PR-bus retired); the existing
    // maybeRedirectToClinician + maybeRenderBackToDashboard helpers
    // surface the dashboard affordance for staff users.
  } catch (e) {
    console.error("Failed to load protocol:", e);
  }
}

function renderProtocol({ protocol }) {
  const list = document.getElementById("protocolExercises");
  const meta = document.getElementById("protocolMeta");
  const exercises = protocol.exercises || [];
  const isPendingIntake =
    !exercises.length ||
    protocol.phase === "pending_intake";

  if (isPendingIntake) {
    meta.textContent = "no protocol yet";
    list.innerHTML = `
      <li class="protocol-empty">
        <div class="empty-headline">No protocol yet</div>
        <div class="empty-sub">Click <strong>Start intake</strong> below to onboard the patient. Coach Maya will draft the initial protocol for clinician review.</div>
      </li>
    `;
    return;
  }

  // Don't render protocol.patient here - it's a denormalized snapshot that
  // drifts between accounts (the "Christian" bug). The signed-in user's name
  // is already on the auth pill at the top of the page.
  meta.textContent = `${protocol.phase || "rehab"} - week ${protocol.week ?? "?"}`;
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
// Tavus session (PR-P: auth-gated, persisted, no silent mock fallback)
//
// State machine:
//   pre-session -> loading -> active   (start a new conversation)
//   pre-session -> loading -> active   (continue an existing one)
//   active      -> pre-session         (End session)
//
// All three buttons live inside the Video Call tab; pre-session shows
// "Start a new session" + (conditionally) "Continue last", active shows
// the Tavus iframe + an "End session" button.
// ---------------------------------------------------------------------------

let activeTavusSessionId = null;
// Daily callObject driving the Tavus conversation (replaces the raw iframe so
// we can sendAppMessage interactions + read the local camera track). null when
// no live call is up. tavusConvId is the Tavus conversation id required by the
// echo/interrupt payloads; null outside a live call so echo helpers no-op.
let tavusCall   = null;
let tavusConvId = null;
let tavusMuted  = false;
let tavusCamOff = false;
// During a live call, Maya is the only voice — the local Web-Speech backstop
// is gated off so it does not double-speak over her echo. Flip to true only
// for debugging a silent echo.
const LOCAL_TTS_IN_CALL = false;

function _getVideoEls() {
  return {
    pre: document.getElementById("preSession"),
    loading: document.getElementById("loadingSession"),
    active: document.getElementById("activeSession"),
    video: document.getElementById("tavusVideo"),
    startBtn: document.getElementById("startBtn"),
    continueBtn: document.getElementById("continueBtn"),
    endBtn: document.getElementById("endBtn"),
  };
}

// Tear down any existing call object. Safe to call repeatedly; guards against
// duplicate call objects / mic conflicts on a start-then-continue without end.
async function _destroyTavusCall() {
  if (!tavusCall) return;
  // Tear down any running in-call set first so the pose camera (which consumes
  // the call's local track) is released before the call is destroyed.
  try {
    if (_inCallSetBtn && _inCallSetBtn.dataset.state === "on") {
      // Log whatever reps were detected before the call ended, then stop pose.
      try { _activeGuidedPartialPoster?.(); } catch (_) {}
      window.PoseFormCheck?.stop?.();
    }
  } catch (_) {}
  _activeGuidedPartialPoster = null;
  _resetInCallSetUi();
  try { await tavusCall.leave(); } catch (_) {}
  try { tavusCall.destroy(); } catch (_) {}
  tavusCall   = null;
  tavusConvId = null;
  tavusMuted  = false;
  tavusCamOff = false;
  const v = document.getElementById("tavusVideo");
  const a = document.getElementById("tavusAudio");
  if (v) v.srcObject = null;
  if (a) a.srcObject = null;
}

// Return a MediaStream wrapping the Daily call's local camera track so pose.js
// can consume the SAME physical camera the call uses (no second getUserMedia).
// Returns null when the call/track is not yet available; callers fall back to
// pose.js opening its own camera (opts.stream omitted).
function _tavusLocalStream() {
  if (!tavusCall) return null;
  try {
    const vt = tavusCall.participants()?.local?.tracks?.video;
    const mst = vt?.persistentTrack || vt?.track;
    if (mst) return new MediaStream([mst]);
  } catch (e) {
    console.warn("tavus local track unavailable", e);
  }
  return null;
}

function _showVideoLoading() {
  const els = _getVideoEls();
  els.pre.style.display = "none";
  els.active.style.display = "none";
  els.loading.style.display = "flex";
  els.startBtn.disabled = true;
  els.continueBtn.disabled = true;
}

function _showVideoPreSession() {
  const els = _getVideoEls();
  els.loading.style.display = "none";
  els.active.style.display = "none";
  els.pre.style.display = "flex";
  els.startBtn.disabled = false;
  els.continueBtn.disabled = false;
  if (els.video) els.video.srcObject = null;
}

// Render the Tavus conversation via a Daily callObject (not a raw iframe) so
// the client can sendAppMessage interactions (the per-rep echo) and read the
// local camera track for pose.js. Maya's remote video is piped into the
// <video id="tavusVideo"> on her track-started event. conversationId is
// stashed for the echo payloads.
async function _showVideoActive(conversationUrl, conversationId) {
  const els = _getVideoEls();
  els.pre.style.display = "none";
  els.loading.style.display = "none";
  els.active.style.display = "flex";

  if (!window.Daily || typeof window.Daily.createCallObject !== "function") {
    showToast("Video SDK failed to load. Hard-refresh and retry.", "error");
    _showVideoPreSession();
    return;
  }

  await _destroyTavusCall();   // guard against duplicate call objects
  try {
    tavusCall = window.Daily.createCallObject({ url: conversationUrl });
    tavusConvId = conversationId || null;
    tavusMuted = false;
    tavusCamOff = false;
    // A custom Daily callObject does NOT auto-play remote media: we attach
    // Maya's remote video to the <video> and her audio to a dedicated <audio>
    // element (the iframe used to do this for us). We ignore the local track —
    // pose.js consumes that separately for rep detection.
    const audioEl = document.getElementById("tavusAudio");
    tavusCall.on("track-started", (ev) => {
      if (!ev || !ev.participant || ev.participant.local) return;
      try {
        if (ev.type === "video") {
          els.video.srcObject = new MediaStream([ev.track]);
        } else if (ev.type === "audio" && audioEl) {
          audioEl.srcObject = new MediaStream([ev.track]);
          audioEl.play?.().catch(() => {});
        }
      } catch (_) {}
    });
    await tavusCall.join({ url: conversationUrl });
  } catch (e) {
    console.error("tavus call join failed", e);
    showToast(`Could not join video call: ${e.message}`, "error");
    await _destroyTavusCall();
    _showVideoPreSession();
  }
}

// ── Tavus interactions: make Maya speak a string verbatim via conversation.echo
// over Daily's data channel (no LLM round-trip, so it works under the custom
// BYO-LLM persona). Counting drives this: it is called from the per-rep branch
// in onPosePayload keyed on the SAME detected rep number. No-op when not in a
// live call. ──────────────────────────────────────────────────────────────
let _lastEchoTs = 0;
const ECHO_INTERRUPT_GAP_MS = 900;   // if a rep lands faster than this, de-garble

function _sendTavusInteraction(payload) {
  if (!tavusCall || !tavusConvId) return false;
  try {
    tavusCall.sendAppMessage(payload, "*");
    return true;
  } catch (e) {
    console.warn("sendAppMessage failed", e);
    return false;
  }
}

// Speak a count word (and, optionally, a short form cue) through Maya. `cue`
// is sent as a second echo shortly after the number so the number is heard
// first; pass it only when the rep's form was off.
function echoMayaCount(word, cue) {
  if (!tavusCall || !tavusConvId || !word) return;
  const now = (typeof performance !== "undefined" ? performance.now() : Date.now());
  // Fast back-to-back reps: interrupt the prior (likely still-playing) count
  // before echoing the new one so the words don't garble together.
  if (now - _lastEchoTs < ECHO_INTERRUPT_GAP_MS) {
    _sendTavusInteraction({
      message_type: "conversation",
      event_type: "conversation.interrupt",
      conversation_id: tavusConvId,
    });
  }
  _lastEchoTs = now;
  _sendTavusInteraction({
    message_type: "conversation",
    event_type: "conversation.echo",
    conversation_id: tavusConvId,
    properties: { modality: "text", text: String(word), done: true },
  });
  if (cue) {
    setTimeout(() => {
      _sendTavusInteraction({
        message_type: "conversation",
        event_type: "conversation.echo",
        conversation_id: tavusConvId,
        properties: { modality: "text", text: String(cue), done: true },
      });
    }, 600);
  }
}

// ── In-call calf-raise set ──────────────────────────────────────────────────
// The live loop entry point: a "Start calf-raise set" control in the video
// stage, co-visible with Maya. It reuses togglePoseFormCheck so the rep count
// stays the single pose.js-gated source (no separate counting path), feeds the
// Daily local track in via _tavusLocalStream() inside that function, and echoes
// each detected rep through Maya. The pose UI shell mounts in the hidden
// #inCallSetMount; the always-correct backstop is the #inCallRepIndicator
// overlay pinned on Maya's video.
//
// _inCallSetBtn is the synthetic button togglePoseFormCheck drives (it carries
// the on/off dataset state the function toggles). We hold a reference so Stop
// can toggle the same instance off.
let _inCallSetBtn = null;
// Registered by the active guided session (togglePoseFormCheck closure) so an
// in-call stop can log the partial set from the real rep history. Null when no
// guided session is running.
let _activeGuidedPartialPoster = null;

function _resetInCallSetUi() {
  const start = document.getElementById("startSetBtn");
  const stop  = document.getElementById("stopSetBtn");
  const hurts = document.getElementById("hurtsSetBtn");
  const mount = document.getElementById("inCallSetMount");
  const ind   = document.getElementById("inCallRepIndicator");
  const stage = document.getElementById("tavusStage");
  if (start) { start.style.display = ""; start.disabled = false; }
  if (stop)  stop.style.display = "none";
  if (hurts) hurts.style.display = "none";
  if (mount) mount.hidden = true;
  if (ind)   ind.hidden = true;
  // Drop the in-set layout: Maya returns to full prominence, hero + self-view
  // PiP retract.
  if (stage) stage.dataset.set = "off";
  _inCallSetBtn = null;
}

// Write the current rep count into the in-call hero (role=status, so screen
// readers announce each change). This is the SINGLE live counter for the call.
// `count` is the integer rep total; `dose` is the subordinate context line
// ("of 15", "Set 1 of 3", or "reps"). Accepting a number (not a pre-formatted
// "Rep N" string) keeps one source of truth — the big number is wired straight
// to the same repCount the metrics pill uses, so the hero can never drift stale
// behind it. No-op when the hero is absent.
function setInCallRep(count, dose) {
  const num  = document.getElementById("inCallRepPill");
  const sub  = document.getElementById("inCallRepDose");
  if (num && count != null) num.textContent = String(count);
  if (sub && dose != null) sub.textContent = dose;
  // The whole indicator is role=status aria-live=polite, so SR reads it as a
  // unit ("7 of 15, Good form") on each detected rep. No per-element aria-label
  // is set here — that would double-announce against the live region.
}

// Write the live form status into the in-call chip. State is conveyed three
// redundant ways so it is never carried by color alone: data-status (CSS color),
// a glyph in .incall-form-icon, and the .incall-form-text label. `text`
// overrides the default per-status label (used for specific correction cues).
const _FORM_STATUS_GLYPH = { good: "✓", warn: "⚠", bad: "✕", idle: "·" };
function setInCallFormStatus(status, text) {
  const chip = document.getElementById("inCallFormChip");
  if (!chip) return;
  const s = status || "idle";
  chip.dataset.status = s;
  const iconEl = chip.querySelector(".incall-form-icon");
  const textEl = chip.querySelector(".incall-form-text");
  if (iconEl) iconEl.textContent = _FORM_STATUS_GLYPH[s] || _FORM_STATUS_GLYPH.idle;
  const label = text || (
    s === "good" ? "Good form"
    : s === "warn" ? "Adjust form"
    : s === "bad"  ? "Check form"
    : "Get into frame"
  );
  if (textEl) textEl.textContent = label;
  else chip.textContent = label;   // defensive: pre-restructure markup
}

// Exercise IDs that ship a demo clip at /static/videos/{id}.mp4. Several
// library entries carry empty youtube_id / null video_url yet have a real clip
// on disk under this convention (notably the ankle set, including the calf
// raises that drive the in-call hero). This set lets _refVideoSrc resolve those
// without re-introducing a broken <video> for IDs that genuinely have no file —
// the no-build frontend has no runtime manifest, so the available clips are
// enumerated here. Source of truth: frontend/videos/*.mp4.
const STATIC_DEMO_VIDEO_IDS = new Set([
  "ankle_alphabet", "ankle_calf_raises_double_leg", "ankle_calf_raises_single_leg",
  "ankle_dorsiflexion_band", "ankle_eversion_band", "ankle_lateral_hops",
  "ankle_single_leg_balance", "ankle_towel_calf_stretch",
  "elbow_eccentric_wrist_extension", "elbow_eccentric_wrist_flexion",
  "elbow_grip_squeeze", "elbow_isometric_extension", "elbow_pronation_supination_band",
  "elbow_radial_nerve_glide", "elbow_wrist_extensor_stretch", "elbow_wrist_flexor_stretch",
  "glute_bridge", "ham_bridge_heel_slide", "ham_nordic_eccentric_assisted",
  "ham_prone_hip_extension", "ham_seated_active_extension", "ham_single_leg_rdl",
  "ham_supine_active_stretch", "ham_supine_curl_ball", "ham_walking_lunge",
  "heel_slides", "lb_bird_dog", "lb_cat_cow", "lb_child_pose", "lb_dead_bug",
  "lb_glute_bridge_lb", "lb_mckenzie_press_up", "lb_pelvic_tilt",
  "lb_supine_knee_to_chest", "mini_squat", "quad_sets", "shoulder_isometric_er",
  "shoulder_isometric_ir", "shoulder_pendulum", "shoulder_prone_t",
  "shoulder_prone_y", "shoulder_scapular_retraction", "shoulder_sleeper_stretch",
  "shoulder_wall_slides", "single_leg_squat", "stationary_bike",
  "terminal_knee_extension", "wall_sit",
]);

// Resolve a reference demo-video URL for an exercise, or "" when none exists.
// Order: explicit generated/url field → static-path convention for known clips
// → "". Callers omit the reference panel entirely when this returns "" so an
// empty source never paints a black void.
function _refVideoSrc(ex) {
  if (!ex) return "";
  if (ex.generated_video_url) return ex.generated_video_url;
  if (ex.video_url) return ex.video_url;
  const id = ex.library_id || ex.id;
  if (id && STATIC_DEMO_VIDEO_IDS.has(id)) return `/static/videos/${id}.mp4`;
  return "";
}

// Build the calf-raise item the pose shell expects. Prefer the patient's
// prescribed row (inherits the active-protocol dose) when today's session has
// loaded; otherwise fall back to the canonical library entry so the in-call
// set works even before startTodaysSession has run.
function _calfRaiseItem() {
  const LIB_ID = "ankle_calf_raises_double_leg";
  const prescribed = (_todaysSessionState.exercises || []).find(
    (e) => e && (e.id === LIB_ID || e.library_id === LIB_ID),
  );
  if (prescribed) return { ex: prescribed };
  return {
    ex: {
      id: LIB_ID,
      library_id: LIB_ID,
      name: "Double-Leg Calf Raises",
      default_dose: "3 x 15",
      form_check_supported: true,
      cues: [
        "Rise onto balls of both feet",
        "Pause 1 second at top",
        "Lower slowly - 3 seconds down",
      ],
    },
  };
}

async function startInCallCalfSet() {
  if (!tavusCall) {
    showToast("Start a video session with Maya first.", "error");
    return;
  }
  const start = document.getElementById("startSetBtn");
  const stop  = document.getElementById("stopSetBtn");
  const hurts = document.getElementById("hurtsSetBtn");
  const mount = document.getElementById("inCallSetMount");
  const ind   = document.getElementById("inCallRepIndicator");
  const stage = document.getElementById("tavusStage");
  if (!mount) return;

  // Promote the in-set layout: the rep hero + self-view PiP become the focal
  // elements, Maya recedes to the coach backdrop.
  if (stage) stage.dataset.set = "on";
  mount.hidden = false;
  if (ind) ind.hidden = false;
  setInCallRep(0);
  setInCallFormStatus("idle");
  if (start) start.style.display = "none";
  if (stop)  stop.style.display = "";
  if (hurts) hurts.style.display = "";

  // togglePoseFormCheck owns the camera/skeleton/rep state machine and the
  // _tavusLocalStream() + echoMayaCount wiring. We hand it the in-call mount
  // (#inCallSetMount holds the #galleryVideoWrap the function queries) and a
  // synthetic off-state button it toggles to "on".
  _inCallSetBtn = mount.querySelector(".incall-set-toggle") || (() => {
    const b = document.createElement("button");
    b.className = "incall-set-toggle";
    b.dataset.state = "off";
    b.style.display = "none";
    mount.appendChild(b);
    return b;
  })();
  _inCallSetBtn.dataset.state = "off";
  await togglePoseFormCheck(mount, _calfRaiseItem(), _inCallSetBtn);
}

// Stop the current in-call set. `hurt` true => the patient hit "Something
// hurts": we still log the partial set (postPoseSession runs inside the shell's
// teardown is NOT guaranteed mid-set, so we do not fabricate a log here) and
// keep the Maya call alive so she can respond. Either way we tear down the pose
// shell via the same toggle the shell uses, never via endTavusSession.
function stopInCallCalfSet(hurt) {
  const stop  = document.getElementById("stopSetBtn");
  const hurts = document.getElementById("hurtsSetBtn");
  if (stop)  stop.disabled = true;
  if (hurts) hurts.disabled = true;
  // Log the partial set from the real detected reps BEFORE teardown clears the
  // guided closure. No-op when zero reps were detected or already submitted.
  try { _activeGuidedPartialPoster?.(); } catch (_) {}
  if (_inCallSetBtn && _inCallSetBtn.dataset.state === "on") {
    // Toggling off runs the shell teardown (PoseFormCheck.stop + in-call UI
    // reset). The Maya call is untouched.
    togglePoseFormCheck(document.getElementById("inCallSetMount"),
                        _calfRaiseItem(), _inCallSetBtn);
  } else {
    _resetInCallSetUi();
  }
  if (stop)  stop.disabled = false;
  if (hurts) hurts.disabled = false;
  if (hurt) {
    // Surface a spoken acknowledgement through Maya so the stop is felt, and
    // give the patient an obvious next step without ending the call.
    echoMayaCount("Okay, stopping the set. Tell me what hurts.");
    showToast("Set stopped. Tell Maya what hurts.", "info");
  }
}

function toggleTavusMute() {
  if (!tavusCall) return;
  tavusMuted = !tavusMuted;
  try { tavusCall.setLocalAudio(!tavusMuted); } catch (_) {}
  const b = document.getElementById("muteBtn");
  if (b) b.textContent = tavusMuted ? "Unmute" : "Mute";
}

function toggleTavusCam() {
  if (!tavusCall) return;
  tavusCamOff = !tavusCamOff;
  try { tavusCall.setLocalVideo(!tavusCamOff); } catch (_) {}
  const b = document.getElementById("camBtn");
  if (b) b.textContent = tavusCamOff ? "Camera on" : "Camera off";
}

async function loadRecentTavusSessions() {
  // Surfaces a "Continue last session" button when the most-recent row is
  // still active. Silently skips when the patient is unauthed (the Video
  // Call tab is gated behind auth at the start-session call anyway).
  const continueBtn = document.getElementById("continueBtn");
  if (!continueBtn) return;
  const jwt = window.RehabAuth?.getJwt?.();
  if (!jwt) {
    continueBtn.style.display = "none";
    return;
  }
  try {
    const res = await authedFetch(`${API_BASE}/tavus/sessions/recent?limit=5`);
    if (!res.ok) {
      continueBtn.style.display = "none";
      return;
    }
    const data = await res.json();
    const lastActive = (data.sessions || []).find((s) => s.is_active);
    if (lastActive && lastActive.conversation_url) {
      continueBtn.dataset.sessionId = lastActive.id;
      continueBtn.dataset.conversationUrl = lastActive.conversation_url;
      continueBtn.dataset.conversationId = lastActive.conversation_id || "";
      continueBtn.style.display = "inline-block";
    } else {
      continueBtn.style.display = "none";
    }
  } catch (e) {
    console.warn("loadRecentTavusSessions failed", e);
    continueBtn.style.display = "none";
  }
}

async function startNewTavusSession() {
  const jwt = window.RehabAuth?.getJwt?.();
  if (!jwt) {
    showToast("Sign in to start a video session.", "error");
    return;
  }
  _showVideoLoading();
  try {
    const res = await authedFetch(`${API_BASE}/start-session`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({}),
    });
    if (res.status === 503) {
      _showVideoPreSession();
      showToast("Video call temporarily unavailable.", "error");
      return;
    }
    if (res.status === 502) {
      _showVideoPreSession();
      showToast("Video provider error. Please retry shortly.", "error");
      return;
    }
    if (res.status === 401) {
      _showVideoPreSession();
      showToast("Sign in to start a video session.", "error");
      return;
    }
    if (!res.ok) {
      const detail = await res.text().catch(() => "");
      _showVideoPreSession();
      showToast(`Could not start session (${res.status})`, "error");
      console.error("start-session failed", res.status, detail);
      return;
    }
    const data = await res.json();
    if (!data.conversation_url) {
      _showVideoPreSession();
      showToast("Video call response missing URL.", "error");
      return;
    }
    activeTavusSessionId = data.tavus_session_id || null;
    await _showVideoActive(data.conversation_url, data.conversation_id);

    if (data.recommendations?.length) {
      const recPanel = document.getElementById("recommendations");
      if (recPanel) {
        renderFocus(data.recommendations);
        recPanel.style.display = "block";
      }
    }
  } catch (e) {
    console.error(e);
    _showVideoPreSession();
    showToast(`Error: ${e.message}`, "error");
  }
}

async function continueTavusSession() {
  const continueBtn = document.getElementById("continueBtn");
  const sessionId = continueBtn?.dataset?.sessionId;
  const url = continueBtn?.dataset?.conversationUrl;
  const convId = continueBtn?.dataset?.conversationId || null;
  if (!sessionId || !url) {
    // No active row found; fall back to start path.
    startNewTavusSession();
    return;
  }
  _showVideoLoading();
  // The conversation URL is reusable for the duration of the call's TTL —
  // no second create_conversation roundtrip needed.
  activeTavusSessionId = sessionId;
  await _showVideoActive(url, convId);
}

async function endTavusSession() {
  const sessionId = activeTavusSessionId;
  // Always tear down the live call object (camera + mic) first so the device
  // is released regardless of the persistence call's outcome.
  await _destroyTavusCall();
  if (!sessionId) {
    _showVideoPreSession();
    return;
  }
  try {
    const res = await authedFetch(
      `${API_BASE}/tavus/sessions/${sessionId}/end`,
      { method: "POST" },
    );
    if (!res.ok && res.status !== 404) {
      const detail = await res.text().catch(() => "");
      console.warn("end_tavus_session failed", res.status, detail);
      showToast("Could not record session end. The call has stopped.", "warn");
    }
  } catch (e) {
    console.warn("end_tavus_session error", e);
  }
  activeTavusSessionId = null;
  _showVideoPreSession();
  loadRecentTavusSessions();
}

// Legacy alias kept so any stray external callers (e.g. an old onclick
// handler not yet refreshed) still resolve. Internal markup uses
// startNewTavusSession directly.
const startSession = startNewTavusSession;

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
// Pending-protocol render — replaces the dead PR-bus surface
// ---------------------------------------------------------------------------
//
// Coach Maya's chat tools (fire_symptom_trigger / fire_checkin_trigger /
// fire_weekly_plan_trigger) save a draft protocol revision as a
// `pending_review` row in the `protocols` table. The patient sees this card;
// the clinician sees the same row from /clinician. Approving here hits
// POST /protocols/{id}/approve which transactionally promotes the row to
// `active` and supersedes the prior active version.

function renderPendingProtocolCard(protocolId, summary, flowLabel) {
  const log = document.getElementById("chatLog");
  if (!log) return;
  const bubble = document.createElement("div");
  bubble.className = "chat-bubble pr-result pending-protocol";
  const safeId = escapeHtml(protocolId);
  const safeSummary = escapeHtml(summary || "Protocol revision queued for clinician review.");
  const safeFlow = escapeHtml(flowLabel || "draft");
  // Patient view: no self-approve button. The pending row is visible to the
  // clinician at /clinician and only the clinician can promote it. The
  // header trust pill (review_status) reflects pending -> approved/rejected.
  bubble.innerHTML = `
    <div class="pr-result-header">${safeFlow} drafted — with your PT for review</div>
    <div class="pr-result-summary">${safeSummary}</div>
    <div class="pr-result-footnote" data-protocol-id="${safeId}">
      You'll see an update here once your PT approves or sends notes back.
    </div>
  `;
  log.appendChild(bubble);
  scrollChatLog?.();
}

// Triage alert (PR-H): patient-side receipt rendered when the symptom
// classifier returned severity=clinician-attention. Always shown so the
// patient knows their PT was flagged AND has an immediate escalation path
// (urgent care / clinic phone) for severe symptoms. The actual chat reply
// from Maya still streams below this; this is a system-level affordance,
// not a Maya turn.
function renderTriageAlert(event) {
  const log = document.getElementById("chatLog");
  if (!log) return;
  const bubble = document.createElement("div");
  bubble.className = "chat-bubble system triage-alert";
  const keyword = event?.symptom_keyword;
  const phone = event?.clinic_phone;
  // Subject line: when keyword is known, use "Your message about [keyword]";
  // otherwise fall back to a generic phrasing.
  const subject = keyword
    ? `Your message about <strong>${escapeHtml(keyword)}</strong> was flagged for your PT.`
    : `Your message was flagged for your PT.`;
  // Escalation copy: phone number (if configured) becomes a tel: link.
  // Without one we say "call your clinic" - intentionally generic so the
  // patient doesn't get a dead link.
  const callCopy = phone
    ? `call your clinic at <a href="tel:${escapeHtml(phone)}">${escapeHtml(phone)}</a>`
    : "call your clinic";
  bubble.innerHTML = `
    <div class="triage-alert-header">PT alert</div>
    <p class="triage-alert-body">${subject} They'll review it shortly.</p>
    <p class="triage-alert-escalation">
      If you have severe pain, swelling, or numbness now, ${callCopy} or go to urgent care.
    </p>
  `;
  log.appendChild(bubble);
  scrollChatLog?.();
}

// Inline error card surfaced when chat_protocol_drafter / save_pending fails.
// We intentionally don't fake success when the LLM is unreachable — the
// patient sees the failure, can retry, and the clinician isn't queueing a
// silent zero-output draft.
function renderPendingProtocolError(message, flowLabel) {
  const log = document.getElementById("chatLog");
  if (!log) return;
  const bubble = document.createElement("div");
  bubble.className = "chat-bubble error pending-protocol-error";
  const safeMsg = escapeHtml(message || "Couldn't draft a protocol revision. Please try again in a moment.");
  const safeFlow = escapeHtml(flowLabel || "draft");
  bubble.innerHTML = `
    <div class="pr-result-header">${safeFlow} draft failed</div>
    <div class="pr-result-summary">${safeMsg}</div>
  `;
  log.appendChild(bubble);
  scrollChatLog?.();
}

function reportSymptom() {
  switchStage("chat");
  clearChatLog();
  activeFlow = { type: "symptom", step: 0, answers: {} };
  updateFlowUI(true);
  appendChatBubble("coach",
    "Let's log your symptom. Five quick questions.\n\n" +
    SYMPTOM_QUESTIONS[0].q
  );
  setTimeout(prefillFlowInput, 50);
}

// ---------------------------------------------------------------------------
// Guided flows: intake / symptom / check-in
// ---------------------------------------------------------------------------

// Demo-mode-only questionnaire used by triggerIntake() when no JWT is
// present (public landing page walkthrough). Authed patients run the
// production conversational intake via IntakeAgent + /patient/interact.
// Per PR-V2: no `default` values for PHI fields — pre-filling identity
// or pain levels in the live input is a consent-pattern violation.
// Hints stay as placeholders only (cleared on focus).
const INTAKE_QUESTIONS = [
  { key: "name",     q: "What's your name?",                                hint: "e.g. Sarah" },
  { key: "age",      q: "How old are you?",                                  hint: "e.g. 26" },
  { key: "injury",   q: "What was your injury or surgery?",                  hint: "e.g. ACL reconstruction" },
  { key: "timing",   q: "When was your surgery or injury?",                  hint: "e.g. 3 weeks ago" },
  { key: "pain",     q: "On a scale of 1–10, what's your current pain level?", hint: "no default — type your number" },
  { key: "symptoms", q: "Any specific symptoms?",                            hint: "e.g. tightness at end-range flexion" },
];

const SYMPTOM_QUESTIONS = [
  { key: "location",  q: "Where is the pain or discomfort?",  hint: "e.g. inner knee" },
  { key: "type",      q: "How would you describe it?",         hint: "e.g. sharp, dull, ache, tightness" },
  { key: "level",     q: "Pain level 1–10?",                   hint: "type your number" },
  { key: "trigger",   q: "When does it happen?",               hint: "e.g. during single-leg squats" },
  { key: "duration",  q: "How long has this been going on?",   hint: "e.g. started today" },
];

const CHECKIN_QUESTIONS = [
  { key: "rating",     q: "How did today's session go overall? (1–10)", hint: "your rating" },
  { key: "completed",  q: "Which exercises did you complete?",          hint: "e.g. heel slides, quad sets" },
  { key: "strong",     q: "What felt strong or improved today?",        hint: "e.g. quad set felt stronger" },
  { key: "difficult",  q: "Anything that felt difficult or caused discomfort? (or type \"none\")", hint: "e.g. single-leg balance was shaky, or 'none'" },
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
    "Demo mode — sign in for a real intake. Six quick questions.\n\n" +
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
    // PR-V2: never seed PHI inputs with `value=...`. Hint text only.
    input.value = "";
    input.placeholder = q.hint || "Type your answer...";
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
  activeFlow.answers[q.key] = text || "";
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
    // Legacy chat-style intake survives only as a demo-mode walkthrough for
    // unauthed users. Authed users reach the structured intake modal via
    // navigateToIntake(). For demo, we just acknowledge and unlock the next
    // step locally; the real protocol draft requires auth.
    appendChatBubble(
      "coach",
      "Got it. Sign in to save this intake and queue a protocol for clinician review.",
    );
    onIntakeComplete();

  } else if (type === "symptom") {
    const symptom_text =
      `${a.location} — ${a.type}, level ${a.level}/10. ` +
      `Occurs ${a.trigger}. Duration: ${a.duration}`;
    appendChatBubble("coach", "Logged. Drafting an adjustment for clinician review...");
    sendChat(`I have a symptom to log: ${symptom_text}`, { skipUserBubble: true });

  } else if (type === "checkin") {
    const checkin_text =
      `Session rating ${a.rating}/10. Completed: ${a.completed}. ` +
      `Strong: ${a.strong}. Difficult: ${a.difficult}`;
    appendChatBubble("coach", "Check-in logged. Drafting a tweak for clinician review...");
    sendChat(`Session check-in: ${checkin_text}`, { skipUserBubble: true });
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
    "Quick session check-in. Four questions.\n\n" +
    CHECKIN_QUESTIONS[0].q
  );
  setTimeout(prefillFlowInput, 50);
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
    const res = await authedFetch(`${API_BASE}/protocol/exercises`);
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

  appendChatBubble("coach", `Scheduled reminder set for ${ordered.join(", ")}. Drafting next week's protocol for clinician review...`);
  onPlanApproved();
  // Fire the chat-driven weekly plan tool. The model picks fire_weekly_plan_trigger
  // and the backend writes a pending_review row; renderPendingProtocolCard
  // shows the patient an Approve button + clinician dashboard link.
  sendChat(`Plan next week for me. ${freqNote}`, { skipUserBubble: true });
  setTimeout(() => triggerExercise(), 2400);
}

// Exercises the user staged in step 2 (by clicking "+ Add to plan")
// These are the only ones shown in step 3 guided exercise.
const _pendingPlan = []; // { id, name, spec, ...card data }

async function showPlanWithApprove() {
  const log = document.getElementById("chatLog");
  if (!log) return;
  try {
    const res = await authedFetch(`${API_BASE}/protocol/exercises`);
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

  const render = (exercises, opts = {}) => {
    if (!exercises.length) {
      appendChatBubble("coach", "No exercises matched - try Browse all to see the full library.");
    } else {
      renderExerciseGallery(exercises);
    }
    appendBrowseAllAffordance(opts.activeTab || "plan");
    scrollChatLog();
  };

  if (approvedPlanExercises.length) {
    render(approvedPlanExercises, { activeTab: "plan" });
    return;
  }
  try {
    const res = await authedFetch(`${API_BASE}/protocol/exercises`);
    if (!res.ok) throw new Error(`status ${res.status}`);
    const data = await res.json();
    render(data.exercises || [], { activeTab: "plan" });
  } catch (e) {
    appendChatBubble("error", `Could not load exercises: ${e.message}`);
  }
}

// "Browse all exercises" affordance below the active gallery. Clicking it
// fetches /exercises (the full library, no auth) and re-renders the gallery
// with the entire catalogue. Includes a phase filter so the patient can
// narrow to acute/subacute/strength.
function appendBrowseAllAffordance(activeTab) {
  const log = document.getElementById("chatLog");
  if (!log) return;
  // Avoid stacking duplicates if loadExerciseCards re-runs.
  const prev = document.getElementById("browseAllPanel");
  if (prev) prev.remove();

  const panel = document.createElement("div");
  panel.id = "browseAllPanel";
  panel.className = "chat-bubble coach browse-all-panel";
  panel.innerHTML = `
    <div class="browse-all-header">
      <strong>Exercise library</strong>
      <span class="browse-all-tabs">
        <button type="button" class="browse-all-tab ${activeTab === "plan" ? "active" : ""}"
                data-tab="plan">My plan</button>
        <button type="button" class="browse-all-tab ${activeTab === "all" ? "active" : ""}"
                data-tab="all">Browse all</button>
      </span>
    </div>
    <div class="browse-all-filters" id="browseAllFilters" style="display:none">
      <label>Phase
        <select id="browseAllPhase">
          <option value="">any</option>
          <option value="acute">acute</option>
          <option value="subacute">subacute</option>
          <option value="strength">strength</option>
        </select>
      </label>
    </div>
  `;
  log.appendChild(panel);

  panel.querySelectorAll(".browse-all-tab").forEach((btn) => {
    btn.addEventListener("click", () => switchExerciseTab(btn.dataset.tab));
  });
  const filters = panel.querySelector("#browseAllFilters");
  if (filters) filters.style.display = activeTab === "all" ? "flex" : "none";
  const phaseEl = panel.querySelector("#browseAllPhase");
  if (phaseEl) phaseEl.addEventListener("change", () => loadAllExercises(phaseEl.value || ""));
}

async function switchExerciseTab(tab) {
  if (tab === "plan") {
    clearChatLog();
    loadExerciseCards();
    return;
  }
  // Browse all
  await loadAllExercises("");
}

async function loadAllExercises(phase) {
  clearChatLog();
  appendChatBubble("coach", phase
    ? `Showing all ${escapeHtml(phase)} exercises in the library.`
    : "Showing the full exercise library.");
  try {
    const url = phase
      ? `${API_BASE}/exercises?phase=${encodeURIComponent(phase)}`
      : `${API_BASE}/exercises`;
    const res = await authedFetch(url);
    if (!res.ok) throw new Error(`status ${res.status}`);
    const data = await res.json();
    const exercises = data.exercises || [];
    if (!exercises.length) {
      appendChatBubble("coach", "No exercises matched that filter.");
    } else {
      renderExerciseGallery(exercises);
    }
    appendBrowseAllAffordance("all");
    // Re-set the phase select to its current value after re-render
    const phaseEl = document.getElementById("browseAllPhase");
    if (phaseEl && phase) phaseEl.value = phase;
    scrollChatLog();
  } catch (e) {
    appendChatBubble("error", `Could not load library: ${e.message}`);
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
    // Cache the friendly name so the today's-session sidebar can render
    // "Wall Squat" rather than "wall_squat" when /sessions/today returns.
    rememberExerciseName(ex.id || ex.name, ex.name || ex.id);
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

  // PR-U4: tear down any running form-check before swapping gallery
  // content. Without this, the live PoseFormCheck singleton keeps
  // running its detect loop against the <video> we're about to remove,
  // and the next innerHTML rewrite leaves stale done / between / preflight
  // overlays from the prior exercise stacked on top of the new one's
  // pane. Andre hit this: Stationary Bike's metadata on the left, Quad
  // Sets's preflight + "Workout complete" overlay rendering on the right
  // simultaneously. Stop the singleton, cancel any speech, and remove
  // the form-check button so maybeAttachFormCheckBtn binds a fresh one
  // closing over the NEW item (the old button's onclick captured the
  // old item).
  const activeBtn = wrap.querySelector(".pose-form-check-btn[data-state='on']");
  if (activeBtn) {
    try { window.PoseFormCheck?.stop?.(); } catch (_) {}
    try { window.speechSynthesis?.cancel?.(); } catch (_) {}
    document.body.classList.remove("pose-active");
  }
  // Always drop the existing form-check button so the rebuilt one is
  // bound to this idx's item, not whatever was active before.
  const oldFcBtn = wrap.querySelector(".pose-form-check-btn");
  if (oldFcBtn) oldFcBtn.remove();

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
// Pose form check
// ---------------------------------------------------------------------------
// The feature is shipped, no longer behind a URL flag. The button renders on
// every gallery card whose exercise_id has a check roster in pose.js's
// EXERCISES map. Camera permission is requested at click-time by the browser;
// nothing else gates discovery.

function maybeAttachFormCheckBtn(wrap, item) {
  if (!window.PoseFormCheck) {
    console.warn("PoseFormCheck not loaded - pose.js failed to initialize");
    return;
  }
  // P1.1: source of truth for guided form-check support is the backend's
  // form_check_supported flag (from knowledge/exercise-library.json),
  // surfaced via to_card. EXERCISES membership is a defense-in-depth
  // fallback so a stale frontend bundle never attaches a button for an
  // exercise the pose engine doesn't know about.
  // PR-U8: prefer library_id over id so planner-generated regressions
  // resolved by the fuzzy matcher inherit the canonical entry's flag.
  const poseKey = item.ex.library_id || item.ex.id;
  const supported = item.ex.form_check_supported === true;
  if (!supported) return;
  if (!window.PoseFormCheck.EXERCISES?.[poseKey]) return;
  const videoWrap = wrap.querySelector("#galleryVideoWrap");
  if (!videoWrap || videoWrap.parentElement.querySelector(".pose-form-check-btn")) return;
  const btn = document.createElement("button");
  btn.type = "button";
  btn.className = "pose-form-check-btn";
  btn.dataset.state = "off";
  btn.textContent = "Start guided form-check";
  btn.title = "Use your webcam for live rep + alignment feedback";
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

// Voice preference order (PR-J spec): macOS Samantha, Windows Aria,
// Chrome Google US English. Falls back to first English voice, then any
// voice. Override via localStorage.poseVoiceURI (set to a voice's URI).
// The voice list loads asynchronously in some browsers — pickVoice()
// returns null on that first call and retries each time.
function pickVoice() {
  if (_poseVoiceObj) return _poseVoiceObj;
  const voices = window.speechSynthesis?.getVoices?.() || [];
  if (!voices.length) return null;
  const override = (typeof localStorage !== "undefined")
    ? localStorage.getItem("poseVoiceURI") : null;
  if (override) {
    const match = voices.find((v) => v.voiceURI === override);
    if (match) { _poseVoiceObj = match; return _poseVoiceObj; }
  }
  const tryMatch = (re) =>
    voices.find((v) => v.lang.startsWith("en") && re.test(v.name));
  _poseVoiceObj =
    tryMatch(/samantha/i) ||
    tryMatch(/microsoft aria|aria/i) ||
    tryMatch(/google us english/i) ||
    tryMatch(/victoria|female/i) ||
    voices.find((v) => v.lang.startsWith("en")) ||
    voices[0];
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

// Like speakCue but cancels any in-flight utterance first so the new cue
// lands immediately. Used by the guided wrapper for time-critical
// correction cues that must beat the next frame's update.
function speakNow(text) {
  if (!text) return;
  if (!poseVoiceEnabled()) return;
  if (!window.speechSynthesis || !window.SpeechSynthesisUtterance) return;
  try { window.speechSynthesis.cancel(); } catch (_) {}
  const u = new SpeechSynthesisUtterance(String(text));
  u.rate   = 1.1;
  u.pitch  = 1.0;
  u.volume = 0.95;
  const v = pickVoice();
  if (v) u.voice = v;
  window.speechSynthesis.speak(u);
}

// Parse "3 x 10", "3x10", "3 sets x 10 reps", "10 reps" → {sets, reps}.
// Defaults to {sets: 1, reps: null} when ambiguous; the guided flow falls
// back to single-set if reps are unknown so we never trap the patient in
// an unending set.
function parseSetsReps(doseStr) {
  if (!doseStr) return { sets: 1, reps: null };
  const s = String(doseStr);
  // Tight form: digits across 'x' or '×' with whitespace only ("3 x 10").
  const tight = s.match(/(\d+)\s*[x×]\s*(\d+)/i);
  if (tight) return { sets: parseInt(tight[1], 10), reps: parseInt(tight[2], 10) };
  // Verbose form: "N sets x M reps" / "N set M reps" with words between.
  const verbose = s.match(/(\d+)\s*sets?\s*[x×]?\s*(\d+)\s*rep/i);
  if (verbose) return { sets: parseInt(verbose[1], 10), reps: parseInt(verbose[2], 10) };
  // Reps-only fallback ("10 reps", "12 repetitions").
  const repsOnly = s.match(/(\d+)\s*rep/i);
  if (repsOnly) return { sets: 1, reps: parseInt(repsOnly[1], 10) };
  return { sets: 1, reps: null };
}

// Pronounce a small int as a word ("one", "two") for the count cue.
const _NUM_WORDS_GUIDED = [
  "zero","one","two","three","four","five","six","seven","eight","nine","ten",
  "eleven","twelve","thirteen","fourteen","fifteen","sixteen","seventeen",
  "eighteen","nineteen","twenty",
];
function spokenCount(n) {
  return _NUM_WORDS_GUIDED[n] || String(n);
}

// Map a rep event's metricId (possibly "L_knee_depth") to the corrections-map
// key ("knee_depth"). Mirrors pose.js correctionKey; used to look up the form
// cue for a non-good rep when echoing through Maya.
function correctionKeyOf(metricId) {
  return String(metricId || "").replace(/^[LR]_/, "");
}

// Pure correction-throttle decision. Given a list of check transitions
// emitted by pose.js this frame, the per-exercise corrections map, and a
// stateful throttle record, returns the cue to speak (or null). Mutates
// `state.spokenKeys` (Set) and `state.lastCueTs` on hit.
//
// Throttle rules (PR-J spec):
//   * Same correctionKey speaks at most once per rep.
//   * Distinct cues are gapped by `gapMs` so they don't trample each other.
//     The very first cue always fires (lastCueTs starts as null/undefined).
//   * Picks the FIRST eligible transition with a known cue, so a single
//     payload yields at most one spoken cue.
//
// Pure / DOM-free so the tests under frontend/tests can exercise it.
function decideCorrectionCue(state, transitions, corrections, nowTs, gapMs) {
  if (!transitions || !transitions.length) return null;
  // lastCueTs == null means "never fired"; the first cue always passes.
  if (state.lastCueTs != null && nowTs - state.lastCueTs < gapMs) return null;
  for (const t of transitions) {
    const key = t.correctionKey;
    if (!key) continue;
    if (state.spokenKeys.has(key)) continue;
    const cue = corrections?.[key];
    if (!cue) continue;
    state.spokenKeys.add(key);
    state.lastCueTs = nowTs;
    return { key, cue, status: t.to };
  }
  return null;
}

// Per-rep boundary: clear the dedupe set when a rep finishes (inRep flips
// true → false). The wrapper calls this so the next rep can re-speak the
// same correction if the form error recurs.
function rolloverRepThrottle(state, prevInRep, nextInRep) {
  if (prevInRep && !nextInRep) state.spokenKeys.clear();
}

// Expose pure helpers for the node-side test harness.
if (typeof window !== "undefined") {
  window.__poseGuidedHelpers = {
    parseSetsReps,
    spokenCount,
    decideCorrectionCue,
    rolloverRepThrottle,
  };
}

// PR-U9: build the preflight help line from the exercise's framing config
// so the patient sees camera-position guidance specific to THIS exercise
// (sit + camera at floor for ankle alphabet, vs full-body framing for a
// squat). Falls back to the generic full-body line when framing is unset
// or when pose.js hasn't loaded yet.
function _preflightHelpText(ex) {
  const exDef = window.PoseFormCheck?.EXERCISES?.[ex.library_id || ex.id];
  const cfg = exDef?.framing && window.PoseFormCheck?.FRAMING_CONFIG?.[exDef.framing];
  if (cfg) return `${cfg.cameraTip} ${cfg.hint}`;
  return "Position your camera waist-high, about 8 feet away. Make sure your full body is visible inside the outline.";
}

// P1.4: render a small SVG diagram of the expected camera POV for the
// exercise's framing preset. Diagrammatic guidance complements the text
// hint — patients have been confused by "lower the camera or step back"
// without seeing what the right setup looks like. Returns "" when the
// exercise has no framing config (presence-mode + non-pose exercises).
//
// Stroke uses --ai-text and fill --ai-bg per Clinical Twilight; size is
// fixed at 56x56 so the diagram fits in the preflight card without
// crowding the cue list.
function _preflightFramingSvg(framing) {
  // Common SVG attrs: 56x56 viewport, currentColor fed by the .pose-framing-icon
  // class so palette swaps don't need an SVG edit.
  const wrap = (inner) =>
    `<svg class="pose-framing-icon" viewBox="0 0 56 56" width="56" height="56"
          xmlns="http://www.w3.org/2000/svg" aria-hidden="true">${inner}</svg>`;
  switch (framing) {
    case "full_body": {
      // Stick figure standing upright on the right; camera on a tripod
      // on the left, lens pointed across at hip height ~6 ft away.
      const figure = `
        <circle cx="42" cy="11" r="3.5" fill="none" stroke="currentColor" stroke-width="1.5"/>
        <line x1="42" y1="14.5" x2="42" y2="32" stroke="currentColor" stroke-width="1.5"/>
        <line x1="42" y1="20" x2="36" y2="26" stroke="currentColor" stroke-width="1.5"/>
        <line x1="42" y1="20" x2="48" y2="26" stroke="currentColor" stroke-width="1.5"/>
        <line x1="42" y1="32" x2="38" y2="46" stroke="currentColor" stroke-width="1.5"/>
        <line x1="42" y1="32" x2="46" y2="46" stroke="currentColor" stroke-width="1.5"/>
      `;
      const camera = `
        <rect x="6" y="22" width="12" height="9" rx="1.5" fill="none" stroke="currentColor" stroke-width="1.5"/>
        <circle cx="12" cy="26.5" r="2.2" fill="none" stroke="currentColor" stroke-width="1.5"/>
        <line x1="12" y1="31" x2="12" y2="46" stroke="currentColor" stroke-width="1.5"/>
        <line x1="7" y1="48" x2="17" y2="48" stroke="currentColor" stroke-width="1.5"/>
        <path d="M18 26 L24 22 L24 31 L18 27 Z" fill="none" stroke="currentColor" stroke-width="1" stroke-dasharray="2 2"/>
      `;
      return wrap(figure + camera);
    }
    case "lower_body": {
      // Legs only on the right; camera low + close on the left.
      const legs = `
        <line x1="40" y1="8" x2="40" y2="30" stroke="currentColor" stroke-width="1.5" stroke-dasharray="2 2"/>
        <line x1="40" y1="30" x2="35" y2="48" stroke="currentColor" stroke-width="1.5"/>
        <line x1="40" y1="30" x2="45" y2="48" stroke="currentColor" stroke-width="1.5"/>
        <circle cx="40" cy="30" r="2" fill="none" stroke="currentColor" stroke-width="1.5"/>
      `;
      const cameraLow = `
        <rect x="6" y="36" width="12" height="9" rx="1.5" fill="none" stroke="currentColor" stroke-width="1.5"/>
        <circle cx="12" cy="40.5" r="2.2" fill="none" stroke="currentColor" stroke-width="1.5"/>
        <path d="M18 40 L26 36 L26 45 L18 41 Z" fill="none" stroke="currentColor" stroke-width="1" stroke-dasharray="2 2"/>
      `;
      return wrap(legs + cameraLow);
    }
    case "feet_seated": {
      // Feet on the right (top-down view of two shoes); camera on floor
      // pointing up at the feet from the left.
      const feet = `
        <ellipse cx="42" cy="22" rx="4" ry="6" fill="none" stroke="currentColor" stroke-width="1.5"/>
        <ellipse cx="42" cy="36" rx="4" ry="6" fill="none" stroke="currentColor" stroke-width="1.5"/>
      `;
      const cameraFloor = `
        <rect x="6" y="44" width="14" height="8" rx="1.5" fill="none" stroke="currentColor" stroke-width="1.5"/>
        <circle cx="13" cy="48" r="2" fill="none" stroke="currentColor" stroke-width="1.5"/>
        <path d="M20 47 L34 30 L34 36 L20 50 Z" fill="none" stroke="currentColor" stroke-width="1" stroke-dasharray="2 2"/>
      `;
      return wrap(feet + cameraFloor);
    }
    case "arms_torso": {
      // Torso + arms on the right; camera at chest height on the left.
      const torso = `
        <circle cx="42" cy="10" r="3.5" fill="none" stroke="currentColor" stroke-width="1.5"/>
        <line x1="42" y1="13.5" x2="42" y2="34" stroke="currentColor" stroke-width="1.5"/>
        <line x1="42" y1="20" x2="32" y2="24" stroke="currentColor" stroke-width="1.5"/>
        <line x1="42" y1="20" x2="52" y2="24" stroke="currentColor" stroke-width="1.5"/>
        <line x1="32" y1="24" x2="30" y2="34" stroke="currentColor" stroke-width="1.5"/>
        <line x1="52" y1="24" x2="54" y2="34" stroke="currentColor" stroke-width="1.5"/>
      `;
      const cameraChest = `
        <rect x="6" y="18" width="12" height="9" rx="1.5" fill="none" stroke="currentColor" stroke-width="1.5"/>
        <circle cx="12" cy="22.5" r="2.2" fill="none" stroke="currentColor" stroke-width="1.5"/>
        <path d="M18 22 L26 18 L26 27 L18 23 Z" fill="none" stroke="currentColor" stroke-width="1" stroke-dasharray="2 2"/>
      `;
      return wrap(torso + cameraChest);
    }
    default:
      return "";
  }
}

// Resolve the framing key for an exercise, or null when no framing is set.
function _preflightFramingKey(ex) {
  const exDef = window.PoseFormCheck?.EXERCISES?.[ex.library_id || ex.id];
  return exDef?.framing || null;
}

// Required-landmarks gate for the preflight overlay. Maps each check id
// back to the joints it needs visible so we can disable/enable Start
// based on what's actually trackable for THIS exercise.
function landmarksRequiredFor(exId) {
  const ex = window.PoseFormCheck?.EXERCISES?.[exId];
  const set = new Set([11, 12, 23, 24]);  // shoulders + hips always
  if (!ex) return [...set];
  for (const c of ex.checks) {
    if (c === "L_knee_depth" || c === "L_knee_valgus") { set.add(25); set.add(27); }
    if (c === "R_knee_depth" || c === "R_knee_valgus") { set.add(26); set.add(28); }
    if (c === "L_hip_angle") { set.add(25); }
    if (c === "R_hip_angle") { set.add(26); }
    if (c === "L_shoulder_abduction" || c === "L_elbow_angle") { set.add(13); set.add(15); }
    if (c === "R_shoulder_abduction" || c === "R_elbow_angle") { set.add(14); set.add(16); }
  }
  return [...set];
}

// PR-J guided-mode constants. Surfaced here so tests / smoke scripts can
// reference them without re-grepping the function body.
const GUIDED = {
  PREFLIGHT_DETECTED_HOLD_MS: 2000,  // landmark-stability hold before Start enables
  REST_SECONDS_DEFAULT:       30,
  CORRECTION_BUBBLE_MS:       1500,
  CORRECTION_GAP_MS:          900,   // min gap between two distinct correction utterances
};

// ── Pose set telemetry (POST /pose/session) ─────────────────────────────────

// Pose set telemetry now uses the shared authedFetch wrapper above — same
// JWT source as /chat and /protocol. Kept the helper removed so there's no
// drift between two ways of building the Authorization header.

// Returns { ok: boolean, sessionId: string|null, worstStatus: string|null }
// — the auto-checkin card uses this to decide whether to render and to
// pre-fill the pain dot. We deliberately do NOT show the card on failure:
// the workout wasn't recorded server-side, so a check-in here would be
// orphan data (PR-N spec: no silent fallbacks).
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
    const res = await authedFetch(`${API_BASE}/pose/session`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    if (res.status === 401) {
      showToast("Set not logged — sign in to save your progress", "info");
      return { ok: false, sessionId: null, worstStatus: null };
    }
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const out = await res.json().catch(() => ({}));
    showToast("Set logged — Maya can see it now", "info");
    return {
      ok: true,
      sessionId: out.session_id || null,
      worstStatus: out.worst_status || null,
    };
  } catch (e) {
    console.warn("postPoseSession failed:", e);
    showToast(`Set log failed: ${e.message}`, "error");
    return { ok: false, sessionId: null, worstStatus: null };
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
    // PR-U9: the generic "no body in frame" pill is misleading for seated
    // exercises (the patient IS in frame, just not from the head down).
    // When framingStatus is present, surface the missing region instead.
    const fs = payload.framingStatus;
    const label = (fs && fs.required > 0 && fs.visible < fs.required)
      ? `need to see your ${fs.missingLabel}`
      : "no body in frame";
    container.innerHTML = `<span class="metric-pill idle">${escapeHtml(label)}</span>`;
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

// PR-J guided exercise mode.
//
// Wraps PoseFormCheck.start() with a multi-set state machine + audio
// coaching layer. Pose detection / rep counting / skeleton drawing all
// stay in pose.js — this function owns the UX shell:
//   * preflight overlay (camera permission + landmark-stability gate)
//   * set/rep counter overlay (large, mobile-first)
//   * correction bubble + TTS (per-rep dedupe via decideCorrectionCue)
//   * status pulse indicator (good/warn/bad)
//   * rest countdown overlay with circular progress ring
//   * tap-when-ready between-set overlay
//   * workout-complete summary + POST /pose/session
//
// Voice driver: pose.js receives suppressInternalVoice: true so the
// wrapper owns all spoken cues (counts, corrections, set-complete, rest).
async function togglePoseFormCheck(wrap, item, btn) {
  const videoWrap = wrap.querySelector("#galleryVideoWrap");
  if (!videoWrap) return;

  if (btn.dataset.state === "on") {
    window.PoseFormCheck.stop();
    _activeGuidedPartialPoster = null;
    btn.dataset.state = "off";
    btn.textContent = "Start guided form-check";
    document.body.classList.remove("pose-active");
    try { window.speechSynthesis?.cancel?.(); } catch (_) {}
    if (wrap.classList.contains("incall-set-mount")) {
      // In-call wrap: reset the mount to an empty placeholder; do NOT touch
      // the gallery (it is on the hidden chat stage). The call stays live.
      videoWrap.innerHTML = "";
      videoWrap.className = "exercise-video-placeholder";
      videoWrap.id = "galleryVideoWrap";
      _resetInCallSetUi();
    } else if (wrap.classList.contains("exercise-card")) {
      videoWrap.innerHTML = `<span class="video-placeholder-text">Add to today to load video</span>`;
      videoWrap.className = "exercise-video-placeholder";
      videoWrap.id = "galleryVideoWrap";
    } else {
      const activeIdx = Array.from(wrap.querySelectorAll(".gallery-thumb-btn"))
        .findIndex((b) => b.classList.contains("active"));
      switchGalleryItem(activeIdx >= 0 ? activeIdx : 0);
    }
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

  // Reference demo video. Prefer an explicit URL on the exercise; otherwise
  // fall back to the static-path convention (/static/videos/{id}.mp4). Several
  // library entries (the ankle set) carry empty youtube_id / null video_url yet
  // DO have a real clip on disk under that convention — _refVideoSrc resolves
  // it. When nothing resolves, refSrc is "" and the reference panel is omitted
  // entirely (no broken black <video> void).
  const refSrc = _refVideoSrc(item.ex);
  const voiceOn = poseVoiceEnabled();

  // PR-U1: when this exercise IS in the patient's active protocol, render
  // the prescribed dose (sets/reps from /protocol/exercises) instead of the
  // generic library default. _todaysSessionState.exercises is populated
  // whenever startTodaysSession has run; a missing match means the patient
  // is exploring the library off-protocol — fall back to library default.
  // This is a frontend-only override; the backend /pose/session write path
  // is unchanged.
  const prescribed = (_todaysSessionState.exercises || []).find(
    (e) => e && e.id === item.ex.id,
  ) || null;
  const effectiveDose = (prescribed && prescribed.default_dose)
    || item.ex.default_dose
    || "";
  const { sets: parsedSets, reps: parsedReps } = parseSetsReps(effectiveDose);
  const totalSets   = Math.max(1, parsedSets || 1);
  const repsPerSet  = parsedReps;  // null when unparseable

  // Only render the reference panel when a video actually resolves. An empty
  // src would paint a black void with a floating "Reference" label (the bug for
  // ankle exercises with no clip); omitting the panel lets the live pose view
  // claim the full width via .pose-split--noref. The slot returns automatically
  // the moment _refVideoSrc resolves a clip for the exercise.
  const refPanel = refSrc
    ? `<div class="pose-split-ref">
        <video src="${escapeHtml(refSrc)}" playsinline muted autoplay loop></video>
        <div class="pose-split-ref-label">Reference</div>
      </div>`
    : "";

  videoWrap.innerHTML = `
    <div class="pose-split${refSrc ? "" : " pose-split--noref"}">
      ${refPanel}
      <div class="pose-root pose-split-live" id="poseRoot">
        <div class="pose-toolbar">
          <div class="pose-title" title="${escapeHtml(item.ex.name)}">
            <span class="pose-title-name">${escapeHtml(item.ex.name)}</span>
            <span class="pose-title-dose">${escapeHtml(effectiveDose)}</span>
            ${prescribed ? `<span class="pose-title-prescribed" title="From your active protocol">prescribed</span>` : ""}
          </div>
          <div class="pose-metrics" id="poseMetrics">
            <span class="metric-pill idle">starting camera...</span>
          </div>
          <button class="pose-voice-btn" id="poseVoiceBtn" data-on="${voiceOn ? "1" : "0"}" title="Voice cues">
            ${voiceOn ? "Voice on" : "Voice off"}
          </button>
          <button class="pose-fullscreen-btn" id="poseFullscreenBtn" title="Toggle fullscreen">[ ]</button>
        </div>
        <div class="pose-warnings" id="poseWarnings" hidden></div>
        <div class="pose-stage" id="poseStage">
          <video id="poseVideo" playsinline muted autoplay></video>
          <canvas id="poseCanvas" class="pose-overlay-canvas"></canvas>
          <div class="pose-frame-guide" aria-hidden="true"></div>
          <div class="pose-overlay" id="poseGuidedOverlay">
            <div class="pose-overlay-set" id="poseSetLabel"></div>
            <div class="pose-overlay-rep" id="poseRepLabel"></div>
            <button class="pose-presence-done" id="posePresenceDoneBtn" type="button" hidden>Mark set done</button>
            <div class="pose-overlay-pulse" id="poseStatusPulse" data-status="idle" aria-hidden="true"></div>
            <div class="pose-correction-bubble" id="poseCorrectionBubble" hidden></div>
            <div class="pose-rest-overlay" id="poseRestOverlay" hidden>
              <div class="pose-rest-ring">
                <svg viewBox="0 0 100 100" aria-hidden="true">
                  <circle cx="50" cy="50" r="45" class="pose-rest-ring-track"></circle>
                  <circle cx="50" cy="50" r="45" class="pose-rest-ring-fill" id="poseRestRingFill"></circle>
                </svg>
                <div class="pose-rest-count" id="poseRestCount">30</div>
              </div>
              <div class="pose-rest-label">Rest</div>
            </div>
            <div class="pose-between-overlay" id="poseBetweenOverlay" hidden>
              <div class="pose-between-title" id="poseBetweenTitle">Set 2 of 3</div>
              <div class="pose-between-sub">Tap when ready</div>
              <button class="pose-between-go" id="poseBetweenGoBtn" type="button">Start set</button>
            </div>
            <div class="pose-done-overlay" id="poseDoneOverlay" hidden>
              <div class="pose-done-title">Workout complete</div>
              <div class="pose-done-sub" id="poseDoneSub"></div>
              <button class="pose-done-close" id="poseDoneCloseBtn" type="button">Done</button>
            </div>
            <div class="pose-preflight" id="posePreflight">
              <div class="pose-preflight-card">
                <div class="pose-preflight-title">${escapeHtml(item.ex.name)}</div>
                <div class="pose-preflight-cues">
                  <ul>${(item.ex.cues || []).slice(0, 3).map((c) => `<li>${escapeHtml(c)}</li>`).join("")}</ul>
                </div>
                <div class="pose-preflight-help-row">
                  ${_preflightFramingSvg(_preflightFramingKey(item.ex))}
                  <div class="pose-preflight-help">
                    ${escapeHtml(_preflightHelpText(item.ex))}
                  </div>
                </div>
                <div class="pose-preflight-status" id="posePreflightStatus">Waiting for camera...</div>
                <button class="pose-preflight-go" id="posePreflightGoBtn" type="button">Start</button>
              </div>
            </div>
          </div>
        </div>
        <div class="pose-session-card" id="poseSession" hidden></div>
      </div>
    </div>
  `;

  const root         = videoWrap.querySelector("#poseRoot");
  const fsBtn        = videoWrap.querySelector("#poseFullscreenBtn");
  const voiceBtn     = videoWrap.querySelector("#poseVoiceBtn");
  const metricsEl    = videoWrap.querySelector("#poseMetrics");
  const warningsEl   = videoWrap.querySelector("#poseWarnings");
  const sessionEl    = videoWrap.querySelector("#poseSession");
  const setLabel     = videoWrap.querySelector("#poseSetLabel");
  const repLabel     = videoWrap.querySelector("#poseRepLabel");
  const pulseEl      = videoWrap.querySelector("#poseStatusPulse");
  const correctionEl = videoWrap.querySelector("#poseCorrectionBubble");
  const restOverlay  = videoWrap.querySelector("#poseRestOverlay");
  const restCount    = videoWrap.querySelector("#poseRestCount");
  const restRing     = videoWrap.querySelector("#poseRestRingFill");
  const betweenOl    = videoWrap.querySelector("#poseBetweenOverlay");
  const betweenTitle = videoWrap.querySelector("#poseBetweenTitle");
  const betweenGo    = videoWrap.querySelector("#poseBetweenGoBtn");
  const doneOverlay  = videoWrap.querySelector("#poseDoneOverlay");
  const doneSub      = videoWrap.querySelector("#poseDoneSub");
  const doneClose    = videoWrap.querySelector("#poseDoneCloseBtn");
  const preflightEl  = videoWrap.querySelector("#posePreflight");
  const preflightSt  = videoWrap.querySelector("#posePreflightStatus");
  const preflightGo  = videoWrap.querySelector("#posePreflightGoBtn");
  const presenceDone = videoWrap.querySelector("#posePresenceDoneBtn");
  // PR-U2: cache the engaged exercise's mode so the active-phase logic
  // can branch on presence vs rep-tracked without re-reading the
  // EXERCISES map every frame. Computed once at engage time.
  const isPresenceMode =
    window.PoseFormCheck?.EXERCISES?.[item.ex.id]?.mode === "presence";
  // Hold duration for presence mode. Library entries don't (yet) carry
  // a duration_min; we default to 60s/set, which lines up with the
  // typical PT cue ("hold for one minute"). When library/protocol
  // payloads start carrying explicit durations, source from there.
  const presenceHoldSeconds = 60;
  const videoEl      = videoWrap.querySelector("#poseVideo");
  const canvasEl     = videoWrap.querySelector("#poseCanvas");
  const stageEl      = videoWrap.querySelector("#poseStage");
  // Show the fit-to-frame guide while preflight is up.
  stageEl.classList.add("pose-stage--preflight");

  fsBtn.onclick = () => {
    if (document.fullscreenElement) document.exitFullscreen();
    else root.requestFullscreen?.();
  };
  voiceBtn.onclick = () => {
    const next = !poseVoiceEnabled();
    setPoseVoiceEnabled(next);
    voiceBtn.dataset.on = next ? "1" : "0";
    voiceBtn.textContent = next ? "Voice on" : "Voice off";
    if (next) {
      // Warm up speechSynthesis on the user gesture (Safari autoplay gate).
      try { speakCue("voice ready"); } catch (_) {}
    } else {
      try { window.speechSynthesis?.cancel?.(); } catch (_) {}
    }
  };

  // ── Guided session state ────────────────────────────────────────────────
  const guided = {
    phase: "preflight",          // preflight | active | rest | between | done
    setIdx: 0,                   // 0-indexed; setIdx === totalSets means done
    totalSets,
    repsPerSet,                  // null when dose is unparseable
    repsHistoryAll: [],          // every rep from every set (flat)
    repsHistoryThisSet: [],      // reset each new set
    warningsAll: [],
    spokenCorrectionsThisRep: new Set(),
    lastInRep: false,
    detectedSinceTs: null,       // preflight: continuous-detection timer
    submitted: false,
    restTimer: null,
    correctionFadeTimer: null,
    lastSpokenCount: -1,
    lastCorrectionTs: null,  // null = "never fired" so first cue always passes
  };

  // Register a partial-set poster so an in-call "Stop set" / "Something hurts"
  // can log whatever reps were actually detected before the stop, using the
  // real rep history from this closure (never fabricated). Guards on
  // guided.submitted so a natural finish (which posts in finishWorkout) is not
  // double-logged. Cleared on teardown.
  _activeGuidedPartialPoster = () => {
    if (guided.submitted) return;
    if (!guided.repsHistoryAll.length) return;
    guided.submitted = true;
    postPoseSession(item.ex, guided.repsHistoryAll, guided.warningsAll, {
      repCount: guided.repsHistoryAll.length,
    });
  };

  function clearTimers() {
    if (guided.restTimer)           { clearInterval(guided.restTimer); guided.restTimer = null; }
    if (guided.correctionFadeTimer) { clearTimeout(guided.correctionFadeTimer); guided.correctionFadeTimer = null; }
    if (guided.presenceTimer)       { clearInterval(guided.presenceTimer); guided.presenceTimer = null; }
  }

  function updateSetRepLabels() {
    if (guided.phase === "active") {
      setLabel.textContent = `Set ${guided.setIdx + 1}/${guided.totalSets}`;
      const repsThis = guided.repsHistoryThisSet.length;
      const total = guided.repsPerSet || 0;
      repLabel.textContent = total
        ? `Rep ${repsThis}/${total}`
        : `Rep ${repsThis}`;
      // In-call hero: the same in-set rep count the gallery shell shows. The
      // dose line carries the set context ("of 15" within "Set 1 of 3"). This
      // is one of two writers to the hero; the other is the always-on
      // repSummary path in onPosePayload, which keeps the hero live during
      // preflight and rest. Both write the SAME detected count, so they cannot
      // disagree.
      const dose = total
        ? `of ${total} · set ${guided.setIdx + 1}/${guided.totalSets}`
        : `set ${guided.setIdx + 1}/${guided.totalSets}`;
      setInCallRep(repsThis, dose);
    } else {
      setLabel.textContent = "";
      repLabel.textContent = "";
    }
  }

  function showCorrectionBubble(text, status) {
    if (!text) return;
    correctionEl.textContent = text;
    correctionEl.dataset.status = status || "warn";
    correctionEl.hidden = false;
    // Mirror the cue text + status into the in-call chip (text + semantic
    // color, never color alone) so the patient watching Maya gets the cue
    // even when the pose shell is the hidden mount.
    setInCallFormStatus(status || "warn", text);
    if (guided.correctionFadeTimer) clearTimeout(guided.correctionFadeTimer);
    guided.correctionFadeTimer = setTimeout(() => {
      correctionEl.hidden = true;
    }, GUIDED.CORRECTION_BUBBLE_MS);
  }

  function statusFromMetrics(metrics) {
    let s = "idle";
    for (const m of metrics || []) {
      if (m.status === "bad")  return "bad";
      if (m.status === "warn") s = "warn";
      else if (m.status === "good" && s !== "warn") s = "good";
    }
    return s;
  }

  function startSet() {
    guided.phase = "active";
    guided.repsHistoryThisSet = [];
    guided.spokenCorrectionsThisRep.clear();
    guided.lastInRep = false;
    guided.lastSpokenCount = -1;
    preflightEl.hidden = true;
    stageEl.classList.remove("pose-stage--preflight");
    restOverlay.hidden = true;
    betweenOl.hidden = true;
    doneOverlay.hidden = true;
    if (isPresenceMode) {
      // PR-U2: presence-mode exercises (ankle alphabet, band isolations,
      // lateral hops) don't have a 2D-trackable rep signal. Run a hold
      // timer instead of the rep state machine; expose a manual "Mark
      // set done" button so the patient can advance early if their PT
      // gave a different cadence. On either path, endSet() fires.
      startPresenceHold();
    } else {
      updateSetRepLabels();
    }
  }

  // PR-U2: presence-mode set runner. Speaks start / halfway / complete
  // cues, ticks an mm:ss progress chip into the rep label slot, and
  // surfaces a "Mark set done" button for the patient to fast-forward.
  // No rep counting — the headline metric is elapsed-vs-target time,
  // not rep count.
  function startPresenceHold() {
    let elapsed = 0;
    const total = presenceHoldSeconds;
    const fmt = (s) => `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`;
    setLabel.textContent = `Set ${guided.setIdx + 1}/${guided.totalSets}`;
    repLabel.textContent = `Hold ${fmt(0)} / ${fmt(total)}`;
    presenceDone.hidden = false;
    speakNow(`Set ${guided.setIdx + 1}, hold for ${total} seconds`);
    if (guided.presenceTimer) clearInterval(guided.presenceTimer);
    guided.presenceTimer = setInterval(() => {
      elapsed += 1;
      repLabel.textContent = `Hold ${fmt(elapsed)} / ${fmt(total)}`;
      if (elapsed === Math.floor(total / 2)) speakNow("Halfway");
      if (elapsed >= total) {
        clearInterval(guided.presenceTimer);
        guided.presenceTimer = null;
        endSet();
      }
    }, 1000);
  }

  // Manual fast-forward — usable any time during a presence-mode set.
  // Idempotent: clearTimers() inside endSet handles re-entry.
  presenceDone.onclick = () => {
    if (guided.phase !== "active" || !isPresenceMode) return;
    if (guided.presenceTimer) {
      clearInterval(guided.presenceTimer);
      guided.presenceTimer = null;
    }
    endSet();
  };

  function startRest() {
    guided.phase = "rest";
    const seconds = GUIDED.REST_SECONDS_DEFAULT;
    let remaining = seconds;
    restOverlay.hidden = false;
    restCount.textContent = String(remaining);
    if (restRing) {
      const c = 2 * Math.PI * 45;
      restRing.style.strokeDasharray  = String(c);
      restRing.style.strokeDashoffset = "0";
    }
    if (guided.restTimer) clearInterval(guided.restTimer);
    guided.restTimer = setInterval(() => {
      remaining -= 1;
      if (restRing) {
        const c = 2 * Math.PI * 45;
        const frac = remaining / seconds;
        restRing.style.strokeDashoffset = String(c * (1 - frac));
      }
      if (remaining <= 0) {
        clearInterval(guided.restTimer);
        guided.restTimer = null;
        restOverlay.hidden = true;
        showBetween();
      } else {
        restCount.textContent = String(remaining);
      }
    }, 1000);
  }

  function showBetween() {
    guided.phase = "between";
    betweenTitle.textContent = `Set ${guided.setIdx + 1} of ${guided.totalSets}`;
    betweenOl.hidden = false;
    speakNow(`Set ${guided.setIdx + 1} of ${guided.totalSets}, ready when you are`);
  }

  function finishWorkout() {
    guided.phase = "done";
    clearTimers();
    setLabel.textContent = "";
    repLabel.textContent = "";
    const totalReps = guided.repsHistoryAll.length;
    const warnCount = guided.repsHistoryAll.filter((r) => r.status !== "good").length;
    doneSub.textContent = `${totalReps} reps across ${guided.totalSets} set${guided.totalSets === 1 ? "" : "s"} - ${warnCount} form warning${warnCount === 1 ? "" : "s"}`;
    doneOverlay.hidden = false;
    speakNow("Workout complete. Logged to your record.");
    if (!guided.submitted) {
      guided.submitted = true;
      const repSummary = { repCount: totalReps };
      // Chain the auto check-in card off the /pose/session POST. We only
      // render the card on success: a failed POST means the workout
      // wasn't recorded, and a check-in linked to a missing session is
      // orphan data (PR-N: no silent fallbacks).
      postPoseSession(
        item.ex, guided.repsHistoryAll, guided.warningsAll, repSummary,
      ).then((result) => {
        if (!result || !result.ok) return;
        renderAutoCheckinCard({
          sessionId: result.sessionId,
          worstStatus: result.worstStatus,
          exerciseName: item.ex.name || item.ex.id,
        });
      });
    }
  }

  function endSet() {
    // PR-U2: hide presence-mode UI bits before transitioning. No-op for
    // rep-tracked exercises (button is already hidden).
    if (presenceDone) presenceDone.hidden = true;
    if (isPresenceMode) {
      speakNow(`Set ${guided.setIdx + 1} complete.`);
    } else {
      const reps = guided.repsHistoryThisSet.length;
      const warns = guided.repsHistoryThisSet.filter((r) => r.status !== "good").length;
      speakNow(`Set ${guided.setIdx + 1} complete. ${reps} reps, ${warns} form warnings.`);
    }
    guided.setIdx += 1;
    if (guided.setIdx >= guided.totalSets) {
      finishWorkout();
    } else {
      startRest();
    }
  }

  betweenGo.onclick = () => { if (guided.phase === "between") startSet(); };
  doneClose.onclick = () => { btn.click(); };

  function handlePreflight(payload) {
    // PR-U5: Start button stays clickable so the patient can step away
    // from the keyboard before the rep loop starts.
    // PR-U9: messaging is now exercise-specific. payload.framingStatus
    // (from pose.js, scored against MediaPipe landmark.visibility) tells
    // us EXACTLY which body region we need to see for THIS exercise. So
    // a "Seated Heel Raise" with no body in frame says "sit and point
    // camera at your ankles" instead of the generic "step into frame"
    // (which is wrong for seated exercises).
    const fs = payload.framingStatus;
    const exDef = window.PoseFormCheck.EXERCISES?.[item.ex.id];
    const expected = (exDef?.checks || []).length;
    const got = (payload.metrics || []).length;
    const trackingChip = `Tracking ${got}/${expected} ${expected === 1 ? "marker" : "markers"}`;

    if (fs && fs.required > 0) {
      const visChip = `Camera sees ${fs.visible}/${fs.required} of ${fs.missingLabel}`;
      if (fs.visible === 0) {
        preflightSt.textContent = `${visChip}. ${fs.hint} ${fs.cameraTip}`;
      } else if (!fs.ready) {
        preflightSt.textContent = `${visChip} — ${fs.hint}`;
      } else {
        preflightSt.textContent = `${trackingChip} · ${visChip} — ready. Tap Start.`;
      }
      if (fs.ready) {
        preflightGo.classList.add("ready");
      } else {
        preflightGo.classList.remove("ready");
      }
      return;
    }

    // Fallback for exercises without a framing declaration (legacy entries
    // not yet annotated). Same behavior as before PR-U9.
    if (got === 0) {
      preflightSt.textContent = `${trackingChip} — step into frame, then tap Start.`;
    } else if (got < expected) {
      preflightSt.textContent = `${trackingChip} — almost there. Tap Start when ready.`;
    } else {
      preflightSt.textContent = `${trackingChip} — ready. Tap Start.`;
    }
    if (got >= 1) {
      preflightGo.classList.add("ready");
    } else {
      preflightGo.classList.remove("ready");
    }
  }

  preflightGo.onclick = () => {
    // First user gesture: warm up speechSynthesis (Safari autoplay gate).
    if (poseVoiceEnabled()) { try { speakNow("Set 1 of " + guided.totalSets); } catch (_) {} }
    startSet();
  };

  // ── Pose payload handler (hot path, ~30fps) ─────────────────────────────
  function onPosePayload(payload) {
    const overall = statusFromMetrics(payload.metrics);
    pulseEl.dataset.status = overall;
    setInCallFormStatus(overall);
    renderPoseMetrics(metricsEl, payload, item.ex);
    renderPoseWarnings(warningsEl, payload);

    // SINGLE-SOURCE hero counter. pose.js publishes the authoritative running
    // count on EVERY frame via payload.repSummary.repCount — the same value the
    // metrics reps-pill reads. Driving the hero from it here is the fix for the
    // stale "Rep 0": the hero now tracks detected reps through preflight and
    // rest, so it can never sit frozen behind the working pill. During the
    // active phase the more precise per-set writer (updateSetRepLabels) owns the
    // hero so the count resets cleanly at a set boundary; this path fills every
    // other phase. Still gated on real detected reps — repCount only advances
    // when pose.js completes a rep.
    if (guided.phase !== "active") {
      const heroCount = payload.repSummary ? payload.repSummary.repCount : 0;
      const total = guided.repsPerSet || 0;
      setInCallRep(heroCount, total ? `of ${total}` : "reps");
    }

    if (guided.phase === "preflight") {
      handlePreflight(payload);
      return;
    }
    if (guided.phase !== "active") return;

    // Per-rep dedupe-set rollover (inRep true → false).
    rolloverRepThrottle(
      { spokenKeys: guided.spokenCorrectionsThisRep },
      guided.lastInRep,
      !!payload.inRep,
    );
    guided.lastInRep = !!payload.inRep;

    // Correction TTS + bubble. Pure throttle decision in decideCorrectionCue.
    const nowTs = performance.now();
    const decision = decideCorrectionCue(
      {
        spokenKeys: guided.spokenCorrectionsThisRep,
        lastCueTs:  guided.lastCorrectionTs,
      },
      payload.checkTransitions || [],
      payload.corrections || {},
      nowTs,
      GUIDED.CORRECTION_GAP_MS,
    );
    if (decision) {
      guided.lastCorrectionTs = nowTs;
      // In a live Maya call the per-rep cue is echoed through Maya (below);
      // suppress the local Web-Speech mid-rep cue so the two voices don't
      // overlap. The on-screen correction bubble still renders either way.
      const inLiveCall = !!(tavusCall && tavusConvId);
      if (!inLiveCall || LOCAL_TTS_IN_CALL) speakNow(decision.cue);
      showCorrectionBubble(decision.cue, decision.status);
    }

    // Rep events: append, speak count, redraw the recent-reps list. This is
    // the SINGLE count source — it advances ONLY when pose.js emitted a real
    // detected rep this frame (repEvents.length > 0). Standing still produces
    // no repEvents, so the count never moves and Maya stays silent.
    const events = payload.repEvents || [];
    if (events.length) {
      for (const ev of events) {
        guided.repsHistoryThisSet.push(ev);
        guided.repsHistoryAll.push(ev);
      }
      const repsThis = guided.repsHistoryThisSet.length;
      if (repsThis !== guided.lastSpokenCount) {
        guided.lastSpokenCount = repsThis;
        // In a live Maya call, Maya is the voice: echo the detected count
        // (and a form cue when the rep's form was off) through her instead of
        // the local Web-Speech backstop, which is gated off to avoid two
        // overlapping voices. The spoken number == the detected rep number.
        const inLiveCall = !!(tavusCall && tavusConvId);
        if (inLiveCall) {
          const lastEv = events[events.length - 1];
          const cue =
            lastEv && lastEv.status && lastEv.status !== "good"
              ? (payload.corrections?.[correctionKeyOf(lastEv.metricId)] || lastEv.msg)
              : null;
          echoMayaCount(spokenCount(repsThis), cue);
        }
        if (!inLiveCall || LOCAL_TTS_IN_CALL) {
          speakNow(spokenCount(repsThis));
        }
      }
      updateSetRepLabels();
      renderPoseSession(sessionEl, guided.repsHistoryAll, payload.repSummary);
    } else {
      updateSetRepLabels();
    }

    if (payload.warnings && payload.warnings.length) {
      for (const w of payload.warnings) guided.warningsAll.push(w);
    }

    // End-of-set: explicit setComplete flag from pose.js, or fallback when
    // we have a target rep count.
    if (payload.setComplete && guided.phase === "active") {
      endSet();
    } else if (
      guided.repsPerSet &&
      guided.repsHistoryThisSet.length >= guided.repsPerSet &&
      guided.phase === "active"
    ) {
      endSet();
    }
  }

  try {
    // PR-U8: pose.js EXERCISES is keyed by canonical library IDs. My plan
    // exercises carry a separate `library_id` resolved server-side by
    // exercise_kb.resolve_to_library; fall back to id if unset (Browse all
    // items + legacy library exercises whose id IS the library id).
    const poseExId = item.ex.library_id || item.ex.id;
    // Camera coexistence: if a live Maya call is up, feed pose.js the SAME
    // local camera track the call already owns so we don't open a second
    // getUserMedia (single physical-camera consumer). Omitted (null) on the
    // plain gallery form-check path → pose.js opens its own camera as before.
    const liveStream = _tavusLocalStream();
    const startOpts = {
      exerciseName:          item.ex.name,
      targetDose:            item.ex.default_dose,
      voice:                 speakCue,
      suppressInternalVoice: true,  // PR-J wrapper drives all voice
    };
    if (liveStream) startOpts.stream = liveStream;
    await window.PoseFormCheck.start(
      videoEl,
      canvasEl,
      poseExId,
      onPosePayload,
      startOpts,
    );
    btn.disabled = false;
    btn.dataset.state = "on";
    btn.textContent = "Stop";
  } catch (e) {
    btn.disabled = false;
    btn.textContent = "Form Check";
    document.body.classList.remove("pose-active");
    showToast(`Camera error: ${e.message}`, "error");
    if (wrap.classList.contains("incall-set-mount")) {
      _resetInCallSetUi();
    } else {
      const activeIdx = Array.from(wrap.querySelectorAll(".gallery-thumb-btn"))
        .findIndex((b) => b.classList.contains("active"));
      switchGalleryItem(activeIdx >= 0 ? activeIdx : 0);
    }
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
  start_intake_tool:         "intake \u2192 captured",
  fire_symptom_trigger:      "symptom \u2192 review queue",
  fire_intake_trigger:       "intake reset",
  fire_checkin_trigger:      "check-in \u2192 review queue",
  fire_weekly_plan_trigger:  "weekly plan \u2192 review queue",
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

  // Drop any "Coach Maya is on it..." transient as soon as a real coach
  // bubble is about to render - they would otherwise stack visually.
  hideCoachWorkingIndicator();

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
      break;

    case "tool_result":
      // fire_*_trigger results carry pending_protocol_id (and optionally
      // an "ok: false" + error string when the drafter or save_pending
      // failed). Library lookup tools (recommend_exercise / list_phase_exercises)
      // emit a card event already and don't reach this branch with an id.
      if (event.result) {
        const r = event.result;
        const flowLabel = r.flow ? r.flow.replace(/_/g, " ") : "draft";
        if (r.pending_protocol_id) {
          renderToolResultLine(event);
          renderPendingProtocolCard(r.pending_protocol_id, r.summary, flowLabel);
          refreshProtocol();
          // Refresh the trust pill so the header flips from "none" to
          // "pending_review" without requiring a page reload.
          refreshPatientState({ openModalIfNeeded: false }).catch(() => {});
        } else if (r.ok === false && r.error) {
          renderPendingProtocolError(r.error, flowLabel);
        }
      }
      break;

    case "triage_alert":
      // PR-H: symptom classifier flagged the patient's message for clinician
      // attention. Render a system message with action guidance + clinic
      // phone if configured. ALWAYS surface this — even if the writer
      // failed — so a patient with severe symptoms knows to call urgent
      // care instead of waiting for an asynchronous PT response.
      renderTriageAlert(event);
      // Also refresh the trust pill: backend just wrote a
      // needs_clinician_review row.
      refreshPatientState({ openModalIfNeeded: false }).catch(() => {});
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
  else if (args.checkin_text) detail = `"${truncate(args.checkin_text, 50)}"`;
  else if (args.reason) detail = `"${truncate(args.reason, 50)}"`;
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
  const phase = result.phase ? `${escapeHtml(result.phase)} wk ${escapeHtml(String(result.week ?? "?"))}` : "queued";
  const idTail = result.pending_protocol_id
    ? `<span class="tool-id">#${escapeHtml(String(result.pending_protocol_id).slice(0, 8))}</span>`
    : "";
  line.innerHTML = `
    <span class="tool-glyph">[draft]</span>
    <span>${phase}</span>
    ${idTail}
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
  attachChatCardFormCheckBtn(wrap, card);
  scrollChatLog();
}

// Attach the "Start guided form-check" button to a chat-rendered exercise
// card (the static card produced by renderExerciseCard, distinct from the
// gallery cards in renderExerciseGallery). Form-check used to live on the
// gallery only — that meant the symptom-adjustment Wall Sit card in chat
// rendered with cues + "Add to today" but no path into the pose feature.
// PR-A dropped the ?pose=1 URL flag; this wires the chat-card surface so
// every supported exercise (PoseFormCheck.EXERCISES key) gets the CTA on
// every render. Camera permission is requested at click time, not on
// page load.
function attachChatCardFormCheckBtn(wrap, card) {
  if (!window.PoseFormCheck) {
    console.warn("PoseFormCheck not loaded - pose.js failed to initialize");
    return;
  }
  // PR-U8: chat cards may carry a planner-generated id; prefer library_id
  // when set so the form-check button shows on regressed-but-resolvable
  // exercises (consistent with maybeAttachFormCheckBtn).
  const exId = card.library_id || card.id || card.name;
  // P1.1: backend's form_check_supported is source of truth; EXERCISES
  // membership is a safety check against stale frontend bundles.
  const supported = card.form_check_supported === true;
  if (!supported) return;
  if (!exId || !window.PoseFormCheck.EXERCISES?.[exId]) return;
  const actions = wrap.querySelector(".exercise-actions");
  if (!actions || actions.querySelector(".pose-form-check-btn")) return;
  const btn = document.createElement("button");
  btn.type = "button";
  btn.className = "pose-form-check-btn";
  btn.dataset.state = "off";
  btn.textContent = "Start guided form-check";
  btn.title = "Use your webcam for live rep + alignment feedback";
  // togglePoseFormCheck expects a `wrap` whose internal #galleryVideoWrap
  // is replaced with the camera frame. Chat cards don't have that
  // structure; we build a single-item shim so the existing toggle logic
  // works unchanged. The shim's #galleryVideoWrap replaces the
  // .exercise-video-placeholder block on this card.
  const item = {
    ex: card,
    genUrl: card.generated_video_url || "",
    ytId: card.youtube_id || "",
    watchUrl: card.youtube_watch_url || "",
    thumb: card.thumbnail_url || "",
  };
  // Tag the placeholder so togglePoseFormCheck can find it via the same
  // #galleryVideoWrap selector used for gallery cards.
  const placeholder = wrap.querySelector(".exercise-video-placeholder");
  if (placeholder) placeholder.id = "galleryVideoWrap";
  btn.onclick = () => togglePoseFormCheck(wrap, item, btn);
  actions.insertBefore(btn, actions.firstChild);
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

// ── Auto check-in card (PR-N) ────────────────────────────────────────────────
//
// Rendered after a guided pose-form-check session completes and the
// /pose/session POST returns success. Pre-fills the pain dot from the
// just-finished set's worst rep (bad -> 5, warn -> 3, else -> 1) and the
// RPE dot at 5. Patient adjusts via clickable scales, optionally adds a
// note, and clicks "Log this check-in" -> POST /checkins. Card persists
// in chat scroll until the patient interacts (no auto-dismiss).

const AUTO_CHECKIN_PAIN_PREFILL = { bad: 5, warn: 3, good: 1 };
const AUTO_CHECKIN_RPE_DEFAULT = 5;
const AUTO_CHECKIN_NOTES_MAX = 200;  // UI cap; backend sanitizes/truncates to 500

function renderAutoCheckinCard({ sessionId, worstStatus, exerciseName }) {
  const log = document.getElementById("chatLog");
  if (!log) return;

  const initialPain =
    AUTO_CHECKIN_PAIN_PREFILL[worstStatus] ?? AUTO_CHECKIN_PAIN_PREFILL.good;
  const state = {
    pain: initialPain,
    rpe: AUTO_CHECKIN_RPE_DEFAULT,
    notes: "",
    sessionId: sessionId || null,
    submitted: false,
  };

  const card = document.createElement("div");
  card.className = "auto-checkin-card";
  card.setAttribute("role", "group");
  card.setAttribute("aria-label", "Post-set check-in");

  const exLabel = exerciseName ? ` after ${escapeHtml(exerciseName)}` : "";

  card.innerHTML = `
    <div class="auto-checkin-header">
      <span class="auto-checkin-title">Set complete${exLabel}. How did that feel?</span>
    </div>

    <div class="auto-checkin-row">
      <label class="auto-checkin-label">Pain right now (0-10)</label>
      <div class="auto-checkin-scale" data-scale="pain" role="radiogroup" aria-label="Pain level"></div>
      <span class="auto-checkin-value" data-value="pain">${initialPain}</span>
    </div>

    <div class="auto-checkin-row">
      <label class="auto-checkin-label">Effort (RPE 1-10)</label>
      <div class="auto-checkin-scale" data-scale="rpe" role="radiogroup" aria-label="Effort level"></div>
      <span class="auto-checkin-value" data-value="rpe">${AUTO_CHECKIN_RPE_DEFAULT}</span>
    </div>

    <div class="auto-checkin-row notes">
      <label class="auto-checkin-label" for="autoCheckinNotes">Notes (optional)</label>
      <input id="autoCheckinNotes" class="auto-checkin-notes-input" type="text"
             maxlength="${AUTO_CHECKIN_NOTES_MAX}"
             placeholder="Anything to flag for Maya?" />
    </div>

    <div class="auto-checkin-actions">
      <button class="auto-checkin-submit" type="button">Log this check-in</button>
    </div>
    <div class="auto-checkin-status" hidden></div>
  `;

  // Build pain scale (0-10) and rpe scale (1-10). Each dot is a tap
  // target ≥32x32 (CSS).
  const buildScale = (key, min, max, initial) => {
    const wrap = card.querySelector(`[data-scale="${key}"]`);
    for (let i = min; i <= max; i++) {
      const dot = document.createElement("button");
      dot.type = "button";
      dot.className = `auto-checkin-dot ${classForPain(key, i)}`;
      dot.setAttribute("role", "radio");
      dot.setAttribute("aria-label", `${key} ${i}`);
      dot.setAttribute("aria-checked", String(i === initial));
      dot.dataset.value = String(i);
      if (i === initial) dot.classList.add("selected");
      dot.addEventListener("click", () => {
        if (state.submitted) return;
        state[key] = i;
        wrap.querySelectorAll(".auto-checkin-dot").forEach((d) => {
          const isSel = Number(d.dataset.value) === i;
          d.classList.toggle("selected", isSel);
          d.setAttribute("aria-checked", String(isSel));
        });
        const valEl = card.querySelector(`[data-value="${key}"]`);
        if (valEl) valEl.textContent = String(i);
      });
      wrap.appendChild(dot);
    }
  };
  buildScale("pain", 0, 10, initialPain);
  buildScale("rpe", 1, 10, AUTO_CHECKIN_RPE_DEFAULT);

  const notesInput = card.querySelector("#autoCheckinNotes");
  notesInput.addEventListener("input", () => {
    state.notes = notesInput.value;
  });

  const submitBtn = card.querySelector(".auto-checkin-submit");
  const statusEl = card.querySelector(".auto-checkin-status");

  const renderRetry = (errMsg) => {
    statusEl.hidden = false;
    statusEl.className = "auto-checkin-status error";
    statusEl.innerHTML = "";
    const msg = document.createElement("span");
    msg.textContent = errMsg || "Couldn't save check-in. Try again.";
    statusEl.appendChild(msg);
    const retry = document.createElement("button");
    retry.type = "button";
    retry.className = "auto-checkin-retry";
    retry.textContent = "Retry";
    retry.addEventListener("click", () => {
      statusEl.hidden = true;
      doSubmit();
    });
    statusEl.appendChild(retry);
    submitBtn.disabled = false;
    submitBtn.textContent = "Log this check-in";
  };

  const renderLogged = () => {
    state.submitted = true;
    // Replace card body with a single confirmation row using Unicode check.
    card.innerHTML = `<div class="auto-checkin-logged">Logged. ✓</div>`;
    // PR-M flow stitching: if the patient is mid-session (started via the
    // post-approval CTA), chain the next-exercise prompt off the logged
    // check-in. No-op when the patient triggered form-check standalone
    // (todaysSessionState.active === false).
    try {
      advanceToNextExercise();
    } catch (e) {
      console.warn("advanceToNextExercise failed:", e);
    }
  };

  async function doSubmit() {
    submitBtn.disabled = true;
    submitBtn.textContent = "Saving...";
    statusEl.hidden = true;
    try {
      const res = await postAutoCheckin({
        pain_level: state.pain,
        rpe: state.rpe,
        notes: state.notes ? state.notes.slice(0, AUTO_CHECKIN_NOTES_MAX) : null,
        associated_session_id: state.sessionId,
      });
      if (res.status === 401) {
        showToast("Sign in to save your check-in", "info");
        renderRetry("Sign in to save your check-in.");
        return;
      }
      if (!res.ok) {
        const txt = await res.text().catch(() => "");
        console.warn("postAutoCheckin failed:", res.status, txt);
        renderRetry();
        return;
      }
      renderLogged();
      showToast("Check-in logged", "info");
    } catch (e) {
      console.warn("postAutoCheckin threw:", e);
      renderRetry();
    }
  }

  submitBtn.addEventListener("click", doSubmit);

  log.appendChild(card);
  scrollChatLog();
  return card;
}

// Color class for a scale dot. Pain: 0-2 good (green), 3-6 warn (amber),
// 7-10 danger (red). RPE: same shape — low effort green, high red.
function classForPain(key, value) {
  if (key === "pain") {
    if (value <= 2) return "tone-good";
    if (value <= 6) return "tone-warn";
    return "tone-danger";
  }
  // rpe: 1-3 good, 4-7 warn, 8-10 danger
  if (value <= 3) return "tone-good";
  if (value <= 7) return "tone-warn";
  return "tone-danger";
}

async function postAutoCheckin(payload) {
  return authedFetch(`${API_BASE}/checkins`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
}

// ── PR-M: Flow stitching after clinician approval ───────────────────────────
//
// After a clinician approves a draft protocol, walk the patient through
// pick → guided → check-in as a single stepped flow rather than three
// disconnected UI surfaces. Trigger condition:
//
//   review_status.state === "recently_approved"
//   AND no /sessions/today rows have status === "completed"
//
// Architecture:
//   * todaysSessionState holds the session position (in-memory; lost on
//     reload — acceptable for v1, patient can pick up from the gallery).
//   * The "Start today's session?" CTA renders both inline at the top of
//     the chat scroll and inside the sidebar today-session card.
//   * Click → fetch /protocol/exercises → render an in-chat picker.
//   * Click an exercise (or "Start with first") → render its library card
//     in chat AND auto-trigger the form-check button on it. Reuses
//     renderExerciseCard + togglePoseFormCheck (no fork of the pose flow).
//   * When the auto check-in card from PR-N logs successfully, advanceToNextExercise()
//     fires the "Next exercise?" CTA (or workout-complete summary if done).
//
// PHI hygiene: console logs only carry session position + exercise_id.
// Patient name / symptom text never reach the console.

const _todaysSessionState = {
  active: false,            // true once startTodaysSession() runs
  exercises: [],            // [{id, name, default_dose, cues, ...}] from /protocol/exercises
  currentIdx: 0,            // 0-based pointer into exercises
  completedIds: [],         // exercise_ids the user finished + checked in for
  skipPick: false,          // "Skip pick — start all" mode: skip intermediate picker
  startedAtMs: null,        // performance.now() at startTodaysSession; powers totalTimeMin
};

// Re-entrancy lock for refreshTodaysFlowCTA. Supabase onChange fires multiple
// times per load (INITIAL_SESSION -> SIGNED_IN/TOKEN_REFRESHED), each firing
// refreshPatientState -> refreshTodaysFlowCTA. Without this, two invocations
// both pass the teardown (DOM empty), both await refreshTodaySession, then
// both append a CTA — rendering two stacked "Your plan is ready" cards.
let _todaysFlowCtaRendering = false;

// Pure helper. Given the current state, return the next exercise to play
// (or null if done). Also returns whether the workout is complete.
// Exposed on window.__flowHelpers for the unit test.
function nextExerciseAfter(state) {
  const exercises = state.exercises || [];
  const nextIdx = (state.currentIdx ?? -1) + 1;
  if (!exercises.length) return { done: true, exercise: null, nextIdx };
  if (nextIdx >= exercises.length) return { done: true, exercise: null, nextIdx };
  return { done: false, exercise: exercises[nextIdx], nextIdx };
}

// Pure helper. Normalize a /protocol/exercises payload into picker rows.
// Filters out exercises without a usable id (defensive — every row should
// have one, but if /protocol/exercises returns malformed data we skip
// rather than render an unclickable card).
function buildPickerItems(exercises) {
  if (!Array.isArray(exercises)) return [];
  return exercises
    .filter((ex) => ex && (ex.id || ex.name))
    .map((ex) => ({
      id: ex.id || ex.name,
      name: ex.name || ex.id || "exercise",
      default_dose: ex.default_dose || ex.spec || "",
      cues: ex.cues || [],
      generated_video_url: ex.generated_video_url || "",
      youtube_id: ex.youtube_id || "",
      youtube_watch_url: ex.youtube_watch_url || "",
      thumbnail_url: ex.thumbnail_url || "",
    }));
}

if (typeof window !== "undefined") {
  window.__flowHelpers = { nextExerciseAfter, buildPickerItems };
}

// ---------------------------------------------------------------------------
// PR-R: state-aware Maya greeting
//
// On auth-resolve, before the patient types anything, render a greeting
// bubble in the chat log that reflects the patient's server-derived state
// (needs_intake / needs_plan / ready) and review_status (pending_review /
// recently_approved / recently_rejected). Returning patients are addressed
// by display_name and prompted with a check-in question; new patients see
// a clear intake CTA.
//
// The selector below is pure (no DOM, no fetch) so it round-trips cleanly
// in the Node test under frontend/tests/state_aware_greeting.test.js.
// renderStateAwareGreeting wraps the selector + the actual DOM write.
// ---------------------------------------------------------------------------

// Module-scope guard so the greeting renders ONCE per page session even if
// onChange fires multiple times (e.g. token refresh).
let _stateAwareGreetingShown = false;

// Threshold for the "Good to see you again — N days" prepend. 24h matches
// the spec; we explicitly do NOT prepend for sub-24h returns to avoid
// pestering same-day users.
const GREETING_DAYS_GAP_MS = 24 * 60 * 60 * 1000;

// LocalStorage key for the last time chat was opened — backend last_active
// is the authoritative source, but we also persist client-side as a fallback
// when the user is in demo mode or the backend field is null.
const GREETING_LAST_CHAT_AT_KEY = "rehab_last_chat_at";

/**
 * Return the time since lastActiveIso in whole days, or null when the
 * input is missing/unparseable or the gap is under 24h. Pure helper.
 *
 * @param {string|null|undefined} lastActiveIso
 * @param {number} [nowMs]  override for tests
 * @returns {number|null}
 */
function daysSinceLastActive(lastActiveIso, nowMs) {
  if (!lastActiveIso) return null;
  const ts = Date.parse(lastActiveIso);
  if (Number.isNaN(ts)) return null;
  const now = typeof nowMs === "number" ? nowMs : Date.now();
  const deltaMs = now - ts;
  if (deltaMs < GREETING_DAYS_GAP_MS) return null;
  return Math.floor(deltaMs / (24 * 60 * 60 * 1000));
}

/**
 * Sanitize a display name. Returns null for empty/whitespace/non-string.
 * Trims and caps at 60 chars to defend against pathological intake input.
 *
 * @param {*} raw
 * @returns {string|null}
 */
function sanitizeDisplayName(raw) {
  if (typeof raw !== "string") return null;
  const trimmed = raw.trim();
  if (!trimmed) return null;
  return trimmed.length > 60 ? trimmed.slice(0, 60) : trimmed;
}

/**
 * Build the greeting copy for the given patient state. Pure — no DOM, no
 * fetch, deterministic given (status, options).
 *
 * State matrix:
 *   needs_intake -> intake CTA (no welcome-back, no days-away prepend)
 *   needs_plan + pending_review/needs_clinician_review -> "plan with PT"
 *   needs_plan otherwise -> "intake done, want a draft?"
 *   ready + recently_approved -> "PT just approved, start session?"
 *   ready + recently_rejected -> "PT had notes, see chat"
 *   ready otherwise -> generic returning welcome
 *
 * If daysAway (>=1) is provided, prepends the "Good to see you again"
 * line — but ONLY for non-needs_intake states (a new patient hasn't been
 * "away," they're new).
 *
 * @param {object} status  /patient/me/intake-status response
 * @param {object} [opts]
 * @param {number|null} [opts.daysAway]
 * @returns {string}
 */
function selectGreetingCopy(status, opts) {
  const o = opts || {};
  const daysAway = typeof o.daysAway === "number" && o.daysAway >= 1
    ? o.daysAway
    : null;

  const state = status && status.state;
  const reviewState = (status && status.review_status && status.review_status.state) || null;
  const name = sanitizeDisplayName(status && status.display_name);
  // "Welcome back, Andre." vs "Welcome back." when name is null.
  const welcomeBack = name ? `Welcome back, ${name}.` : "Welcome back.";

  let body;
  if (state === "needs_intake") {
    // Brand-new patient — no "welcome back" and no days-away prepend.
    return (
      "Hi, I'm Coach Maya — your AI rehab partner. To build your plan, "
      + "I'll ask a few quick questions about your injury. Tap Start "
      + "intake below, or just tell me about your injury here in chat. "
      + "I work with knee, ankle, hip, low-back, shoulder, and elbow rehab."
    );
  } else if (state === "needs_plan") {
    if (
      reviewState === "pending_review"
      || reviewState === "needs_clinician_review"
    ) {
      body = (
        `${welcomeBack} Your draft plan is with your PT for review — `
        + "they'll have eyes on it shortly. While we wait, anything new "
        + "today? Pain, swelling, sleep changes, or did anything aggravate "
        + "the injury?"
      );
    } else {
      body = (
        `${welcomeBack} Your intake is in. Want me to draft your first `
        + "weekly plan? Tap Draft next week or just say so here."
      );
    }
  } else if (state === "ready") {
    if (reviewState === "recently_approved") {
      body = (
        `${welcomeBack} Your PT just approved your plan. Want to start `
        + "today's session, log a check-in, or talk about how this week's "
        + "going?"
      );
    } else if (reviewState === "recently_rejected") {
      body = (
        `${welcomeBack} Your PT had some notes on your last draft — see `
        + "the chat for details. We can revisit when you're ready."
      );
    } else {
      body = (
        `${welcomeBack} How are you doing? Want to log a check-in, browse `
        + "exercises, or talk about progress?"
      );
    }
  } else {
    // Unknown state — defensive fallback that doesn't fake a state.
    body = "Hi, I'm Coach Maya. How can I help you today?";
  }

  if (daysAway && state !== "needs_intake") {
    const dayWord = daysAway === 1 ? "day" : "days";
    return `Good to see you again — it's been ${daysAway} ${dayWord}. ${body}`;
  }
  return body;
}

if (typeof window !== "undefined") {
  window.__greetingHelpers = {
    daysSinceLastActive,
    sanitizeDisplayName,
    selectGreetingCopy,
  };
}

/**
 * Render the state-aware Maya greeting once per page session.
 *
 * Pulls the latest /patient/me/intake-status (already cached in
 * patientState by refreshPatientState), picks the copy via
 * selectGreetingCopy, and appends a coach bubble. Idempotent — guarded
 * by _stateAwareGreetingShown.
 *
 * No silent fallbacks: if patientState is missing (anon / fetch error),
 * we log + skip; the existing chat-empty surface stays as-is so the
 * patient still has a usable chat input.
 */
function renderStateAwareGreeting() {
  if (_stateAwareGreetingShown) return;
  if (!window.RehabAuth || !window.RehabAuth.getJwt || !window.RehabAuth.getJwt()) {
    // Anon / demo mode — keep the legacy empty state.
    return;
  }
  if (!patientState || !patientState.state) {
    console.warn("state-aware greeting skipped: no patientState");
    return;
  }

  // Compute days-away. Prefer backend last_active (authoritative); fall
  // back to localStorage when backend returns null (e.g. flat-file or
  // sqlite dev backend on the very first auth-status call).
  let daysAway = daysSinceLastActive(patientState.last_active);
  if (daysAway === null) {
    daysAway = daysSinceLastActive(
      localStorage.getItem(GREETING_LAST_CHAT_AT_KEY),
    );
  }

  const copy = selectGreetingCopy(patientState, { daysAway });

  // PHI hygiene: log state enum + review_state only. Never log copy or
  // display_name.
  console.info(
    "state-aware greeting state=%s review_state=%s days_away=%s",
    patientState.state,
    (patientState.review_status && patientState.review_status.state) || "none",
    daysAway === null ? "n/a" : String(daysAway),
  );

  // Replace the static empty-state node so the greeting reads as the
  // first message rather than appearing below an empty-state placeholder.
  const empty = document.getElementById("chatEmpty");
  if (empty) empty.remove();

  appendChatBubble("coach", copy);
  _stateAwareGreetingShown = true;

  // Stamp localStorage so the next session can compute daysAway even if
  // the backend last_active is unavailable.
  try {
    localStorage.setItem(
      GREETING_LAST_CHAT_AT_KEY,
      new Date().toISOString(),
    );
  } catch (_) {
    // Private mode / quota exceeded — non-fatal.
  }
}

if (typeof window !== "undefined") {
  // Expose for manual repro / e2e harness.
  window.renderStateAwareGreeting = renderStateAwareGreeting;
}

// Refresh the "Start today's session" CTA. Decides visibility from
// patientState + today's session list; renders into both the chat scroll
// and the sidebar today-session card. Idempotent — safe to call on every
// patient-state poll.
async function refreshTodaysFlowCTA() {
  // Serialize concurrent invocations (see _todaysFlowCtaRendering above). A
  // second overlapping call would otherwise append a duplicate CTA across the
  // `await refreshTodaySession()` below. Skipping it is safe — the in-flight
  // call renders from current state, and any later state change re-invokes.
  if (_todaysFlowCtaRendering) return;
  _todaysFlowCtaRendering = true;
  try {
    const sidebarSlot = document.getElementById("todaysFlowSidebarCTA");
    const chatLog = document.getElementById("chatLog");

    // Tear down stale CTAs first; we always rebuild from current state.
    if (sidebarSlot) {
      sidebarSlot.innerHTML = "";
      sidebarSlot.hidden = true;
    }
    const existingChatCta = document.getElementById("todaysFlowChatCTA");
    if (existingChatCta) existingChatCta.remove();

    // Don't show the CTA mid-flow — once the patient clicks Start, the picker
    // owns the chat scroll. The flow itself surfaces the next-exercise CTA.
    if (_todaysSessionState.active) return;

    const reviewState = patientState?.review_status?.state;
    if (reviewState !== "recently_approved") return;

    // Today's session: refresh if we don't already have a fresh mirror, then
    // gate on completion count. If any row is completed today, the patient
    // has already started — don't double-prompt.
    if (window.RehabAuth?.getJwt?.()) {
      try {
        await refreshTodaySession();
      } catch (e) {
        console.warn("refreshTodaysFlowCTA: refreshTodaySession failed:", e);
      }
    }
    const completedToday = todaySession.filter((s) => s.status === "completed").length;
    if (completedToday > 0) return;

    // Render inline CTA at the top of the chat scroll.
    if (chatLog) {
      // Atomic teardown-before-insert: defends against any stale node that
      // slipped in across the await above (belt-and-suspenders with the lock).
      const stale = document.getElementById("todaysFlowChatCTA");
      if (stale) stale.remove();
      const cta = document.createElement("div");
      cta.id = "todaysFlowChatCTA";
      cta.className = "todays-session-cta";
      cta.innerHTML = `
        <div class="todays-session-cta-title">Your plan is ready.</div>
        <div class="todays-session-cta-sub">Start today's session?</div>
        <button type="button" class="todays-session-cta-btn">Start session →</button>
      `;
      cta.querySelector("button").addEventListener("click", startTodaysSession);
      // Insert right after the chat-empty placeholder if present, else prepend.
      const empty = document.getElementById("chatEmpty");
      if (empty && empty.parentElement === chatLog) {
        empty.insertAdjacentElement("afterend", cta);
      } else {
        chatLog.insertBefore(cta, chatLog.firstChild);
      }
    }

    // Mirror in the sidebar.
    if (sidebarSlot) {
      sidebarSlot.hidden = false;
      sidebarSlot.innerHTML = `
        <button type="button" class="todays-session-sidebar-btn">
          Start today's session →
        </button>
      `;
      sidebarSlot.querySelector("button").addEventListener("click", startTodaysSession);
    }
  } finally {
    _todaysFlowCtaRendering = false;
  }
}

// Click handler for both CTAs. Fetches the active protocol's exercises
// from /protocol/exercises, populates _todaysSessionState, and renders
// the in-chat picker. Surfaces a friendly toast on failure (no silent
// fallback — an empty picker would confuse the patient).
async function startTodaysSession() {
  if (_todaysSessionState.active) return;
  // Tear down the entry CTAs immediately so the picker has a clean slate.
  const sidebarSlot = document.getElementById("todaysFlowSidebarCTA");
  if (sidebarSlot) { sidebarSlot.hidden = true; sidebarSlot.innerHTML = ""; }
  const chatCta = document.getElementById("todaysFlowChatCTA");
  if (chatCta) chatCta.remove();

  showCoachWorkingIndicator();
  try {
    const res = await authedFetch(`${API_BASE}/protocol/exercises`);
    if (!res.ok) throw new Error(`status ${res.status}`);
    const data = await res.json();
    const items = buildPickerItems(data.exercises || []);
    if (!items.length) {
      hideCoachWorkingIndicator();
      showToast("No exercises in your protocol yet — check back after your PT updates it.", "info");
      return;
    }
    _todaysSessionState.active = true;
    _todaysSessionState.exercises = items;
    _todaysSessionState.currentIdx = -1;
    _todaysSessionState.completedIds = [];
    _todaysSessionState.skipPick = false;
    _todaysSessionState.startedAtMs = (typeof performance !== "undefined" ? performance.now() : Date.now());
    console.info("[flow-m] startTodaysSession exercises=%d", items.length);
    hideCoachWorkingIndicator();
    renderExercisePicker();
  } catch (e) {
    hideCoachWorkingIndicator();
    console.warn("startTodaysSession failed:", e);
    showToast(`Couldn't load today's plan: ${e.message || e}`, "error");
  }
}

// Render the intermediate exercise picker into the chat scroll. Lists
// every exercise in the active protocol with a checkbox-style row and
// two affordances: "Start with first exercise →" and "Skip pick — start all".
// The picker is the canonical entry into each guided session; clicking
// any single exercise jumps directly to that one (and the rest of the
// flow loops from there).
function renderExercisePicker() {
  const log = document.getElementById("chatLog");
  if (!log) return;
  // Drop any prior picker (re-renders happen if the patient bails + restarts).
  const prev = document.getElementById("exercisePickerCard");
  if (prev) prev.remove();

  const card = document.createElement("div");
  card.id = "exercisePickerCard";
  card.className = "exercise-picker-card";

  const items = _todaysSessionState.exercises;
  const completed = new Set(_todaysSessionState.completedIds);
  const remaining = items.filter((e) => !completed.has(e.id));
  const total = items.length;
  const doneCount = items.length - remaining.length;

  const headline = doneCount === 0
    ? `Today's plan: ${total} exercise${total === 1 ? "" : "s"}`
    : `Today's plan: ${doneCount} of ${total} done — ${remaining.length} to go`;

  const rows = items.map((ex) => {
    const isDone = completed.has(ex.id);
    const dose = ex.default_dose ? `<span class="picker-row-dose">${escapeHtml(ex.default_dose)}</span>` : "";
    const checkGlyph = isDone ? "✓" : "";
    return `
      <button type="button" class="picker-row ${isDone ? "done" : ""}" data-ex-id="${escapeHtml(ex.id)}" ${isDone ? "disabled" : ""}>
        <span class="picker-row-check" aria-hidden="true">${checkGlyph}</span>
        <span class="picker-row-name">${escapeHtml(ex.name)}</span>
        ${dose}
      </button>
    `;
  }).join("");

  card.innerHTML = `
    <div class="exercise-picker-header">${escapeHtml(headline)}</div>
    <div class="exercise-picker-list">${rows}</div>
    <div class="exercise-picker-actions">
      <button type="button" class="picker-action primary" id="pickerStartFirst">
        ${doneCount === 0 ? "Start with first exercise →" : "Continue with next →"}
      </button>
      <button type="button" class="picker-action" id="pickerSkipPick">
        Skip pick — start all
      </button>
      <button type="button" class="picker-action ghost" id="pickerBail">
        Take a break, log later
      </button>
    </div>
  `;

  log.appendChild(card);

  // Wire row clicks: jump straight to that exercise.
  card.querySelectorAll(".picker-row").forEach((row) => {
    row.addEventListener("click", () => {
      const exId = row.dataset.exId;
      const idx = items.findIndex((e) => e.id === exId);
      if (idx < 0) return;
      _todaysSessionState.currentIdx = idx - 1;  // launch advances by 1
      _todaysSessionState.skipPick = false;
      launchCurrentExercise();
    });
  });

  card.querySelector("#pickerStartFirst").addEventListener("click", () => {
    // Start with the first not-yet-completed exercise.
    const firstUndoneIdx = items.findIndex((e) => !completed.has(e.id));
    if (firstUndoneIdx < 0) {
      // Everything done — bounce to the workout-complete card.
      renderWorkoutComplete();
      return;
    }
    _todaysSessionState.currentIdx = firstUndoneIdx - 1;
    _todaysSessionState.skipPick = false;
    launchCurrentExercise();
  });

  card.querySelector("#pickerSkipPick").addEventListener("click", () => {
    const firstUndoneIdx = items.findIndex((e) => !completed.has(e.id));
    if (firstUndoneIdx < 0) {
      renderWorkoutComplete();
      return;
    }
    _todaysSessionState.currentIdx = firstUndoneIdx - 1;
    _todaysSessionState.skipPick = true;
    launchCurrentExercise();
  });

  card.querySelector("#pickerBail").addEventListener("click", bailFlow);

  scrollChatLog();
}

// Advance currentIdx by 1 and render that exercise's card + auto-open
// the form-check. Called by both the picker (initial launch) and the
// "Continue →" CTA (loop iteration).
function launchCurrentExercise() {
  const next = nextExerciseAfter(_todaysSessionState);
  if (next.done) {
    renderWorkoutComplete();
    return;
  }
  _todaysSessionState.currentIdx = next.nextIdx;
  const ex = next.exercise;
  console.info("[flow-m] launchCurrentExercise idx=%d ex=%s", next.nextIdx, ex.id);

  // Drop the picker — it'll re-render on next exercise advance.
  const picker = document.getElementById("exercisePickerCard");
  if (picker) picker.remove();

  // Render the exercise card via the existing chat-card renderer. This
  // automatically attaches "Add to today" + "Start guided form-check"
  // (when EXERCISES has the id), so we get the full surface the same
  // way the gallery would.
  renderExerciseCard(ex);

  // Auto-trigger the form-check button. We have to wait for the next
  // tick because attachChatCardFormCheckBtn is sync but pose.init is
  // async; clicking immediately works because togglePoseFormCheck
  // handles the loading state internally.
  setTimeout(() => {
    // Find the most-recently-rendered exercise card with this id and
    // click its form-check button. We don't tag the card with the id,
    // so use lastElementChild that is .exercise-card.
    const log = document.getElementById("chatLog");
    if (!log) return;
    const cards = log.querySelectorAll(".exercise-card");
    const card = cards[cards.length - 1];
    if (!card) {
      console.warn("[flow-m] no exercise card found to auto-launch form-check");
      return;
    }
    const btn = card.querySelector(".pose-form-check-btn");
    if (!btn) {
      // P1.4: form_check_supported=false on the library means there is no
      // tuned check roster (e.g., stationary_bike). Render a "Self-paced"
      // pill + 2-line explanation so the patient knows this is intentional,
      // not a bug, and isn't waiting for camera prompts that never come.
      // Pin the pill onto the card title row when available.
      const titleRow = card.querySelector(".exercise-title-row");
      if (titleRow && !titleRow.querySelector(".self-paced-pill")) {
        const pill = document.createElement("span");
        pill.className = "self-paced-pill";
        pill.textContent = "Self-paced";
        pill.title = "No guided form-check for this exercise";
        titleRow.appendChild(pill);
      }
      const nudge = document.createElement("div");
      nudge.className = "flow-nudge self-paced-nudge";
      nudge.innerHTML = `
        <div class="self-paced-explainer">
          <strong>This exercise is self-paced.</strong>
          <span>Watch the demo, complete the prescribed sets, then tap Mark done.</span>
        </div>
        <button type="button" class="picker-action primary" id="flowMarkManualDone">Mark done</button>
      `;
      card.appendChild(nudge);
      nudge.querySelector("#flowMarkManualDone").addEventListener("click", () => {
        // No pose session, no auto check-in card. Mark complete + advance.
        const exId = ex.id;
        if (!_todaysSessionState.completedIds.includes(exId)) {
          _todaysSessionState.completedIds.push(exId);
        }
        renderNextExerciseCTA();
      });
      return;
    }
    btn.click();
  }, 0);

  scrollChatLog();
}

// Called from renderAutoCheckinCard's renderLogged when a patient
// finishes a check-in. If the flow is active, advance: mark current
// exercise as completed, then either render the next-exercise CTA
// (or skip straight to launching the next one in skipPick mode) or
// render the workout-complete summary.
function advanceToNextExercise() {
  if (!_todaysSessionState.active) return;
  const items = _todaysSessionState.exercises;
  const idx = _todaysSessionState.currentIdx;
  const current = items[idx];
  if (current && !_todaysSessionState.completedIds.includes(current.id)) {
    _todaysSessionState.completedIds.push(current.id);
  }
  console.info(
    "[flow-m] advanceToNextExercise idx=%d done=%d/%d",
    idx, _todaysSessionState.completedIds.length, items.length,
  );

  // All done?
  if (_todaysSessionState.completedIds.length >= items.length) {
    renderWorkoutComplete();
    return;
  }

  // skipPick mode: jump directly to next exercise without an interstitial.
  if (_todaysSessionState.skipPick) {
    launchCurrentExercise();
    return;
  }

  renderNextExerciseCTA();
}

function renderNextExerciseCTA() {
  const log = document.getElementById("chatLog");
  if (!log) return;
  const items = _todaysSessionState.exercises;
  const completed = new Set(_todaysSessionState.completedIds);
  const justFinished = items[_todaysSessionState.currentIdx];
  const nextIdx = items.findIndex((e) => !completed.has(e.id));
  const next = nextIdx >= 0 ? items[nextIdx] : null;

  if (!next) {
    renderWorkoutComplete();
    return;
  }

  const card = document.createElement("div");
  card.className = "next-exercise-cta";
  const finishedName = justFinished?.name || "exercise";
  const nextName = next.name || "next exercise";
  const nextDose = next.default_dose ? ` (${next.default_dose})` : "";
  const position = `${_todaysSessionState.completedIds.length + 1} of ${items.length}`;

  card.innerHTML = `
    <div class="next-exercise-row">
      <span class="next-exercise-check" aria-hidden="true">✓</span>
      <span class="next-exercise-finished">${escapeHtml(finishedName)} complete</span>
    </div>
    <div class="next-exercise-prompt">
      Next: ${escapeHtml(nextName)}${escapeHtml(nextDose)} (${escapeHtml(position)})?
    </div>
    <div class="next-exercise-actions">
      <button type="button" class="picker-action primary" data-action="continue">Continue →</button>
      <button type="button" class="picker-action ghost" data-action="bail">Take a break, log later</button>
    </div>
  `;

  card.querySelector('[data-action="continue"]').addEventListener("click", () => {
    card.remove();
    // Move currentIdx to one before next so launchCurrentExercise advances onto it.
    _todaysSessionState.currentIdx = nextIdx - 1;
    launchCurrentExercise();
  });
  card.querySelector('[data-action="bail"]').addEventListener("click", bailFlow);

  log.appendChild(card);
  scrollChatLog();
}

function renderWorkoutComplete() {
  const log = document.getElementById("chatLog");
  if (!log) return;
  const items = _todaysSessionState.exercises;
  const totalDone = _todaysSessionState.completedIds.length;
  const totalPlan = items.length;
  const startedMs = _todaysSessionState.startedAtMs;
  const nowMs = (typeof performance !== "undefined" ? performance.now() : Date.now());
  const elapsedMin = startedMs ? Math.max(1, Math.round((nowMs - startedMs) / 60000)) : null;

  // Pull form-warning summary from the last guided session if present —
  // pose.js stores warnings on the session row, but we don't have a
  // cross-session aggregator yet, so we just count the local state.
  const summaryLines = [
    `Today's session: ${totalDone} of ${totalPlan} exercise${totalPlan === 1 ? "" : "s"} ✓`,
  ];
  if (elapsedMin) summaryLines.push(`Total time: ${elapsedMin} min`);

  const card = document.createElement("div");
  card.className = "workout-complete-card";
  card.innerHTML = `
    <div class="workout-complete-title">Workout complete</div>
    <div class="workout-complete-summary">
      ${summaryLines.map((l) => `<div>${escapeHtml(l)}</div>`).join("")}
    </div>
    <div class="workout-complete-actions">
      <button type="button" class="picker-action primary" id="workoutCompleteRecord">View today's record</button>
    </div>
  `;
  card.querySelector("#workoutCompleteRecord").addEventListener("click", () => {
    // Open the recap modal with today's actual rows. Falls back to the
    // sidebar scroll if the modal markup is missing for any reason.
    if (document.getElementById("recapModal")) {
      openWorkoutRecapModal();
    } else {
      const todaySidebar = document.getElementById("todaySessionCard");
      if (todaySidebar) todaySidebar.scrollIntoView({ behavior: "smooth", block: "start" });
    }
  });

  log.appendChild(card);

  // Reset flow state so the entry CTA is suppressed (completedToday > 0
  // gates it via /sessions/today on next refresh).
  _todaysSessionState.active = false;
  _todaysSessionState.exercises = [];
  _todaysSessionState.currentIdx = -1;
  _todaysSessionState.completedIds = [];
  _todaysSessionState.skipPick = false;
  _todaysSessionState.startedAtMs = null;

  // Refresh today-session sidebar mirror (status flags should be up to date
  // from the /pose/session writes during the flow) and the CTA.
  refreshTodaySession().catch(() => {});
  refreshTodaysFlowCTA().catch(() => {});
  scrollChatLog();
}

// Patient bails out mid-flow. We keep the completedIds (already persisted
// via /sessions POSTs / /pose/session writes), drop the active flag so
// the entry CTA can re-render on next refresh, and clear the in-flight
// picker / next-CTA. Re-entry from the gallery still works.
function bailFlow() {
  console.info(
    "[flow-m] bailFlow done=%d/%d",
    _todaysSessionState.completedIds.length,
    _todaysSessionState.exercises.length,
  );
  _todaysSessionState.active = false;
  _todaysSessionState.skipPick = false;

  const picker = document.getElementById("exercisePickerCard");
  if (picker) picker.remove();
  document.querySelectorAll(".next-exercise-cta").forEach((n) => n.remove());

  showToast("Saved your progress. Pick up anytime from the exercise tab.", "info");
  // Don't re-show the post-approval CTA right away — the patient just
  // dismissed it. refreshTodaysFlowCTA() gates on completedToday from
  // /sessions/today; the CTA returns naturally if zero completed today.
}

// "Coach Maya is on it" transient. Quick-action buttons + sendChat() both
// invoke this so the patient sees an immediate visual ack while the LLM /
// chat-tool round-trip is in flight. Idempotent: calling twice in a row
// does not stack indicators.
let _coachWorkingTimer = null;
function showCoachWorkingIndicator() {
  const log = document.getElementById("chatLog");
  if (!log) return;
  let el = document.getElementById("coachWorkingIndicator");
  if (!el) {
    el = document.createElement("div");
    el.id = "coachWorkingIndicator";
    el.className = "chat-bubble coach thinking coach-working";
    el.textContent = "Coach Maya is on it...";
    log.appendChild(el);
  }
  scrollChatLog();
  // Watchdog: if nothing else hits the chat for 12s, drop the indicator so
  // it doesn't linger after a button that didn't actually fire a chat tool.
  if (_coachWorkingTimer) clearTimeout(_coachWorkingTimer);
  _coachWorkingTimer = setTimeout(hideCoachWorkingIndicator, 12_000);
}

function hideCoachWorkingIndicator() {
  if (_coachWorkingTimer) {
    clearTimeout(_coachWorkingTimer);
    _coachWorkingTimer = null;
  }
  const el = document.getElementById("coachWorkingIndicator");
  if (el) el.remove();
}

// ---------------------------------------------------------------------------
// Today's session (DB-backed via /sessions/*)
// ---------------------------------------------------------------------------
// Adds from the chat exercise cards land in public.sessions, scoped to the
// authenticated patient (RLS). The clinician dashboard reads the same rows
// for the adherence panel. Protocol changes still go through clinician
// review (chat-tool fires); "Add to today" is just "I plan to do this set
// today" - no protocol mutation.
//
// We keep an in-memory mirror so re-renders don't always hit the network,
// but truth lives on the server. On every meaningful event we re-fetch
// /sessions/today.

let todaySession = []; // mirror of /sessions/today; rows from session_repo

async function refreshTodaySession() {
  if (!window.RehabAuth?.getJwt?.()) {
    // No JWT: the in-memory array stays empty; the card stays hidden.
    todaySession = [];
    renderTodaySession();
    return;
  }
  try {
    const tz = (Intl?.DateTimeFormat?.().resolvedOptions().timeZone) || "UTC";
    const res = await authedFetch(`${API_BASE}/sessions/today`, {
      headers: { "X-Timezone": tz },
    });
    if (res.status === 401) {
      todaySession = [];
      renderTodaySession();
      return;
    }
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const data = await res.json();
    todaySession = (data.sessions || []).map((s) => ({
      id: s.id,
      exercise_id: s.exercise_id,
      name: s.exercise_id,  // friendly name lookup happens at render time
      status: s.status,
      planned_sets: s.planned_sets,
      planned_reps: s.planned_reps,
      // PR-T2: enrichment fields. is_current_region tells the renderer
      // whether to dim + label the row as "from a prior protocol." We
      // preserve the body_region literal so the label can show "prior:
      // knee" rather than just a generic dim.
      body_region: s.body_region || null,
      is_current_region: s.is_current_region === true,
    }));
    renderTodaySession();
  } catch (e) {
    console.warn("refreshTodaySession failed:", e);
  }
}

async function addToTodayFromBtn(btn) {
  const id   = btn.dataset.addId   || "";
  const name = btn.dataset.addName || id || "exercise";
  if (!id) return;

  // Optimistic UX: disable the button immediately. Re-fetch /sessions/today
  // on success so the sidebar reflects what the server actually accepted.
  if (todaySession.some((e) => e.exercise_id === id)) {
    showToast(`${name} is already in today's session`, "info");
    return;
  }
  if (!window.RehabAuth?.getJwt?.()) {
    showToast("Sign in to log this to your record", "info");
    return;
  }

  btn.disabled = true;
  const prevText = btn.textContent;
  btn.textContent = "Adding...";

  try {
    const res = await authedFetch(`${API_BASE}/sessions`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ exercise_id: id }),
    });
    if (res.status === 401) {
      btn.disabled = false;
      btn.textContent = prevText;
      showToast("Sign in to log this to your record", "info");
      return;
    }
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    btn.textContent = "Added to today";
    btn.classList.remove("primary");
    showToast(`${name} added to today`, "info");
    // Reveal the video on the card now that the exercise is confirmed.
    const wrap = btn.closest(".exercise-card");
    if (wrap) revealVideoOnCard(wrap);
    await refreshTodaySession();
  } catch (e) {
    console.warn("addToToday failed:", e);
    btn.disabled = false;
    btn.textContent = prevText;
    showToast(`Could not log: ${e.message}`, "error");
  }
}

const _EXERCISE_NAME_LOOKUP = {}; // exercise_id -> friendly name; populated by gallery renders

function rememberExerciseName(id, name) {
  if (id && name) _EXERCISE_NAME_LOOKUP[id] = name;
}

function exerciseDisplayName(id) {
  return _EXERCISE_NAME_LOOKUP[id] || id;
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
    .map((e) => {
      const friendly = exerciseDisplayName(e.exercise_id);
      const statusGlyph =
        e.status === "completed" ? "✓" :
        e.status === "in_progress" ? "..." :
        e.status === "skipped" ? "—" : "";
      const statusClass = e.status || "planned";
      // PR-T2 + PR-U7: dim + label rows ONLY when we have a confirmed
      // out-of-region body_region. Planner-generated exercise IDs that
      // aren't in the library return body_region: null from the backend;
      // those rows used to render as "prior: unknown" + dimmed, which is
      // misleading because the unknown ones are usually current-region
      // regressions the planner just renamed. Treat null as "no info"
      // and render normally.
      const knownOutOfRegion = e.is_current_region === false && !!e.body_region;
      const outOfRegionClass = knownOutOfRegion ? " out-of-region" : "";
      const regionTag = knownOutOfRegion
        ? `<span class="today-session-region-tag">prior: ${escapeHtml(e.body_region)}</span>`
        : "";
      return `
    <li class="today-session-item ${statusClass}${outOfRegionClass}">
      <span class="today-session-name">${escapeHtml(friendly)}</span>${regionTag}
      <span class="today-session-status">${escapeHtml(statusGlyph)}</span>
      <button class="today-session-remove"
              onclick="markSessionSkipped('${escapeHtml(e.id)}')"
              title="Skip">x</button>
    </li>
  `;
    })
    .join("");
}

async function markSessionSkipped(sessionId) {
  if (!sessionId) return;
  try {
    const res = await authedFetch(`${API_BASE}/sessions/${encodeURIComponent(sessionId)}`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ status: "skipped" }),
    });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    await refreshTodaySession();
  } catch (e) {
    console.warn("markSessionSkipped failed:", e);
    showToast(`Could not skip: ${e.message}`, "error");
  }
}

// Backwards-compat alias for any cached HTML referring to removeFromToday.
function removeFromToday(idOrSessionId) {
  // The new flow operates on session_id, but legacy markup may still pass
  // an exercise_id. Try to resolve.
  const target = todaySession.find(
    (e) => e.id === idOrSessionId || e.exercise_id === idOrSessionId,
  );
  if (target) markSessionSkipped(target.id);
}

// ---------------------------------------------------------------------------
// Protocol re-fetch (called after a real protocol-changing PR opens)
// ---------------------------------------------------------------------------
async function refreshProtocol() {
  try {
    const res = await authedFetch(`${API_BASE}/protocol`);
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

// ---------------------------------------------------------------------------
// Patient history pane (Surface B)
//
// Reads /sessions/recent?days=30 (no `target` — backend resolves
// current_user_id from JWT). Groups rows by their created_at date,
// rendering most-recent day first. Reuses the session-region-tag pattern
// from PR-T2 for out-of-region rows. Read-only MVP — no charts, no
// filters, no exports.
// ---------------------------------------------------------------------------

let _historyLoading = false;

async function loadPatientHistory() {
  const host = document.getElementById("historyBody");
  if (!host) return;
  if (_historyLoading) return;

  if (!window.RehabAuth?.getJwt?.()) {
    host.innerHTML = `<div class="history-empty">
      Sign in to see your session history.
    </div>`;
    return;
  }

  _historyLoading = true;
  host.innerHTML = `<div class="history-empty">Loading…</div>`;

  let data;
  try {
    const res = await authedFetch(`${API_BASE}/sessions/recent?days=30`);
    if (res.status === 401) {
      host.innerHTML = `<div class="history-empty">
        Sign in to see your session history.
      </div>`;
      return;
    }
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    data = await res.json();
  } catch (e) {
    console.warn("loadPatientHistory failed:", e);
    host.innerHTML = `<div class="history-empty">
      Could not load history: ${escapeHtml(e.message || "unknown error")}
    </div>`;
    return;
  } finally {
    _historyLoading = false;
  }

  renderPatientHistory(data?.sessions || []);
}

function renderPatientHistory(sessions) {
  const host = document.getElementById("historyBody");
  if (!host) return;
  if (!sessions.length) {
    host.innerHTML = `<div class="history-empty">
      No sessions yet. Start today's session from the Coach Chat tab.
    </div>`;
    return;
  }

  // Bucket by created_at YYYY-MM-DD (UTC, matching the clinician dashboard).
  const byDay = new Map();
  for (const s of sessions) {
    const d = (s.created_at || "").slice(0, 10) || "unknown";
    if (!byDay.has(d)) byDay.set(d, []);
    byDay.get(d).push(s);
  }
  // Most recent date first.
  const days = Array.from(byDay.entries()).sort((a, b) => b[0].localeCompare(a[0]));

  const html = days.map(([day, rows]) => {
    const header = formatHistoryDayHeader(day);
    const rowHtml = rows.map((s) => {
      const friendly = exerciseDisplayName(s.exercise_id);
      const status = s.status || "planned";
      const glyph =
        status === "completed" ? "✓" :
        status === "skipped" ? "⊘" :
        status === "in_progress" ? "…" : "·";
      const knownOutOfRegion = s.is_current_region === false && !!s.body_region;
      const outOfRegionClass = knownOutOfRegion ? " out-of-region" : "";
      const regionTag = knownOutOfRegion
        ? `<span class="session-region-tag">prior: ${escapeHtml(s.body_region)}</span>`
        : "";
      const meta = [];
      if (s.pose_metrics?.rep_count != null) meta.push(`${s.pose_metrics.rep_count} reps`);
      if (s.pose_metrics?.worst_status) meta.push(escapeHtml(s.pose_metrics.worst_status));
      const metaLine = meta.length
        ? `<div class="history-row-meta">${meta.join(" · ")}</div>`
        : "";
      // PR-Y: pain/RPE/notes from the linked auto-checkin row, when present.
      // Skip the line entirely if the session has no checkin (don't render
      // a "no check-in logged" stub).
      const checkinLine = renderCheckinFeelLine(s, "history-row-checkin");
      return `
        <div class="history-row ${status}${outOfRegionClass}">
          <div class="history-row-head">
            <span class="history-row-status">${escapeHtml(glyph)}</span>
            <span>${escapeHtml(friendly)}</span>
            ${regionTag}
          </div>
          ${metaLine}
          ${checkinLine}
        </div>`;
    }).join("");
    return `
      <div class="history-day">
        <div class="history-day-header">${escapeHtml(header)}</div>
        ${rowHtml}
      </div>`;
  }).join("");

  host.innerHTML = html;
}

// Pretty-format a YYYY-MM-DD bucket key. "Today — May 7, 2026", "Yesterday
// — May 6, 2026", or "May 4, 2026". Anything that fails to parse falls
// back to the raw key so the UI never collapses to "Invalid Date".
function formatHistoryDayHeader(ymd) {
  if (!ymd || ymd === "unknown") return "Unknown date";
  const parts = ymd.split("-");
  if (parts.length !== 3) return ymd;
  // Construct as a local-noon Date so timezone ticks don't shift the day.
  const d = new Date(Number(parts[0]), Number(parts[1]) - 1, Number(parts[2]), 12, 0, 0);
  if (Number.isNaN(d.getTime())) return ymd;

  const today = new Date();
  const yesterday = new Date();
  yesterday.setDate(today.getDate() - 1);
  const sameYMD = (a, b) =>
    a.getFullYear() === b.getFullYear() &&
    a.getMonth() === b.getMonth() &&
    a.getDate() === b.getDate();

  const pretty = d.toLocaleDateString("en-US", {
    month: "long", day: "numeric", year: "numeric",
  });
  if (sameYMD(d, today)) return `Today — ${pretty}`;
  if (sameYMD(d, yesterday)) return `Yesterday — ${pretty}`;
  return pretty;
}

// ---------------------------------------------------------------------------
// PR-Y: shared "How it felt" rendering for session rows.
//
// /sessions/today and /sessions/recent both LEFT JOIN the most recent linked
// checkin. Surface pain (0-10) and RPE (1-10) inline; render notes (PHI) on
// a second line, escaped, italicized, prefixed with "flag:". When the row
// has no checkin (`checkin_id` null), return "" so the caller emits nothing
// — we explicitly do NOT render a "no check-in logged" stub.
// ---------------------------------------------------------------------------
function renderCheckinFeelLine(row, baseClass) {
  if (!row || row.checkin_id == null) return "";
  const pain = row.checkin_pain_level;
  const rpe = row.checkin_rpe;
  const notes = row.checkin_notes;
  const segments = [];
  if (pain != null) segments.push(`Pain ${escapeHtml(String(pain))}/10`);
  if (rpe != null) segments.push(`RPE ${escapeHtml(String(rpe))}/10`);
  if (!segments.length && !notes) return "";
  const headLine = segments.length
    ? `<div class="${baseClass}">${segments.join(" · ")}</div>`
    : "";
  const notesLine = notes
    ? `<div class="${baseClass}-notes"><em>flag: ${escapeHtml(notes)}</em></div>`
    : "";
  return headLine + notesLine;
}

// ---------------------------------------------------------------------------
// Workout-recap modal (Surface A)
//
// Renders today's rows from the in-memory `todaySession` cache populated by
// refreshTodaySession() — same shape the sidebar uses, so we don't refetch.
// Completed rows render first, skipped exercises grouped at the bottom in a
// muted style. Out-of-region rows reuse the existing `prior: <region>` tag.
// Read-only — no mutations.
// ---------------------------------------------------------------------------

function openWorkoutRecapModal() {
  const modal = document.getElementById("recapModal");
  if (!modal) return;
  renderWorkoutRecap();
  modal.hidden = false;

  // Wire close handlers once. Idempotent — re-attaching is harmless because
  // we replace listeners by toggling a marker dataset.
  if (!modal.dataset.wired) {
    modal.dataset.wired = "1";
    document
      .getElementById("recapModalClose")
      ?.addEventListener("click", closeWorkoutRecapModal);
    modal.addEventListener("click", (e) => {
      if (e.target === modal) closeWorkoutRecapModal();
    });
    document.addEventListener("keydown", (e) => {
      if (e.key === "Escape" && !modal.hidden) closeWorkoutRecapModal();
    });
  }
}

function closeWorkoutRecapModal() {
  const modal = document.getElementById("recapModal");
  if (modal) modal.hidden = true;
}

function renderWorkoutRecap() {
  const dateEl = document.getElementById("recapModalDate");
  const body = document.getElementById("recapModalBody");
  if (!body) return;

  if (dateEl) {
    dateEl.textContent = new Date().toLocaleDateString("en-US", {
      weekday: "long", month: "long", day: "numeric",
    });
  }

  const rows = Array.isArray(todaySession) ? todaySession : [];
  if (!rows.length) {
    body.innerHTML = `<div class="recap-empty">No exercises logged for today yet.</div>`;
    return;
  }

  const completed = rows.filter((r) => r.status === "completed");
  const skipped = rows.filter((r) => r.status === "skipped");
  const other = rows.filter(
    (r) => r.status !== "completed" && r.status !== "skipped",
  );

  const renderRow = (r, statusGlyph) => {
    const friendly = exerciseDisplayName(r.exercise_id);
    const knownOutOfRegion = r.is_current_region === false && !!r.body_region;
    const outOfRegionClass = knownOutOfRegion ? " out-of-region" : "";
    const regionTag = knownOutOfRegion
      ? `<span class="today-session-region-tag">prior: ${escapeHtml(r.body_region)}</span>`
      : "";
    // pose_metrics: rep count + worst form status from the live form-check
    // run. PR-Y: also surface pain/RPE/notes from the linked auto-checkin
    // when the patient logged one post-session. No checkin -> render no
    // extra line (no stub).
    const meta = [];
    if (r.pose_metrics?.rep_count != null) meta.push(`${r.pose_metrics.rep_count} reps`);
    if (r.pose_metrics?.worst_status) meta.push(escapeHtml(r.pose_metrics.worst_status));
    const metaLine = meta.length
      ? `<div class="recap-row-meta">${meta.join(" · ")}</div>`
      : "";
    const checkinLine = renderCheckinFeelLine(r, "recap-row-checkin");
    const statusClass = r.status === "skipped" ? " skipped" : "";
    return `
      <div class="recap-row${statusClass}${outOfRegionClass}">
        <div class="recap-row-head">
          <span class="recap-row-status">${escapeHtml(statusGlyph)}</span>
          <span>${escapeHtml(friendly)}</span>
          ${regionTag}
        </div>
        ${metaLine}
        ${checkinLine}
      </div>`;
  };

  const parts = [];
  if (completed.length) {
    parts.push(`<div class="recap-section-title">Completed</div>`);
    parts.push(completed.map((r) => renderRow(r, "✓")).join(""));
  }
  if (other.length) {
    parts.push(`<div class="recap-section-title">In progress</div>`);
    parts.push(other.map((r) => renderRow(r, "…")).join(""));
  }
  if (skipped.length) {
    const names = skipped
      .map((r) => exerciseDisplayName(r.exercise_id))
      .map(escapeHtml)
      .join(", ");
    parts.push(
      `<div class="recap-skipped-list"><strong>Skipped:</strong> ${names}</div>`,
    );
  }
  if (!parts.length) {
    body.innerHTML = `<div class="recap-empty">No exercises logged for today yet.</div>`;
    return;
  }
  body.innerHTML = parts.join("");
}

function showToast(msg, type = "info") {
  const toast = document.getElementById("toast");
  toast.textContent = msg;
  toast.className = `toast show ${type}`;
  setTimeout(() => toast.classList.remove("show"), 4000);
}
