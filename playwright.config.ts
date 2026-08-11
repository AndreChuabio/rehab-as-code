import { defineConfig, devices } from "@playwright/test";
import dotenv from "dotenv";
import { existsSync, mkdirSync, readFileSync, writeFileSync } from "node:fs";
import { dirname, join } from "node:path";

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

/**
 * Guarantee the storage-state file exists before any test context is built.
 *
 * Playwright resolves `storageState` when it creates the browser context, which
 * happens BEFORE any beforeEach hook. So the `test.skip(!hasSavedSession())`
 * guard the @authed specs already carry is too late to help: with no captured
 * session those specs died with
 *   `Error reading storage state from playwright/.auth/patient.json: ENOENT`
 * instead of skipping — breaking `npm run qa` on a fresh clone and any CI run
 * without E2E credentials, contrary to what tests/e2e/README.md promised.
 *
 * Writing an empty-but-valid state makes the context anonymous rather than
 * fatal, which lets the in-test skips do their job. `hasSavedSession()` in
 * fixtures.ts therefore tests for real session CONTENT, never mere existence.
 */
function ensureStorageStateFile(): void {
  if (existsSync(STORAGE_STATE)) return;
  mkdirSync(dirname(STORAGE_STATE), { recursive: true });
  writeFileSync(STORAGE_STATE, JSON.stringify({ cookies: [], origins: [] }));
}

ensureStorageStateFile();

/**
 * Refuse to run write-tests against the production database.
 *
 * "local" is NOT a safe place to mutate. The repo's .env sets
 * STORAGE_BACKEND=postgres with DATABASE_URL pointing at the live Supabase
 * project, so a locally-served backend writes to PROD patient-state tables.
 * This has already caused real damage: a local run from an unmerged branch on
 * 2026-06-29 wrote two auto_applied protocol rows to prod, one of which was
 * still a patient's active plan six weeks later.
 *
 * So the guard keys off the resolved DATABASE_URL, never off the target name.
 */
const PROD_SUPABASE_REF = "cljqfgpivrxeupnhfmrp";

/**
 * Resolve the DATABASE_URL the local backend will actually use.
 *
 * backend/main.py calls a bare `load_dotenv()`, which walks UP from the process
 * cwd until it finds a .env. Run from a git worktree (which has no .env of its
 * own, since .env is gitignored) that search still reaches the main checkout's
 * .env and picks up prod credentials. Mirror that walk instead of only looking
 * in cwd, or the guard reports "safe" from a worktree and is useless exactly
 * where isolation made it feel safest.
 */
function resolveLocalDatabaseUrl(): { url: string; source: string } | null {
  if (process.env.DATABASE_URL) return { url: process.env.DATABASE_URL, source: "environment" };

  let dir = process.cwd();
  for (let depth = 0; depth < 8; depth++) {
    const candidate = join(dir, ".env");
    if (existsSync(candidate)) {
      try {
        const parsed = dotenv.parse(readFileSync(candidate, "utf8"));
        if (parsed.DATABASE_URL) return { url: parsed.DATABASE_URL, source: candidate };
      } catch {
        return null; // unreadable -> treat as unknown, and fail closed below
      }
    }
    const parent = dirname(dir);
    if (parent === dir) break;
    dir = parent;
  }
  return null;
}

function assertMutationSafety(): void {
  if (process.env.QA_ALLOW_MUTATION !== "1") return;
  if (process.env.QA_ALLOW_PROD_WRITES === "1") return;

  const refuse = (why: string[]): never => {
    throw new Error(
      [
        "",
        "  REFUSING TO RUN: QA_ALLOW_MUTATION=1 may write to the PRODUCTION database.",
        "",
        ...why.map((l) => `  ${l}`),
        "",
        "  Point DATABASE_URL at a scratch database first:",
        "    DATABASE_URL=postgresql://…/scratch QA_TARGET=local QA_ALLOW_MUTATION=1 npm run qa",
        "",
        "  If you genuinely intend production writes, set QA_ALLOW_PROD_WRITES=1.",
        "",
      ].join("\n"),
    );
  };

  if (!isLocal) {
    refuse([`Target is ${baseURL} (production).`]);
  }

  // Local target: safe only if we can PROVE the backend is not pointed at prod.
  const resolved = resolveLocalDatabaseUrl();
  if (!resolved) {
    refuse([
      "Target is local, but the backend's DATABASE_URL could not be determined,",
      "so it cannot be shown to be a scratch database. Failing closed.",
      "Set DATABASE_URL explicitly for this run.",
    ]);
  }

  if (/supabase\.(com|co)/i.test(resolved.url) || resolved.url.includes(PROD_SUPABASE_REF)) {
    refuse([
      "Target is local, but the backend resolves DATABASE_URL to the live",
      `Supabase project (${PROD_SUPABASE_REF}) from ${resolved.source},`,
      "so a locally-served backend still writes to PROD patient-state tables.",
      "",
      "Precedent: a local run from an unmerged branch on 2026-06-29 wrote two",
      "auto_applied protocol rows to prod; one was still a patient's active",
      "plan six weeks later.",
    ]);
  }
}

// Fails the run at config load, before a single browser or write can start.
assertMutationSafety();

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
