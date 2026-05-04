// auth.js — Supabase magic-link sign-in.
//
// Loads the Supabase JS client from the ESM CDN, fetches /config to get the
// project URL + anon (publishable) key, and exposes a tiny API:
//
//   await window.RehabAuth.init()
//   await window.RehabAuth.sendMagicLink(email)
//   window.RehabAuth.getJwt()                  -> string | null
//   window.RehabAuth.getUser()                 -> { id, email } | null
//   window.RehabAuth.signOut()
//   window.RehabAuth.onChange((session) => {...})
//
// JWT is stored in localStorage.supabaseJwt for app.js fetch wrappers.

(function () {
  const SB_CDN = "https://esm.sh/@supabase/supabase-js@2.45.4";
  const SESSION_KEY = "supabaseJwt";

  let client = null;
  let currentSession = null;
  const listeners = [];

  async function fetchConfig() {
    const res = await fetch("/config");
    if (!res.ok) throw new Error(`/config returned ${res.status}`);
    const body = await res.json();
    if (!body.supabase_url || !body.supabase_anon_key) {
      throw new Error("supabase config missing — set SUPABASE_URL and SUPABASE_ANON_KEY in Vercel");
    }
    return body;
  }

  function setSession(session) {
    currentSession = session || null;
    const jwt = session?.access_token || null;
    if (jwt) localStorage.setItem(SESSION_KEY, jwt);
    else localStorage.removeItem(SESSION_KEY);
    for (const cb of listeners) {
      try { cb(currentSession); } catch (e) { console.warn("auth listener threw:", e); }
    }
  }

  async function init() {
    if (client) return client;
    const cfg = await fetchConfig();
    const mod = await import(/* @vite-ignore */ SB_CDN);
    client = mod.createClient(cfg.supabase_url, cfg.supabase_anon_key, {
      auth: {
        persistSession: true,
        autoRefreshToken: true,
        detectSessionInUrl: true,  // pick up magic-link tokens on redirect
      },
    });
    // Pick up an existing session (returning visit) before notifying listeners.
    const { data } = await client.auth.getSession();
    setSession(data?.session || null);
    client.auth.onAuthStateChange((_event, session) => setSession(session || null));
    return client;
  }

  async function sendMagicLink(email) {
    if (!client) await init();
    const redirectTo = `${window.location.origin}${window.location.pathname}`;
    const { error } = await client.auth.signInWithOtp({
      email: String(email || "").trim(),
      options: { emailRedirectTo: redirectTo },
    });
    if (error) throw error;
  }

  async function signOut() {
    if (!client) return;
    await client.auth.signOut();
    setSession(null);
  }

  function getJwt() {
    return currentSession?.access_token || localStorage.getItem(SESSION_KEY) || null;
  }

  function getUser() {
    const u = currentSession?.user;
    return u ? { id: u.id, email: u.email } : null;
  }

  function onChange(cb) {
    if (typeof cb !== "function") return () => {};
    listeners.push(cb);
    // Fire once with current state so caller can render immediately.
    try { cb(currentSession); } catch (e) {}
    return () => {
      const i = listeners.indexOf(cb);
      if (i >= 0) listeners.splice(i, 1);
    };
  }

  window.RehabAuth = { init, sendMagicLink, signOut, getJwt, getUser, onChange };
})();
