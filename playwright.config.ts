import { defineConfig, devices } from "@playwright/test";
import dotenv from "dotenv";
import { existsSync } from "node:fs";

// Local QA credentials + default target. Gitignored; absent in CI, where the
// same variables come from the environment instead.
dotenv.config({ path: ".env.qa.local", quiet: true });

/**
 * Playwright QA harness for rehab-as-code.
 *
 * Target selection (QA_BASE_URL wins, then QA_TARGET, then local):
 *   QA_TARGET=local  -> http://127.0.0.1:8000   (uvicorn, auto-started below)
 *   QA_TARGET=prod   -> https://rehab-as-code-five.vercel.app
 *   QA_BASE_URL=...  -> explicit override (preview deploys)
 *
 * Auth: tests/e2e/auth.setup.ts produces STORAGE_STATE. It logs in with
 * E2E_EMAIL / E2E_PASSWORD when those are set; otherwise it reuses a session
 * captured by `npm run qa:login` (headed browser, you log in by hand).
 * Specs tagged @authed are skipped when neither exists, so the unauthenticated
 * smoke suite still runs on a clean checkout.
 *
 * PHI: the multi-agent pipeline sends un-redacted intake JSON to Anthropic and
 * notification email egresses to Resend. Only ever point this harness at
 * test-data accounts. See CLAUDE.md "Things that bite".
 */

const PROD_URL = "https://rehab-as-code-five.vercel.app";
// Port 8000 is the documented local default, but it is a busy port. QA_PORT
// lets a run step aside from an unrelated server without editing this file.
const LOCAL_PORT = process.env.QA_PORT ?? "8000";
const LOCAL_URL = `http://127.0.0.1:${LOCAL_PORT}`;

const target = process.env.QA_TARGET ?? "local";
const baseURL =
  process.env.QA_BASE_URL ?? (target === "prod" ? PROD_URL : LOCAL_URL);

const isLocal = baseURL.startsWith("http://127.0.0.1") || baseURL.startsWith("http://localhost");

// `python` is not on PATH on this machine (only `python3`), and the project
// venv is 3.11 while system python3 is 3.14. Prefer the venv, then an explicit
// override, then python3.
const pythonBin =
  process.env.QA_PYTHON ??
  (existsSync(".venv/bin/python") ? ".venv/bin/python" : "python3");

export const STORAGE_STATE = "playwright/.auth/patient.json";

export default defineConfig({
  testDir: "./tests/e2e",
  // E2E hits a shared Supabase project; parallel writers make state assertions
  // race. Keep files parallel but workers modest.
  fullyParallel: true,
  workers: process.env.CI ? 2 : 3,
  forbidOnly: !!process.env.CI,
  retries: process.env.CI ? 2 : 0,
  timeout: 45_000,
  expect: { timeout: 10_000 },

  reporter: process.env.CI
    ? [["github"], ["html", { open: "never" }]]
    : [["list"], ["html", { open: "never" }]],

  use: {
    baseURL,
    // Traces/screenshots only on failure keeps the artifact dir small while
    // still giving a full timeline to debug from.
    trace: "retain-on-failure",
    screenshot: "only-on-failure",
    video: "retain-on-failure",
    actionTimeout: 15_000,
    navigationTimeout: 30_000,
  },

  projects: [
    // Runs first, writes STORAGE_STATE. Skips itself (rather than failing) when
    // no credentials are available.
    { name: "setup", testMatch: /.*\.setup\.ts/ },

    {
      name: "chromium",
      use: { ...devices["Desktop Chrome"] },
      dependencies: ["setup"],
    },

    // Patient UI was re-skinned for desktop (Deep Plum) but is used on phones;
    // keep a mobile lane so layout regressions surface.
    {
      name: "mobile-safari",
      use: { ...devices["iPhone 14"] },
      dependencies: ["setup"],
    },
  ],

  // Only manage a server for local runs; never try to boot uvicorn when the
  // target is prod or a preview URL.
  webServer: isLocal
    ? {
        command: `${pythonBin} -m uvicorn main:app --app-dir backend --port ${LOCAL_PORT} --host 127.0.0.1`,
        url: `${LOCAL_URL}/config`,
        reuseExistingServer: !process.env.CI,
        timeout: 120_000,
        stdout: "ignore",
        stderr: "pipe",
      }
    : undefined,
});
