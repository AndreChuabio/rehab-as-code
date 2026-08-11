import { test as base, expect, type Page } from "@playwright/test";
import { readFileSync } from "node:fs";
import { STORAGE_STATE } from "../../playwright.config";

/**
 * Shared fixtures for the QA suite.
 *
 * `pageErrors` collects uncaught exceptions and console.error output for the
 * lifetime of a test. Assert on it explicitly - it is deliberately NOT auto-
 * asserted, because a few third-party embeds (Tavus/Daily, the Supabase ESM
 * CDN) log benign errors we do not control.
 */

/** Console noise we never want to fail a run on. */
const IGNORED_ERROR_PATTERNS: RegExp[] = [
  /favicon/i,
  /net::ERR_BLOCKED_BY_CLIENT/i, // ad blockers on the real profile
  /Failed to load resource.*\b40[134]\b/i, // optional/gated endpoints
  /daily-js|daily-co|tavus/i, // third-party video SDK chatter
];

export type PageErrors = {
  /** Every error seen, unfiltered. */
  all: string[];
  /** Errors after IGNORED_ERROR_PATTERNS is applied - assert on this. */
  significant(): string[];
};

export const test = base.extend<{ pageErrors: PageErrors }>({
  pageErrors: async ({ page }, use) => {
    const all: string[] = [];

    page.on("pageerror", (err) => all.push(`pageerror: ${err.message}`));
    page.on("console", (msg) => {
      if (msg.type() === "error") all.push(`console.error: ${msg.text()}`);
    });

    const errors: PageErrors = {
      all,
      significant: () =>
        all.filter((e) => !IGNORED_ERROR_PATTERNS.some((p) => p.test(e))),
    };

    await use(errors);
  },
});

export { expect };

/**
 * True when a real authenticated session is on disk.
 *
 * Deliberately checks CONTENT, not existence. playwright.config.ts writes an
 * empty-but-valid state file up front so that a missing session produces an
 * anonymous context instead of an ENOENT crash at context-creation time (which
 * is before any beforeEach skip can run). An existence check here would read
 * that placeholder as a live session and the @authed specs would run signed-out.
 */
export function hasSavedSession(): boolean {
  try {
    const state = JSON.parse(readFileSync(STORAGE_STATE, "utf8"));
    return (
      (Array.isArray(state.origins) && state.origins.length > 0) ||
      (Array.isArray(state.cookies) && state.cookies.length > 0)
    );
  } catch {
    return false;
  }
}

/**
 * Dismiss the sign-in overlay by taking the app's built-in demo path.
 *
 * The button is painted before app.js attaches its click listener (that wiring
 * waits on RehabAuth.init -> fetch /config -> ESM CDN import of supabase-js),
 * so an early click silently no-ops. Retry until the overlay actually closes
 * rather than trusting a single click.
 */
export async function continueAsDemo(page: Page): Promise<void> {
  const overlay = page.locator("#authOverlay");
  if (!(await overlay.isVisible().catch(() => false))) return;

  await expect(async () => {
    await page.locator("#authSkip").click();
    await expect(overlay).toBeHidden({ timeout: 2_000 });
  }).toPass({ timeout: 30_000 });
}

/**
 * Keep a staff account on the patient dashboard.
 *
 * app.js `maybeRedirectToClinician()` bounces role=clinician|admin to
 * /clinician unless sessionStorage.asPatient is set - the same override the
 * "View as patient" button uses. Because that redirect awaits /me/role, a spec
 * that skips this races it and asserts against whichever page wins.
 *
 * Must be called BEFORE page.goto().
 */
export async function usePatientView(page: Page): Promise<void> {
  await page.addInitScript(() => {
    try {
      sessionStorage.setItem("asPatient", "1");
    } catch {
      /* storage disabled; the redirect assertion will surface it */
    }
  });
}

/**
 * Call a JWT-gated API endpoint the way the app does.
 *
 * The backend authenticates via `Authorization: Bearer <supabase jwt>`, and the
 * app's fetch wrapper reads that token from localStorage. `page.request` only
 * inherits cookies, so calling a protected route through it returns 401 and a
 * naive gate check silently skips the whole suite. Read the token out of the
 * page and attach it explicitly.
 */
export async function authedRequest(page: Page, path: string) {
  const jwt = await page.evaluate(() => window.localStorage.getItem("supabaseJwt"));
  if (!jwt) throw new Error(`No supabaseJwt in localStorage; cannot call ${path}`);

  return page.request.get(path, { headers: { Authorization: `Bearer ${jwt}` } });
}

export { STORAGE_STATE };
