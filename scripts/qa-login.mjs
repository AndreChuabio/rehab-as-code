#!/usr/bin/env node
/**
 * Interactive login capture for the Playwright QA harness.
 *
 * Opens a real (headed) Chromium window, waits for you to sign in by hand,
 * then writes the resulting session to playwright/.auth/patient.json so the
 * test suite runs pre-authenticated. Nothing is typed for you and no password
 * is read, stored, or logged by this script.
 *
 * Usage:
 *   npm run qa:login                 # target from QA_TARGET (default local)
 *   QA_TARGET=prod npm run qa:login
 *   QA_BASE_URL=https://... npm run qa:login
 *   npm run qa:login -- --clinician  # writes playwright/.auth/clinician.json
 *
 * The app stores its Supabase JWT in localStorage.supabaseJwt (frontend/auth.js),
 * which is exactly what Playwright's storageState persists.
 */

import { chromium } from "@playwright/test";
import { mkdirSync } from "node:fs";
import { dirname, resolve } from "node:path";

const PROD_URL = "https://rehab-as-code-five.vercel.app";
const LOCAL_URL = "http://127.0.0.1:8000";
const JWT_KEY = "supabaseJwt";
const POLL_MS = 1000;
const TIMEOUT_MS = 10 * 60 * 1000; // 10 minutes to finish signing in

const isClinician = process.argv.includes("--clinician");
const target = process.env.QA_TARGET ?? "local";
const baseURL =
  process.env.QA_BASE_URL ?? (target === "prod" ? PROD_URL : LOCAL_URL);
const entryPath = isClinician ? "/clinician" : "/";
const statePath = resolve(
  process.cwd(),
  isClinician ? "playwright/.auth/clinician.json" : "playwright/.auth/patient.json",
);

/** Reachability check so a dead local server fails loudly instead of hanging. */
async function assertReachable(url) {
  try {
    const res = await fetch(`${url}/config`, { signal: AbortSignal.timeout(8000) });
    if (!res.ok) throw new Error(`/config returned ${res.status}`);
  } catch (err) {
    console.error(`\n  Cannot reach ${url} (${err.message}).`);
    if (url === LOCAL_URL) {
      console.error("  Start the backend first:");
      console.error("    python -m uvicorn main:app --reload --app-dir backend --port 8000");
      console.error("  ...or capture against prod instead:");
      console.error("    QA_TARGET=prod npm run qa:login\n");
    }
    process.exit(1);
  }
}

async function main() {
  await assertReachable(baseURL);
  mkdirSync(dirname(statePath), { recursive: true });

  const browser = await chromium.launch({ headless: false, slowMo: 50 });
  const context = await browser.newContext({ baseURL, viewport: { width: 1440, height: 900 } });
  const page = await context.newPage();

  let closed = false;
  browser.on("disconnected", () => { closed = true; });

  await page.goto(`${baseURL}${entryPath}`, { waitUntil: "domcontentloaded" });

  console.log(`\n  Chromium is open at ${baseURL}${entryPath}`);
  console.log(`  Sign in as the ${isClinician ? "clinician" : "patient"} account in that window.`);
  console.log("  Use a TEST-DATA account - the plan pipeline sends intake JSON to Anthropic.");
  console.log("  Detecting sign-in automatically; leave the window open until it confirms.\n");

  const deadline = Date.now() + TIMEOUT_MS;
  let jwtSeen = false;

  while (Date.now() < deadline && !closed && !jwtSeen) {
    try {
      jwtSeen = await page.evaluate(
        (key) => Boolean(window.localStorage.getItem(key)),
        JWT_KEY,
      );
    } catch {
      // Navigation mid-evaluate, or the window went away. Retry next tick.
    }
    if (!jwtSeen) await new Promise((r) => setTimeout(r, POLL_MS));
  }

  if (!jwtSeen) {
    console.error(
      closed
        ? "\n  Window closed before sign-in completed. No session saved.\n"
        : "\n  Timed out waiting for sign-in. No session saved.\n",
    );
    if (!closed) await browser.close();
    process.exit(1);
  }

  // Let Supabase finish persisting its own keys before snapshotting.
  await page.waitForTimeout(1500);
  await context.storageState({ path: statePath });

  const email = await page.evaluate(() => {
    const el = document.getElementById("authPillEmail");
    return el ? el.textContent.trim() : "";
  });

  console.log(`  Session captured${email ? ` for ${email}` : ""}.`);
  console.log(`  Saved to ${statePath}`);
  console.log("  Run the suite with:  npm run qa\n");

  await browser.close();
}

main().catch((err) => {
  console.error("qa-login failed:", err);
  process.exit(1);
});
