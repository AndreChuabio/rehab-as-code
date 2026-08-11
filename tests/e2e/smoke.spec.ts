import { test, expect, continueAsDemo } from "./fixtures";

/**
 * Unauthenticated smoke suite.
 *
 * Runs with no credentials and no saved session, so it works on a clean clone
 * and is the right first signal that a deploy is alive. Everything here is
 * read-only - it never signs in, submits intake, or triggers the plan pipeline.
 */

test.describe("smoke @public", () => {
  test("patient app loads with its shell intact", async ({ page, pageErrors }) => {
    await page.goto("/");

    await expect(page).toHaveTitle(/RehabAsCode/i);
    await expect(page.locator(".logo-text")).toHaveText(/RehabAsCode/i);

    // Wearable, protocol and stage regions are the load-bearing dashboard
    // furniture; if any is missing the page rendered but the app did not boot.
    await expect(page.locator("#healthStats")).toBeAttached();
    await expect(page.locator("#protocolExercises")).toBeAttached();
    await expect(page.locator("#stageChat")).toBeAttached();

    expect(pageErrors.significant(), "unexpected console errors on load").toEqual([]);
  });

  test("sign-in overlay is offered to unauthenticated visitors", async ({ page }) => {
    await page.goto("/");

    const overlay = page.locator("#authOverlay");
    await expect(overlay).toBeVisible();
    await expect(page.locator("#authEmail")).toBeVisible();
    await expect(page.locator("#authPassword")).toBeVisible();
    await expect(page.locator("#authSubmit")).toBeEnabled();
  });

  test("demo mode dismisses the overlay and reveals the dashboard", async ({ page }) => {
    await page.goto("/");
    await continueAsDemo(page);

    await expect(page.locator("#authOverlay")).toBeHidden();
    await expect(page.locator(".main")).toBeVisible();
  });

  test("/config exposes Supabase wiring without leaking secrets", async ({ request }) => {
    const res = await request.get("/config");
    expect(res.status()).toBe(200);

    const body = await res.json();
    expect(body.supabase_url).toMatch(/^https:\/\/.+\.supabase\.co$/);
    expect(body.supabase_anon_key, "anon key should be present").toBeTruthy();

    // The anon key is public by design; a service-role key here would be a leak.
    const decoded = JSON.parse(
      Buffer.from(body.supabase_anon_key.split(".")[1], "base64").toString(),
    );
    expect(decoded.role, "/config must never serve a service_role key").toBe("anon");

    // No server-side secret should ever ride along on this endpoint.
    const keys = Object.keys(body).join(",");
    expect(keys).not.toMatch(/service_role|secret|database_url|anthropic|openai/i);
  });

  test("clinician console loads and gates behind sign-in", async ({ page }) => {
    await page.goto("/clinician");

    await expect(page.locator("#clinicianHeaderTitle")).toBeVisible();
    // The queue shell must exist even before auth resolves.
    await expect(page.locator("#queueList")).toBeAttached();
  });

  test("swagger docs respond", async ({ request }) => {
    const res = await request.get("/docs");
    expect(res.status()).toBe(200);
  });

  // PR #123/#125 wired Coach Maya's auto-apply tier. These routes cross
  // patients, so they must reject anonymous callers outright.
  test("auto-apply routes reject anonymous callers", async ({ request }) => {
    const feed = await request.get("/protocols/auto-applied");
    expect(feed.status(), "/protocols/auto-applied must not be public").toBe(401);

    // A nonexistent id: auth is checked before the row is looked up, so this
    // cannot revert anything even if the guard were broken.
    const revert = await request.post(
      "/protocols/00000000-0000-0000-0000-000000000000/revert",
    );
    expect(revert.status(), "revert must not be public").toBe(401);
  });
});
