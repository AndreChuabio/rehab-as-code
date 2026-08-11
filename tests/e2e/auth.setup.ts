import { test as setup, expect } from "@playwright/test";
import { existsSync, statSync } from "node:fs";
import { STORAGE_STATE } from "../../playwright.config";

/**
 * Produces the authenticated storage state consumed by @authed specs.
 *
 * Two ways in, in priority order:
 *   1. E2E_EMAIL + E2E_PASSWORD set -> sign in through the real UI here.
 *   2. A state file already captured by `npm run qa:login` (headed, manual).
 *
 * With neither, this skips rather than fails so the unauthenticated smoke
 * suite still runs on a fresh clone with no secrets configured.
 */

const MAX_STATE_AGE_MS = 1000 * 60 * 60 * 12; // Supabase access tokens are short-lived

setup("authenticate", async ({ page }) => {
  const email = process.env.E2E_EMAIL;
  const password = process.env.E2E_PASSWORD;

  if (!email || !password) {
    const hasState = existsSync(STORAGE_STATE);
    if (hasState) {
      const ageMs = Date.now() - statSync(STORAGE_STATE).mtimeMs;
      if (ageMs > MAX_STATE_AGE_MS) {
        console.warn(
          `[auth.setup] ${STORAGE_STATE} is ${Math.round(ageMs / 3_600_000)}h old; ` +
            "the Supabase token has likely expired. Re-run: npm run qa:login",
        );
      }
      setup.skip(true, "Reusing session captured by `npm run qa:login`.");
      return;
    }
    setup.skip(
      true,
      "No E2E_EMAIL/E2E_PASSWORD and no saved session. Run `npm run qa:login` to capture one.",
    );
    return;
  }

  await page.goto("/");

  // The overlay renders on load for unauthed visitors (frontend/index.html).
  const overlay = page.locator("#authOverlay");
  await expect(overlay).toBeVisible();

  // The form is painted and fillable before app.js wires its submit handler
  // (that wiring waits on RehabAuth.init -> fetch /config -> ESM CDN import of
  // supabase-js). A single early click is swallowed, so retry the submit until
  // the JWT actually lands. Re-clicking is safe: the handler disables the
  // button while a sign-in is in flight.
  await expect(async () => {
    // Surface a genuine credential rejection instead of burning the timeout.
    const status = page.locator("#authStatus");
    if (await status.isVisible().catch(() => false)) {
      const text = (await status.textContent())?.trim() ?? "";
      if (/failed|already registered|different password/i.test(text)) {
        throw new Error(`Sign-in rejected by the app: ${text}`);
      }
    }

    await page.locator("#authEmail").fill(email);
    await page.locator("#authPassword").fill(password);
    await page.locator("#authSubmit").click();

    // Sign-in is confirmed by the JWT landing in localStorage, which is also
    // exactly what storageState persists.
    await expect
      .poll(() => page.evaluate(() => window.localStorage.getItem("supabaseJwt")), {
        timeout: 6_000,
      })
      .toBeTruthy();
  }).toPass({ timeout: 60_000 });

  await expect(overlay).toBeHidden();

  await page.context().storageState({ path: STORAGE_STATE });
});
